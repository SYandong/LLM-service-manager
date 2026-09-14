# Generated-By: Claude Code / claude-fable-5-1
"""Shared-root discovery, llmsvc.json overrides and generated catalog profiles."""

import json
import os
import shlex
from pathlib import Path

import pytest

from llmsvc.discovery import (
    CONFIG_FILENAME,
    ModelDiscovery,
    measure_weights_gb,
    normalize_model_name,
    parse_import_config,
)
from llmsvc.registry import (
    ImportOverrides,
    RegistryError,
    add_full_weight_model,
    cross_check_catalog_profile,
    generated_catalog_profile,
)


BASE_CMD = (
    "/usr/local/bin/vllm-wrapper serve "
    "--vllm-url http://127.0.0.1:8101 --listen :${PORT} "
    "--journal-unit vllm-base-model.service -- "
    "/usr/local/bin/vllm-launch 0.72 vllm-base-model -- "
    "vllm serve /srv/models/base --gpu-memory-utilization 0.72 "
    "--port 8101 --max-model-len 8101 --served-model-name base-model"
)
BASE_STOP = "/usr/local/bin/vllm-wrapper sleep --vllm-url http://127.0.0.1:8101 --stop-pid ${PID}"


def base_config():
    return {"models": {"base-model": {"cmd": BASE_CMD, "cmdStop": BASE_STOP}},
            "groups": {"default": {"members": ["base-model"]}}}


def weights(directory, *, descriptor=None, size=4096, name="model.safetensors"):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.json").write_text('{"architectures": ["Test"]}')
    (directory / name).write_bytes(b"w" * size)
    if descriptor is not None:
        (directory / CONFIG_FILENAME).write_text(json.dumps(descriptor))
    return directory


@pytest.fixture
def root(tmp_path):
    shared = tmp_path / "models"
    shared.mkdir()
    return shared


def test_only_directories_with_a_descriptor_are_listed_and_sorted(root):
    weights(root / "Zeta-7B", descriptor={"base": "base-model"})
    weights(root / "Alpha", descriptor={"base": "base-model"})
    weights(root / "NoDescriptor")
    (root / "loose.txt").write_text("ignored")
    rows = [item.to_dict() for item in ModelDiscovery([root]).candidates()]
    assert [row["name"] for row in rows] == ["alpha", "zeta-7b"]
    assert all(row["status"] == "importable" and row["reason"] is None for row in rows)
    assert rows[0]["path"] == str(root / "Alpha") and rows[0]["base"] == "base-model"


def test_configured_names_mark_imported_without_a_second_scan(root):
    weights(root / "already", descriptor={"base": "base-model"})
    discovery = ModelDiscovery([root])
    assert [item.status for item in discovery.candidates(["already"])] == ["imported"]
    assert [item.status for item in discovery.candidates([])] == ["importable"]


def test_symlinked_and_unreadable_entries_are_rejected_not_followed(root, tmp_path):
    outside = weights(tmp_path / "outside")
    (outside / CONFIG_FILENAME).write_text(json.dumps({"base": "base-model"}))
    os.symlink(outside, root / "linked")
    rows = [item.to_dict() for item in ModelDiscovery([root]).candidates()]
    assert [(row["name"], row["status"]) for row in rows] == [("linked", "invalid")]
    assert "symlink" in rows[0]["reason"]
    missing = [item.to_dict() for item in ModelDiscovery([tmp_path / "absent"]).candidates()]
    assert [(row["name"], row["status"]) for row in missing] == [("absent", "invalid")]
    assert "not readable" in missing[0]["reason"]


def test_candidate_count_is_bounded(root):
    for index in range(8):
        weights(root / ("model-%02d" % index), descriptor={"base": "base-model"})
    rows = ModelDiscovery([root], max_candidates=5).candidates()
    assert [item.name for item in rows] == ["model-%02d" % index for index in range(5)]


