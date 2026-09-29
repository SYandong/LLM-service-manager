# Generated-By: Claude Code / claude-opus-5-5
"""Operator maintenance apply (#296): plan, blockers, profile rows and a real transaction."""
import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest
import yaml

from llmsvc.catalog import default_demotion
from llmsvc.leases import UnitObservation
from llmsvc.maintenance_apply import ApplyError, command_blockers, plan_apply, run_apply
from llmsvc.state import Activity, ModelState, StateSnapshot
from test_catalog_lifecycle import catalog  # noqa: F401  (fixture)
from test_maintenance_lifecycle import maintenance  # noqa: F401  (fixture)

WRAPPER = ("/opt/llama-swap/vllm-wrapper serve --vllm-url http://127.0.0.1:{port} --listen :{listen} "
           "-- /opt/rev/A/deploy/vllm-launch 0.6 vllm-{name} -- vllm serve /m --port {port}")


def config(models, preload=None):
    document = {"models": {name: dict(block) for name, block in models.items()}}
    if preload is not None:
        document["hooks"] = {"on_startup": {"preload": preload}}
    return yaml.safe_dump(document).encode()


def block(name, port, listen):
    return {"cmd": WRAPPER.format(name=name, port=port, listen=listen), "proxy": "http://localhost:%d" % listen}


def test_plan_detects_changed_and_command_changed_models():
    original = config({"a": block("a", 8101, 5806), "b": block("b", 8102, 5806), "c": block("c", 8103, 5800)},
                      preload=["a"])
    edited = yaml.safe_load(original)
    edited["hooks"]["on_startup"]["preload"] = []
    edited["models"]["b"] = block("b", 8102, 5807)
    edited["models"]["c"]["name"] = "Model C"
    plan = plan_apply(original, yaml.safe_dump(edited).encode())
    assert plan["changed_models"] == ["b", "c"]
    assert plan["command_changed"] == ["b"]
    assert "-  - a" in plan["diff"] or "- a" in plan["diff"]
    assert plan["base_sha256"] == hashlib.sha256(original).hexdigest()


@pytest.mark.parametrize("edit,message", [
    (lambda d: d["models"].pop("b"), "same models"),
    (lambda d: d["models"].update(z=block("z", 8110, 5810)), "same models"),
    (lambda d: None, "identical"),
])
def test_plan_refuses_model_set_changes_and_no_ops(edit, message):
    original = config({"a": block("a", 8101, 5800), "b": block("b", 8102, 5801)})
    document = yaml.safe_load(original)
    edit(document)
    candidate = original if message == "identical" else yaml.safe_dump(document).encode()
    with pytest.raises(ApplyError, match=message):
        plan_apply(original, candidate)


def test_plan_refuses_an_invalid_candidate():
    with pytest.raises(ApplyError, match="not a valid llama-swap configuration"):
        plan_apply(config({"a": block("a", 8101, 5800)}), b"models: [unclosed")


def snapshot(state="stopped", unit_active=False, in_flight=0, errors=()):
    return StateSnapshot(models=(ModelState("b", state=state, unit_active=unit_active),),
                         activity=(Activity("b", in_flight=in_flight),), errors=errors)


@pytest.mark.parametrize("snap,reasons", [
    (snapshot(), []),
    (snapshot(state="awake", unit_active=True), ["model_not_stopped"]),
    (snapshot(state="sleeping", unit_active=True), ["model_not_stopped"]),
    (snapshot(unit_active=None), ["model_not_stopped"]),
    (snapshot(in_flight=2), ["model_in_flight"]),
    (snapshot(errors=("gpus: deadline exceeded",)), ["state_unknown"]),
])
def test_command_change_needs_an_observed_idle_stopped_model(snap, reasons):
    assert [b["reason"] for b in command_blockers(snap, ["b"])] == reasons
    assert command_blockers(snap, []) == []


def test_default_demotion_is_the_only_allowed_profile_change():
    profile = {"unit": "vllm-a.service", "port": 8101, "util": .3, "is_default": True}
    assert default_demotion(profile, {**profile, "is_default": False})
    assert not default_demotion({**profile, "is_default": False}, profile)            # promotion
    assert not default_demotion(profile, {**profile, "is_default": False, "util": .4})
    assert not default_demotion(profile, profile)


def test_prepare_demotes_a_default_only_inside_an_apply_scope(catalog):
    c = catalog
    c.runtime.manifest["active"]["base"] = {**c.models["base"], "is_default": True}
    # A changed scheduler config alone (e.g. a registry import) cannot drop the role.
    with pytest.raises(ValueError, match="separate configuration reconciliation"):
        c.runtime.prepare(c.candidate, c.models, binding=c.binding)
    c.runtime.allow_default_demotion = frozenset({"base"})
    c.runtime.prepare(c.candidate, c.models, binding=c.binding)   # demoted inside the scope: accepted
    c.runtime.manifest["active"]["base"] = {**c.models["base"], "util": .3}
    with pytest.raises(ValueError, match="separate configuration reconciliation"):
        c.runtime.prepare(c.candidate, c.models, binding=c.binding)


