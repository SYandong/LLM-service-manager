# Generated-By: Codex / gpt-6-astra
"""Pure generation planning and existing add/remove/witness composition."""
import hashlib
from dataclasses import replace

import pytest
import yaml

from llmsvc.registry import (ModelRegistry, RegistryError, add_full_weight_model,
                            plan_generation_candidate, remove_temporary_model)
from llmsvc.reload_witness import (BindingObservation, CandidateBinding, GenerationRead,
                                  InstanceIdentity, check_visibility)
from llmsvc.state import Activity, ModelState
from test_registry_api import registry

OLD = "gen_" + "1" * 32
NEW = "gen_" + "2" * 32
ENDPOINT = "http://127.0.0.1:19001/api/mcp"
INSTANCE = InstanceIdentity(123, "456")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def plan(data, **overrides):
    arguments = dict(expected_sha256=digest(data), generation=NEW, endpoint=ENDPOINT, instance=INSTANCE)
    arguments.update(overrides)
    return plan_generation_candidate(data, **arguments)


def source(marker="", newline="\n"):
    return ("# 管理员配置\n---\nmacros: # preserve header\n"
            + marker + "  other: '001' # keep scalar spelling\n"
            "\n# unchanged models and unrelated anchors\nmodels:\n"
            "  base: &resident {cmd: 'serve', metadata: {label: 'foo'}}\n"
            "extra: *resident # keep alias\n...\n# tail\n").replace("\n", newline).encode()


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("quote", ["", "'", '"'])
def test_existing_scalar_only_is_replaced_and_bound(newline, quote):
    original = source(f"  llmsvc_reload_generation: {quote}{OLD}{quote} # marker note\n", newline)
    result = plan(original)
    assert result.candidate == original.replace(OLD.encode(), NEW.encode())
    assert result.previous_generation == OLD
    assert result.source_sha256 == digest(original)
    assert result.binding == CandidateBinding(ENDPOINT, NEW, INSTANCE, digest(result.candidate))
    assert CandidateBinding.from_dict(result.binding.to_dict()) == result.binding
    assert plan(original) == result  # No random nonce or stateful freshness claim.


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_missing_marker_insert_preserves_every_original_byte(newline):
    original = source(newline=newline)
    insertion = ("  llmsvc_reload_generation: " + NEW + newline).encode()
    result = plan(original)
    assert result.candidate.replace(insertion, b"", 1) == original
    expected = yaml.safe_load(original)
    expected["macros"]["llmsvc_reload_generation"] = NEW
    assert yaml.safe_load(result.candidate) == expected
    assert result.previous_generation is None


@pytest.mark.parametrize("bad", ["no_macros", "empty_macros", "flow_macros", "duplicate_macros",
    "duplicate_marker", "merge_macros", "anchored_macros", "aliased_macros", "anchored_marker",
    "aliased_marker", "tagged_marker", "multiline_marker", "invalid_marker", "flow_root",
    "no_final_newline", "mixed_newlines", "invalid_utf8", "nested_definition", "business_reference"])
def test_unsupported_or_referenced_layouts_fail_closed(bad):
    original = source()
    if bad == "no_macros":
        original = b"models: {}\n"
    elif bad == "empty_macros":
        original = b"macros: {}\nmodels: {}\n"
    elif bad == "flow_macros":
        original = b"macros: {x: y}\nmodels: {}\n"
    elif bad == "duplicate_macros":
        original = b"macros:\n  x: y\n" + original.replace(b"---\n", b"")
    elif bad == "duplicate_marker":
        original = source((f"  llmsvc_reload_generation: {OLD}\n") * 2)
    elif bad == "merge_macros":
        original = source("  <<: {foo: bar}\n")
    elif bad == "anchored_macros":
        original = original.replace(b"macros: #", b"macros: &copied #")
    elif bad == "aliased_macros":
        original = b"anchor: &macros\n  x: y\nmacros: *macros\nmodels: {}\n"
    elif bad in ("anchored_marker", "tagged_marker"):
        prefix = "&g " if bad == "anchored_marker" else "!!str "
        original = source(f"  llmsvc_reload_generation: {prefix}{OLD}\n")
    elif bad == "aliased_marker":
        original = source(f"  x: &g {OLD}\n  llmsvc_reload_generation: *g\n")
    elif bad == "multiline_marker":
        original = source(f"  llmsvc_reload_generation: >-\n    {OLD}\n")
    elif bad == "invalid_marker":
        original = source("  llmsvc_reload_generation: 42\n")
    elif bad == "flow_root":
        original = b"{macros: {x: y}, models: {}}\n"
    elif bad == "no_final_newline":
        original = original.rstrip(b"\n")
    elif bad == "mixed_newlines":
        original = original.replace(b"macros: #", b"\r\nmacros: #")
    elif bad == "invalid_utf8":
        original = b"\xff" + original
    elif bad == "nested_definition":
        original = original.replace(b"metadata: {label: 'foo'}", b"macros: {llmsvc_reload_generation: unused}")
    else:
        original = original.replace(b"cmd: 'serve'", b"cmd: 'serve ${llmsvc_reload_generation}'")
    with pytest.raises(RegistryError):
        plan(original)


