# Generated-By: Claude Code / claude-fable-5-1
"""Imports on a native-maintenance site: helper stop clones and profile rows.

The adapter stops each model through its reviewed helper instead of the
wrapper's --vllm-url form, and refuses a candidate whose models have no row in
its private profile. These tests use a fake profile in a temporary directory;
they establish no live adapter, unit or settlement behaviour.
"""

import hashlib
import json
import os
import shlex
import stat
import sys
from dataclasses import replace

import pytest
import yaml

from llmsvc.native_profile import (NativeMaintenanceProfile, candidate_profile_entries,
                                   maintenance_profile_path)
from llmsvc.registry import RegistryError, add_full_weight_model
from llmsvc.reload import ReloadError
from test_catalog_lifecycle import catalog, install
from test_maintenance_lifecycle import maintenance
from test_registry import BASE_CMD, BASE_STOP, _config, _full_weight_model


HELPER_PYTHON = "/opt/llmsvc-native-maintenance/revisions/aa11/venv/bin/python"
HELPER_PROGRAM = "/opt/llmsvc-native-maintenance/revisions/aa11/deploy/maintenance_native.py"
HELPER_PROFILE = "/etc/llmsvc/native-maintenance.json"
# The live site spells this with single quotes around the paths and ${PID}.
HELPER_STOP = ("'" + HELPER_PYTHON + "' -B '" + HELPER_PROGRAM + "' helper "
               "--profile " + HELPER_PROFILE + " --model base-model --pid '${PID}'")


def helper_argv(name):
    return [HELPER_PYTHON, "-B", HELPER_PROGRAM, "helper", "--profile", HELPER_PROFILE,
            "--model", name, "--pid", "${PID}"]


def helper_config(cmd_stop=HELPER_STOP):
    config = _config()
    config["models"]["base-model"]["cmdStop"] = cmd_stop
    return config


def added_block(tmp_path, config, *, name="new-model"):
    model = _full_weight_model(tmp_path / "models" / "ft")
    result = add_full_weight_model(config, {}, name=name, model_path=model,
                                   base_model="base-model", shared_roots=(tmp_path / "models",),
                                   daemon_port_range=(8105, 8106), created_at=123.0)
    return result


def test_helper_cmd_stop_clone_renames_only_the_model_argument(tmp_path):
    result = added_block(tmp_path, helper_config())
    block = result.config["models"]["new-model"]
    assert shlex.split(block["cmdStop"]) == helper_argv("new-model")
    # The daemon port comes from cmd alone; the helper argv carries no port.
    assert "http://127.0.0.1:8105" in shlex.split(block["cmd"])
    assert "8105" not in block["cmdStop"] and "8101" not in block["cmdStop"]
    assert result.record["daemon_port"] == 8105


def test_helper_cmd_stop_must_name_the_base_model(tmp_path):
    config = helper_config(HELPER_STOP.replace("--model base-model", "--model other-model"))
    with pytest.raises(RegistryError, match="must name the base model"):
        added_block(tmp_path, config)


def test_cmd_stop_without_helper_or_upstream_url_is_rejected(tmp_path):
    config = helper_config("/usr/local/bin/vllm-wrapper sleep --stop-pid ${PID}")
    with pytest.raises(RegistryError, match="maintenance helper form or --vllm-url"):
        added_block(tmp_path, config)


def test_cmd_stop_cannot_mix_the_helper_and_an_upstream_url(tmp_path):
    config = helper_config(HELPER_STOP + " --vllm-url http://127.0.0.1:8101")
    with pytest.raises(RegistryError, match="not both"):
        added_block(tmp_path, config)


def test_helper_cmd_stop_needs_one_separate_model_option(tmp_path):
    config = helper_config(HELPER_STOP.replace("--model base-model", "--model=base-model"))
    with pytest.raises(RegistryError, match="one --model <name> option"):
        added_block(tmp_path, config)


