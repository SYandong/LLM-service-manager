# Generated-By: Codex / gpt-6-astra
"""Immutable policy results and local projections; no execution or persistence."""

from dataclasses import dataclass, replace
from math import isfinite
from typing import Mapping, Optional, Tuple

from llmsvc.state import Action, Activity, Blocker, ModelState, StateSnapshot
from .ranking import keep_value


@dataclass(frozen=True)
class PolicySettings:
    exclusive_gpu: int = 0
    fixed_ttl_seconds: float = 600.0
    exclusive_ttl_seconds: float = 3600.0
    shared_ttl_seconds: float = 300.0
    shared_external_threshold_gb: float = 1.0
    shared_free_threshold_gb: float = 10.0
    sleeping_residual_gb: float = 2.0

    def __post_init__(self):
        if isinstance(self.exclusive_gpu, bool) or not isinstance(self.exclusive_gpu, int) or self.exclusive_gpu < 0:
            raise ValueError("exclusive_gpu must be a non-negative integer")
        for key, value in vars(self).items():
            if key != "exclusive_gpu" and not known_number(value):
                raise ValueError(f"{key} must be finite and non-negative")


@dataclass(frozen=True)
class Decision:
    actions: Tuple[Action, ...] = ()
    blocked_by: Tuple[Blocker, ...] = ()
    estimated_freed_gb: float = 0.0
    gpu: Optional[int] = None


def known_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) and value >= 0


def snapshot_blockers(snapshot):
    if not known_number(snapshot.sampled_at):
        return (Blocker(None, "unknown_sample_time"),)
    if snapshot.errors:
        return (Blocker(None, "unknown_snapshot: " + "; ".join(snapshot.errors)),)
    if len({m.name for m in snapshot.models}) != len(snapshot.models):
        return (Blocker(None, "duplicate_model_accounting"),)
    if len({a.model for a in snapshot.activity}) != len(snapshot.activity):
        return (Blocker(None, "duplicate_activity"),)
    if len({g.index for g in snapshot.gpus}) != len(snapshot.gpus):
        return (Blocker(None, "duplicate_gpu"),)
    return ()