def test_plan_refuses_a_command_change_for_a_model_left_in_preload():
    original = config({"a": block("a", 8101, 5806), "b": block("b", 8102, 5806)}, preload=["a"])
    kept = config({"a": block("a", 8101, 5807), "b": block("b", 8102, 5806)}, preload=["a"])
    with pytest.raises(ApplyError, match="must not stay in the startup preload: a"):
        plan_apply(original, kept)
    dropped = config({"a": block("a", 8101, 5807), "b": block("b", 8102, 5806)}, preload=[])
    plan = plan_apply(original, dropped)
    assert plan["command_changed"] == ["a"] and plan["preload_removed"] == ["a"]


def test_changed_command_rows_follow_only_after_release(catalog):
    c = catalog
    calls = []
    fake = SimpleNamespace(add=lambda entries: calls.append(("add", dict(entries))) or b"",
                           remove=lambda names: calls.append(("remove", tuple(names))),
                           restore=lambda raw: calls.append(("restore",)))
    c.runtime.transition = SimpleNamespace(native_profile=lambda: fake)
    original = config({"a": block("a", 8101, 5806), "b": block("b", 8102, 5806)})
    candidate = config({"a": block("a", 8101, 5806), "b": block("b", 8102, 5807)})
    restore, removed, updated = c.runtime._sync_native_profile(original, candidate)
    assert restore is None and removed == () and calls == []          # nothing written before release
    assert list(updated) == ["b"] and ":5807" in updated["b"]["process_argv"]
    c.runtime.profile_retire = ("job", removed, updated)
    c.runtime._retire_native_profile("other-job")                      # another job: untouched
    assert calls == []
    c.runtime.profile_retire = ("job", removed, updated)
    c.runtime._retire_native_profile("job")
    assert calls == [("add", updated)]


def _apply_through_fixture(c, monkeypatch, candidate):
    binding = replace(c.binding, candidate_sha256=hashlib.sha256(candidate).hexdigest())
    def submit_change(transform, *, description, precheck=None, dry_run=False, after_apply=None):
        original, _ = c.q._read()
        assert transform(original) == candidate
        prepared = c.runtime.prepare(candidate, {"base": c.models["base"]}, binding=binding)
        return c.runtime.enqueue(prepared, precheck=precheck, description=description)
    monkeypatch.setattr(c.runtime, "submit_change", submit_change)
    return run_apply(c.s, candidate, timeout_seconds=30, sleep=lambda s: None)


def test_run_apply_completes_a_real_maintenance_transaction(maintenance, monkeypatch):
    c = maintenance
    candidate = yaml.safe_dump({"macros": {"llmsvc_reload_generation": "gen_" + "1" * 32},
                                "models": {"base": {"name": "Base"}}}).encode()
    result = _apply_through_fixture(c, monkeypatch, candidate)
    assert result["job"]["status"] == "applied", result
    assert result["ok"] and result["profile_rows_error"] is None
    assert c.runtime.allow_default_demotion == frozenset()      # scope never outlives the submission
    assert result["changed_models"] == ["base"] and not result["fenced"]
    assert c.q._read()[0] == candidate
    assert c.backend.calls.count("stop_old") == 1 and c.backend.calls.count("start_candidate") == 1


def test_run_apply_reports_a_failed_profile_row_rewrite(maintenance, monkeypatch):
    c = maintenance
    candidate = yaml.safe_dump({"macros": {"llmsvc_reload_generation": "gen_" + "1" * 32},
                                "models": {"base": {"name": "Base"}}}).encode()
    original_process = c.runtime.process_once
    def process_once():
        result = original_process()
        for job_id in list(c.runtime.jobs):
            c.runtime.profile_errors.setdefault(job_id, "OSError: read-only profile")
        return result
    monkeypatch.setattr(c.runtime, "process_once", process_once)
    result = _apply_through_fixture(c, monkeypatch, candidate)
    assert result["job"]["status"] == "applied"
    assert result["ok"] is False and "read-only profile" in result["profile_rows_error"]


def test_run_apply_refuses_a_command_change_for_an_awake_model(maintenance, monkeypatch):
    c = maintenance
    c.world["units"]["base"] = UnitObservation(True, False, True, "lease", "0" * 31 + "1")
    c.s.sample_once()
    candidate = yaml.safe_dump({"models": {"base": {"cmd": "changed"}}}).encode()
    with pytest.raises(ApplyError, match="model_not_stopped:base"):
        _apply_through_fixture(c, monkeypatch, candidate)
    assert c.backend.calls.count("stop_old") == 0


def test_run_apply_refuses_an_unexpected_base(maintenance):
    c = maintenance
    candidate = yaml.safe_dump({"models": {"base": {"name": "Base"}}}).encode()
    with pytest.raises(ApplyError, match="expected base"):
        run_apply(c.s, candidate, expected_base_sha256="0" * 64, timeout_seconds=1)
