# Generated-By: Codex / gpt-6-astra
"""Actual HTTP -> relay -> scheduler SSE, bounded backpressure and lifecycle."""

import http.client
import json
import queue
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from llmsvc.__main__ import build_event_relay
from llmsvc.collectors.relay import DataPlaneEventBuffer
from llmsvc.config import SchedulerConfig
from llmsvc.scheduler import Scheduler
from llmsvc.server import SchedulerHTTPServer
from llmsvc.state import ModelState, StateSnapshot


def model_frame(state="ready", model="m"):
    return {"type": "modelStatus", "data": [{"id": model, "state": state, "name": "PRIVATE_MARKER"}]}


def until(predicate, seconds=2):
    deadline = time.monotonic()+seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate(), "fixture condition did not progress"


class BufferedRelay:
    def __init__(self, capacity=16):
        self.buffer = DataPlaneEventBuffer(["m"], capacity=capacity)
        self.starts = 0
        self.closes = 0
        self.drains = []

    def start(self):
        self.starts += 1

    def close(self):
        self.closes += 1
        self.buffer.connection("closed")

    def drain(self, max_events=128):
        self.drains.append(max_events)
        return self.buffer.drain(max_events)


def config(**kwargs):
    return SchedulerConfig("127.0.0.1", 8011, data_plane_events_enabled=True,
        data_plane_event_interval_seconds=0.01, data_plane_event_timeout_seconds=0.3,
        event_heartbeat_seconds=0.02, **kwargs)


@pytest.fixture
def live_bridge():
    outgoing = queue.Queue()
    done = threading.Event()
    connections = []
    class Source(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            assert self.path == "/api/events"
            connections.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            while not done.is_set():
                try:
                    item = outgoing.get(timeout=0.03)
                    frame = ("data: "+json.dumps(item)+"\n\n").encode()
                except queue.Empty:
                    frame = b": heartbeat\n\n"
                try:
                    self.wfile.write(frame)
                    self.wfile.flush()
                except (OSError, ValueError):
                    return
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Source)
    upstream_thread = threading.Thread(target=lambda: upstream.serve_forever(poll_interval=0.01))
    upstream_thread.start()
    cfg = config(collectors={"swap_url": "http://127.0.0.1:"+str(upstream.server_port), "models": {"m": {}}},
                 data_plane_event_capacity=4, data_plane_event_batch_size=1)
    relay = build_event_relay(cfg)
    scheduler = Scheduler(cfg, lambda: StateSnapshot(sampled_at=time.time(),
        models=(ModelState("m", state="awake", unit_active=True),)), event_relay=relay)
    server = SchedulerHTTPServer(("127.0.0.1", 0), scheduler)
    server_thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    server_thread.start()
    scheduler.start()
    try:
        until(lambda: bool(connections))
        yield scheduler, relay, outgoing, server.server_address, connections
    finally:
        scheduler.stop()
        done.set()
        server.shutdown()
        upstream.shutdown()
        server.server_close()
        upstream.server_close()
        server_thread.join(2)
        upstream_thread.join(2)
        assert not relay.subscription._thread.is_alive()
        assert not scheduler.event_bridge.thread.is_alive()