def test_wrapper_cmd_stop_port_mismatch_keeps_its_existing_error(tmp_path):
    config = _config()
    config["models"]["base-model"]["cmdStop"] = BASE_STOP.replace("8101", "8102")
    with pytest.raises(RegistryError, match="same upstream"):
        added_block(tmp_path, config)


def test_import_candidate_yields_the_row_the_adapter_rebuilds(tmp_path):
    result = added_block(tmp_path, helper_config())
    candidate = yaml.safe_dump(result.config).encode()
    entries = candidate_profile_entries(candidate, ["new-model"])
    saved = yaml.safe_load(candidate)["models"]["new-model"]
    assert entries == {"new-model": {
        "unit": "vllm-new-model.service", "backend_origin": "http://127.0.0.1:8105",
        "process_argv": shlex.split(saved["cmd"])}}
    # The adapter rebuilds both sides from the same profile row.
    assert shlex.split(saved["cmdStop"]) == helper_argv("new-model")


def test_import_candidate_rejects_a_non_literal_upstream_host(tmp_path):
    config = helper_config()
    config["models"]["base-model"]["cmd"] = BASE_CMD.replace("http://127.0.0.1:8101",
                                                             "http://localhost:8101")
    candidate = yaml.safe_dump(added_block(tmp_path, config).config).encode()
    with pytest.raises(RegistryError, match="literal http://IP:PORT"):
        candidate_profile_entries(candidate, ["new-model"])


PROFILE_DOCUMENT = {
    "native_adapter": True,
    "helper_python": HELPER_PYTHON,
    "native_binary_sha256": "a" * 64,
    "models": {"base": {"unit": "vllm-base.service", "backend_origin": "http://127.0.0.1:21000",
                        "process_argv": ["/usr/local/bin/vllm-wrapper", "serve"]}},
    "fragment_sha256": "b" * 64,
}


def write_profile(path, document=None):
    path.write_text(json.dumps(PROFILE_DOCUMENT if document is None else document, indent=2) + "\n")
    path.chmod(0o600)
    return NativeMaintenanceProfile(path)


def test_profile_path_comes_from_the_configured_adapter_argv():
    assert maintenance_profile_path([sys.executable, "adapter", "--profile", "/etc/x.json"]) == "/etc/x.json"
    assert maintenance_profile_path([sys.executable, "adapter", "--profile=/etc/x.json"]) == "/etc/x.json"
    assert maintenance_profile_path([sys.executable, "adapter"]) is None


def test_profile_edits_are_private_and_keep_every_other_field(tmp_path):
    path = tmp_path / "native-maintenance.json"
    profile = write_profile(path)
    row = {"unit": "vllm-fine.service", "backend_origin": "http://127.0.0.1:21001",
           "process_argv": ["/usr/local/bin/vllm-wrapper", "serve", "--vllm-url", "http://127.0.0.1:21001"]}
    original = profile.add({"fine": row})

    saved = json.loads(path.read_text())
    assert saved["models"] == {**PROFILE_DOCUMENT["models"], "fine": row}
    assert list(saved) == list(PROFILE_DOCUMENT)  # Key order and unrelated fields survive.
    assert saved["native_binary_sha256"] == "a" * 64 and saved["fragment_sha256"] == "b" * 64
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert not list(tmp_path.glob('.llmsvc-profile-*'))

    profile.remove(["fine"])
    assert json.loads(path.read_text())["models"] == PROFILE_DOCUMENT["models"]
    profile.remove(["absent"])  # A row that is already gone is not an error.
    profile.restore(original)
    assert path.read_bytes() == original


def test_profile_that_is_missing_or_misshapen_reports_and_changes_nothing(tmp_path):
    missing = NativeMaintenanceProfile(tmp_path / "absent.json")
    with pytest.raises(RegistryError, match="unreadable"):
        missing.add({"fine": {}})
    assert not list(tmp_path.iterdir())

    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    with pytest.raises(RegistryError, match="not valid JSON"):
        NativeMaintenanceProfile(broken).add({"fine": {}})
    assert broken.read_text() == "{not json"

    shapeless = tmp_path / "shapeless.json"
    shapeless.write_text(json.dumps({"models": []}))
    with pytest.raises(RegistryError, match="no models mapping"):
        NativeMaintenanceProfile(shapeless).add({"fine": {}})
    assert json.loads(shapeless.read_text()) == {"models": []}


