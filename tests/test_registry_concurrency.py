# Generated-By: OpenCode / deepseek-v4.1-flash
"""Per-model llama-swap concurrencyLimit: clone plumbing and convergence."""

import threading
from dataclasses import replace

import yaml

from llmsvc.discovery import Candidate
from llmsvc.registry import ImportOverrides, ModelRegistry, add_full_weight_model
from llmsvc.reload import QuietPeriod, ReloadQueue
from llmsvc.state import Activity, MemoryState, ModelState, StateSnapshot


BASE_CMD = (
    "/usr/local/bin/vllm-wrapper serve "
    "--vllm-url http://127.0.0.1:8101 --listen :${PORT} "
    "--journal-unit vllm-base-model.service -- "
    "/usr/local/bin/vllm-launch 0.72 vllm-base-model -- "
    "vllm serve /srv/models/base --gpu-memory-utilization 0.72 "
    "--port 8101 --max-model-len 8101 --served-model-name base-model"
)
BASE_STOP = "/usr/local/bin/vllm-wrapper sleep --vllm-url http://127.0.0.1:8101 --stop-pid ${PID}"


class Clock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now


class FakeDiscovery:
    def __init__(self, candidate):
        self.candidate = candidate

    def candidates(self, configured_names=()):
        return [self.candidate]

    def resolve(self, name, configured_names=()):
        return self.candidate


def base_config(*, concurrency=None):
    block = {"cmd": BASE_CMD, "cmdStop": BASE_STOP}
    if concurrency is not None:
        block["concurrencyLimit"] = concurrency
    return {"models": {"base-model": block}, "groups": {"default": {"members": ["base-model"]}}}


def simple_config(*names, concurrency=None):
    models = {}
    for name in names:
        block = {"cmd": "echo " + name, "cmdStop": "echo stop " + name}
        if concurrency is not None:
            block["concurrencyLimit"] = concurrency
        models[name] = block
    return {"models": models}


def full_weight_model(path):
    path.mkdir(parents=True)
    (path / "config.json").write_text('{"architectures": ["Test"]}')
    (path / "model.safetensors").write_text("weights")
    return path


def make_runtime(tmp_path, config, *, default_limit=None, discover=None):
    clock = Clock()
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    names = list(config["models"])
    state = [StateSnapshot(
        sampled_at=clock(), models=tuple(ModelState(name, state="stopped") for name in names),
        activity=tuple(Activity(name, in_flight=0) for name in names),
        memory=MemoryState(500, 0), read_only=False)]
    quiet = QuietPeriod(clock)
    queue = ReloadQueue(path, action_lock=threading.RLock(), quiet=quiet,
                        snapshot=lambda: replace(state[0], sampled_at=clock()),
                        validate=lambda candidate: yaml.safe_load(candidate.read_text()),
                        notify_reload=lambda **kwargs: None,
                        log=lambda event: None, clock=clock, wall_clock=clock)
    (tmp_path / "models").mkdir(exist_ok=True)
    registry = ModelRegistry(
        queue, shared_roots=(tmp_path / "models",), daemon_port_range=(8101, 8110),
        reserved_ports=lambda: (), now=clock, discover=discover, default_concurrency_limit=default_limit)

    def drain():
        quiet.observe(0)
        for _ in range(5):
            clock.now += 1
            quiet.heartbeat()
        return queue.process_once()
    return registry, queue, drain, clock


# ------------------------------------------------------------------ clone

def test_clone_inherits_base_concurrency_limit(tmp_path):
    root = tmp_path / "models"
    model = full_weight_model(root / "ft")
    result = add_full_weight_model(
        base_config(concurrency=5), {}, name="ft", model_path=model, base_model="base-model",
        shared_roots=(root,), daemon_port_range=(8101, 8110), created_at=1.0)
    assert result.config["models"]["ft"]["concurrencyLimit"] == 5
    assert "concurrency_limit" not in result.record


def test_effective_value_reaches_the_clone_and_override_the_record(tmp_path):
    root = tmp_path / "models"
    model = full_weight_model(root / "ft")
    result = add_full_weight_model(
        base_config(concurrency=5), {}, name="ft", model_path=model, base_model="base-model",
        shared_roots=(root,), daemon_port_range=(8101, 8110), created_at=1.0,
        overrides=ImportOverrides(concurrency_limit=8), concurrency_limit=8)
    assert result.config["models"]["ft"]["concurrencyLimit"] == 8
    assert result.record["concurrency_limit"] == 8


def test_default_is_applied_when_the_base_lacks_the_scalar(tmp_path):
    root = tmp_path / "models"
    model = full_weight_model(root / "ft")
    result = add_full_weight_model(
        base_config(), {}, name="ft", model_path=model, base_model="base-model",
        shared_roots=(root,), daemon_port_range=(8101, 8110), created_at=1.0, concurrency_limit=64)
    assert result.config["models"]["ft"]["concurrencyLimit"] == 64
    assert "concurrency_limit" not in result.record


