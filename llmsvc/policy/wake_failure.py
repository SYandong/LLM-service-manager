# Generated-By: Claude Code / claude-opus-5-5
"""Detect a wedged warm wake: data plane ready while the backend still sleeps (#300).

vllm-wrapper ignores a failed ``/wake_up`` (for example a CUDA OOM while the
GPU was taken by another tenant) once ``/health`` passes, so llama-swap marks
the model ``ready`` and proxies requests to a daemon that still reports
``is_sleeping: true``. Those requests hang until the client gives up, which
keeps ``in_flight`` above zero forever and the ordinary protection table then
blocks both wake and stop. This pure function turns the persistent
ready+sleeping combination into a ``wake_failed`` stop decision. It performs
no I/O; the executor in ``llmsvc/wake_failure.py`` owns transport and fences.
"""

from dataclasses import dataclass, field
from typing import Mapping, Optional, Tuple

from llmsvc.state import Action, Blocker, ModelState, StateSnapshot
from .common import known_number, snapshot_blockers


WAKE_FAILED_REASON = "wake_failed"
# Distinct published rounds required on top of the grace period, so a single
# racy round can never satisfy the predicate however long the gap before it.
MIN_SAMPLES = 3

# model -> (first matching sampled_at, last matching sampled_at, distinct rounds)
Window = Tuple[float, float, int]


@dataclass(frozen=True)
class WakeFailureDecision:
    actions: Tuple[Action, ...] = ()
    blocked_by: Tuple[Blocker, ...] = ()
    windows: Mapping[str, Window] = field(default_factory=dict)


def wedged(model: ModelState) -> bool:
    """Backend alive and healthy but asleep, while the data plane says ready."""
    return (model.state == "sleeping" and model.unit_active is True and model.health_ok is True
            and model.is_sleeping is True and model.swap_state == "ready")


def plan_wake_failure(
    snapshot: StateSnapshot, windows: Optional[Mapping[str, Window]] = None, *,
    grace_seconds: float, exclusions: Optional[Mapping[str, str]] = None,
    min_samples: int = MIN_SAMPLES,
) -> WakeFailureDecision:
    """Advance per-model evidence windows and decide which models to recover.

    The caller passes back the returned ``windows`` with the next published
    snapshot. Re-evaluating the same snapshot is idempotent. A snapshot with
    collection errors or an unknown time is no evidence and restarts every
    window, as does any round in which the model is not wedged.

    ``in_flight`` is deliberately not a protection here: requests routed to a
    sleeping backend cannot complete (DESIGN §4 fault exception). Pins and the
    default model keep their protection and are reported as blockers only.
    ``exclusions`` carries controller eligibility guards (unmanaged unit,
    missing confirmed lease, pending fault/recovery fences, busy model).
    """
    if not known_number(grace_seconds) or grace_seconds <= 0:
        raise ValueError("grace_seconds must be a finite positive number")
    if type(min_samples) is not int or min_samples < 2:
        raise ValueError("min_samples must be an integer >= 2")
    blockers = snapshot_blockers(snapshot)
    if blockers:
        return WakeFailureDecision(blocked_by=blockers)
    previous = dict(windows or {})
    guards = dict(exclusions or {})
    sampled = snapshot.sampled_at
    activity = {item.model: item for item in snapshot.activity}
    current = {}
    actions = []
    blocked = []
    for model in sorted(snapshot.models, key=lambda item: item.name):
        if not wedged(model):
            continue
        window = previous.get(model.name)
        if (window is None or len(window) != 3 or not known_number(window[0])
                or not known_number(window[1]) or not window[0] <= window[1] <= sampled):
            window = (sampled, sampled, 1)
        elif sampled > window[1]:
            window = (window[0], sampled, window[2] + 1)
        current[model.name] = window
        if sampled - window[0] < grace_seconds or window[2] < min_samples:
            continue
        row = activity.get(model.name)
        in_flight = row.in_flight if row is not None else None
        user = (", ".join(row.by) or None) if row is not None else None
        if any(pin.model == model.name and (not known_number(pin.until) or pin.until > sampled)
               for pin in snapshot.pins):
            reason = "pinned_until"
        elif model.is_default is not False:
            # AGENTS.md §6: the default model is never hard-stopped. Only the
            # strict #130 fault path may take that exception; surface it here.
            reason = "default_model"
        else:
            reason = guards.get(model.name)
        if reason:
            blocked.append(Blocker(model.name, reason, model.gpu, user, in_flight))
        else:
            actions.append(Action("stop", model.name, WAKE_FAILED_REASON, model.gpu))
    return WakeFailureDecision(tuple(actions), tuple(blocked), current)