IMPORT_CMD = ("/usr/local/bin/vllm-wrapper serve --vllm-url http://127.0.0.1:21001 "
              "--listen :${PORT} --journal-unit vllm-new.service -- "
              "/usr/local/bin/vllm-launch 0.4 vllm-new -- vllm serve /srv/models/new --port 21001")


def native_candidate(models):
    return yaml.safe_dump({"macros": {"llmsvc_reload_generation": "gen_" + "1" * 32},
                           "models": models}).encode()


def native(c, tmp_path, candidate):
    """Point the fixture adapter at a private profile and stage one candidate."""
    path = tmp_path / "native-maintenance.json"
    profile = write_profile(path)
    c.s.config = replace(c.s.config, maintenance_command=[sys.executable, "fixture",
                                                          "--profile", str(path)])
    c.candidate = candidate
    c.binding = replace(c.binding, candidate_sha256=hashlib.sha256(candidate).hexdigest())
    return path, profile


def test_imported_row_exists_before_validation_and_removed_rows_go_after_release(maintenance, tmp_path):
    c = maintenance
    c.models = {"new": c.models["new"]}
    path, _ = native(c, tmp_path, native_candidate({"new": {"cmd": IMPORT_CMD,
                                                           "cmdStop": HELPER_STOP.replace("base-model", "new")}}))
    c.runtime.enqueue(c.runtime.prepare(c.candidate, c.models, binding=c.binding))
    staged = json.loads(path.read_text())["models"]
    assert "validate" in c.backend.calls  # The row was already there when the adapter validated.
    assert staged["new"] == {"unit": "vllm-new.service", "backend_origin": "http://127.0.0.1:21001",
                             "process_argv": shlex.split(IMPORT_CMD)}
    assert "base" in staged  # The retiring model keeps its row while it is stopped.

    assert c.runtime.process_once()["status"] == "applied"
    released = json.loads(path.read_text())
    assert set(released["models"]) == {"new"} and list(released) == list(PROFILE_DOCUMENT)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_rejected_submission_withdraws_the_row_it_added(maintenance, tmp_path):
    c = maintenance
    path, _ = native(c, tmp_path, native_candidate({"base": {}, "new": {"cmd": IMPORT_CMD}}))
    c.models = {**c.models, "new": c.models["new"]}
    before = path.read_bytes()
    original = c.backend.request
    def refuse(operation, context, *, deadline):
        result = original(operation, context, deadline=deadline)
        return {**result, "accepted": False} if operation == "validate" else result
    c.backend.request = refuse
    with pytest.raises(ReloadError, match="validation failed"):
        c.runtime.enqueue(c.runtime.prepare(c.candidate, c.models, binding=c.binding))
    assert path.read_bytes() == before and not c.q._pending
    assert not list(tmp_path.glob('.llmsvc-profile-*'))


def test_dry_run_submission_never_writes_the_profile(maintenance, tmp_path):
    c = maintenance
    path, _ = native(c, tmp_path, native_candidate({"base": {}, "new": {"cmd": IMPORT_CMD}}))
    c.models = {**c.models, "new": c.models["new"]}
    before = path.read_bytes()
    prepared = c.runtime.prepare(c.candidate, c.models, binding=c.binding)
    assert c.runtime.enqueue(prepared, dry_run=True)["would"]
    assert path.read_bytes() == before and "validate" not in c.backend.calls


def test_hot_reload_catalog_leaves_any_profile_alone(catalog, tmp_path):
    c = catalog
    path = tmp_path / "native-maintenance.json"
    write_profile(path)
    before = path.read_bytes()
    c.s.config = replace(c.s.config, maintenance_command=[sys.executable, "fixture",
                                                          "--profile", str(path)])
    assert install(c)[0]["status"] == "applied"
    assert path.read_bytes() == before
