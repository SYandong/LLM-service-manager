# Generated-By: Codex / gpt-6-astra
"""Temporary full-weight registry helper tests."""

import copy
import json
import shlex
from pathlib import Path

import pytest

from llmsvc.registry import (
    RegistryError,
    add_full_weight_model,
    expired_temporary_models,
    plan_temporary_model_removal,
    remove_temporary_model,
    validate_full_weight_model_dir,
)
from llmsvc.state import Activity, Lease, ModelState, Pin, StateSnapshot


BASE_CMD = (
    "/usr/local/bin/vllm-wrapper serve "
    "--vllm-url http://127.0.0.1:8101 --listen :${PORT} "
    "--wait-timeout 15m --journal-unit vllm-base-model.service -- "
    "/usr/local/bin/vllm-launch 0.72 vllm-base-model -- "
    "vllm serve /srv/models/base --gpu-memory-utilization 0.72 "
    "--port 8101 --max-model-len 8101 --served-model-name base-model "
    "--speculative-config '{\"model\":\"/srv/models/base\",\"draft\":true}'"
)
BASE_STOP = "/usr/local/bin/vllm-wrapper sleep --vllm-url http://127.0.0.1:8101 --stop-pid ${PID}"


def test_add_validation_errors_do_not_mutate_config_or_records(tmp_path):
    root = tmp_path / "models"
    model = _full_weight_model(root / "candidate")
    config = _config()
    records = {}
    before_config = copy.deepcopy(config)
    before_records = copy.deepcopy(records)

    with pytest.raises(RegistryError):
        add_full_weight_model(
            config,
            records,
            name="../bad",
            model_path=model,
            base_model="base-model",
            shared_roots=(root,),
            daemon_port_range=(8101, 8103),
            created_at=10.0,
        )

    assert config == before_config
    assert records == before_records


def test_model_path_accepts_complete_shards_and_rejects_symlink_escape(tmp_path):
    root = tmp_path / "models"
    model = _full_weight_model(root / "candidate", weight_name="model-00001-of-00002.safetensors")
    (model / "model-00002-of-00002.safetensors").write_text("weights", encoding="utf-8")
    info = validate_full_weight_model_dir(model, (root,))
    assert info.weight_files == ("model-00001-of-00002.safetensors", "model-00002-of-00002.safetensors")

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("x", encoding="utf-8")
    (model / "escape").symlink_to(outside / "secret")
    with pytest.raises(RegistryError, match="symlink escape"):
        validate_full_weight_model_dir(model, (root,))


def test_model_path_rejects_incomplete_shards_and_bad_index(tmp_path):
    root = tmp_path / "models"
    incomplete = _full_weight_model(root / "incomplete", weight_name="model-00001-of-00002.safetensors")
    with pytest.raises(RegistryError, match="missing shard"):
        validate_full_weight_model_dir(incomplete, (root,))

    indexed = root / "indexed"
    indexed.mkdir(parents=True)
    (indexed / "config.json").write_text(json.dumps({"architectures": ["Test"]}), encoding="utf-8")
    (indexed / "model.safetensors.index.json").write_text(
        json.dumps({"weight_map": {"layer": "missing.safetensors"}}),
        encoding="utf-8",
    )
    with pytest.raises(RegistryError, match="missing weight"):
        validate_full_weight_model_dir(indexed, (root,))


def test_model_path_uses_index_weight_map_and_rejects_empty_or_non_object_config(tmp_path):
    root = tmp_path / "models"
    model = root / "indexed"
    model.mkdir(parents=True)
    (model / "config.json").write_text(json.dumps({"architectures": ["Test"]}), encoding="utf-8")
    (model / "model-00001-of-00002.safetensors").write_text("a", encoding="utf-8")
    (model / "model-00002-of-00002.safetensors").write_text("b", encoding="utf-8")
    (model / "model.safetensors.index.json").write_text(
        json.dumps({"weight_map": {"a": "model-00001-of-00002.safetensors", "b": "model-00002-of-00002.safetensors"}}),
        encoding="utf-8",
    )
    assert validate_full_weight_model_dir(model, (root,)).weight_files == (
        "model-00001-of-00002.safetensors",
        "model-00002-of-00002.safetensors",
    )

    (model / "model-00002-of-00002.safetensors").write_text("", encoding="utf-8")
    with pytest.raises(RegistryError, match="non-empty"):
        validate_full_weight_model_dir(model, (root,))

    bad_config = _full_weight_model(root / "bad-config")
    (bad_config / "config.json").write_text(json.dumps(["not", "object"]), encoding="utf-8")
    with pytest.raises(RegistryError, match="JSON object"):
        validate_full_weight_model_dir(bad_config, (root,))


