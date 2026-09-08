# Generated-By: Codex / gpt-6-astra
"""Free, M2 fixed TTL and RAM budget policies from DESIGN §4."""

from typing import Mapping, Optional

from llmsvc.state import Blocker, StateSnapshot
from .common import Decision, PolicySettings, Projection, known_number


def plan_free(
    snapshot: StateSnapshot, *, gpu: Optional[int] = None, ram: bool = False,
    need_gb: Optional[float] = None, settings: PolicySettings = PolicySettings(),
    exclusions: Optional[Mapping[str, str]] = None,
) -> Decision:
    """Plan releases; ``need_gb`` is additional memory to release, in GiB.

    ``ram=True`` selects already sleeping models for stop. VRAM free selects
    awake models for sleep after >30 seconds idle. Reported GiB are estimates;
    the executor must measure actual release after actions complete.
    """
    if need_gb is not None and not known_number(need_gb):
        raise ValueError("need_gb must be finite and non-negative")
    p = Projection(snapshot, settings, exclusions=exclusions)
    if p.blockers or need_gb == 0:
        return p.result()
    state = "sleeping" if ram else "awake"
    selected = [m for m in snapshot.models if (gpu is None or m.gpu == gpu) and m.state != "stopped"]
    for model in selected:
        if model.state == "unknown":
            p.block(model, "unknown_model_state")
    candidates = p.candidates([m for m in selected if m.state == state], stop=ram, min_idle=30)
    freed = 0.0
    for model in candidates:
        if ram:
            if not known_number(model.weights_gb):
                p.block(model, "unknown_memory")
                continue
            p.stop(model, "free_ram")
            freed += model.weights_gb
        elif p.sleep(model, "free"):
            resident = model.resident_gb
            if known_number(resident):
                remaining = 0 if p.models[model.name].state == "stopped" else settings.sleeping_residual_gb
                freed += max(0.0, resident - remaining)
        if need_gb is not None and freed >= need_gb:
            break
    if need_gb is not None and freed < need_gb:
        p.blockers.append(Blocker(None, "insufficient_reclaimable_memory", gpu))
    return p.result(freed=freed)


def plan_idle_sleep(
    snapshot: StateSnapshot, *, settings: PolicySettings = PolicySettings(),
    exclusions: Optional[Mapping[str, str]] = None,
) -> Decision:
    """M2 fixed ten-minute TTL; M3 pressure policy provides per-GPU TTL."""
    p = Projection(snapshot, settings, exclusions=exclusions)
    if p.blockers:
        return p.result()
    models = [m for m in snapshot.models if m.state == "awake"]
    for model in p.candidates(models, min_idle=settings.fixed_ttl_seconds):
        p.sleep(model, "idle_ttl")
    return p.result()


def plan_memory_pressure(
    snapshot: StateSnapshot, *, settings: PolicySettings = PolicySettings(),
    exclusions: Optional[Mapping[str, str]] = None,
) -> Decision:
    """Stop lowest-value unprotected sleepers until both RAM limits hold."""
    p = Projection(snapshot, settings, exclusions=exclusions)
    if p.blockers:
        return p.result()
    if not p.memory_known():
        return Decision(blocked_by=(Blocker(None, "unknown_memory"),))
    memory = snapshot.memory
    def under_pressure():
        return p.sleeping > memory.budget_gb or p.available < memory.host_min_available_gb
    if not under_pressure():
        return p.result()
    candidates = p.candidates([m for m in snapshot.models if m.state == "sleeping"], stop=True)
    for model in candidates:
        p.stop(model, "memory_pressure")
        if not under_pressure():
            break
    if under_pressure():
        p.blockers.append(Blocker(None, "memory_budget"))
    return p.result()


def reload_admission(snapshot: StateSnapshot, *, settings: PolicySettings = PolicySettings()) -> Decision:
    """Check the entire awake batch without evictions or individual sleeps.

    Registry owns five-second quietness and final reload. Empty actions with
    empty blockers means admitted; all failures return blockers and no actions.
    """
    p = Projection(snapshot, settings)
    if p.blockers:
        return p.result()
    awake = [m for m in snapshot.models if m.state == "awake"]
    for model in snapshot.models:
        if model.state == "unknown":
            p.block(model, "unknown_model_state")
    for model in awake:
        reason = p.protection(model)
        if reason:
            p.block(model, reason)
    if any(not known_number(m.weights_gb) for m in awake) or not p.memory_known():
        p.blockers.append(Blocker(None, "unknown_memory"))
    elif not p.fits_sleep(sum(m.weights_gb for m in awake)):
        p.blockers.append(Blocker(None, "memory_budget"))
    return p.result()


def plan_reserve(
    snapshot: StateSnapshot, *, gpu: int, settings: PolicySettings = PolicySettings(),
    exclusions: Optional[Mapping[str, str]] = None,
) -> Decision:
    """Clear eligible sleepers from a reserved GPU; persistence is core-owned.

    Awake models are left alone. Protected sleepers report blockers. Subsequent
    cold placement must exclude active reserves; relocation is an M3 action.
    """
    if isinstance(gpu, bool) or not isinstance(gpu, int) or gpu < 0:
        raise ValueError("gpu must be a non-negative integer")
    p = Projection(snapshot, settings, exclusions=exclusions)
    if p.blockers:
        return p.result()
    for model in p.candidates(
        [m for m in snapshot.models if m.gpu == gpu and m.state == "sleeping"], stop=True,
    ):
        p.stop(model, "reserve")
    for model in snapshot.models:
        if model.gpu == gpu and model.state == "unknown":
            p.block(model, "unknown_model_state")
    return p.result()