def test_oversized_descriptor_and_invalid_json_stay_invalid(root):
    weights(root / "toobig", descriptor={"base": "base-model", "name": "x" * 400})
    (root / "broken").mkdir()
    (root / "broken" / CONFIG_FILENAME).write_text("{not json")
    rows = {item.name: item for item in ModelDiscovery([root], model_config_max_bytes=32).candidates()}
    assert rows["toobig"].status == "invalid" and "unreadable" in rows["toobig"].reason
    assert rows["broken"].status == "invalid" and "not valid JSON" in rows["broken"].reason


def test_scan_caches_until_the_descriptor_changes(root, monkeypatch):
    directory = weights(root / "cached", descriptor={"base": "base-model"})
    discovery = ModelDiscovery([root])
    assert discovery.scan()[0].base == "base-model"
    reads = []
    original = discovery._read
    monkeypatch.setattr(discovery, "_read", lambda *a: reads.append(a) or original(*a))
    assert discovery.scan()[0].base == "base-model" and reads == []
    descriptor = directory / CONFIG_FILENAME
    descriptor.write_text(json.dumps({"base": "other-base"}))
    os.utime(descriptor, (1, 1))
    assert discovery.scan()[0].base == "other-base" and len(reads) == 1


def test_weights_are_measured_from_the_index_then_from_plain_files(root):
    sharded = weights(root / "sharded", name="model-00001-of-00002.safetensors", size=1024)
    (sharded / "model-00002-of-00002.safetensors").write_bytes(b"w" * 2048)
    (sharded / "extra.safetensors").write_bytes(b"w" * 4096)
    (sharded / "model.safetensors.index.json").write_text(json.dumps({"weight_map": {
        "a": "model-00001-of-00002.safetensors", "b": "model-00001-of-00002.safetensors",
        "c": "model-00002-of-00002.safetensors"}}))
    assert measure_weights_gb(sharded, [root]) == pytest.approx(3072 / 1024 ** 3)
    plain = weights(root / "plain", size=5120)
    assert measure_weights_gb(plain, [root]) == pytest.approx(5120 / 1024 ** 3)
    assert measure_weights_gb(weights(root / "none", name="model.bin"), [root]) is None


def test_measured_weights_reach_the_candidate_unless_declared(root):
    weights(root / "measured", descriptor={"base": "base-model"}, size=2048)
    weights(root / "declared", descriptor={"base": "base-model", "weights_gb": 12.5}, size=2048)
    rows = {item.name: item for item in ModelDiscovery([root]).candidates()}
    assert rows["measured"].overrides.weights_gb == pytest.approx(2048 / 1024 ** 3)
    assert rows["declared"].overrides.weights_gb == 12.5


@pytest.mark.parametrize("directory,expected", [
    ("Foo-7B", "foo-7b"), ("Qwen3_32B@v2", "qwen3_32b-v2"), ("--lead", "lead"), ("a.b.c", "a.b.c")])
def test_default_name_normalizes_the_directory(directory, expected):
    assert normalize_model_name(directory) == expected


@pytest.mark.parametrize("directory", ["", "---", "vllm-thing", "."])
def test_unusable_directory_names_are_rejected(directory):
    with pytest.raises(RegistryError):
        normalize_model_name(directory)


@pytest.mark.parametrize("document,message", [
    ({"base": "b", "is_default": True}, "is_default"),
    ({"base": "b", "cmd": "vllm serve x"}, "command"),
    ({"base": "b", "argv": ["vllm"]}, "command"),
    ({"base": "b", "shell": "rm -rf /"}, "environment"),
    ({"base": "b", "tensor_parallel_size": 2}, "unsupported keys"),
    ({}, "requires base"),
    ({"base": "b", "util": 0}, "util"),
    ({"base": "b", "util": 1.5}, "util"),
    ({"base": "b", "util": True}, "util"),
    ({"base": "b", "max_model_len": 0}, "max_model_len"),
    ({"base": "b", "max_model_len": 4096.0}, "max_model_len"),
    ({"base": "b", "aliases": ["ok", "ok"]}, "distinct"),
    ({"base": "b", "aliases": "ok"}, "aliases must be a list"),
    ({"base": "b", "aliases": ["vllm-bad"]}, "reserved"),
    ({"base": "b", "weights_gb": -1}, "weights_gb"),
    ({"base": "b", "name": 7}, "name must be a string"),
    ([], "JSON object"),
])
def test_descriptor_rejections_name_the_offending_field(document, message):
    with pytest.raises(RegistryError, match=message):
        parse_import_config(document, directory_name="Foo")


