# Generated-By: OpenCode / deepseek-v4.1-flash
"""Immediate local command transitions in the TUI STATE cell (#250).

Headless CPU tests with fixture snapshots only: no GPU, HTTP server or
production action runs.  The local intent is display-only and must never edit
the authoritative snapshot or fabricate byte progress.
"""

import asyncio
import copy
import runpy
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("textual")

from textual.widgets import DataTable
from tui.app import QueueEntry, SchedulerApp
from test_llm import snapshot
from test_tui import IdleEvents

CLI = Path(__file__).resolve().parents[1] / "cli" / "llm"


class TransitionClient:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = []
        self.block = False
        self.entered = threading.Event()
        self.release = threading.Event()
        self.fail = set()

    def request(self, method, path, payload=None, **kwargs):
        self.calls.append((method, path))
        if method != "GET":
            if path in self.fail:
                raise OSError("fixture transport failure")
            if self.block:
                self.entered.set()
                if not self.release.wait(5):
                    raise OSError("fixture wait timed out")
            verb, _, model = path.removeprefix("/v1/").partition("/")
            reply = {"model": model, "status": "ready", "elapsed_seconds": 1.0}
            if verb == "wake":
                return {**reply, "ready": True, "cold_start": False}
            if verb == "preload":
                return {**reply, "state": "sleeping", "already_resident": False}
            return {**reply, "state": "sleeping" if verb == "sleep" else "stopped"}
        return self.snapshot


def transition_app(snap=None):
    client = TransitionClient(snap if snap is not None else snapshot)
    api = SimpleNamespace(**runpy.run_path(str(CLI)))
    return SchedulerApp(client, api, event_reader=IdleEvents()), client


def state_cell(app, name):
    return app.query_one("#models", DataTable).get_cell(name, "STATE").plain


def posts(client):
    return [path for method, path in client.calls if method != "GET"]


def entry(command, model, ident=1):
    return QueueEntry(ident, SimpleNamespace(command=command, model=model, dry_run=False),
                      "%s %s" % (command, model), command, model, model)


def test_accepted_operation_shows_the_target_phase_before_any_http_ack(snapshot):
    async def scenario():
        app, client = transition_app(copy.deepcopy(snapshot))
        client.block = True
        async with app.run_test(size=(120, 40)) as pilot:
            await app.workers.wait_for_complete()
            for timer in app._ui_timers:
                timer.pause()
            # The command was accepted and rendered synchronously, before the
            # worker could send anything: no POST is even in flight yet.
            app.submit_command("wake cold-model")
            assert state_cell(app, "cold-model") == "SSDtoGPU"
            assert not posts(client)
            # The snapshot itself is untouched: observed state and no fake field.
            model = next(m for m in app.snapshot["models"] if m["name"] == "cold-model")
            assert model["state"] == "stopped" and "transition" not in model
            assert await asyncio.to_thread(client.entered.wait, 2)
            client.block = False
            client.release.set()
            await app.workers.wait_for_complete()
            # The observed state returns once the local owner is cleaned up.
            assert state_cell(app, "cold-model") == "stopped"
            assert app.local_transition("cold-model") is None
    asyncio.run(scenario())


def test_queued_operation_keeps_the_active_phase_and_a_queue_marker(snapshot):
    async def scenario():
        app, client = transition_app(copy.deepcopy(snapshot))
        client.block = True
        async with app.run_test(size=(120, 40)) as pilot:
            await app.workers.wait_for_complete()
            for timer in app._ui_timers:
                timer.pause()
            app.submit_command("preload cold-model")
            assert await asyncio.to_thread(client.entered.wait, 2)
            app.submit_command("wake cold-model")
            # Active intent outranks the queued one; the queued position is a
            # separate marker on the model name, not a faked running state.
            assert state_cell(app, "cold-model") == "SSDtoMEM"
            assert "[q1]" in app.query_one("#models", DataTable).get_cell("cold-model", "MODEL").plain
            client.block = False
            client.release.set()
            await app.workers.wait_for_complete()
            assert posts(client) == ["/v1/preload/cold-model", "/v1/wake/cold-model"]
            assert state_cell(app, "cold-model") == "stopped"
    asyncio.run(scenario())


def test_local_transition_prefers_active_then_the_next_queued_intent(snapshot):
    app, _ = transition_app(copy.deepcopy(snapshot))
    app.snapshot = copy.deepcopy(snapshot)
    app._current_write = entry("preload", "cold-model", 1)
    app._queue.append(entry("wake", "cold-model", 2))
    assert app.local_transition("cold-model") == "SSDtoMEM"  # Active first.
    app._current_write = None
    assert app.local_transition("cold-model") == "SSDtoGPU"  # Then the next queued intent.


