# Generated-By: Codex / gpt-6-astra
"""CPU-only data-plane HTTP -> actual scheduler SSE -> TUI, never live inference."""

import asyncio
import copy
import json
import threading
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("textual")

from textual.widgets import RichLog, Static
from tui.app import SchedulerApp
from llmsvc.collectors.subscription import _HTTPEventStream
from test_core_event_bridge import live_bridge, model_frame
from test_llm_events import api
from test_tui import FakeClient, snapshot
from test_tui_events import BufferedEvents


async def visible(app, pilot, predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate(app.event_history):
        if time.monotonic() >= deadline:
            probe = getattr(app, "_relay_pipeline_probe", None)
            report = {"status": str(app.dashboard.query_one("#event-status", Static).render()),
                      "pipeline": probe.diagnostics(app) if probe is not None else None,
                      "visible_events": [(item["id"], item["kind"]) for item in app.event_history]}
            raise AssertionError("Relay visibility stalled:\n" + json.dumps(report, indent=2))
        await pilot.pause(0.02)


class RelayPipelineProbe:
    """Test-only receipts at existing boundaries; never changes production budgets."""
    def __init__(self, monkeypatch, scheduler, relay):
        self.scheduler, self.relay = scheduler, relay
        self.frames, self.details, self.delivered = {}, {}, set()
        self.local = threading.local()
        self.lock, self.drain_lock = threading.RLock(), threading.Lock()
        self.drain_paused = False
        self.room_changed = threading.Event()
        self.hold_publication = None
        self.publication_entered = threading.Event()
        self.release_publication = threading.Event()
        append, drop, drain, emit = (relay.buffer._append_locked, relay.buffer._drop_locked,
                                      relay.drain, scheduler.emit)

        def tracked_append(kind, model, detail, received_at):
            accepted = append(kind, model, detail, received_at)
            name = getattr(self.local, "frame", None)
            if name is not None:
                with self.lock:
                    admission = {"kind": kind, "accepted": accepted, "drained": False,
                                 "publication_entered": False, "scheduler_ids": []}
                    self.frames[name]["admissions"].append(admission)
                    if accepted:
                        # Called with the buffer mutex held: retain the actual
                        # queued object so identity cannot be reused before emit.
                        entry = relay.buffer._queue[-1]
                        self.details[id(entry["detail"])] = (entry, name, admission)
            return accepted

        def tracked_drop(reason, count=1):
            drop(reason, count)
            name = getattr(self.local, "frame", None)
            if name is not None:
                with self.lock:
                    reasons = self.frames[name]["discard_reasons"]
                    reasons[reason] = reasons.get(reason, 0) + count

        def tracked_drain(max_events=128):
            with self.drain_lock:
                if self.drain_paused:
                    return {"events": [], "dropped": 0, "dropped_by_reason": {}, "upstream_loss_unknown": True}
                batch = drain(max_events=max_events)
                with self.lock:
                    for item in batch["events"]:
                        receipt = self.details.get(id(item["detail"]))
                        if receipt is not None:
                            receipt[2]["drained"] = True
            self.room_changed.set()
            return batch

        def tracked_emit(kind, **kwargs):
            with self.lock:
                receipt = self.details.get(id(kwargs.get("detail")))
                if receipt is not None:
                    receipt[2]["publication_entered"] = True
            if receipt is not None and receipt[1] == self.hold_publication:
                self.publication_entered.set()
                assert self.release_publication.wait(5), "test did not release publication"
            event = emit(kind, **kwargs)
            if receipt is not None:
                with self.lock:
                    receipt[2]["scheduler_ids"].append(event.id)
            return event

        monkeypatch.setattr(relay.buffer, "_append_locked", tracked_append)
        monkeypatch.setattr(relay.buffer, "_drop_locked", tracked_drop)
        monkeypatch.setattr(relay, "drain", tracked_drain)
        monkeypatch.setattr(scheduler, "emit", tracked_emit)

    def attach(self, monkeypatch, app):
        app._relay_pipeline_probe = self
        drain = app.event_reader.drain
        def delivered():
            update = drain()
            with self.lock:
                self.delivered.update(item["id"] for item in update["events"])
            return update
        monkeypatch.setattr(app.event_reader, "drain", delivered)

    def pause_drain(self):
        # Synchronize with any in-progress drain before filling the buffer.
        with self.drain_lock:
            self.drain_paused = True

    def resume_drain(self):
        with self.drain_lock:
            self.drain_paused = False
        self.room_changed.set()

    def release_all(self):
        self.resume_drain()
        self.release_publication.set()

    def wait_for_room(self):
        deadline = time.monotonic() + 3
        while True:
            self.room_changed.clear()
            with self.relay.buffer._lock:
                if len(self.relay.buffer._queue) < self.relay.buffer.capacity:
                    return
            remaining = deadline - time.monotonic()
            assert remaining > 0 and self.room_changed.wait(remaining), "test admission wait expired"

    @staticmethod
    def new_frame(origin):
        return {"origin": origin, "source_sent": False, "source_received": False,
                "dispatch_entered": False, "dispatch_returned": False, "dispatch_completed": False,
                "admissions": [], "discard_reasons": {}}

    def sent(self, name):
        with self.lock:
            assert name not in self.frames, "fixture frame IDs must be unique"
            self.frames[name] = self.new_frame("source")
            self.frames[name]["source_sent"] = True

    def run(self, name, operation, *, admit=True, after=None, origin="source"):
        with self.lock:
            frame = self.frames.setdefault(name, self.new_frame(origin))
            assert not frame["source_received"] and not frame["dispatch_entered"], "duplicate fixture dispatch"
            frame["source_received"] = origin == "source"
        self.local.frame = name
        try:
            if admit:
                # Delay only this test's next input at the existing producer
                # boundary. The sole producer cannot fill the freed slot while
                # its own dispatch is here; the real consumer remains running.
                self.wait_for_room()
            with self.lock:
                frame["dispatch_entered"] = True
            try:
                return operation()
            finally:
                with self.lock:
                    frame["dispatch_returned"] = True
                if after is not None:
                    after()
                with self.lock:
                    frame["dispatch_completed"] = True
        finally:
            self.local.frame = None

    def frame(self, name):
        with self.lock:
            return copy.deepcopy(self.frames.get(name, {}))

    def published_ids(self, name):
        return [event_id for item in self.frame(name).get("admissions", []) for event_id in item["scheduler_ids"]]

    def diagnostics(self, app):
        with self.lock:
            frames, delivered = copy.deepcopy(self.frames), sorted(self.delivered)
        with self.relay.buffer._lock:
            buffer = {"queued": len(self.relay.buffer._queue), "capacity": self.relay.buffer.capacity,
                      "discard_reasons": dict(self.relay.buffer._dropped)}
        with app.event_reader._lock:
            queued = [item["id"] for item in app.event_reader._queue]
        return {"frames": frames, "buffer": buffer, "client_queued_ids": queued,
                "client_delivered_ids": delivered, "ui_visible_ids": [item["id"] for item in app.event_history]}


def event_of(app, kind):
    return next(event for event in app.event_history if event["kind"] == kind)


def app_for(api, address):
    url = "http://%s:%s" % address
    client = api["SchedulerClient"](url, timeout=2)
    requests = []
    opener = client.opener

    def tracked(request, **kwargs):
        requests.append((request.get_method(), request.full_url))
        return opener(request, **kwargs)

    client.opener = tracked
    reader = api["EventReader"](client, retry_delay=5)
    return SchedulerApp(client, SimpleNamespace(**api), event_reader=reader), requests, url


@pytest.mark.parametrize("size", [(100, 30), (40, 24)])
def test_real_two_source_panel_preserves_provenance_and_local_filtering(api, live_bridge, size, monkeypatch):
    async def scenario():
        scheduler, relay, outgoing, address, connections = live_bridge
        owner = relay.subscription._thread
        opened_by, dispatch_requests = [], []
        frame_ids = ("ignored-log", "state", "inflight", "filtered-model", "invalid-payload")
        dispatched = {name: threading.Event() for name in frame_ids}
        invalid_dispatch_returned, release_invalid = threading.Event(), threading.Event()
        probe = RelayPipelineProbe(monkeypatch, scheduler, relay)
        force_timeout, timeout_raised = threading.Event(), threading.Event()
        open_stream, read_line = _HTTPEventStream.__init__, _HTTPEventStream.readline
        dispatch = relay.subscription._dispatch

        def tracked_open(stream, *args, **kwargs):
            opened_by.append(threading.current_thread())
            return open_stream(stream, *args, **kwargs)

        def controlled_read(stream, limit):
            if force_timeout.is_set() and not timeout_raised.is_set():
                timeout_raised.set()
                raise TimeoutError("controlled fixture reconnect")
            return read_line(stream, limit)

        def tracked_dispatch(lines, state):
            payload = json.loads(b"\n".join(line[5:].lstrip() for line in lines if line.startswith(b"data:")))
            frame_id = payload.get("fixture_dispatch_id")
            before = len(opened_by)
            def hold_bookkeeping():
                if frame_id == "invalid-payload":
                    invalid_dispatch_returned.set()
                    assert release_invalid.wait(5), "test did not release invalid dispatch"
            try:
                if frame_id not in dispatched:
                    return dispatch(lines, state)
                return probe.run(frame_id, lambda: dispatch(lines, state), after=hold_bookkeeping)
            finally:
                dispatch_requests.append(len(opened_by) - before)
                if frame_id in dispatched:
                    dispatched[frame_id].set()

        async def send_frame(app, pilot, frame_id, payload, *, wait=True):
            probe.sent(frame_id)
            outgoing.put({**payload, "fixture_dispatch_id": frame_id})
            if wait:
                await visible(app, pilot, lambda events: dispatched[frame_id].is_set())

        monkeypatch.setattr(_HTTPEventStream, "__init__", tracked_open)
        monkeypatch.setattr(_HTTPEventStream, "readline", controlled_read)
        monkeypatch.setattr(relay.subscription, "_dispatch", tracked_dispatch)
        app, requests, url = app_for(api, address)
        probe.attach(monkeypatch, app)
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            await send_frame(app, pilot, "ignored-log", {"type": "logData", "data": "PRIVATE_MARKER raw log/request body"})
            await send_frame(app, pilot, "state", model_frame("stopped"))
            native = scheduler.emit("pin", model="m", detail={"fixture": True})
            await visible(app, pilot, lambda events: any(e["kind"] == "data_plane_state" for e in events)
                          and any(e["id"] == native.id for e in events))
            await send_frame(app, pilot, "inflight", {"type": "inflight", "data": {"operation": "snapshot", "requests": [
                {"id": "PRIVATE_MARKER", "model": "m", "body": "PRIVATE_MARKER"}]}})
            await visible(app, pilot, lambda events: any(e["kind"] == "data_plane_inflight"
                          and e["detail"]["operation"] == "snapshot" for e in events))
            # The relay first publishes an unknown count on connection; wait for
            # our actual snapshot rather than assuming the first row is known.
            inflight = next(e for e in app.event_history if e["kind"] == "data_plane_inflight"
                            and e["detail"]["operation"] == "snapshot")
            assert inflight["detail"]["count"] == 1 and inflight["detail"]["operation"] == "snapshot"
            await send_frame(app, pilot, "filtered-model", model_frame(model="not-allowlisted-PRIVATE_MARKER"))
            await visible(app, pilot, lambda events: any(e["kind"] == "data_plane_dropped" for e in events))
            # Deterministically reproduce an earlier unrelated error without
            # waiting for a socket timeout or changing the shared fixture budget.
            probe.run("earlier-timeout", lambda: relay.buffer.error("timeout"), origin="buffer injection")
            await visible(app, pilot, lambda events: any(e["id"] in probe.published_ids("earlier-timeout") for e in events))
            timeout = next(e for e in app.event_history if e["id"] in probe.published_ids("earlier-timeout"))
            await send_frame(app, pilot, "invalid-payload", {"type": "modelStatus", "data": "PRIVATE_MARKER invalid JSON"}, wait=False)
            def intended_error(event):
                return (event["id"] in probe.published_ids("invalid-payload")
                        and event["kind"] == "data_plane_error" and event["detail"]["reason"] == "invalid_event")

            try:
                await visible(app, pilot, lambda events: invalid_dispatch_returned.is_set()
                              and any(intended_error(e) for e in events))
                assert not dispatched["invalid-payload"].is_set()
                assert all(dispatched[name].is_set() for name in frame_ids if name != "invalid-payload")
                assert not any(dispatch_requests)
            finally:
                release_invalid.set()
            await visible(app, pilot, lambda events: all(done.is_set() for done in dispatched.values()))
            invalid = next(e for e in app.event_history if intended_error(e))
            receipt = probe.frame("invalid-payload")
            assert receipt["admissions"] == [{"kind": "data_plane_error", "accepted": True,
                                                "drained": True, "publication_entered": True,
                                                "scheduler_ids": [invalid["id"]]}]
            assert invalid["id"] in probe.delivered
            assert timeout in app.event_history  # Unrelated errors are retained, not filtered away.
            assert event_of(app, "data_plane_error")["detail"]["reason"] == "timeout"
            plane = event_of(app, "data_plane_state")
            dropped = event_of(app, "data_plane_dropped")
            assert plane["model"] == "m" and plane["detail"]["state"] == "stopped"
            for event in [plane, dropped, inflight, timeout, invalid]:
                assert event["detail"]["source"] == "llama-swap"
                assert event["detail"]["trusted_for_quiet"] is False
                assert isinstance(event["detail"]["received_at"], (int, float))
            assert dropped["detail"]["dropped_by_reason"]["unlisted_model"] == 1
            assert dropped["detail"]["upstream_loss_unknown"] is True
            # Raw source stopped must not become a daemon-state update or resource action.
            assert app.snapshot["models"][0]["state"] == "awake"
            assert scheduler.snapshot().models[0].state == "awake"
            ids = [event["id"] for event in app.event_history]
            assert len(ids) == len(set(ids))
            server_events = {event.id: event for event in scheduler.events_since(0)}
            assert all(server_events[event["id"]].detail == event.get("detail", {}) for event in app.event_history)
            # RichLog now wraps at the actual panel width; whitespace at line
            # boundaries is presentation, while every provenance phrase remains.
            log_text = " ".join(" ".join(line.text for line in app.dashboard.query_one("#events", RichLog).lines).split())
            assert "[data-plane]" in log_text and "[scheduler]" in log_text
            details_text = app.event_export_text()
            assert "[llama-swap]" in details_text
            assert "unlisted_model" in details_text and "intentional filtering" in details_text
            assert "timeout" in log_text and "invalid_event" in log_text
            assert "upstream loss unknown" in details_text
            assert "PRIVATE_MARKER" not in log_text + json.dumps(app.event_history)
            assert "fixture_dispatch_id" not in json.dumps(app.event_history)
            assert "not a daemon stop" in app.format_event(plane).plain
            assert str(app.format_event(plane).style) == "blue"
            assert str(app.format_event(next(e for e in app.event_history if e["id"] == native.id)).style) == "cyan"
            assert "Events via scheduler" in str(app.dashboard.query_one("#event-title", Static).render())
            assert all(method == "GET" and target.startswith(url + "/v1/") for method, target in requests)
            assert any("/v1/events?" in target for _, target in requests)
            # All original functional checks have passed. Force a real transport
            # exception now, instead of relying on a host pause or a short timeout.
            before_reconnect = max(e["id"] for e in app.event_history)
            force_timeout.set()
            await visible(app, pilot, lambda events: timeout_raised.is_set() and any(
                e["id"] > before_reconnect and e["kind"] == "data_plane_error"
                and e["detail"]["reason"] == "timeout" for e in events))
            reconnect_error = next(e for e in app.event_history if e["id"] > before_reconnect
                                   and e["kind"] == "data_plane_error" and e["detail"]["reason"] == "timeout")
            await visible(app, pilot, lambda events: any(
                e["id"] > reconnect_error["id"] and e["kind"] == "data_plane_connection"
                and e["detail"]["status"] == "connected" for e in events))
            assert len(connections) >= 2  # The controlled fault really re-opened HTTP.
            assert relay.subscription._thread is owner and owner.is_alive()
            assert opened_by and all(thread is owner for thread in opened_by)
            # Every source frame (including local filtering/invalid payloads) was
            # dispatched without creating a request. Reconnect opens belong only
            # to the original subscription worker, never the UI/filter callbacks.
            assert all(done.is_set() for done in dispatched.values())
            assert not any(dispatch_requests)
            assert reconnect_error in app.event_history and invalid in app.event_history
            assert reconnect_error["detail"]["source"] == "llama-swap"
            assert reconnect_error["detail"]["trusted_for_quiet"] is False
            assert isinstance(reconnect_error["detail"]["received_at"], (int, float))
            assert "PRIVATE_MARKER" not in json.dumps(app.event_history)
            assert all(method == "GET" and target.startswith(url + "/v1/") for method, target in requests)
            assert relay.subscription.ordered_source is False
        assert not app.event_reader.thread.is_alive()
        # Closing one dashboard does not shut down the daemon-owned shared relay.
        assert relay.subscription._thread.is_alive()
    asyncio.run(scenario())



@pytest.mark.parametrize("size", [(100, 30), (40, 24)])
@pytest.mark.parametrize("accepted", [True, False], ids=["accepted", "discarded"])
def test_pipeline_distinguishes_admission_publication_and_discard(api, live_bridge, monkeypatch, size, accepted):
    async def scenario():
        scheduler, relay, outgoing, address, _ = live_bridge
        probe = RelayPipelineProbe(monkeypatch, scheduler, relay)
        dispatch = relay.subscription._dispatch
        name = "admission-target"
        def tracked_dispatch(lines, state):
            payload = json.loads(b"\n".join(line[5:].lstrip() for line in lines if line.startswith(b"data:")))
            if payload.get("fixture_dispatch_id") != name:
                return dispatch(lines, state)
            return probe.run(name, lambda: dispatch(lines, state), admit=accepted)
        monkeypatch.setattr(relay.subscription, "_dispatch", tracked_dispatch)
        app, _, _ = app_for(api, address)
        probe.attach(monkeypatch, app)
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            await visible(app, pilot, lambda events: any(e["kind"] == "data_plane_connection"
                          and e["detail"]["status"] == "connected" for e in events))
            probe.pause_drain()
            try:
                with relay.buffer._lock:
                    assert not relay.buffer._queue
                for number in range(relay.buffer.capacity):
                    probe.run("filler-%s" % number, lambda: relay.buffer.error("timeout"),
                              admit=False, origin="buffer injection")
                if accepted:
                    probe.hold_publication = name
                probe.sent(name)
                outgoing.put({"type": "modelStatus", "data": "PRIVATE_MARKER invalid JSON", "fixture_dispatch_id": name})
                await visible(app, pilot, lambda events: probe.frame(name).get("source_received", False))
                if accepted:
                    # Full buffer: this input must wait at the test-controlled
                    # admission boundary, rather than promising a dropped event.
                    assert not probe.frame(name)["admissions"]
                    probe.resume_drain()
                    await visible(app, pilot, lambda events: probe.publication_entered.is_set()
                                  and probe.frame(name)["dispatch_completed"])
                    receipt = probe.frame(name)
                    assert receipt["admissions"] == [{"kind": "data_plane_error", "accepted": True,
                                                        "drained": True, "publication_entered": True, "scheduler_ids": []}]
                    assert not probe.published_ids(name)  # Drained is not yet published.
                    probe.release_publication.set()
                    await visible(app, pilot, lambda events: any(e["id"] in probe.published_ids(name) for e in events))
                    ids = probe.published_ids(name)
                    assert len(ids) == 1 and ids[0] in probe.delivered
                    event = next(e for e in app.event_history if e["id"] == ids[0])
                    assert event["detail"]["reason"] == "invalid_event"
                    assert event["detail"]["source"] == "llama-swap"
                    assert event["detail"]["trusted_for_quiet"] is False
                else:
                    await visible(app, pilot, lambda events: probe.frame(name).get("dispatch_completed", False))
                    receipt = probe.frame(name)
                    assert receipt["admissions"] == [{"kind": "data_plane_error", "accepted": False,
                                                        "drained": False, "publication_entered": False, "scheduler_ids": []}]
                    assert receipt["discard_reasons"] == {"invalid_event": 1, "buffer_full": 1}
                    # Ensure a future failure exposes each phase, without another
                    # wall-clock timeout or an elided raw-event tuple.
                    with pytest.raises(AssertionError, match="Relay visibility stalled") as failure:
                        await visible(app, pilot, lambda events: False, timeout=0)
                    diagnostics = json.loads(str(failure.value).split("\n", 1)[1])["pipeline"]
                    assert diagnostics["frames"][name] == receipt
                    assert "client_queued_ids" in diagnostics and "client_delivered_ids" in diagnostics
                    assert "ui_visible_ids" in diagnostics
                    probe.resume_drain()
                    await visible(app, pilot, lambda events: any(e["kind"] == "data_plane_dropped"
                                  and e["detail"]["dropped_by_reason"].get("buffer_full") == 1 for e in events))
                    # The actual consumer has now drained the filled buffer and
                    # published its discard summary. A rejected notification does
                    # not magically become the missing error at any later stage.
                    assert not probe.published_ids(name)
                    assert not any(e.kind == "data_plane_error" and e.detail["reason"] == "invalid_event"
                                   for e in scheduler.events_since(0))
                    assert not any(e["kind"] == "data_plane_error" and e["detail"]["reason"] == "invalid_event"
                                   for e in app.event_history)
                    summary = next(e for e in app.event_history if e["kind"] == "data_plane_dropped"
                                   and e["detail"]["dropped_by_reason"].get("buffer_full") == 1)
                    assert summary["detail"]["trusted_for_quiet"] is False
                    assert summary["detail"]["upstream_loss_unknown"] is True
                assert "PRIVATE_MARKER" not in json.dumps(app.event_history)
                assert "fixture_dispatch_id" not in json.dumps(app.event_history)
                assert app.snapshot["models"][0]["state"] == "awake"
            finally:
                probe.release_all()
        assert not app.event_reader.thread.is_alive()
        assert relay.subscription._thread.is_alive()
    asyncio.run(scenario())

def test_core_final_source_event_reaches_ui_and_both_readers_close(api, live_bridge):
    async def scenario():
        scheduler, relay, outgoing, address, _ = live_bridge
        app, _, _ = app_for(api, address)
        async with app.run_test(size=(40, 24)) as pilot:
            await app.workers.wait_for_complete()
            outgoing.put(model_frame("ready"))
            await visible(app, pilot, lambda events: any(e["kind"] == "data_plane_state" for e in events))
            native = scheduler.emit("native_before_shutdown", model="m")
            await asyncio.to_thread(scheduler.stop)
            await visible(app, pilot, lambda events: any(e["id"] == native.id for e in events) and any(
                e["kind"] == "data_plane_connection" and e["detail"]["status"] == "closed" for e in events))
            assert not relay.subscription._thread.is_alive()
            assert not scheduler.event_bridge.thread.is_alive()
            assert app.is_running
            assert "closed" in str(app.dashboard.query_one("#source-status", Static).render())
            assert '"status": "closed"' in app.event_export_text()
        assert not app.event_reader.thread.is_alive()
    asyncio.run(scenario())


@pytest.mark.parametrize("detail", [
    {"source": "llama-swap", "received_at": 123.5, "trusted_for_quiet": False,
     "dropped": 7, "dropped_by_reason": {"unlisted_model": 4, "invalid_event": 1, "buffer_full": 1, "limit_exceeded": 1},
     "upstream_loss_unknown": True},
    {"source": "llama-swap", "received_at": 123.5, "trusted_for_quiet": False,
     "dropped": 4, "dropped_by_reason": {"unlisted_model": 4}, "upstream_loss_unknown": True},
])
def test_relay_counters_remain_separate_from_client_queue_and_history_loss(api, snapshot, detail):
    async def scenario():
        events = BufferedEvents()  # Existing reader fixture reports3 client queue discards.
        app = SchedulerApp(FakeClient(snapshot), SimpleNamespace(**api), event_reader=events)
        original = copy.deepcopy(detail)
        async with app.run_test(size=(40, 24)) as pilot:
            await app.workers.wait_for_complete()
            events.events = [{"id": 99, "timestamp": 100, "kind": "data_plane_dropped", "model": None, "detail": detail}]
            app.update_events()
            await pilot.pause()
            text = app.format_event(app.event_history[0]).plain
            for phrase in ["#99", "local relay discard counts", "upstream loss unknown", "intentional filtering",
                           "payload/framing/schema", "local buffer capacity", "local source bound", '"received_at": 123.5',
                           '"trusted_for_quiet": false']:
                assert phrase in text
            assert detail == original and app.event_history[0]["detail"] == original
            assert "3 events dropped from delivery queue" in str(app.dashboard.query_one("#event-status", Static).render())
            assert "transport loss" not in text
    asyncio.run(scenario())


@pytest.mark.parametrize("detail", [None, ["untyped detail"], "untyped detail"])
def test_untyped_event_detail_still_renders_safely(api, snapshot, detail):
    app = SchedulerApp(FakeClient(snapshot), SimpleNamespace(**api), event_reader=BufferedEvents())
    rendered = app.format_event({"id": 1, "timestamp": 100, "kind": "data_plane_unknown", "detail": detail})
    assert "[scheduler] #1" in rendered.plain