def test_supported_descriptor_parses_into_whitelisted_overrides():
    name, base, overrides = parse_import_config(
        {"base": "base-model", "util": .45, "max_model_len": 4096, "aliases": ["foo"], "weights_gb": 14.5},
        directory_name="Foo-7B")
    assert (name, base) == ("foo-7b", "base-model")
    assert overrides == ImportOverrides(util=.45, max_model_len=4096, aliases=("foo",), weights_gb=14.5)


def test_overrides_rewrite_util_length_and_aliases_only(root):
    model = weights(root / "ft")
    result = add_full_weight_model(base_config(), {}, name="ft", model_path=model,
        base_model="base-model", shared_roots=(root,), daemon_port_range=(8105, 8110),
        created_at=10.0, overrides=ImportOverrides(util=.45, max_model_len=4096,
                                                   aliases=("ft-alias",), weights_gb=14.5))
    argv = shlex.split(result.config["models"]["ft"]["cmd"])
    assert argv[argv.index("--gpu-memory-utilization") + 1] == "0.45"
    assert argv[argv.index("--max-model-len") + 1] == "4096"
    assert argv[argv.index("--") + 2] == "0.45"  # The launcher share follows vLLM.
    assert result.config["models"]["ft"]["aliases"] == ["ft-alias"]
    assert result.record["util"] == .45 and result.record["weights_gb"] == 14.5
    assert "0.72" not in result.config["models"]["ft"]["cmd"]


def test_util_override_updates_a_macro_instead_of_a_literal(root):
    model = weights(root / "ft")
    config = base_config()
    block = config["models"]["base-model"]
    block["macros"] = {"util": "0.72"}
    block["cmd"] = BASE_CMD.replace("vllm-launch 0.72", "vllm-launch ${util}").replace(
        "--gpu-memory-utilization 0.72", "--gpu-memory-utilization ${util}")
    result = add_full_weight_model(config, {}, name="ft", model_path=model, base_model="base-model",
        shared_roots=(root,), daemon_port_range=(8105, 8110), created_at=10.0,
        overrides=ImportOverrides(util=.5))
    added = result.config["models"]["ft"]
    assert added["macros"]["util"] == "0.5" and "${util}" in added["cmd"]
    assert config["models"]["base-model"]["macros"]["util"] == "0.72"


def test_util_override_needs_a_utilization_option_or_macro(root):
    model = weights(root / "ft")
    config = base_config()
    config["models"]["base-model"]["cmd"] = BASE_CMD.replace("--gpu-memory-utilization 0.72 ", "")
    with pytest.raises(RegistryError, match="no --gpu-memory-utilization"):
        add_full_weight_model(config, {}, name="ft", model_path=model, base_model="base-model",
            shared_roots=(root,), daemon_port_range=(8105, 8110), created_at=10.0,
            overrides=ImportOverrides(util=.5))
    # A block util macro covers expansions this planner cannot see inside other
    # macros, so it alone is enough to move both accounting and allocation.
    config["models"]["base-model"]["macros"] = {"util": "0.72"}
    config["models"]["base-model"]["cmd"] = config["models"]["base-model"]["cmd"].replace(
        "vllm-launch 0.72", "vllm-launch ${util}")
    result = add_full_weight_model(config, {}, name="ft", model_path=model, base_model="base-model",
        shared_roots=(root,), daemon_port_range=(8105, 8110), created_at=10.0,
        overrides=ImportOverrides(util=.5))
    assert result.config["models"]["ft"]["macros"]["util"] == "0.5"