def test_dry_run_never_shows_a_transition(snapshot):
    async def scenario():
        app, client = transition_app(copy.deepcopy(snapshot))
        async with app.run_test(size=(120, 40)) as pilot:
            await app.workers.wait_for_complete()
            for timer in app._ui_timers:
                timer.pause()
            app.submit_command("wake cold-model --dry-run")
            await app.workers.wait_for_complete()
            assert state_cell(app, "cold-model") == "stopped"
            assert app.local_transition("cold-model") is None
    asyncio.run(scenario())


def test_cancelled_confirmation_never_registers_a_transition(snapshot):
    async def scenario():
        app, client = transition_app(copy.deepcopy(snapshot))
        async with app.run_test(size=(120, 40)) as pilot:
            await app.workers.wait_for_complete()
            for timer in app._ui_timers:
                timer.pause()
            app.menu_action("stop", "research-model")
            assert app.pending_confirm is not None
            assert app.local_transition("research-model") is None
            app.resolve_confirm("n")
            await pilot.pause()
            assert posts(client) == []
            assert app.local_transition("research-model") is None
    asyncio.run(scenario())


def test_cancel_clears_only_the_queued_intent_not_the_active_owner(snapshot):
    async def scenario():
        app, client = transition_app(copy.deepcopy(snapshot))
        client.block = True
        async with app.run_test(size=(120, 40)) as pilot:
            await app.workers.wait_for_complete()
            for timer in app._ui_timers:
                timer.pause()
            app.submit_command("preload cold-model")
            assert await asyncio.to_thread(client.entered.wait, 2)
            app.submit_command("wake cold-model")
            assert app.local_transition("cold-model") == "SSDtoMEM"
            assert app.cancel_queue("cold-model") == 1
            assert not app._queue
            assert app.local_transition("cold-model") == "SSDtoMEM"  # Active untouched.
            client.block = False
            client.release.set()
            await app.workers.wait_for_complete()
            assert posts(client) == ["/v1/preload/cold-model"]
            assert app.local_transition("cold-model") is None
    asyncio.run(scenario())


def test_failure_and_success_clear_the_local_owner(snapshot):
    async def scenario():
        app, client = transition_app(copy.deepcopy(snapshot))
        client.fail = {"/v1/wake/cold-model"}
        async with app.run_test(size=(120, 40)) as pilot:
            await app.workers.wait_for_complete()
            for timer in app._ui_timers:
                timer.pause()
            app.submit_command("wake cold-model")
            assert state_cell(app, "cold-model") == "SSDtoGPU"
            await app.workers.wait_for_complete()
            assert state_cell(app, "cold-model") == "stopped"
            assert app.local_transition("cold-model") is None
            # A later unrelated read cannot resurrect the finished intent.
            await app.refresh_state().wait()
            assert state_cell(app, "cold-model") == "stopped"
            assert app.local_transition("cold-model") is None
    asyncio.run(scenario())


def test_unknown_source_is_honest_loading_or_queued(snapshot):
    snap = copy.deepcopy(snapshot)
    for model in snap["models"]:
        if model["name"] == "cold-model":
            model["state"] = None
    app, _ = transition_app(snap)
    app.snapshot = snap
    app._current_write = entry("wake", "cold-model", 1)
    assert app.local_transition("cold-model") == "loading"
    assert "SSD" not in app.local_transition("cold-model")
    app._current_write = None
    app._queue.append(entry("wake", "cold-model", 2))
    assert app.local_transition("cold-model") == "queued"


def test_stale_transition_cannot_overwrite_a_delayed_snapshot(snapshot):
    async def scenario():
        app, client = transition_app(copy.deepcopy(snapshot))
        client.block = True
        async with app.run_test(size=(120, 40)) as pilot:
            await app.workers.wait_for_complete()
            for timer in app._ui_timers:
                timer.pause()
            app.submit_command("sleep research-model")
            assert state_cell(app, "research-model") == "GPUtoMEM"
            client.block = False
            client.release.set()
            await app.workers.wait_for_complete()
            # The authoritative snapshot has no transition; the STATE cell shows
            # the observed state rather than keeping local intent forever.
            assert state_cell(app, "research-model") == "awake"
            assert app.local_transition("research-model") is None
    asyncio.run(scenario())


