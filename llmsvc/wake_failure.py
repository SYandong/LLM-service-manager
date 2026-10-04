# Generated-By: Claude Code / claude-opus-5-5
"""Opt-in recovery of a wedged warm wake (#300); see llmsvc/FAULTS.md.

A model whose data plane is ``ready`` while its backend still reports
``is_sleeping: true`` past the grace period cannot serve: the wrapper proxies
to a sleeping daemon and every request hangs. The pure decision comes from
``plan_wake_failure``. This executor uses only the permitted operations: the
protected dispatcher's stop of the configured ``vllm-*.service`` unit (whose
in-flight gate does not apply to ``wake_failed``), the existing exit/lease
reconciliation, and then llama-swap ``POST /api/models/unload/{id}`` so the
next request cold-starts through ``/v1/place``. Pins and the default model are
never touched; they are reported as blockers.

Catalog publication replaces the model-action controller, its transport and
placement accounting; ``CatalogRuntime._publish`` rebuilds this controller
too, and an instance bound to a replaced controller refuses to act.
"""

import json
import logging
from dataclasses import asdict
from urllib.parse import quote

from llmsvc.actions import ActionDispatchError
from llmsvc.policy.wake_failure import plan_wake_failure

LOG = logging.getLogger("llmsvc.wake_failure")

# Unload attempts per released lease, including the first one. A retry is only
# sent when a later published sample still shows llama-swap ``ready`` for the
# model while its unit is inactive and its account is released.
MAX_UNLOAD_ATTEMPTS = 3


