# Generated-By: Codex / gpt-6-astra
"""Single-GPU placement with exhaustive feasible eviction sets (DESIGN §4.2)."""

from dataclasses import dataclass
from itertools import combinations
from math import isfinite
from typing import Optional

from llmsvc.state import Action, Blocker, ModelState, StateSnapshot
from .common import Decision, PolicySettings, Projection, known_number, snapshot_blockers


@dataclass(frozen=True)
class PlacementDecision(Decision):
    budget_gb: Optional[float] = None
    eviction_cost: float = 0.0


def _budget(record, total):
    """Respect explicit accounting and a larger configured fraction if present."""
    values = []
    if record.budget_gb is not None:
        if not known_number(record.budget_gb) or record.budget_gb <= 0:
            return None
        values.append(record.budget_gb)
    if record.util is not None:
        if not known_number(record.util) or not 0 < record.util <= 1:
            return None
        values.append(record.util * total)
    return max(values) if values else None


def _accounting(snapshot):
    """Return unique-model allocations and per-GPU uncertainty blockers.

    Confirmed leases are history only when a daemon or confirmed stopped record
    accounts for the model. Pending/stale records never expire inside policy.
    Same-model overlap on one GPU reserves the maximum known budget; conflicting
    GPU assignments block both cards until core reconciles them.
    """
    gpus = {g.index: g for g in snapshot.gpus}
    models = {m.name: m for m in snapshot.models}
    allocations, leased, blockers = {}, set(), []

    def add(record, name):
        gpu = gpus.get(record.gpu)
        if gpu is None:
            blockers.append(Blocker(name, "unknown_accounting_gpu"))
            return
        budget = _budget(record, gpu.total_gb) if known_number(gpu.total_gb) else None
        if budget is None:
            blockers.append(Blocker(name, "unknown_budget", record.gpu))
            return
        if name in allocations:
            old_gpu, old_budget = allocations[name]
            if old_gpu != record.gpu:
                blockers.extend((Blocker(name, "conflicting_accounting", old_gpu),
                                 Blocker(name, "conflicting_accounting", record.gpu)))
                return
            budget = max(budget, old_budget)
        allocations[name] = (record.gpu, budget)

    for model in snapshot.models:
        if model.state in ("awake", "sleeping"):
            add(model, model.name)
        elif model.state != "stopped" or model.unit_active is True:
            blockers.append(Blocker(model.name, "unknown_model_accounting", model.gpu))
    for lease in snapshot.leases:
        if lease.status == "released":
            continue
        model = models.get(lease.model)
        if lease.status == "confirmed":
            if model is not None and (model.state in ("awake", "sleeping") or
                                      (model.state == "stopped" and model.unit_active is False)):
                continue
            blockers.append(Blocker(lease.model, "unreconciled_confirmed_lease", lease.gpu))
            continue
        if lease.status not in ("pending", "stale"):
            blockers.append(Blocker(lease.model, "unknown_lease_status", lease.gpu))
            continue
        leased.add(lease.model)
        add(lease, lease.model)
    return allocations, leased, blockers