def test_actual_data_plane_and_native_events_share_scheduler_sse_ids(live_bridge):
    scheduler, relay, outgoing, address, connections = live_bridge
    connection = http.client.HTTPConnection(*address, timeout=2)
    try:
        connection.request("GET", "/v1/events")
        response = connection.getresponse()
        assert response.status == 200
        outgoing.put({"type": "logData", "data": "PRIVATE_MARKER Authorization request text"})
        outgoing.put(model_frame("stopped"))
        native = scheduler.emit("native_fixture", detail={"value": 1})
        events = []
        while not (any(e["kind"] == "data_plane_state" for e in events)
                   and any(e["id"] == native.id for e in events)):
            line = response.readline()
            if line.startswith(b"data: "):
                events.append(json.loads(line[6:]))
        ids = [e["id"] for e in events]
        assert ids == sorted(set(ids))
        plane = next(e for e in events if e["kind"] == "data_plane_state")
        assert plane["model"] == "m" and plane["detail"]["state"] == "stopped"
        assert plane["detail"]["source"] == "llama-swap"
        assert plane["detail"]["trusted_for_quiet"] is False
        assert isinstance(plane["detail"]["received_at"], (float, int))
        assert "PRIVATE_MARKER" not in json.dumps(events)
        assert scheduler.snapshot().models[0].state == "awake"  # data-plane != daemon state
        assert relay.subscription.ordered_source is False
        assert len(connections) == 1  # one continuous reader, not one per SSE client
    finally:
        connection.close()


def source_drops(relay):
    with relay.buffer._lock:
        return relay.buffer._dropped.get("buffer_full", 0)


def test_source_reader_progresses_while_global_action_lock_held(live_bridge):
    scheduler, relay, outgoing, _, _ = live_bridge
    with scheduler.action_lock:
        for n in range(80):
            outgoing.put(model_frame("starting" if n % 2 else "ready"))
        # The consumer may already have detached the counters into its pending
        # batch before discovering the action lock is busy. Both locations prove
        # the reader consumed the burst without waiting for that lock.
        until(lambda: source_drops(relay) + (scheduler.event_bridge.pending or {}).get(
            "dropped_by_reason", {}).get("buffer_full", 0) > 10)
        assert relay.subscription._thread.is_alive()
    until(lambda: any(e.kind == "data_plane_dropped" for e in scheduler.events_since(0)))
    summary = next(e for e in scheduler.events_since(0) if e.kind == "data_plane_dropped")
    assert summary.detail["dropped_by_reason"]["buffer_full"] > 0
    assert summary.detail["upstream_loss_unknown"] is True
    assert summary.detail["trusted_for_quiet"] is False


def test_source_reader_progresses_while_actual_consumer_emit_is_stalled(live_bridge):
    scheduler, relay, outgoing, _, _ = live_bridge
    entered, release = threading.Event(), threading.Event()
    emit = scheduler.emit
    def stalled(kind, **kwargs):
        if kind == "data_plane_state":
            entered.set()
            assert release.wait(2)
        return emit(kind, **kwargs)
    scheduler.emit = stalled
    try:
        outgoing.put(model_frame())
        assert entered.wait(1)
        assert relay.buffer._lock.acquire(blocking=False)
        relay.buffer._lock.release()  # emit happens after drain released the producer mutex
        for n in range(60):
            outgoing.put(model_frame("starting" if n % 2 else "ready"))
        until(lambda: source_drops(relay) > 5)
    finally:
        release.set()
        scheduler.emit = emit


def test_local_filter_and_frame_reasons_are_separate_from_upstream_loss():
    relay = BufferedRelay()
    scheduler = Scheduler(config(), event_relay=relay)
    relay.buffer.observe(model_frame(model="not-allowlisted"))
    relay.buffer.error("invalid_event")
    scheduler.event_bridge.drain_once()
    events = scheduler.events_since(0)
    summary = next(e for e in events if e.kind == "data_plane_dropped")
    assert summary.detail["dropped"] == 2
    assert summary.detail["dropped_by_reason"] == {"unlisted_model": 1, "invalid_event": 1}
    assert summary.detail["upstream_loss_unknown"] is True
    scheduler.stop()


