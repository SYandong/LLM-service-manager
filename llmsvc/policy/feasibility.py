# Generated-By: Codex / gpt-6.1-sol
"""Conservative, pure classification of placement blockers (#303)."""

from typing import Mapping, Optional

from llmsvc.state import StateSnapshot
from .common import Decision, PolicySettings, snapshot_blockers


# These conditions cannot be removed by llmsvc's placement actions. They may
# still change outside the controller, so the caller must observe a grace period.
HOPELESS_REASONS = frozenset({
    "external_pressure", "outside_placement_pool", "gpu_exclusions",
    "default_requires_exclusive_gpu", "unknown_request_budget",
    "request_exceeds_gpu_capacity",
})


def placement_unplaceable(
    snapshot: StateSnapshot, decision: Decision, *,
    settings: PolicySettings = PolicySettings(),
    gpu_exclusions: Optional[Mapping[int, str]] = None,
) -> bool:
    """Require only hopeless blockers on every observed candidate GPU.

    Missing pool members, global uncertainty, unrecognised reasons and any
    actionable plan remain transient. The summary ``no_feasible_gpu`` alone is
    never evidence. Caller-supplied GPU exclusions are destination constraints;
    their custom reasons are recognised only on the explicitly excluded card.
    """
    if decision.actions or decision.gpu is not None or snapshot_blockers(snapshot):
        return False
    observed = {gpu.index for gpu in snapshot.gpus}
    pool = observed if settings.placement_gpus is None else set(settings.placement_gpus)
    if not pool or not pool <= observed:
        return False
    blockers = snapshot.blocked_by + decision.blocked_by
    if any(blocker.gpu is None and blocker.reason != "no_feasible_gpu" for blocker in blockers):
        return False
    exclusions = gpu_exclusions or {}
    for index in pool:
        reasons = [blocker.reason for blocker in blockers if blocker.gpu == index]
        if not reasons or any(reason not in HOPELESS_REASONS and reason != exclusions.get(index)
                              for reason in reasons):
            return False
    return True