def test_alias_collisions_and_bad_override_shapes_are_rejected(root):
    model = weights(root / "ft")
    config = base_config()
    config["models"]["base-model"]["aliases"] = ["taken"]
    for overrides in (ImportOverrides(aliases=("taken",)), ImportOverrides(aliases=("ft",)),
                      ImportOverrides(aliases=("base-model",))):
        with pytest.raises(RegistryError, match="alias already exists"):
            add_full_weight_model(config, {}, name="ft", model_path=model, base_model="base-model",
                shared_roots=(root,), daemon_port_range=(8105, 8110), created_at=10.0, overrides=overrides)
    with pytest.raises(RegistryError, match="supported import shape"):
        add_full_weight_model(config, {}, name="ft", model_path=model, base_model="base-model",
            shared_roots=(root,), daemon_port_range=(8105, 8110), created_at=10.0,
            overrides={"util": .5})


def test_record_util_falls_back_to_the_base_share(root):
    model = weights(root / "ft")
    plain = add_full_weight_model(base_config(), {}, name="ft", model_path=model,
        base_model="base-model", shared_roots=(root,), daemon_port_range=(8105, 8110), created_at=10.0)
    assert plain.record["util"] == .72 and "weights_gb" not in plain.record
    config = base_config()
    config["models"]["base-model"]["cmd"] = BASE_CMD.replace(
        "--gpu-memory-utilization 0.72", "--gpu-memory-utilization ${util}")
    unknown = add_full_weight_model(config, {}, name="ft", model_path=model,
        base_model="base-model", shared_roots=(root,), daemon_port_range=(8105, 8110), created_at=10.0)
    assert "util" not in unknown.record  # An unresolved macro is unknown, not a guess.


def test_generated_profile_follows_the_saved_record(root):
    model = weights(root / "ft")
    result = add_full_weight_model(base_config(), {}, name="ft", model_path=model,
        base_model="base-model", shared_roots=(root,), daemon_port_range=(8105, 8110),
        created_at=10.0, overrides=ImportOverrides(util=.4, weights_gb=10.0))
    profile = generated_catalog_profile(result.record, gpu_total_gb=141.0)
    assert profile == {"unit": "vllm-ft.service", "daemon_url": "http://127.0.0.1:8105",
                       "port": 8105, "util": .4, "weights_gb": 10.0,
                       "is_default": False, "budget_gb": pytest.approx(56.4)}
    cross_check_catalog_profile(profile, result.record)
    with pytest.raises(RegistryError, match="disagrees on port"):
        cross_check_catalog_profile({**profile, "port": 8106}, result.record)
    with pytest.raises(RegistryError, match="disagrees on util"):
        cross_check_catalog_profile({**profile, "util": .9}, result.record)


@pytest.mark.parametrize("record,message", [
    ({"name": "ft", "daemon_port": 8105, "weights_gb": 10.0}, "util"),
    ({"name": "ft", "daemon_port": 8105, "util": .4}, "weights_gb"),
    ({"name": "ft", "daemon_port": 0, "util": .4, "weights_gb": 1.0}, "daemon_port"),
    ({"name": "vllm-x", "daemon_port": 8105, "util": .4, "weights_gb": 1.0}, "reserved"),
])
def test_profile_generation_refuses_incomplete_records(record, message):
    with pytest.raises(RegistryError, match=message):
        generated_catalog_profile(record, gpu_total_gb=141.0)


def test_profile_generation_needs_an_observed_card_size():
    record = {"name": "ft", "daemon_port": 8105, "util": .4, "weights_gb": 10.0}
    for total in (0, -1, float("inf")):
        with pytest.raises(RegistryError, match="gpu_total_gb"):
            generated_catalog_profile(record, gpu_total_gb=total)
