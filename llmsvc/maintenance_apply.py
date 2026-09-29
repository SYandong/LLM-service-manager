# Generated-By: Claude Code / claude-opus-5-5
"""Operator-only: apply an explicit llama-swap candidate through a maintenance transaction.

The registry can only add or remove directory-registered models. Everything
else an operator needs to change on a native-maintenance site (a wrapper port,
a display name, the startup preload, a pinned launcher path) had no path that
kept the catalog settled: editing the file by hand fences the scheduler with
``catalog_reconciliation_required`` (#296).

This submits the operator's candidate through the same ``submit_change`` path
the registry uses, so it gets a real witness binding, marker and maintenance
transaction; nothing here writes the configuration or a checkpoint itself.
The candidate must keep the same model set, and every model whose command
changes must be stopped, idle and observed, both now and when the queue claims
the job. Its native profile row follows the new command only after release.
"""

import difflib
import time
from typing import Any, Callable, Optional

from llmsvc.catalog import digest
from llmsvc.reload import ReloadError

TERMINAL = ("applied", "failed", "timed_out", "reconciliation_required")


class ApplyError(RuntimeError):
    pass


def plan_apply(original: bytes, candidate: bytes) -> dict[str, Any]:
    """Pure comparison of the live configuration and the operator candidate."""
    from llmsvc.registry import ModelRegistry, RegistryError
    try:
        before = ModelRegistry._decode(original)[0]
        after = ModelRegistry._decode(candidate)[0]
    except (RegistryError, ValueError) as exc:
        raise ApplyError("candidate is not a valid llama-swap configuration: " + str(exc)) from exc
    before_models, after_models = before.get("models"), after.get("models")
    if not isinstance(before_models, dict) or not isinstance(after_models, dict):
        raise ApplyError("configuration must contain a models mapping")
    if set(before_models) != set(after_models):
        raise ApplyError("candidate must keep the same models; add or remove them through the registry")
    if candidate == original:
        raise ApplyError("candidate is identical to the live configuration")
    changed = sorted(name for name in after_models if before_models[name] != after_models[name])
    command_changed = sorted(
        name for name in changed
        if not isinstance(before_models[name], dict) or not isinstance(after_models[name], dict)
        or before_models[name].get("cmd") != after_models[name].get("cmd")
        or before_models[name].get("cmdStop") != after_models[name].get("cmdStop"))
    preload_before, preload_after = _preload(before), _preload(after)
    # The new instance would start a preloaded model with its new command while
    # the profile row still holds the old one until release; the candidate
    # observation cannot attribute it, so the transaction would roll back.
    still_preloaded = sorted(set(command_changed) & set(preload_after))
    if still_preloaded:
        raise ApplyError("a model whose command changes must not stay in the startup preload: "
                         + ", ".join(still_preloaded))
    diff = "".join(difflib.unified_diff(
        original.decode("utf-8", "replace").splitlines(keepends=True),
        candidate.decode("utf-8", "replace").splitlines(keepends=True),
        fromfile="live", tofile="candidate"))
    return {"base_sha256": digest(original), "candidate_sha256": digest(candidate),
            "changed_models": changed, "command_changed": command_changed,
            "preload_removed": sorted(set(preload_before) - set(preload_after)), "diff": diff}


def _preload(document) -> list:
    hooks = document.get("hooks")
    startup = hooks.get("on_startup") if isinstance(hooks, dict) else None
    preload = startup.get("preload") if isinstance(startup, dict) else None
    if preload is None:
        return []
    if not isinstance(preload, list) or not all(isinstance(name, str) for name in preload):
        raise ApplyError("hooks.on_startup.preload must be a list of model names")
    return preload


