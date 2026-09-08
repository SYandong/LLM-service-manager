# Generated-By: Codex / gpt-6-astra
"""Actual keyboard interactions; HTTP/SQLite are disposable and effects simulated."""

import asyncio
import shlex
from dataclasses import replace

import pytest

pytest.importorskip("textual")

from textual.widgets import DataTable, Input
from tui.app import ConfirmRam
from test_llm_actions import action_service, pin_api, pin_service
from test_tui_actions import app_for, output


def entry(app):
    return app.dashboard.query_one("#command", Input)


def select(app, name):
    table = app.dashboard.query_one("#models", DataTable)
    table.move_cursor(row=app.model_names.index(name))
    table.focus()


async def finish(app, pilot):
    await app.workers.wait_for_complete()
    await pilot.pause()


@pytest.mark.parametrize("size", [(100, 30), (40, 24)])
def test_free_shortcut_only_prefills_and_retains_ram_confirmation(pin_api, action_service, size):
    async def scenario():
        action_service.effects["model"] = replace(action_service.effects["model"],
            state="sleeping", is_sleeping=True, resident_gb=2)
        action_service.scheduler.sample_once()
        app = app_for(pin_api, action_service)
        async with app.run_test(size=size) as pilot:
            await finish(app, pilot)
            before = list(action_service.requests)
            await pilot.press("f")
            assert app.focused is entry(app) and entry(app).value == "free "
            assert action_service.requests == before
            await pilot.press(*"--ram --need 20G", "enter")
            assert isinstance(app.screen, ConfirmRam)
            assert app.screen.focused.id == "ram-cancel"
            assert action_service.requests == before
            # Printable shortcuts cannot edit the hidden field or issue actions.
            await pilot.press("f", "p", "w")
            assert isinstance(app.screen, ConfirmRam) and entry(app).value == ""
            assert action_service.requests == before
            await pilot.press("escape")
            assert app.screen is app.dashboard and "cancelled" in output(app)
            assert not action_service.effects["calls"]
            # The same prefill plus explicit submit still reaches the common executor.
            select(app, "model")
            await pilot.press("f")
            await pilot.press(*"--ram --need 20G", "enter")
            await pilot.click("#ram-confirm")
            await finish(app, pilot)
            assert action_service.requests[-2:] == [("POST", "/v1/free"), ("GET", "/v1/state")]
            assert app.snapshot["models"][0]["state"] == "stopped"
            assert "Free status: complete" in output(app)
    asyncio.run(scenario())


@pytest.mark.parametrize("name", ["model", "org/a b?#%/模型", "literal%2Fmodel", "-leading-dash"])
def test_pin_shortcut_requires_typed_duration_and_keeps_exact_model(pin_api, pin_service, name):
    async def scenario():
        app = app_for(pin_api, pin_service)
        async with app.run_test(size=(40, 24)) as pilot:
            await finish(app, pilot)
            select(app, name)
            before = list(pin_service.requests)
            await pilot.press("p")
            assert app.focused is entry(app)
            assert entry(app).value == "pin --for  -- " + shlex.quote(name)
            assert entry(app).cursor_position == len("pin --for ")
            assert pin_service.requests == before
            await pilot.press("enter")  # Blank duration cannot create a pin.
            await finish(app, pilot)
            assert pin_service.requests == before and not pin_service.scheduler.snapshot().pins
            select(app, name)
            await pilot.press("p", "0", "h", "enter")
            await finish(app, pilot)
            assert pin_service.requests == before and not pin_service.scheduler.snapshot().pins
            select(app, name)
            await pilot.press("p", "1", "h", "enter")
            await finish(app, pilot)
            pins = pin_service.scheduler.snapshot().pins
            assert len(pins) == 1 and pins[0].model == name and pins[0].by == "actual-owner"
            assert "owner actual-owner" in output(app)
            assert pin_service.requests[-2:] == [("POST", "/v1/pin"), ("GET", "/v1/state")]
    asyncio.run(scenario())


@pytest.mark.parametrize("name", ["org/a b?#%/模型", "literal%2Fmodel", "-leading-dash"])
def test_wake_shortcut_uses_safe_name_and_waits_for_enter(pin_api, action_service, name):
    async def scenario():
        index = action_service.names.index(name)
        action_service.effects["model"] = replace(action_service.effects["model"], name=name,
            unit="vllm-%s.service" % index, state="sleeping", is_sleeping=True, resident_gb=2)
        action_service.scheduler.sample_once()
        app = app_for(pin_api, action_service)
        async with app.run_test(size=(100, 30)) as pilot:
            await finish(app, pilot)
            before = list(action_service.requests)
            await pilot.press("w")
            assert shlex.split(entry(app).value) == ["wake", "--", name]
            assert action_service.requests == before and not action_service.effects["calls"]
            await pilot.press("enter")
            await finish(app, pilot)
            assert app.snapshot["models"][0]["state"] == "awake"
            assert "status: ready" in output(app)
            assert action_service.requests[-2:] == [("POST", "/v1/wake/" + pin_api["quote"](name, safe="")), ("GET", "/v1/state")]
    asyncio.run(scenario())