def test_model_path_rejects_lora_only_directory(tmp_path):
    root = tmp_path / "models"
    model = root / "adapter"
    model.mkdir(parents=True)
    (model / "config.json").write_text(json.dumps({"peft_type": "LORA"}), encoding="utf-8")
    (model / "adapter_model.safetensors").write_text("adapter", encoding="utf-8")

    with pytest.raises(RegistryError, match="LoRA"):
        validate_full_weight_model_dir(model, (root,))


def test_add_clones_base_flags_rewrites_ports_names_and_groups(tmp_path):
    root = tmp_path / "models"
    model = _full_weight_model(root / "ft")
    config = _config()
    config["models"]["base-model"]["aliases"] = ["old-alias"]
    config["models"]["base-model"]["setParamsByID"] = {"base-model": {"ttl": 0}, "old-alias": {"ttl": 0}}
    config["models"]["other"] = {
        "cmd": BASE_CMD.replace("8101", "8102").replace("base-model", "other"),
        "cmdStop": BASE_STOP.replace("8101", "8102"),
    }
    records = {"old-temp": {"daemon_port": 8104}}

    result = add_full_weight_model(
        config,
        records,
        name="new.ft-1",
        model_path=model,
        base_model="base-model",
        shared_roots=(root,),
        daemon_port_range=(8101, 8105),
        reserved_ports=(8103,),
        created_at=123.0,
    )

    added = result.config["models"]["new.ft-1"]
    argv = shlex.split(added["cmd"])
    stop_argv = shlex.split(added["cmdStop"])
    assert added["useModelName"] == "new.ft-1"
    assert "aliases" not in added
    assert "setParamsByID" not in added
    assert "http://127.0.0.1:8105" in argv
    assert "http://127.0.0.1:8105" in stop_argv
    assert "vllm-new.ft-1.service" in argv
    assert "vllm-new.ft-1" in argv
    assert argv[argv.index("serve", argv.index("--") + 1) + 1] == str(model.resolve())
    assert argv[argv.index("--served-model-name") + 1] == "new.ft-1"
    assert argv[argv.index("--gpu-memory-utilization") + 1] == "0.72"
    assert argv[argv.index("--max-model-len") + 1] == "8101"
    speculative = json.loads(argv[argv.index("--speculative-config") + 1])
    assert speculative == {"draft": True, "model": str(model.resolve())}
    assert result.config["groups"]["default"]["members"] == ["base-model", "new.ft-1"]
    assert result.record["daemon_port"] == 8105
    assert result.records["new.ft-1"]["path"] == str(model.resolve())
    assert "new.ft-1" not in config["models"]


def test_add_rejects_alias_setparams_and_default_model_name_collisions(tmp_path):
    root = tmp_path / "models"
    model = _full_weight_model(root / "ft")
    for reserved_name in ("old-alias", "param-id", "default-alias"):
        config = _config()
        config["defaultModel"] = "default-alias"
        config["models"]["base-model"]["aliases"] = ["old-alias"]
        config["models"]["base-model"]["setParamsByID"] = {"param-id": {"temperature": 0}}
        with pytest.raises(RegistryError, match="already exists"):
            add_full_weight_model(
                config,
                {},
                name=reserved_name,
                model_path=model,
                base_model="base-model",
                shared_roots=(root,),
                daemon_port_range=(8101, 8103),
                created_at=123.0,
            )


def test_add_rejects_unsupported_shell_template(tmp_path):
    root = tmp_path / "models"
    model = _full_weight_model(root / "ft")
    config = _config()
    config["models"]["base-model"]["cmd"] = BASE_CMD + " ; echo unsafe"

    with pytest.raises(RegistryError, match="shell"):
        add_full_weight_model(
            config,
            {},
            name="new-model",
            model_path=model,
            base_model="base-model",
            shared_roots=(root,),
            daemon_port_range=(8101, 8103),
            created_at=123.0,
        )