class Projection:
    """Private speculative state: inputs remain unchanged, even for dry-run.

    The action layer must stop on failure and recollect/replan before executing
    dependent actions. Estimated RAM/VRAM releases are never observed results.
    """

    def __init__(
        self, snapshot: StateSnapshot, settings: PolicySettings, *,
        exclusions: Optional[Mapping[str, str]] = None,
    ):
        self.exclusions = {} if exclusions is None else dict(exclusions)
        if any(not isinstance(name, str) or not name.strip()
               or not isinstance(reason, str) or not reason.strip()
               for name, reason in self.exclusions.items()):
            raise ValueError("exclusions require nonempty model names and reasons")
        self.snapshot = snapshot
        self.settings = settings
        self.models = {m.name: m for m in snapshot.models}
        self.activity = {a.model: a for a in snapshot.activity}
        self.actions = []
        self.blockers = list(snapshot_blockers(snapshot))
        self.available = snapshot.memory.host_available_gb
        self.sleeping = snapshot.memory.sleeping_weights_gb
        sleepers = [m for m in snapshot.models if m.state == "sleeping"]
        if known_number(self.sleeping) and all(known_number(m.weights_gb) for m in sleepers):
            self.sleeping = max(self.sleeping, sum(m.weights_gb for m in sleepers))
        elif sleepers:
            self.sleeping = None

    def block(self, model, reason):
        activity = self.activity.get(model.name, Activity(model.name))
        blocker = Blocker(model.name, reason, model.gpu, ", ".join(activity.by) or None, activity.in_flight)
        if blocker not in self.blockers:
            self.blockers.append(blocker)
        # Real user intent and independent controller eligibility are separate
        # causes. Preserve the pin blocker instead of relabeling it as a guard.
        exclusion = self.exclusions.get(model.name)
        if reason == "pinned_until" and exclusion is not None:
            excluded = replace(blocker, reason=exclusion)
            if excluded not in self.blockers:
                self.blockers.append(excluded)

    def protection(self, model, *, stop=False, min_idle=None):
        if model.state not in ("awake", "sleeping"):
            return "unknown_model_state"
        if any(p.model == model.name and (not known_number(p.until) or p.until > self.snapshot.sampled_at) for p in self.snapshot.pins):
            return "pinned_until"
        if model.name in self.exclusions:
            return self.exclusions[model.name]
        activity = self.activity.get(model.name)
        if activity is None or not isinstance(activity.in_flight, int) or isinstance(activity.in_flight, bool) or activity.in_flight < 0:
            return "unknown_in_flight"
        if activity.in_flight:
            return "in_flight"
        if stop and model.is_default:
            return "default_model"
        if min_idle is not None:
            if not known_number(activity.last_request_at) or activity.last_request_at > self.snapshot.sampled_at:
                return "unknown_activity"
            if self.snapshot.sampled_at - activity.last_request_at <= min_idle:
                return "recently_active"
        return None

    def score(self, model):
        activity = self.activity.get(model.name)
        if activity is None or not known_number(activity.last_request_at) or activity.last_request_at > self.snapshot.sampled_at:
            raise ValueError("unknown_activity")
        try:
            return keep_value(activity.requests_last_hour, model.cold_start_seconds, self.snapshot.sampled_at - activity.last_request_at)
        except ValueError as exc:
            raise ValueError("unknown_keep_value") from exc

    def candidates(self, models, *, stop=False, min_idle=None):
        ranked = []
        for model in models:
            reason = self.protection(model, stop=stop, min_idle=min_idle)
            if reason:
                self.block(model, reason)
                continue
            try:
                ranked.append(((model.is_default, self.score(model), model.name), model))
            except ValueError as exc:
                self.block(model, str(exc))
        return [model for _, model in sorted(ranked, key=lambda item: item[0])]

    def memory_known(self):
        return all(known_number(value) for value in (
            self.available, self.sleeping, self.snapshot.memory.budget_gb,
            self.snapshot.memory.host_min_available_gb,
        )) and all(m.state != "unknown" for m in self.models.values())

    def fits_sleep(self, weight):
        return self.memory_known() and known_number(weight) and (
            self.available - weight >= self.snapshot.memory.host_min_available_gb
            and self.sleeping + weight <= self.snapshot.memory.budget_gb
        )

    def stop(self, model, reason):
        # A later stop makes this decision's earlier sleep unnecessary. Keep the
        # stop position and virtual sleep/release accounting below unchanged.
        self.actions = [a for a in self.actions if not (a.kind == "sleep" and a.model == model.name)]
        self.actions.append(Action("stop", model.name, reason, model.gpu))
        if model.state == "sleeping":
            if known_number(self.available) and known_number(model.weights_gb):
                self.available += model.weights_gb
            else:
                self.available = None
            if known_number(self.sleeping) and known_number(model.weights_gb):
                self.sleeping = max(0.0, self.sleeping - model.weights_gb)
            else:
                self.sleeping = None
        self.models[model.name] = replace(model, state="stopped")

    def sleep(self, model, reason, *, reclaim=True):
        """Admit before sleep; ordinary known over-budget models stop instead.

        Unknown RAM blocks the decision, including the direct-stop fallback.
        Only known insufficient RAM permits that fallback under DESIGN §4.3.
        """
        protected = self.protection(model)
        if protected:
            self.block(model, protected)
            return False
        if not self.memory_known() or not known_number(model.weights_gb):
            self.block(model, "unknown_memory")
            return False
        if not self.fits_sleep(model.weights_gb) and reclaim:
            sleepers = [m for m in self.models.values() if m.state == "sleeping"]
            for sleeper in self.candidates(sleepers, stop=True):
                self.stop(sleeper, "sleep_memory_admission")
                if self.fits_sleep(model.weights_gb):
                    break
        if not self.fits_sleep(model.weights_gb):
            if model.is_default:
                self.block(model, "memory_budget")
                return False
            self.stop(model, "sleep_memory_admission")
            return True
        self.actions.append(Action("sleep", model.name, reason, model.gpu))
        self.sleeping += model.weights_gb
        self.available -= model.weights_gb
        self.models[model.name] = replace(model, state="sleeping", resident_gb=self.settings.sleeping_residual_gb)
        return True

    def result(self, *, freed=0.0, gpu=None):
        return Decision(tuple(self.actions), tuple(self.blockers), freed, gpu)
