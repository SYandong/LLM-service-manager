# Generated-By: Codex / gpt-6-astra
"""Protected sleeping-model retirement/relocation; never fault cleanup."""

from dataclasses import replace
from typing import Mapping, Optional

from llmsvc.state import Action, Blocker, ModelState, Reserve, StateSnapshot
from .common import PolicySettings, Projection, known_number, snapshot_blockers
from .placement import PlacementDecision, _accounting, _budget, plan_placement


def _trigger(snapshot, model, reason):
    """Prove the requested trigger from current data, or return a blocker."""
    gpu = next((g for g in snapshot.gpus if g.index == model.gpu), None)
    if gpu is None:
        return Blocker(model.name, "unknown_accounting_gpu", model.gpu)
    if reason == "reserve":
        reserves = [r for r in snapshot.reserves if r.gpu == model.gpu]
        if any(not known_number(r.until) for r in reserves):
            return Blocker(model.name, "unknown_reserve_expiry", model.gpu)
        if not any(r.until > snapshot.sampled_at for r in reserves):
            return Blocker(model.name, "reserve_not_active", model.gpu)
        return None
    if not all(known_number(v) for v in (gpu.total_gb, gpu.external_gb)) or gpu.total_gb <= 0:
        return Blocker(model.name, "unknown_gpu_capacity", model.gpu)
    allocations, _, blockers = _accounting(snapshot)
    unknown = next((b for b in blockers if b.gpu in (None, model.gpu)), None)
    if unknown:
        return unknown
    occupied = sum(budget for index, budget in allocations.values() if index == model.gpu)
    if occupied <= gpu.total_gb - gpu.external_gb:
        return Blocker(model.name, "wake_budget_available", model.gpu)
    return None