def test_printable_keys_belong_to_input_and_keep_drafts(pin_api, pin_service):
    async def scenario():
        app = app_for(pin_api, pin_service)
        async with app.run_test(size=(40, 24)) as pilot:
            await finish(app, pilot)
            before = list(pin_service.requests)
            await pilot.press("slash")
            assert app.focused is entry(app)
            await pilot.press("f", "p", "w", "u", "question_mark", "q", "r", "slash")
            assert entry(app).value == "fpwu?qr/"
            assert app.is_running and not app.usage_active and pin_service.requests == before
            select(app, "model")
            await pilot.press("w")
            assert entry(app).value == "fpwu?qr/" and app.focused is entry(app)
            assert "draft retained" in output(app)
            assert pin_service.requests == before
    asyncio.run(scenario())


@pytest.mark.parametrize("key", ["p", "w"])
def test_absent_or_stale_selection_never_prefills_or_submits(pin_api, pin_service, key):
    async def scenario():
        app = app_for(pin_api, pin_service)
        async with app.run_test(size=(100, 30)) as pilot:
            await finish(app, pilot)
            before = list(pin_service.requests)
            original = app.snapshot
            app.snapshot = dict(original, models=[])
            # Old table/cursor still references a name missing from the latest snapshot.
            await pilot.press(key)
            assert not entry(app).value and "No current model selection" in output(app)
            app.render_snapshot()  # Empty table also has no valid target.
            await pilot.press(key)
            assert not entry(app).value and pin_service.requests == before
            app.snapshot = original
            app.render_snapshot()
            await pilot.press(key)
            if key == "p":
                await pilot.press("1", "h")
            app.snapshot = dict(original, models=[])
            await pilot.press("enter")
            await finish(app, pilot)
            assert "no longer in the snapshot" in output(app)
            assert pin_service.requests == before
    asyncio.run(scenario())


def test_quoted_name_roundtrip_and_selection_change_never_retargets(pin_api, pin_service):
    async def scenario():
        app = app_for(pin_api, pin_service)
        async with app.run_test(size=(100, 30)) as pilot:
            await finish(app, pilot)
            name = "org/a 'quoted' \"model\"; $(literal)"
            app.snapshot = dict(app.snapshot, models=[dict(app.snapshot["models"][0], name=name)])
            app.render_snapshot()
            before = list(pin_service.requests)
            await pilot.press("p", "2", "h")
            args = pin_api["build_parser"]().parse_args(shlex.split(entry(app).value))
            assert args.model == name and args.duration == 7200
            app.model_names = ["different model"]  # A later cursor change cannot rewrite the draft.
            assert pin_api["build_parser"]().parse_args(shlex.split(entry(app).value)).model == name
            assert pin_service.requests == before
    asyncio.run(scenario())


def test_existing_usage_help_and_quit_keys_work_outside_input(pin_api, pin_service):
    async def scenario():
        app = app_for(pin_api, pin_service)
        async with app.run_test(size=(100, 30)) as pilot:
            await finish(app, pilot)
            await pilot.press("u")
            await finish(app, pilot)
            assert app.usage_active and app.dashboard.has_class("usage")
            assert any(path.startswith("/v1/usage?") for method, path in pin_service.requests)
            await pilot.press("u")
            await finish(app, pilot)
            assert not app.usage_active and app.focused.id == "models"
            await pilot.press("question_mark")
            for text in ["f prefill", "p prefill", "duration required", "w prefill", "u usage", "q quit"]:
                assert text in output(app)
            await pilot.press("q")
            assert not app.is_running
    asyncio.run(scenario())


def test_shortcut_does_not_queue_behind_a_write(pin_api, pin_service):
    async def scenario():
        app = app_for(pin_api, pin_service)
        async with app.run_test(size=(40, 24)) as pilot:
            await finish(app, pilot)
            before = list(pin_service.requests)
            app._write_busy = True
            try:
                await pilot.press("f", "p", "w")
                assert entry(app).value == "" and "already running" in output(app)
                assert pin_service.requests == before
            finally:
                app._write_busy = False
    asyncio.run(scenario())


def test_delayed_prefill_cursor_cannot_replace_typing_or_touch_teardown(pin_api, pin_service):
    from test_tui_teardown import TeardownApp

    async def scenario():
        app = app_for(pin_api, pin_service, TeardownApp)
        queued = []
        original = app.position_prefill_cursor
        app.position_prefill_cursor = lambda *args: queued.append(args)
        async with app.run_test(size=(40, 24)) as pilot:
            await finish(app, pilot)
            await pilot.press("p")
            assert len(queued) == 1
            field, value, cursor = queued.pop()
            # User input arrives before a delayed layout callback. It owns the cursor.
            field.value = "status --json"
            field.cursor_position = 3
            original(field, value, cursor)
            assert field.cursor_position == 3 and field.value == "status --json"

            async def boundary():
                original(field, field.value, 0)
                assert field.cursor_position == 3
            app.at_teardown = boundary
    asyncio.run(scenario())