class WakeFailureRecoveryController:
    """One observation per published sample; at most one recovery per cycle."""

    def __init__(self, scheduler, *, accounting=None, monotonic=None):
        self.scheduler = scheduler
        self.controller = scheduler.model_actions
        self.monotonic = monotonic or scheduler.monotonic
        if accounting is None and self.controller is not None:
            from llmsvc.leases import PlacementController
            accounting = PlacementController(scheduler, self.controller.transport, monotonic=self.monotonic)
        self.accounting = accounting
        self.windows = {}
        self.generation = None
        self.reported = set()
        self.previewed = set()
        # model -> {"lease_id", "attempts"}: released by this path, unload unfinished.
        self.unloads = {}

    def configured(self):
        return self.scheduler.config.wake_failure_recovery_enabled

    def current(self):
        """False once a catalog publication replaced the controller we hold."""
        return self.controller is not None and self.controller is self.scheduler.model_actions

    def enabled(self):
        config = self.scheduler.config
        return (config.wake_failure_recovery_enabled and config.model_actions_enabled and not config.read_only
                and self.current() and self.accounting is not None
                and self.scheduler.store is not None and not self.scheduler.store.read_only)

    def _guards(self, snapshot):
        controller = self.controller
        confirmed = {lease.model for lease in snapshot.leases if lease.status == "confirmed"}
        guards = {}
        for model in snapshot.models:
            unit = controller.transport.units.get(model.name)
            if (unit is None or model.unit != unit or model.name not in controller.transport.active_models
                    or self.accounting.transport.units.get(model.name) != unit):
                guards[model.name] = "unmanaged_or_changed_unit"
            elif model.name not in confirmed:
                guards[model.name] = "unleased_model"
            elif controller._fault_pending(model.name):
                guards[model.name] = "fault_recovery_pending"
            elif controller._recovery_pending(model.name):
                guards[model.name] = "sleeping_recovery_pending"
            elif model.name in controller.pending or controller.free_active:
                guards[model.name] = "operation_in_progress"
        return guards

    def plan(self, snapshot):
        """Pure decision against the current windows; caller holds action_lock."""
        from llmsvc.policy.wake_failure import WakeFailureDecision
        from llmsvc.state import Blocker
        if not self.current() or self.accounting is None:
            return WakeFailureDecision(blocked_by=(Blocker(None, "wake_failure_executor_unavailable"),))
        if not self.controller._fresh(snapshot):
            return WakeFailureDecision(blocked_by=(Blocker(None, "unknown_or_stale_snapshot"),))
        return plan_wake_failure(snapshot, self.windows, exclusions=self._guards(snapshot),
                                 grace_seconds=self.scheduler.config.wake_failure_grace_seconds)

    def _observe(self):
        """Advance the windows once per published sample; None if nothing new."""
        with self.scheduler.action_lock:
            generation = self.scheduler._sample_published
            if generation == self.generation:
                return None
            self.generation = generation
            decision = self.plan(self.controller._snapshot() if self.current() else self.scheduler.snapshot())
            self.windows = dict(decision.windows)
            keys = {(name, window[0]) for name, window in self.windows.items()}
            self.reported &= keys
            self.previewed &= keys
            return decision

    def run_once(self, *, dry_run=False):
        decision = self._observe()
        if decision is None:
            return {"status": "observing"}
        would = [asdict(action) for action in decision.actions]
        blocked = [asdict(blocker) for blocker in decision.blocked_by if blocker.model is not None]
        if dry_run or not self.enabled():
            # Read-only/--dry-run: plan only. No events, probes, transport or writes.
            # The periodic preview logs each (model, window) once; an explicit
            # dry run always logs.
            fresh = {(item["model"], self.windows.get(item["model"], (None,))[0]) for item in would + blocked}
            if dry_run or fresh - self.previewed:
                self.previewed |= fresh
                LOG.info(json.dumps({"kind": "wake_failure_preview", "dry_run": True,
                                     "would": would, "blocked_by": blocked}, allow_nan=False))
            return {"status": "preview", "would": would, "blocked_by": blocked}
        with self.scheduler.changed:
            for blocker in decision.blocked_by:
                key = (blocker.model, self.windows.get(blocker.model, (None,))[0])
                if blocker.model is not None and key not in self.reported:
                    self.reported.add(key)
                    self.scheduler.emit("wake_failure_blocked", model=blocker.model,
                                        detail={**asdict(blocker), "dry_run": False})
        if decision.actions:
            return self._recover(decision.actions[0])
        retry = self._retry_unload()
        if retry is not None:
            return retry
        return {"status": "blocked" if blocked else "observing", "blocked_by": blocked}

    def _unload(self, name):
        """Own bounded budget: llama-swap may hold the reply while cmdStop fails."""
        config = self.scheduler.config
        deadline = self.monotonic() + config.request_timeout_seconds + config.wake_failure_unload_allowance_seconds
        with self.controller._locked(deadline):
            self.controller._enabled()
        # Outside the lock so the rest of the scheduler keeps running.
        try:
            status = self.controller.transport.http_request(
                "POST", "/api/models/unload/" + quote(name, safe=""), deadline=deadline)
        except ActionDispatchError:
            raise
        except Exception as exc:
            raise ActionDispatchError("proxy_unload_uncertain") from exc
        if type(status) is not int or not 200 <= status < 300:
            raise ActionDispatchError("proxy_unload_rejected")

    def _retry_unload(self):
        """Retry one unfinished unload for a lease this path released; bounded."""
        with self.scheduler.action_lock:
            snapshot = self.controller._snapshot()
            fresh = self.controller._fresh(snapshot)
            target = None
            for name, entry in sorted(self.unloads.items()):
                models = [model for model in snapshot.models if model.name == name]
                accounts = [lease for lease in snapshot.leases if lease.model == name]
                if (any(lease.lease_id != entry["lease_id"] or lease.status != "released" for lease in accounts)
                        or entry["attempts"] >= MAX_UNLOAD_ATTEMPTS or len(models) != 1
                        or models[0].unit_active is True or models[0].swap_state == "stopped"):
                    # A new account/instance, exhausted budget, or settled proxy.
                    self.unloads.pop(name)
                    continue
                if (target is None and fresh and models[0].unit_active is False
                        and models[0].swap_state == "ready" and name not in self.controller.pending):
                    target = name
            if target is None:
                return None
            entry = self.unloads[target]
            entry["attempts"] += 1
            self.controller.pending.add(target)
        result = {"model": target, "lease_id": entry["lease_id"], "attempt": entry["attempts"],
                  "status": "complete", "error": None}
        try:
            self._unload(target)
            self.unloads.pop(target, None)
        except Exception as exc:
            result.update(status="failed", error=exc.reason if isinstance(exc, ActionDispatchError)
                          else "wake_failure_" + type(exc).__name__)
        finally:
            with self.scheduler.changed:
                self.controller.pending.discard(target)
                self.scheduler.emit("wake_failure_unload_retry", model=target, detail={**result, "dry_run": False})
                self.scheduler.changed.notify_all()
        return result

    def _recover(self, action):
        config = self.scheduler.config
        controller = self.controller
        deadline = self.monotonic() + config.free_timeout_seconds
        first, last, samples = self.windows[action.model]
        result = {"model": action.model, "gpu": action.gpu, "lease_id": None, "status": "blocked",
                  "stop_confirmed": False, "proxy_unloaded": False, "error": None,
                  "since": first, "samples": samples}
        owned = submitted = False
        try:
            controller._enabled()
            controller._refresh(deadline)  # A fresh round, as an explicit stop takes.
            with controller._locked(deadline):
                controller._enabled()
                snapshot = controller._snapshot()
                # Revalidate against the same windows; never act on a stale plan.
                if action not in self.plan(snapshot).actions:
                    raise ActionDispatchError("wake_failure_condition_changed")
                account = next((lease for lease in snapshot.leases
                                if lease.model == action.model and lease.status == "confirmed"), None)
                identity = self.accounting._inspect(action.model, deadline)
                if account is None or not identity.active or identity.lease_id != account.lease_id:
                    raise ActionDispatchError("unit_identity_unconfirmed")
                result["lease_id"] = account.lease_id
                result["in_flight"] = next((item.in_flight for item in snapshot.activity
                                            if item.model == action.model), None)
                self.scheduler.emit("wake_failure_detected", model=action.model, detail={
                    "lease_id": account.lease_id, "gpu": action.gpu, "since": first, "last": last,
                    "samples": samples, "in_flight": result["in_flight"],
                    "grace_seconds": config.wake_failure_grace_seconds, "dry_run": False})
                controller.pending.add(action.model)
                owned = True
                try:
                    controller.dispatcher.execute(action, dry_run=False, deadline=deadline)
                except ActionDispatchError as exc:
                    if not exc.attempted:
                        raise
                    result["error"] = exc.reason  # A failed stop may still have taken effect.
                submitted = True
            # Existing observation path: two newer stopped rounds, and the
            # explicitly stopped confirmed lease reconciled to released.
            confirmed = self.accounting._observe_victim(action, deadline, reconcile_exit=True)
            result["stop_confirmed"] = confirmed
            if not confirmed:
                raise ActionDispatchError(result["error"] or "stop_effect_unconfirmed")
            with self.scheduler.action_lock:
                # Later samples retry the unload if this attempt does not settle.
                self.unloads[action.model] = {"lease_id": account.lease_id, "attempts": 1}
            self._unload(action.model)
            with self.scheduler.action_lock:
                self.unloads.pop(action.model, None)
            result["proxy_unloaded"] = True
            result["status"] = "partial" if result["error"] else "complete"
        except Exception as exc:
            reason = exc.reason if isinstance(exc, ActionDispatchError) else "wake_failure_" + type(exc).__name__
            result["error"] = result["error"] or reason
            result["status"] = "partial" if result["stop_confirmed"] else "failed" if submitted else "blocked"
        finally:
            with self.scheduler.changed:
                if owned:
                    controller.pending.discard(action.model)
                self.windows.pop(action.model, None)
                self.scheduler.emit("wake_failure_result", model=action.model,
                                    detail={**result, "dry_run": False})
                self.scheduler.changed.notify_all()
        return result
