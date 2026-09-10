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


async def visible(app, pilot, predicate):
    deadline = time.monotonic() + 3
    while not predicate(app.event_history):
        assert time.monotonic() < deadline, (app.event_history, str(app.dashboard.query_one("#event-status", Static).render()))
        await pilot.pause(0.02)


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
        invalid_published, release_invalid = threading.Event(), threading.Event()
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
            try:
                return dispatch(lines, state)
            finally:
                if frame_id == "invalid-payload":
                    # The real error is published before the dispatch returns.
                    # Hold only test bookkeeping to exercise that interleaving.
                    invalid_published.set()
                    assert release_invalid.wait(5), "test did not release invalid dispatch"
                dispatch_requests.append(len(opened_by) - before)
                if frame_id in dispatched:
                    dispatched[frame_id].set()

        async def send_frame(app, pilot, frame_id, payload, *, wait=True):
            outgoing.put({**payload, "fixture_dispatch_id": frame_id})
            if wait:
                await visible(app, pilot, lambda events: dispatched[frame_id].is_set())

        monkeypatch.setattr(_HTTPEventStream, "__init__", tracked_open)
        monkeypatch.setattr(_HTTPEventStream, "readline", controlled_read)
        monkeypatch.setattr(relay.subscription, "_dispatch", tracked_dispatch)
        app, requests, url = app_for(api, address)
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
            relay.buffer.error("timeout")
            await visible(app, pilot, lambda events: any(e["kind"] == "data_plane_error"
                          and e["detail"]["reason"] == "timeout" for e in events))
            timeout = next(e for e in app.event_history if e["kind"] == "data_plane_error"
                           and e["detail"]["reason"] == "timeout")
            await send_frame(app, pilot, "invalid-payload", {"type": "modelStatus", "data": "PRIVATE_MARKER invalid JSON"}, wait=False)
            def intended_error(event):
                return (event["kind"] == "data_plane_error" and event["id"] > timeout["id"]
                        and event["detail"]["reason"] == "invalid_event")

            try:
                await visible(app, pilot, lambda events: invalid_published.is_set()
                              and any(intended_error(e) for e in events))
                assert not dispatched["invalid-payload"].is_set()
                assert all(dispatched[name].is_set() for name in frame_ids if name != "invalid-payload")
                assert not any(dispatch_requests)
            finally:
                release_invalid.set()
            await visible(app, pilot, lambda events: all(done.is_set() for done in dispatched.values()))
            invalid = next(e for e in app.event_history if intended_error(e))
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
            assert "[llama-swap]" in log_text and "[scheduler]" in log_text
            assert "unlisted_model" in log_text and "intentional filtering" in log_text
            assert "timeout" in log_text and "invalid_event" in log_text
            assert "upstream loss unknown" in log_text
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
            assert "closed" in " ".join(line.text for line in app.dashboard.query_one("#events", RichLog).lines)
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