def command_blockers(snapshot, names) -> list[dict[str, Any]]:
    """A model whose command changes must be observed stopped with nothing in flight."""
    if not names:
        return []
    if snapshot is None or getattr(snapshot, "errors", ()):
        return [{"reason": "state_unknown", "model": None}]
    models = {m.name: m for m in snapshot.models}
    activity = {a.model: a for a in getattr(snapshot, "activity", ())}
    blockers = []
    for name in names:
        model = models.get(name)
        if model is None:
            blockers.append({"reason": "state_unknown", "model": name})
        elif model.state != "stopped" or model.unit_active is not False:
            blockers.append({"reason": "model_not_stopped", "model": name})
        elif (activity.get(name) is not None and activity[name].in_flight is not None
                and activity[name].in_flight > 0):
            blockers.append({"reason": "model_in_flight", "model": name})
    return blockers


def preview_apply(scheduler, original: bytes, candidate: bytes, *,
                  expected_base_sha256: Optional[str] = None) -> dict[str, Any]:
    """Dry run: the diff and today's blockers; no adapter call, submission or write."""
    plan = plan_apply(original, candidate)
    snapshot = scheduler.sample_once()
    blockers = command_blockers(snapshot, plan["command_changed"])
    if expected_base_sha256 is not None and plan["base_sha256"] != expected_base_sha256:
        blockers.append({"reason": "unexpected_base", "model": None})
    runtime = scheduler.catalog
    queue = getattr(runtime, "queue", None)
    fenced = bool(scheduler.catalog_fenced or (queue is not None and (queue.fenced or queue._pending)))
    return {"dry_run": True, **plan, "blocked_by": blockers, "fenced": fenced}


def run_apply(scheduler, candidate: bytes, *, expected_base_sha256: Optional[str] = None,
              timeout_seconds: float = 900.0,
              sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    runtime = scheduler.catalog
    if runtime is None or runtime.transition is None:
        raise ApplyError("maintenance apply requires the configured maintenance transition")
    queue = runtime.queue
    original, _ = queue._read()
    plan = plan_apply(original, candidate)
    if expected_base_sha256 is not None and plan["base_sha256"] != expected_base_sha256:
        raise ApplyError("live configuration is not the expected base")
    scheduler.sample_once()
    blockers = command_blockers(queue.snapshot(), plan["command_changed"])
    if scheduler.catalog_fenced or queue.fenced or queue._pending:
        raise ApplyError("catalog or queue requires reconciliation")
    if blockers:
        raise ApplyError("blocked: " + ", ".join(
            b["reason"] + (":" + b["model"] if b["model"] else "") for b in blockers))
    if not runtime.can_submit():
        raise ApplyError("trusted catalog submission is unavailable")

    def transform(raw: bytes) -> bytes:
        if digest(raw) != plan["base_sha256"]:
            raise ReloadError("catalog source changed")
        return candidate

    def precheck() -> list[dict[str, Any]]:
        return command_blockers(queue.snapshot(), plan["command_changed"])

    # Only a model this candidate takes out of the preload may give up the
    # default role, and only for this submission (both prepare passes run
    # inside submit_change).
    runtime.allow_default_demotion = frozenset(plan["preload_removed"])
    try:
        submitted = runtime.submit_change(transform, precheck=precheck, description={
            "kind": "maintenance_apply", "models": plan["changed_models"],
            "candidate_sha256": plan["candidate_sha256"]})
    finally:
        runtime.allow_default_demotion = frozenset()
    job_id = submitted["id"]
    deadline = time.monotonic() + timeout_seconds
    job = queue.get(job_id)
    while job["status"] not in TERMINAL and time.monotonic() < deadline:
        scheduler.sample_once()
        runtime.process_once()
        job = queue.get(job_id)
        if job["status"] not in TERMINAL:
            sleep(1.0)
    profile_error = runtime.profile_errors.pop(job_id, None)
    return {"job": job, "ok": job["status"] == "applied" and profile_error is None,
            "profile_rows_error": profile_error, "changed_models": plan["changed_models"],
            "preload_removed": plan["preload_removed"],
            "base_sha256": plan["base_sha256"], "candidate_sha256": plan["candidate_sha256"],
            "fenced": bool(scheduler.catalog_fenced)}