def test_add_rejects_bad_ports_created_at_and_mismatched_upstream(tmp_path):
    root = tmp_path / "models"
    model = _full_weight_model(root / "ft")
    config = _config()

    with pytest.raises(RegistryError, match="created_at"):
        add_full_weight_model(
            config,
            {},
            name="new-model",
            model_path=model,
            base_model="base-model",
            shared_roots=(root,),
            daemon_port_range=(8101, 8103),
            created_at=float("nan"),
        )

    with pytest.raises(RegistryError, match="port range"):
        add_full_weight_model(
            config,
            {},
            name="new-model",
            model_path=model,
            base_model="base-model",
            shared_roots=(root,),
            daemon_port_range=(8103, 8101),
            created_at=123.0,
        )

    mismatched = _config()
    mismatched["models"]["base-model"]["cmdStop"] = BASE_STOP.replace("8101", "8102")
    with pytest.raises(RegistryError, match="same upstream"):
        add_full_weight_model(
            mismatched,
            {},
            name="new-model",
            model_path=model,
            base_model="base-model",
            shared_roots=(root,),
            daemon_port_range=(8101, 8103),
            created_at=123.0,
        )

    mismatched_vllm = _config()
    mismatched_vllm["models"]["base-model"]["cmd"] = BASE_CMD.replace("--port 8101", "--port 8103")
    with pytest.raises(RegistryError, match="must match"):
        add_full_weight_model(
            mismatched_vllm,
            {},
            name="new-model",
            model_path=model,
            base_model="base-model",
            shared_roots=(root,),
            daemon_port_range=(8101, 8103),
            created_at=123.0,
        )


def test_add_preserves_configured_upstream_host_and_rewrites_only_port_options(tmp_path):
    root = tmp_path / "models"
    model = _full_weight_model(root / "ft")
    config = _config()
    config["models"]["base-model"]["cmd"] = BASE_CMD.replace("http://127.0.0.1:8101", "http://localhost:8101")
    config["models"]["base-model"]["cmdStop"] = BASE_STOP.replace("http://127.0.0.1:8101", "http://localhost:8101")

    result = add_full_weight_model(
        config,
        {},
        name="new-model",
        model_path=model,
        base_model="base-model",
        shared_roots=(root,),
        daemon_port_range=(8101, 8102),
        created_at=123.0,
    )

    argv = shlex.split(result.config["models"]["new-model"]["cmd"])
    assert "http://localhost:8102" in argv
    assert argv[argv.index("--max-model-len") + 1] == "8101"


def test_removal_and_expiry_protect_default_pin_inflight_and_unknown_activity():
    now = 1_000_000.0
    records = {
        "old": {"created_at": now - 8 * 24 * 60 * 60},
        "fresh-by-activity": {"created_at": now - 8 * 24 * 60 * 60},
        "pinned": {"created_at": now - 8 * 24 * 60 * 60},
        "busy": {"created_at": now - 8 * 24 * 60 * 60},
        "unknown-activity": {"created_at": now - 8 * 24 * 60 * 60},
        "default-temp": {"created_at": now - 8 * 24 * 60 * 60},
    }
    snapshot = StateSnapshot(
        models=(
            ModelState(name="old", state="stopped"),
            ModelState(name="fresh-by-activity", state="stopped"),
            ModelState(name="pinned", state="sleeping"),
            ModelState(name="busy", state="awake"),
            ModelState(name="unknown-activity", state="stopped"),
            ModelState(name="default-temp", state="stopped", is_default=True),
        ),
        activity=(
            Activity(model="old", last_request_at=now - 8 * 24 * 60 * 60, in_flight=0),
            Activity(model="fresh-by-activity", last_request_at=now - 60, in_flight=0),
            Activity(model="pinned", last_request_at=now - 8 * 24 * 60 * 60, in_flight=0),
            Activity(model="busy", last_request_at=now - 8 * 24 * 60 * 60, in_flight=2),
            Activity(model="unknown-activity", last_request_at=None, in_flight=None),
            Activity(model="default-temp", last_request_at=now - 8 * 24 * 60 * 60, in_flight=0),
        ),
        pins=(Pin(model="pinned", until=now + 3600, by="ctr-a"),),
        sampled_at=now,
    )

    assert expired_temporary_models(records, snapshot, now=now) == ("old",)
    assert plan_temporary_model_removal("pinned", records, snapshot, now=now).blockers[0].reason == "pinned"
    assert plan_temporary_model_removal("busy", records, snapshot, now=now).blockers[0].reason == "in_flight"
    assert plan_temporary_model_removal("unknown-activity", records, snapshot, now=now).blockers[0].reason == "unknown_activity"
    assert plan_temporary_model_removal("default-temp", records, snapshot, now=now).blockers[0].reason == "default_model"