@pytest.mark.parametrize("override", [{"generation": OLD}, {"generation": "bad"},
    {"generation": "gen_" + "A" * 32}, {"generation": None}, {"expected_sha256": "0" * 64},
    {"instance": None}, {"instance": InstanceIdentity(True, "1")},
    {"endpoint": "http://localhost:19001/api/mcp"}, {"endpoint": "https://127.0.0.1/api/mcp"}])
def test_bad_stale_or_unchanged_binding_is_rejected(override):
    with pytest.raises(RegistryError):
        plan(source(f"  llmsvc_reload_generation: {OLD}\n"), **override)


def test_existing_nonce_elsewhere_is_not_reused():
    with pytest.raises(RegistryError, match="every existing scalar"):
        plan(source().replace(b"'001'", NEW.encode()))


def test_planner_bounds_input_and_candidate():
    prefix = source()
    original = prefix + b"#" + b" " * (1024 * 1024 - len(prefix) - 2) + b"\n"
    with pytest.raises(RegistryError, match="candidate exceeds"):
        plan(original)
    with pytest.raises(RegistryError, match="source bytes"):
        plan(original + b"\n")
    with pytest.raises(RegistryError, match="source bytes"):
        plan_generation_candidate("text", expected_sha256="", generation=NEW, endpoint=ENDPOINT, instance=INSTANCE)


def test_add_marker_remove_marker_chain_is_pure_and_visibility_is_not_settlement(registry, monkeypatch):
    api, queue, weights, state, clock, calls, units, _ = registry
    original = queue.path.read_bytes()
    before = {p: p.read_bytes() for p in queue.path.parent.rglob("*") if p.is_file()}
    def forbidden(*args, **kwargs):
        pytest.fail("Offline candidate planner invoked a runtime or persistence operation")
    for name in ("enqueue", "_stage", "notify_reload", "validate", "process_once"):
        monkeypatch.setattr(queue, name, forbidden)
    monkeypatch.setattr("llmsvc.reload_witness.NativeGenerationReader.read", forbidden)
    monkeypatch.setattr("llmsvc.reload.uuid.uuid4", forbidden)
    added = add_full_weight_model(yaml.safe_load(original), {}, name="fine", model_path=weights,
        base_model="base", shared_roots=(weights.parent,), daemon_port_range=(8101, 8110), created_at=clock[0])
    model_candidate = ModelRegistry._encode(original, added.config, added.records)
    first = plan(model_candidate)
    snapshot = replace(state[0], models=state[0].models + (ModelState("fine", state="stopped"),),
        activity=state[0].activity + (Activity("fine", last_request_at=clock[0], in_flight=0),))
    removed = remove_temporary_model(yaml.safe_load(first.candidate), added.records,
        name="fine", snapshot=snapshot, now=clock[0])
    removed_bytes = ModelRegistry._encode(first.candidate, removed.config, removed.records)
    second = plan(removed_bytes, generation="gen_" + "3" * 32)
    assert second.previous_generation == NEW
    assert yaml.safe_load(second.candidate)["models"] == yaml.safe_load(original)["models"]
    assert second.binding.candidate_sha256 != first.binding.candidate_sha256
    reading = GenerationRead(ENDPOINT, "fixture", 1, 2, 5, second.binding.generation, 200)
    checked = check_visibility(second.binding,
        BindingObservation(1, INSTANCE, digest(second.candidate)), reading,
        BindingObservation(2, INSTANCE, digest(second.candidate)), now=2)
    assert checked.candidate_generation_visible is True and checked.settlement_confirmed is None
    assert {p: p.read_bytes() for p in queue.path.parent.rglob("*") if p.is_file()} == before
    assert not queue._pending and not queue._jobs and not calls and not units