def plan_relocation(
    snapshot: StateSnapshot, *, model: str, reason: str,
    settings: PolicySettings = PolicySettings(),
    exclusions: Optional[Mapping[str, str]] = None,
    replacement_requests: Optional[Mapping[str, ModelState]] = None,
) -> PlacementDecision:
    """Plan one sleeper's response to an active reserve or proven wake deficit.

    Reason is ``reserve`` or ``cannot_wake``. A known zero hourly request count
    permits stop; a recent-use sleeper gets a preflighted different-GPU cold
    placement. No feasible destination means zero actions, including no source
    stop or destination eviction. Normal protection applies to the source too:
    default/pin/inflight never policy-stop. Core's proven-fault cleanup is a
    separate protocol and is deliberately not available through this function.
    """
    if reason not in ("reserve", "cannot_wake"):
        raise ValueError("reason must be reserve or cannot_wake")
    blockers = snapshot_blockers(snapshot)
    if blockers:
        return PlacementDecision(blocked_by=blockers)
    p = Projection(snapshot, settings, exclusions=exclusions)
    source = p.models.get(model)
    if source is None or source.state != "sleeping":
        return PlacementDecision(blocked_by=(Blocker(model, "model_not_sleeping"),))
    protected = p.protection(source, stop=True)
    if protected:
        p.block(source, protected)
        return PlacementDecision(blocked_by=tuple(p.blockers))
    _, leased, accounting_blockers = _accounting(snapshot)
    if source.name in leased:
        return PlacementDecision(blocked_by=(Blocker(model, "outstanding_lease", source.gpu),))
    unknown = tuple(b for b in accounting_blockers if b.gpu in (None, source.gpu))
    if unknown:
        return PlacementDecision(blocked_by=unknown)
    trigger = _trigger(snapshot, source, reason)
    if trigger:
        return PlacementDecision(blocked_by=(trigger,))
    activity = p.activity[source.name]
    count = activity.requests_last_hour
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        p.block(source, "unknown_activity")
        return PlacementDecision(blocked_by=tuple(p.blockers))
    try:
        p.score(source)
    except ValueError as exc:
        p.block(source, str(exc))
        return PlacementDecision(blocked_by=tuple(p.blockers))
    if count == 0:
        return PlacementDecision(actions=(Action("stop", source.name, reason, source.gpu),))
    if not p.memory_known() or not known_number(source.weights_gb):
        return PlacementDecision(blocked_by=(Blocker(model, "unknown_memory", source.gpu),))
    request = None
    if replacement_requests is not None:
        profiles = dict(replacement_requests)
        if source.name not in profiles:
            return PlacementDecision(blocked_by=(Blocker(model, "missing_replacement_request", source.gpu),))
        request = profiles[source.name]
        if (not isinstance(request, ModelState) or request.name != source.name
                or request.state != "stopped" or request.unit_active is True
                or type(request.is_default) is not bool or request.is_default != source.is_default
                or not known_number(request.weights_gb) or _budget(request, 1.0) is None):
            return PlacementDecision(blocked_by=(Blocker(model, "invalid_replacement_request", source.gpu),))
        pending_weight = 0.0
        for lease in snapshot.leases:
            if lease.status not in ("pending", "stale"):
                continue
            pending = profiles.get(lease.model)
            if (not isinstance(pending, ModelState) or pending.name != lease.model
                    or not known_number(pending.weights_gb)):
                return PlacementDecision(blocked_by=(Blocker(lease.model, "unknown_memory", lease.gpu),))
            pending_weight += pending.weights_gb
        # Match normal cold placement admission, using only the observed source
        # weight as a possible release. Do not borrow destination victim RAM.
        available_after_stop = p.available + source.weights_gb
        if not known_number(available_after_stop) or not known_number(pending_weight):
            return PlacementDecision(blocked_by=(Blocker(model, "unknown_memory", source.gpu),))
        if available_after_stop - request.weights_gb - pending_weight < snapshot.memory.host_min_available_gb:
            return PlacementDecision(blocked_by=(Blocker(model, "host_memory_floor", source.gpu),))
    # This is a detached hypothetical state for preflight, never persisted. A
    # successful source stop must be observed before core uses the freed RAM.
    p.stop(source, reason)
    stopped_source = replace(source, state="stopped", gpu=None, unit_active=False)
    request = stopped_source if request is None else request
    models = tuple(stopped_source if m.name == source.name else m for m in snapshot.models)
    memory = replace(snapshot.memory, host_available_gb=p.available, sleeping_weights_gb=p.sleeping)
    # Exclude the source even when pressure later clears; this operation means
    # replacement on a different card. The synthetic reserve exists only here.
    exclusion = Reserve("policy-relocation-source", source.gpu, 0,
                        snapshot.sampled_at + 1, "policy-preflight")
    hypothetical = replace(snapshot, models=models, memory=memory,
                           reserves=snapshot.reserves + (exclusion,))
    placement = plan_placement(hypothetical, request, settings=settings, exclusions=p.exclusions)
    if placement.gpu is None:
        return replace(placement, blocked_by=placement.blocked_by +
                       (Blocker(source.name, "relocation_unavailable", source.gpu),))
    # Keep returned budget for core's destination lease. Core must execute the
    # source stop, observe exit, revalidate and atomically admit the destination.
    return replace(placement, actions=tuple(p.actions) + placement.actions)


def plan_sleeping_recovery(
    snapshot: StateSnapshot, *, settings: PolicySettings = PolicySettings(),
    exclusions: Optional[Mapping[str, str]] = None,
    replacement_requests: Optional[Mapping[str, ModelState]] = None,
) -> PlacementDecision:
    """Choose one lowest-value actionable sleeper; core replans after execution.

    Do not concatenate calls from the same snapshot: each relocation changes
    destination accounting. This entry point ensures a single preflighted move
    or unused-sleeper stop per decision, with blockers for protected candidates.
    """
    p = Projection(snapshot, settings, exclusions=exclusions)
    if p.blockers:
        return PlacementDecision(blocked_by=tuple(p.blockers))
    profiles = None if replacement_requests is None else dict(replacement_requests)
    candidates = p.candidates([m for m in snapshot.models if m.state == "sleeping"], stop=True)
    for model in candidates:
        reserves = [r for r in snapshot.reserves if r.gpu == model.gpu and
                    (not known_number(r.until) or r.until > snapshot.sampled_at)]
        reason = "reserve" if reserves else "cannot_wake"
        decision = plan_relocation(snapshot, model=model.name, reason=reason, settings=settings,
                                   exclusions=p.exclusions, replacement_requests=profiles)
        if decision.actions:
            return replace(decision, blocked_by=tuple(p.blockers) + decision.blocked_by)
        p.blockers.extend(b for b in decision.blocked_by if b.reason != "wake_budget_available")
    return PlacementDecision(blocked_by=tuple(p.blockers))