def test_removal_fails_closed_on_stale_errors_leases_and_negative_inflight():
    now = 1_000_000.0
    records = {
        "stale": {"created_at": now - 10.0},
        "errored": {"created_at": now - 10.0},
        "leased": {"created_at": now - 10.0},
        "negative": {"created_at": now - 10.0},
    }
    base_models = (
        ModelState(name="stale", state="stopped"),
        ModelState(name="errored", state="stopped"),
        ModelState(name="leased", state="stopped"),
        ModelState(name="negative", state="stopped"),
    )
    base_activity = (
        Activity(model="stale", last_request_at=now - 10.0, in_flight=0),
        Activity(model="errored", last_request_at=now - 10.0, in_flight=0),
        Activity(model="leased", last_request_at=now - 10.0, in_flight=0),
        Activity(model="negative", last_request_at=now - 10.0, in_flight=-1),
    )

    assert plan_temporary_model_removal(
        "stale",
        records,
        StateSnapshot(models=base_models, activity=base_activity, sampled_at=now - 120),
        now=now,
    ).blockers[0].reason == "stale_snapshot"
    assert plan_temporary_model_removal(
        "errored",
        records,
        StateSnapshot(models=base_models, activity=base_activity, sampled_at=now, errors=("collector failed",)),
        now=now,
    ).blockers[0].reason == "snapshot_errors"
    assert plan_temporary_model_removal(
        "leased",
        records,
        StateSnapshot(
            models=base_models,
            activity=base_activity,
            sampled_at=now,
            leases=(Lease("lease-1", "leased", 0, 0.7, now + 60, 100.0),),
        ),
        now=now,
    ).blockers[0].reason == "active_lease"
    assert plan_temporary_model_removal(
        "negative",
        records,
        StateSnapshot(models=base_models, activity=base_activity, sampled_at=now),
        now=now,
    ).blockers[0].reason == "invalid_activity"


def test_remove_detaches_config_records_and_returns_stop_action(tmp_path):
    now = 1_000_000.0
    config = _config()
    config["models"]["temp"] = copy.deepcopy(config["models"]["base-model"])
    config["groups"]["default"]["members"].append("temp")
    records = {"temp": {"created_at": now - 10.0, "daemon_port": 8102}}
    snapshot = StateSnapshot(
        models=(ModelState(name="temp", state="sleeping", gpu=0),),
        activity=(Activity(model="temp", last_request_at=now - 10.0, in_flight=0),),
        sampled_at=now,
    )

    result = remove_temporary_model(config, records, name="temp", snapshot=snapshot, now=now)

    assert "temp" not in result.config["models"]
    assert result.config["groups"]["default"]["members"] == ["base-model"]
    assert result.records == {}
    assert result.actions[0].kind == "stop"
    assert result.actions[0].model == "temp"
    assert "temp" in config["models"]
    assert records == {"temp": {"created_at": now - 10.0, "daemon_port": 8102}}


def _config():
    return {
        "models": {
            "base-model": {
                "cmd": BASE_CMD,
                "cmdStop": BASE_STOP,
                "useModelName": False,
            }
        },
        "groups": {
            "default": {"swap": False, "members": ["base-model"]},
            "unrelated": {"members": ["another"]},
        },
    }


def _full_weight_model(path: Path, *, weight_name: str = "model.safetensors") -> Path:
    path.mkdir(parents=True)
    (path / "config.json").write_text(json.dumps({"architectures": ["TestForCausalLM"]}), encoding="utf-8")
    (path / weight_name).write_text("weights", encoding="utf-8")
    return path
