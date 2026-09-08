# Generated-By: Codex / gpt-6-astra
"""Core wiring tests: configured adapters, unknown data and collector ownership."""

import sqlite3
import threading
import time
from types import SimpleNamespace

from llmsvc.__main__ import build_collector, build_usage
from llmsvc.config import SchedulerConfig
from llmsvc.scheduler import Scheduler
from llmsvc.state import GPUState, StateSnapshot


def activity_db(path):
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE activity (id INTEGER PRIMARY KEY, ts_created INTEGER, model_id TEXT, input_tokens INTEGER, output_tokens INTEGER, metadata_json TEXT)")
        db.execute("INSERT INTO activity VALUES (1, ?, 'example', 10, 20, ?)",
                   (int(time.time()) - 10, '{"fifo_priority": 0}'))


def test_configured_collector_preserves_unknown_origin_and_host_ram(tmp_path, monkeypatch):
    path = tmp_path / "activity.sqlite"
    activity_db(path)
    captured = {}
    class Probes:
        def __init__(self, swap_url, **kwargs):
            captured.update(swap_url=swap_url, **kwargs)
        def gpus(self):
            return (GPUState(0, uuid="gpu", total_gb=100, used_gb=2, free_gb=98),)
        def processes(self):
            return {}
        def units(self):
            return {"vllm-example.service": {"model": "example", "unit_active": True, "gpu": 0, "util": 0.5}}
        def memory(self):
            raise ValueError("trusted host meminfo not configured")
        def running(self):
            return {"example": "ready"}
        def events(self):
            return SimpleNamespace(states={"example": "ready"}, count=lambda model: 0)
        def health(self, url):
            return True
        def sleeping(self, url):
            return True
    monkeypatch.setattr("llmsvc.collectors.Probes", Probes)
    config = SchedulerConfig("127.0.0.1", 8011, memory_budget_gb=300,
        collectors={"swap_url": "http://127.0.0.1:9000", "activity_path": str(path),
            "models": {"example": {"daemon_url": "http://127.0.0.1:9001", "weights_gb": 40}},
            "nvidia_smi": "/configured/nvidia-smi", "systemctl": "/configured/systemctl", "proc_root": "/configured/proc",
            "memory_budget_gb": 999})
    collector = build_collector(config)
    service = Scheduler(config, collector, usage=build_usage(collector))
    try:
        snapshot = service.sample_once()
        assert captured["nvidia_smi"] == "/configured/nvidia-smi"
        assert captured["systemctl"] == "/configured/systemctl"
        assert captured["proc_root"] == "/configured/proc"
        assert "host_meminfo_path" not in captured
        assert collector.memory_budget_gb == snapshot.memory.budget_gb == 300
        assert snapshot.memory.host_available_gb is None
        assert any(error.startswith("memory:") for error in snapshot.errors)
        model = snapshot.models[0]
        assert (model.unit_active, model.health_ok, model.is_sleeping, model.swap_state) == (True, True, True, "ready")
        assert snapshot.activity[0].by == ("unknown",)
        collector.activity_reader.last_error = "sampler-owned-error"
        usage = service.usage(days=1, by="container")
        assert usage["known"] is True
        assert usage["totals"] == {"requests": 1, "input_tokens": 10, "output_tokens": 20}
        assert usage["rows"][0]["container"] == "unknown"
        assert usage["rows"][0]["source_known"] is False
        assert collector.activity_reader.last_error == "sampler-owned-error"
    finally:
        service.stop()


def test_scheduler_closes_owned_collector_once():
    class Collector:
        closed = 0
        def __call__(self):
            return StateSnapshot()
        def close(self):
            self.closed += 1
    collector = Collector()
    service = Scheduler(SchedulerConfig("127.0.0.1", 8011), collector)
    service.sample_once()
    service.stop()
    service.stop()
    assert collector.closed == 1


def test_multiple_inflight_observations_never_wait_for_action_lock():
    class Quiet:
        def __init__(self):
            self.mutex = threading.Lock()
            self.count = None
            self.connected = False
            self.heartbeats = 0
            self.observations = []
        def observe(self, count, connected=True):
            with self.mutex:
                self.count, self.connected = count, connected
                self.observations.append(count)
        def heartbeat(self):
            with self.mutex:
                self.heartbeats += 1
        def blockers(self):
            with self.mutex:
                return [] if self.connected and self.count == 0 else ["inflight_or_unknown"]
    service = Scheduler(SchedulerConfig("127.0.0.1", 8011))
    quiet = Quiet()
    observe, heartbeat = service.quiet_callbacks(quiet)
    done = threading.Event()
    def read_stream():
        observe(0)
        heartbeat()
        observe(2)
        observe(3)
        done.set()
    with service.action_lock:
        thread = threading.Thread(target=read_stream)
        thread.start()
        completed_while_locked = done.wait(1)
        blocked = quiet.blockers()
    thread.join(1)
    assert completed_while_locked
    assert quiet.observations == [0, 2, 3]
    assert quiet.heartbeats == 1
    assert blocked == ["inflight_or_unknown"]
    observe(None, connected=False)
    assert quiet.blockers()


def test_entrypoint_closes_collector_for_check_once_and_bind_failure(monkeypatch):
    import pytest
    import llmsvc.__main__ as entry
    class Collector:
        activity_reader = None
        def __init__(self):
            self.closed = 0
            self.samples = 0
        def __call__(self):
            self.samples += 1
            return StateSnapshot()
        def close(self):
            self.closed += 1
    config = SchedulerConfig("127.0.0.1", 8011)
    monkeypatch.setattr(entry, "load_config", lambda path: config)
    for option in ("--check-config", "--once"):
        collector = Collector()
        monkeypatch.setattr(entry, "build_collector", lambda cfg: collector)
        monkeypatch.setattr("sys.argv", ["llmsvc", "--config", "unused", option])
        assert entry.main() == 0
        assert collector.closed == 1
        assert collector.samples == (1 if option == "--once" else 0)
    collector = Collector()
    monkeypatch.setattr(entry, "build_collector", lambda cfg: collector)
    def fail_bind(*args):
        raise OSError("test address unavailable")
    monkeypatch.setattr(entry, "SchedulerHTTPServer", fail_bind)
    monkeypatch.setattr("sys.argv", ["llmsvc", "--config", "unused"])
    with pytest.raises(SystemExit) as exc:
        entry.main()
    assert exc.value.code == 2
    assert collector.closed == 1