def plan_placement(
    snapshot: StateSnapshot, request: ModelState, *, waiting: bool = False,
    settings: PolicySettings = PolicySettings(),
) -> PlacementDecision:
    """Choose one GPU, then emit its complete eviction plan and final place.

    ``request`` carries trusted registry metadata (including default status and
    either budget_gb or util). Existing snapshot default status cannot be masked
    by the request. ``waiting=True`` requires eviction candidates idle >30 s;
    core owns the 120-second wait, lock, revalidation, unit checks and lease.
    No feasible GPU means no actions, including no speculative eviction.
    """
    blockers = list(snapshot_blockers(snapshot))
    if blockers:
        return PlacementDecision(blocked_by=tuple(blockers))
    if not request.name or request.state != "stopped":
        return PlacementDecision(blocked_by=(Blocker(request.name, "request_not_stopped"),))
    current = next((m for m in snapshot.models if m.name == request.name), None)
    if current is not None and (current.state != "stopped" or current.unit_active is True):
        return PlacementDecision(blocked_by=(Blocker(request.name, "model_already_accounted", current.gpu),))
    allocations, leased, accounting_blockers = _accounting(snapshot)
    if request.name in leased:
        return PlacementDecision(blocked_by=(Blocker(request.name, "outstanding_lease"),))
    blockers.extend(accounting_blockers)
    if any(b.gpu is None for b in accounting_blockers):
        return PlacementDecision(blocked_by=tuple(blockers))
    default = request.is_default or (current is not None and current.is_default)
    projection = Projection(snapshot, settings)
    feasible = []
    candidate_cards = []
    for gpu in sorted(snapshot.gpus, key=lambda g: g.index):
        if default and gpu.index != settings.exclusive_gpu:
            blockers.append(Blocker(request.name, "default_requires_exclusive_gpu", gpu.index))
            continue
        if any(b.gpu == gpu.index for b in accounting_blockers):
            continue
        if not known_number(gpu.total_gb) or gpu.total_gb <= 0 or not known_number(gpu.external_gb):
            blockers.append(Blocker(None, "unknown_gpu_capacity", gpu.index))
            continue
        active_reserves = [r for r in snapshot.reserves if r.gpu == gpu.index and
                           (not known_number(r.until) or r.until > snapshot.sampled_at)]
        if active_reserves:
            blockers.append(Blocker(None, "reserved", gpu.index, ", ".join(r.by for r in active_reserves)))
            continue
        if gpu.index != settings.exclusive_gpu and gpu.external_gb >= settings.shared_external_threshold_gb:
            blockers.append(Blocker(None, "external_pressure", gpu.index))
            continue
        budget = _budget(request, gpu.total_gb)
        if budget is None:
            blockers.append(Blocker(request.name, "unknown_request_budget", gpu.index))
            continue
        available = gpu.total_gb - gpu.external_gb - sum(
            amount for index, amount in allocations.values() if index == gpu.index)
        if available >= budget:
            feasible.append((gpu.index, budget))
        candidate_cards.append((gpu, budget, available))
    # A free fit always wins over even a zero-score eviction on another card.
    if feasible:
        gpu, budget = min(feasible)
        return PlacementDecision(actions=(Action("place", request.name, "placement", gpu),),
                                 gpu=gpu, budget_gb=budget)
    options = []
    for gpu, budget, available in candidate_cards:
        residents = [m for m in snapshot.models if m.gpu == gpu.index and m.state in ("awake", "sleeping")]
        eligible = []
        for model in projection.candidates(residents, stop=True, min_idle=30 if waiting else None):
            if model.name in leased:
                projection.block(model, "outstanding_lease")
            elif model.state == "awake" and (not projection.memory_known() or not known_number(model.weights_gb)):
                projection.block(model, "unknown_memory")
            else:
                eligible.append(model)
        for size in range(1, len(eligible) + 1):
            for selected in combinations(eligible, size):
                if available + sum(allocations[m.name][1] for m in selected) < budget:
                    continue
                cost = sum(projection.score(m) for m in selected)
                if isfinite(cost):
                    names = tuple(sorted(m.name for m in selected))
                    options.append(((cost, len(selected), gpu.index, names), gpu.index, budget, selected))
        for model in residents:
            projection.block(model, "occupied_budget")
    if not options:
        blockers.extend(projection.blockers)
        blockers.append(Blocker(request.name, "no_feasible_gpu"))
        return PlacementDecision(blocked_by=tuple(blockers))
    key, gpu, budget, selected = min(options, key=lambda item: item[0])
    plan = Projection(snapshot, settings)
    # Stop selected sleepers first to release RAM. Admission never evicts a model
    # outside the priced set. Projection.stop coalesces each local sleep/stop
    # pair after its admission bookkeeping, avoiding an intermediate transfer.
    for model in sorted(selected, key=lambda m: (m.state != "sleeping", projection.score(m), m.name)):
        if model.state == "awake":
            if not plan.sleep(model, "placement_eviction", reclaim=False):
                return PlacementDecision(blocked_by=tuple(plan.blockers))
            model = plan.models[model.name]
        if model.state != "stopped":
            plan.stop(model, "placement_eviction")
    plan.actions.append(Action("place", request.name, "placement", gpu))
    return PlacementDecision(actions=tuple(plan.actions), gpu=gpu, budget_gb=budget, eviction_cost=key[0])