def set_observed(app, name, state):
    for model in app.snapshot["models"]:
        if model["name"] == name:
            model["state"] = state


def test_active_label_is_frozen_while_the_observed_source_changes(snapshot):
    async def scenario():
        app, client = transition_app(copy.deepcopy(snapshot))
        client.block = True
        async with app.run_test(size=(120, 40)) as pilot:
            await app.workers.wait_for_complete()
            for timer in app._ui_timers:
                timer.pause()
            app.submit_command("preload cold-model")  # stopped -> SSDtoMEM
            assert await asyncio.to_thread(client.entered.wait, 2)
            assert state_cell(app, "cold-model") == "SSDtoMEM"
            # The backend reaches the target (awake) before the HTTP returns.
            set_observed(app, "cold-model", "awake")
            app.render_snapshot()
            assert state_cell(app, "cold-model") == "SSDtoMEM"  # Frozen, not dropped.
            # An intermediate state and then an unknown sample cannot replace it.
            set_observed(app, "cold-model", "sleeping")
            app.render_snapshot()
            assert state_cell(app, "cold-model") == "SSDtoMEM"
            set_observed(app, "cold-model", None)
            app.render_snapshot()
            assert state_cell(app, "cold-model") == "SSDtoMEM"
            set_observed(app, "cold-model", "awake")
            client.block = False
            client.release.set()
            await app.workers.wait_for_complete()
            assert state_cell(app, "cold-model") == "awake"  # Observed returns.
            assert app.local_transition("cold-model") is None
    asyncio.run(scenario())


def test_active_operation_does_not_fall_through_to_a_queued_intent(snapshot):
    async def scenario():
        app, client = transition_app(copy.deepcopy(snapshot))
        client.block = True
        async with app.run_test(size=(120, 40)) as pilot:
            await app.workers.wait_for_complete()
            for timer in app._ui_timers:
                timer.pause()
            app.submit_command("preload cold-model")   # active SSDtoMEM
            assert await asyncio.to_thread(client.entered.wait, 2)
            app.submit_command("wake cold-model")      # queued: would be SSDtoGPU
            assert app.local_transition("cold-model") == "SSDtoMEM"
            # Even after the source changes so the queued intent would map to a
            # different phase, the active operation still owns the cell.
            set_observed(app, "cold-model", "awake")
            app.render_snapshot()
            assert state_cell(app, "cold-model") == "SSDtoMEM"
            client.block = False
            client.release.set()
            await app.workers.wait_for_complete()
            assert app.local_transition("cold-model") is None
    asyncio.run(scenario())


@pytest.mark.parametrize("command", ["wake research-model --dry-run", "wake research-model"])
def test_dry_run_or_noop_active_never_claims_a_queued_transfer(snapshot, command):
    async def scenario():
        app, client = transition_app(copy.deepcopy(snapshot))
        client.block = True
        async with app.run_test(size=(120, 40)) as pilot:
            await app.workers.wait_for_complete()
            for timer in app._ui_timers:
                timer.pause()
            # research-model is awake: wake is a no-op and dry-run records nothing.
            app.submit_command(command)
            assert await asyncio.to_thread(client.entered.wait, 2)
            app.submit_command("sleep research-model")  # queued awake -> GPUtoMEM
            assert app.local_transition("research-model") is None
            assert state_cell(app, "research-model") == "awake"
            client.block = False
            client.release.set()
            await app.workers.wait_for_complete()
    asyncio.run(scenario())


def test_next_queued_operation_takes_over_with_the_new_state(snapshot):
    app, _ = transition_app(copy.deepcopy(snapshot))
    app.snapshot = copy.deepcopy(snapshot)
    active = entry("wake", "cold-model", 1)
    active.transition = app.intent_label(active, active=True)  # stopped -> SSDtoGPU
    app._current_write = active
    app._queue.append(entry("sleep", "cold-model", 2))
    assert app.local_transition("cold-model") == "SSDtoGPU"
    # The active operation finishes; the observed source is now awake, so the
    # queued sleep captures GPUtoMEM when it is actually dispatched.
    app._current_write = None
    set_observed(app, "cold-model", "awake")
    following = app._queue.popleft()
    following.transition = app.intent_label(following, active=True)
    app._current_write = following
    before = copy.deepcopy(app.snapshot)
    assert app.local_transition("cold-model") == "GPUtoMEM"
    assert app.snapshot == before  # The transition is display-only.