def test_name_only_add_uses_the_configured_default(tmp_path):
    root = tmp_path / "models"
    model = full_weight_model(root / "ft")
    registry, queue, drain, _ = make_runtime(tmp_path, base_config(), default_limit=64)
    registry.add({"name": "ft", "path": str(model), "base": "base-model"})
    assert drain()["status"] == "applied"
    saved = yaml.safe_load(queue.path.read_text())
    assert saved["models"]["ft"]["concurrencyLimit"] == 64
    assert "concurrency_limit" not in saved["models"]["ft"]["metadata"]["llmsvc_registry"]


def test_import_override_beats_the_configured_default(tmp_path):
    root = tmp_path / "models"
    model = full_weight_model(root / "ft")
    candidate = Candidate("ft", str(model), base="base-model", status="pending",
                          overrides=ImportOverrides(concurrency_limit=8))
    registry, queue, drain, _ = make_runtime(
        tmp_path, base_config(), default_limit=64, discover=FakeDiscovery(candidate))
    registry.add({"import": "ft"})
    assert drain()["status"] == "applied"
    saved = yaml.safe_load(queue.path.read_text())
    assert saved["models"]["ft"]["concurrencyLimit"] == 8
    assert saved["models"]["ft"]["metadata"]["llmsvc_registry"]["concurrency_limit"] == 8


# -------------------------------------------------------------- converge

def test_converge_is_a_queue_free_noop_without_a_default_or_overrides(tmp_path):
    registry, queue, _, _ = make_runtime(tmp_path, simple_config("a", "b"))
    outcome = registry.converge_concurrency()
    assert outcome == {"kind": "set_concurrency_limit", "changed": [], "submitted": False}
    assert not queue._jobs and not queue._pending


def test_converge_is_a_noop_when_every_entry_matches(tmp_path):
    registry, queue, _, _ = make_runtime(tmp_path, simple_config("a", "b", concurrency=64), default_limit=64)
    before = queue.path.read_bytes()
    outcome = registry.converge_concurrency()
    assert outcome == {"kind": "set_concurrency_limit", "changed": [], "submitted": False}
    assert queue.path.read_bytes() == before and not queue._jobs and not queue._pending


def test_converge_sets_several_entries_in_one_transaction(tmp_path):
    registry, queue, drain, _ = make_runtime(tmp_path, simple_config("a", "b"), default_limit=64)
    outcome = registry.converge_concurrency()
    assert outcome["changed"] == ["a", "b"] and outcome["submitted"] is True
    assert outcome["description"] == {"kind": "set_concurrency_limit", "models": ["a", "b"], "limit": 64}
    assert len([job for job in queue.queue_snapshot()["jobs"] if job["pending"]]) == 1
    assert drain()["status"] == "applied"
    saved = yaml.safe_load(queue.path.read_text())
    assert saved["models"]["a"]["concurrencyLimit"] == 64
    assert saved["models"]["b"]["concurrencyLimit"] == 64


def test_converge_respects_a_record_override(tmp_path):
    config = simple_config("base")
    config["models"]["fine"] = {
        "cmd": "echo fine", "cmdStop": "echo stop fine",
        "metadata": {"llmsvc_registry": {
            "name": "fine", "kind": "full_weight", "base": "base", "path": "/srv/models/fine",
            "created_at": 1.0, "daemon_port": 8102, "concurrency_limit": 8}}}
    registry, queue, drain, _ = make_runtime(tmp_path, config, default_limit=64)
    outcome = registry.converge_concurrency()
    assert outcome["changed"] == ["base", "fine"]
    assert outcome["description"]["limit"] is None
    assert drain()["status"] == "applied"
    saved = yaml.safe_load(queue.path.read_text())
    assert saved["models"]["base"]["concurrencyLimit"] == 64
    assert saved["models"]["fine"]["concurrencyLimit"] == 8
    assert saved["models"]["fine"]["metadata"]["llmsvc_registry"]["concurrency_limit"] == 8


def test_converge_dry_run_previews_without_writing(tmp_path):
    registry, queue, _, _ = make_runtime(tmp_path, simple_config("a"), default_limit=64)
    before = queue.path.read_bytes()
    outcome = registry.converge_concurrency(dry_run=True)
    assert outcome["changed"] == ["a"] and outcome["submitted"] is True
    assert outcome["would"] == [{"kind": "set_concurrency_limit", "models": ["a"], "limit": 64}]
    assert queue.path.read_bytes() == before and not queue._jobs and not queue._pending


# -------------------------------------------------------------- inventory

def test_inventory_reports_the_current_concurrency_limit(tmp_path):
    config = simple_config("a", "b")
    config["models"]["a"]["concurrencyLimit"] = 5
    registry, _, _, _ = make_runtime(tmp_path, config)
    rows = {row["name"]: row for row in registry.inventory()["models"]}
    assert rows["a"]["concurrency_limit"] == 5
    assert rows["b"]["concurrency_limit"] is None