def test_bounded_final_drain_reaches_sse_before_stream_closes():
    relay = BufferedRelay(capacity=8)
    scheduler = Scheduler(config(data_plane_event_capacity=8, data_plane_event_batch_size=1), event_relay=relay)
    for value in ("starting", "ready", "stopping", "stopped"):
        relay.buffer.observe(model_frame(value))
    native = scheduler.emit("native_before_shutdown")
    server = SchedulerHTTPServer(("127.0.0.1", 0), scheduler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    thread.start()
    connection = http.client.HTTPConnection(*server.server_address, timeout=2)
    try:
        connection.request("GET", "/v1/events")
        response = connection.getresponse()
        assert response.readline() == b": connected\n"
        scheduler.stop()
        events = [json.loads(line[6:]) for line in response if line.startswith(b"data: ")]
        assert events[0]["id"] == native.id
        assert [e["detail"]["state"] for e in events if e["kind"] == "data_plane_state"] == ["starting", "ready", "stopping", "stopped"]
        assert events[-1]["detail"]["status"] == "closed"
        assert relay.drains == [1, 8]
        scheduler.stop()
        assert relay.closes == 1
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_final_drain_never_waits_for_action_lock_and_reports_local_discard(caplog):
    relay = BufferedRelay()
    scheduler = Scheduler(config(data_plane_event_capacity=16, data_plane_event_batch_size=1), event_relay=relay)
    relay.buffer.observe(model_frame())
    acquired, release = threading.Event(), threading.Event()
    def hold():
        with scheduler.action_lock:
            acquired.set()
            release.wait(2)
    thread = threading.Thread(target=hold)
    thread.start()
    assert acquired.wait(1)
    started = time.monotonic()
    try:
        scheduler.event_bridge.close()
        assert time.monotonic()-started < 0.2
        report = scheduler.event_bridge.shutdown_discard
        assert report["unpublished_events"] == 2
        assert report["upstream_loss_unknown"] is True
        assert "data_plane_bridge_shutdown_discard" in caplog.text
        assert relay.closes == 1
    finally:
        release.set()
        thread.join(2)
        scheduler.stop()


@pytest.mark.parametrize("option,value", [("data_plane_events_enabled", 1), ("data_plane_event_capacity", 0),
    ("data_plane_event_batch_size", 4097), ("data_plane_event_interval_seconds", float("nan")),
    ("data_plane_event_timeout_seconds", 0), ("data_plane_event_reconnect_seconds", True)])
def test_bridge_configuration_has_explicit_boolean_and_finite_bounds(option, value):
    with pytest.raises(ValueError):
        SchedulerConfig("127.0.0.1", 8011, **{option: value})


def test_factory_disabled_does_not_construct_or_connect(monkeypatch):
    monkeypatch.setattr("llmsvc.collectors.relay.DataPlaneEventRelay", lambda *a, **k: pytest.fail("unexpected construction"))
    assert build_event_relay(SchedulerConfig("127.0.0.1", 8011)) is None
    with pytest.raises(ValueError, match="explicit opt-in"):
        Scheduler(SchedulerConfig("127.0.0.1", 8011), event_relay=BufferedRelay())


@pytest.mark.parametrize("url", ["", "file:///tmp/events", "http://u:p@127.0.0.1", "http://127.0.0.1/upstream/m", "http://127.0.0.1:bad", []])
def test_factory_rejects_unsafe_event_origin_without_network(url):
    with pytest.raises(ValueError):
        build_event_relay(config(collectors={"swap_url": url, "models": {}}))


def test_entrypoint_validation_once_and_bind_failure_never_start_relay(monkeypatch):
    import llmsvc.__main__ as entry
    class Collector:
        def __init__(self):
            self.closed = 0
        def __call__(self):
            return StateSnapshot()
        def close(self):
            self.closed += 1
    monkeypatch.setattr(entry, "load_config", lambda path: config())
    for option in ("--check-config", "--once", "bind-failure"):
        relay, collector = BufferedRelay(), Collector()
        monkeypatch.setattr(entry, "build_event_relay", lambda cfg: relay)
        monkeypatch.setattr(entry, "build_collector", lambda cfg: collector)
        args = ["llmsvc", "--config", "unused"]
        if option != "bind-failure":
            args.append(option)
        else:
            def fail(*args):
                raise OSError("test bind failure")
            monkeypatch.setattr(entry, "SchedulerHTTPServer", fail)
        monkeypatch.setattr("sys.argv", args)
        if option == "bind-failure":
            with pytest.raises(SystemExit):
                entry.main()
        else:
            assert entry.main() == 0
        assert relay.starts == 0 and relay.closes == 1
        assert collector.closed == 1


def test_failed_relay_start_closes_all_owned_resources():
    class FailingRelay(BufferedRelay):
        def start(self):
            raise RuntimeError("start failed")
    relay = FailingRelay()
    class Collector:
        closed = 0
        def close(self):
            self.closed += 1
    collector = Collector()
    scheduler = Scheduler(config(), collector, event_relay=relay)
    with pytest.raises(RuntimeError, match="start failed"):
        scheduler.start()
    assert collector.closed == relay.closes == 1
    assert scheduler.events_closed.is_set()
    assert not scheduler._thread.is_alive()


def test_failed_relay_close_still_closes_collector_and_ends_sse():
    class FailingRelay(BufferedRelay):
        def close(self):
            super().close()
            raise RuntimeError("close failed")
    relay = FailingRelay()
    class Collector:
        closed = 0
        def close(self):
            self.closed += 1
    collector = Collector()
    scheduler = Scheduler(config(), collector, event_relay=relay)
    with pytest.raises(RuntimeError, match="close failed"):
        scheduler.stop()
    assert collector.closed == relay.closes == 1
    assert scheduler.events_closed.is_set()


def test_existing_subscription_buffer_can_be_bridged_without_another_reader():
    import io
    from types import SimpleNamespace
    from llmsvc.collectors.subscription import InflightSubscription
    from llmsvc.reload import QuietPeriod
    opened, release = threading.Event(), threading.Event()
    buffer = DataPlaneEventBuffer(["m"])
    quiet = QuietPeriod()
    raw = ("data: "+json.dumps(model_frame())+"\n\n").encode()
    class Stream(io.BytesIO):
        def readline(self, limit):
            if self.tell() == len(raw):
                release.wait(1)
            return super().readline(limit)
        def close(self):
            release.set()
            super().close()
    streams = []
    def source():
        stream = Stream(raw)
        streams.append(stream)
        opened.set()
        return stream
    subscription = InflightSubscription("http://unused", quiet.observe, event_buffer=buffer,
        ordered_source=False, stream_factory=source, timeout=0.3, reconnect_delay=1)
    existing = SimpleNamespace(start=subscription.start, close=subscription.close, drain=buffer.drain)
    scheduler = Scheduler(config(), event_relay=existing)
    try:
        scheduler.start()
        assert opened.wait(1)
        until(lambda: any(e.kind == "data_plane_state" for e in scheduler.events_since(0)))
        assert len(streams) == 1
        assert quiet.blockers() == [{"reason": "inflight_stream_unknown"}]
    finally:
        scheduler.stop()
        assert not subscription._thread.is_alive()


def test_close_failure_on_check_config_still_closes_database(monkeypatch):
    import llmsvc.__main__ as entry
    class Store:
        def __init__(self):
            self.action_lock = threading.RLock()
            self.read_only = True
            self.closed = 0
        def close(self):
            self.closed += 1
    class FailedClose(BufferedRelay):
        def close(self):
            self.closes += 1
            raise RuntimeError("close failed")
    store, relay = Store(), FailedClose()
    monkeypatch.setattr(entry, "load_config", lambda path: config(state_db_path="fixture.sqlite"))
    monkeypatch.setattr(entry, "build_collector", lambda cfg: None)
    monkeypatch.setattr(entry, "build_event_relay", lambda cfg: relay)
    monkeypatch.setattr(entry, "IntentStore", lambda *a, **k: store)
    monkeypatch.setattr("sys.argv", ["llmsvc", "--config", "unused", "--check-config"])
    with pytest.raises(RuntimeError, match="close failed"):
        entry.main()
    assert store.closed == relay.closes == 1
