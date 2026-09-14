# Generated-By: Claude Code / claude-fable-5-1
# Generated-By: OpenCode / deepseek-v4.1-flash
"""Item menu, inline confirmation and copy actions over the shared command path."""

import asyncio
import inspect
import runpy
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest

pytest.importorskip("textual")

from textual.pilot import Pilot
from textual.widgets import DataTable, Input, OptionList, RichLog, Static
from tui.app import MENU_ITEMS, QueueEntry, SchedulerApp
from test_tui import IdleEvents, clipboard, record_clipboard, reset_clipboard, snapshot

CLI = Path(__file__).resolve().parents[1] / "cli" / "llm"

# Textual 0.70's Pilot.click has no button= parameter (right-click support).
PILOT_CLICK_BUTTON = "button" in inspect.signature(Pilot.click).parameters


def _textual_version():
    from textual import __version__
    parts = [int(p) if p.isdigit() else 0 for p in __version__.split(".")[:2]]
    return tuple(parts)


# 0.70's OptionList does not select from the test pilot's synthetic mouse click.
OPTION_CLICK_SELECTS = _textual_version() >= (0, 71)


def menu_options(option_list):
    """Ordered options cross-version (0.70 has no OptionList.options property)."""
    return [option_list.get_option(key) for key, _ in MENU_ITEMS]


class ActionClient:
    """Answers the shared executor's own requests; the menu adds no request path."""

    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = []

    def request(self, method, path, payload=None, **kwargs):
        self.calls.append((method, path))
        if method == "GET":
            return self.snapshot
        verb, _, model = path.removeprefix("/v1/").partition("/")
        reply = {"model": model, "status": "ready", "elapsed_seconds": 1.0}
        if verb == "wake":
            return {**reply, "ready": True, "cold_start": False}
        if verb == "preload":
            return {**reply, "state": "sleeping", "already_resident": False}
        return {**reply, "state": "sleeping" if verb == "sleep" else "stopped"}


def action_app(snapshot):
    client = ActionClient(snapshot)
    api = SimpleNamespace(**runpy.run_path(str(CLI)))
    return record_clipboard(SchedulerApp(client, api, event_reader=IdleEvents())), client


def entry(app):
    return app.dashboard.query_one("#command")


def output(app):
    return str(app.dashboard.query_one("#result", Static).render())


def bottom(app):
    return str(app.dashboard.query_one("#event-status", Static).render())


def menu(app):
    return app.dashboard.query_one("#model-menu", OptionList)


def writes(client):
    return [call for call in client.calls if call[0] != "GET"]


async def settle(app, pilot):
    await app.workers.wait_for_complete()
    await pilot.pause()


async def open_menu(app, pilot, name):
    app.dashboard.query_one("#models", DataTable).move_cursor(row=app.model_names.index(name))
    await pilot.press("ctrl+o")
    await pilot.pause()


async def choose(app, pilot, option_id):
    """Walk the highlight with the keyboard; disabled entries are skipped, not hidden."""
    target = [key for key, _ in MENU_ITEMS].index(option_id)
    for _ in range(len(MENU_ITEMS)):
        if menu(app).highlighted == target:
            break
        await pilot.press("down")
    assert menu(app).highlighted == target
    await pilot.press("enter")


@pytest.mark.parametrize("name, disabled", [
    ("default-model", {"preload", "sleep", "stop", "cancel-queue"}),   # sleeping, default, no queue
    ("research-model", {"wake", "preload", "cancel-queue"}),           # awake, no queue
    ("cold-model", {"sleep", "stop", "cancel-queue"}),                 # stopped, no queue
])
def test_menu_is_english_fixed_order_and_greys_out_by_state(snapshot, name, disabled):
    async def scenario():
        app, _ = action_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(app, pilot)
            await open_menu(app, pilot, name)
            assert app.menu_model == name
            options = menu_options(menu(app))
            assert [option.id for option in options] == [key for key, _ in MENU_ITEMS]
            assert [str(option.prompt) for option in options][:8] == [
                "Load into memory", "Bring online", "Sleep to memory",
                "Free from memory" + (" (default)" if name == "default-model" else ""),
                "Cancel queued operations", "Copy name", "Copy status line", "Insert into command line"]
            assert {option.id for option in options if option.disabled} == disabled
            # Greyed-out entries stay visible rather than disappearing.
            assert len(options) == len(MENU_ITEMS)
            assert app.focused is entry(app)
    asyncio.run(scenario())


@pytest.mark.parametrize("name, verb", [
    ("cold-model", "preload"),
    ("cold-model", "wake"),
    ("research-model", "sleep"),
])
def test_menu_entries_reach_the_same_request_as_typing(snapshot, name, verb):
    async def scenario():
        app, client = action_app(snapshot)
        expected = ("POST", "/v1/%s/%s" % (verb, name))
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(app, pilot)
            for timer in app._ui_timers:
                timer.pause()
            await open_menu(app, pilot, name)
            await choose(app, pilot, verb)
            await settle(app, pilot)
            assert app.menu_model is None and not menu(app).display
            assert writes(client) == [expected]
            assert "%s %s status: ready" % (verb.capitalize(), name) in output(app)
            assert client.calls[-1] == ("GET", "/v1/state")  # The write refreshes state.
            typed = list(client.calls)
            entry(app).value = "%s %s" % (verb, name)
            await pilot.press("enter")
            await settle(app, pilot)
            assert writes(client) == [expected, expected]
            assert client.calls[len(typed):] == [expected, ("GET", "/v1/state")]
    asyncio.run(scenario())


def test_free_from_memory_confirms_inline_and_only_y_sends(snapshot):
    async def scenario():
        app, client = action_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(app, pilot)
            for timer in app._ui_timers:
                timer.pause()
            for reply, sent in [("n", []), ("y", [("POST", "/v1/stop/research-model")])]:
                await open_menu(app, pilot, "research-model")
                await choose(app, pilot, "stop")
                await pilot.pause()
                assert app.screen is app.dashboard  # Inline prompt, not a modal.
                assert output(app) == "Free research-model from memory? [y/N]"
                assert writes(client) == sent[:0]
                await pilot.press(reply)
                await settle(app, pilot)
                assert app.pending_confirm is None
                assert writes(client) == sent
                if not sent:
                    assert "cancelled; no request sent" in output(app)
            assert "Stop research-model status: ready" in output(app)
    asyncio.run(scenario())


def test_clicking_a_row_opens_its_menu_and_keeps_the_command_line_focused(snapshot):
    async def scenario():
        app, client = action_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(app, pilot)
            for timer in app._ui_timers:
                timer.pause()
            await pilot.click("#models", offset=(1, 2))  # Row 0 is the header line.
            await pilot.pause()
            assert app.selected_model() == "research-model"
            assert app.menu_model == "research-model"
            assert menu(app).display and app.focused is entry(app)
            # Clicking an entry runs it; the command line still owns the keyboard.
            if OPTION_CLICK_SELECTS:
                await pilot.click("#model-menu", offset=(2, 1 + 2))  # "Sleep to memory"
            else:
                await choose(app, pilot, "sleep")
            await settle(app, pilot)
            assert app.menu_model is None and not menu(app).display
            assert app.focused is entry(app)
            assert writes(client) == [("POST", "/v1/sleep/research-model")]
    asyncio.run(scenario())


def test_menu_navigation_skips_disabled_entries_and_escape_sends_nothing(snapshot):
    async def scenario():
        app, client = action_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(app, pilot)
            await open_menu(app, pilot, "default-model")
            assert menu(app).highlighted == 1  # "Load into memory" is greyed out here.
            await pilot.press("down")
            assert menu(app).highlighted == 5  # Sleep/Free/Cancel are greyed out for the default.
            await pilot.press("escape")
            await settle(app, pilot)
            assert app.menu_model is None and not menu(app).display
            assert writes(client) == []
            assert "Menu closed" in bottom(app)
    asyncio.run(scenario())


def test_copy_entries_use_the_clipboard_and_the_command_line(snapshot):
    async def scenario():
        app, _ = action_app(snapshot)
        async with app.run_test(size=(200, 30)) as pilot:
            await settle(app, pilot)
            await open_menu(app, pilot, "research-model")
            await choose(app, pilot, "copy-name")
            await settle(app, pilot)
            assert clipboard(app) == "research-model"
            assert entry(app).value == "research-model"  # Names reach the command line too.
            assert "Copy requested" in bottom(app)
            entry(app).value = ""
            await open_menu(app, pilot, "research-model")
            await choose(app, pilot, "copy-row")
            await settle(app, pilot)
            assert clipboard(app).startswith("research-model  awake  0  73G")
            assert entry(app).value == ""
            await open_menu(app, pilot, "cold-model")
            await choose(app, pilot, "insert")
            await settle(app, pilot)
            assert entry(app).value == "cold-model"
            assert clipboard(app).startswith("research-model")  # Insert never copies.
    asyncio.run(scenario())


def test_clicking_a_gpu_line_or_an_event_line_copies_it(snapshot):
    async def scenario():
        app, _ = action_app(snapshot)
        events = []

        class Reader(IdleEvents):
            def drain(self):
                batch, events[:] = list(events), []
                return {"generation": 0, "events": batch, "status": "connected", "dropped": 0}

        app.event_reader = Reader()
        # Wide enough that each GPU line does not wrap: one visual line per GPU.
        async with app.run_test(size=(200, 40)) as pilot:
            await settle(app, pilot)
            await pilot.click("#gpus", offset=(2, 0))
            await pilot.pause()
            assert clipboard(app) == "GPU0 used 87/144G  llmsvc 77G  ext 10  free 57"
            assert "gpu 0" in bottom(app)
            await pilot.click("#gpus", offset=(2, 1))
            await pilot.pause()
            assert clipboard(app) == "GPU1 used ?/?G  llmsvc ?G  ext ?  free ?"
            events.append({"id": 4, "timestamp": 4, "kind": "sleep", "model": "research-model"})
            app.update_events()
            await settle(app, pilot)
            assert app.query_one("#events", RichLog).lines
            await pilot.click("#events", offset=(1, 0))
            await pilot.pause()
            # A wrapped log line copies the visible strip the click landed on.
            assert clipboard(app).startswith("00:00:04 [scheduler] #4 sleep")
            assert app.focused is entry(app)
    asyncio.run(scenario())


def test_copy_slash_command_covers_models_gpus_and_events(snapshot):
    async def scenario():
        app, _ = action_app(snapshot)
        async with app.run_test(size=(200, 30)) as pilot:
            await settle(app, pilot)
            for timer in app._ui_timers:
                timer.pause()
            for command, expected in [("/copy research-model", "research-model  awake"),
                                      ("/copy gpu 1", "GPU1 used ?/?G"),
                                      ("/copy events", "Events summary")]:
                entry(app).value = command
                await pilot.press("enter")
                await settle(app, pilot)
                assert clipboard(app).startswith(expected), command
            entry(app).value = "/copy gpu 9"
            await pilot.press("enter")
            await settle(app, pilot)
            assert "needs an observed GPU index" in output(app)
            entry(app).value = "/copy missing-model"
            await pilot.press("enter")
            await settle(app, pilot)
            assert "needs a model in the current snapshot" in output(app)
    asyncio.run(scenario())


@pytest.mark.parametrize("name", ["org/a b?#%/模型", "literal%2Fmodel", "-leading-dash"])
def test_menu_keeps_exact_names_and_the_shared_parser_as_the_only_semantics(snapshot, name):
    async def scenario():
        snapshot["models"][2]["name"] = name  # The stopped model gets an awkward name.
        app, client = action_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(app, pilot)
            for timer in app._ui_timers:
                timer.pause()
            await open_menu(app, pilot, name)
            await choose(app, pilot, "wake")
            await settle(app, pilot)
            # Quoting and the -- separator survive the shared parser untouched.
            assert writes(client) == [("POST", "/v1/wake/" + quote(name, safe=""))]
            # An unknown verb is the parser's answer, not a TUI-invented request.
            before = list(client.calls)
            entry(app).value = "definitely-not-a-command"
            await pilot.press("enter")
            await settle(app, pilot)
            assert "invalid choice" in output(app)
            assert client.calls == before
    asyncio.run(scenario())


def test_clicking_blank_panel_or_composer_closes_the_menu(snapshot):
    async def scenario():
        app, _ = action_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(app, pilot)
            for target in ["#details", "#command", "#memory", "#events"]:
                await open_menu(app, pilot, "research-model")
                assert app.menu_model == "research-model" and menu(app).display
                await pilot.click(target)
                await pilot.pause()
                assert app.menu_model is None and not menu(app).display, target
                assert app.focused is entry(app)
    asyncio.run(scenario())


def test_clicking_the_open_row_again_toggles_the_menu_closed(snapshot):
    async def scenario():
        app, client = action_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(app, pilot)
            for timer in app._ui_timers:
                timer.pause()
            await pilot.click("#models", offset=(1, 2))  # Opens for research-model.
            await pilot.pause()
            assert app.menu_model == "research-model"
            await pilot.click("#models", offset=(1, 2))  # Same row: toggle off.
            await pilot.pause()
            assert app.menu_model is None and not menu(app).display
            assert writes(client) == []
    asyncio.run(scenario())


@pytest.mark.skipif(not PILOT_CLICK_BUTTON, reason="Textual 0.70 Pilot.click has no button= parameter")
def test_right_click_opens_the_model_menu(snapshot):
    async def scenario():
        app, _ = action_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(app, pilot)
            await pilot.click("#models", offset=(1, 2), button=3)
            await pilot.pause()
            assert app.menu_model == "research-model" and menu(app).display
            assert app.focused is entry(app)
    asyncio.run(scenario())


def test_open_menu_reflects_a_finished_loading_action(snapshot):
    async def scenario():
        app, _ = action_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(app, pilot)
            for timer in app._ui_timers:
                timer.pause()
            # While the sleep request for this model is loading, Sleep is disabled.
            app._current_write = QueueEntry(1, SimpleNamespace(command="sleep", model="research-model"),
                                            "sleep research-model", "sleep",
                                            "research-model", "research-model")
            app.open_model_menu(app.model_names.index("research-model"))
            options = {option.id: option for option in menu_options(menu(app))}
            assert options["sleep"].disabled
            # The operation finishes; the still-open menu re-enables Sleep live.
            app._current_write = None
            app.render_snapshot()
            options = {option.id: option for option in menu_options(menu(app))}
            assert not options["sleep"].disabled
    asyncio.run(scenario())


def test_blank_table_area_closes_the_menu_but_rows_and_options_still_work(snapshot):
    async def scenario():
        app, client = action_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(app, pilot)
            for timer in app._ui_timers:
                timer.pause()
            # Clicking the opening row still opens the menu.
            await pilot.click("#models", offset=(1, 2))
            await pilot.pause()
            assert app.menu_model == "research-model" and menu(app).display
            # Clicking blank space below the rows closes it and sends nothing.
            table = app.query_one("#models", DataTable)
            assert table.row_count + 1 <= table.size.height  # Blank space exists.
            await pilot.click("#models", offset=(2, table.size.height - 1))
            await pilot.pause()
            assert app.menu_model is None and not menu(app).display
            assert writes(client) == []
            # An internal option selection is unaffected and sends exactly once.
            await pilot.click("#models", offset=(1, 2))
            await pilot.pause()
            if OPTION_CLICK_SELECTS:
                await pilot.click("#model-menu", offset=(2, 1 + 2))
            else:
                await choose(app, pilot, "sleep")
            await settle(app, pilot)
            assert app.menu_model is None and not menu(app).display
            assert writes(client) == [("POST", "/v1/sleep/research-model")]
    asyncio.run(scenario())


def test_backend_owned_transition_greys_and_labels_loading_and_allows_followup(snapshot):
    async def scenario():
        # Another session owns a preload (stopped -> MEM); this UI sees only the
        # transition and must not enable a conflicting immediate operation.
        snapshot["models"][2]["transition"] = "SSDtoMEM"  # cold-model is stopped
        app, client = action_app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(app, pilot)
            for timer in app._ui_timers:
                timer.pause()
            state_cell = app.query_one("#models", DataTable).get_cell("cold-model", "STATE")
            assert state_cell.plain == "SSDtoMEM"
            assert "dim" in str(state_cell.style)  # Intent, not a measured state.
            await open_menu(app, pilot, "cold-model")
            options = {option.id: option for option in menu_options(menu(app))}
            assert options["preload"].disabled
            assert "running" in str(options["preload"].prompt)
            assert not options["wake"].disabled  # A queued follow-up is allowed.
            # A queued follow-up (created without dispatching it) is cancellable;
            # the backend-owned active operation itself is not.
            app._queue.append(QueueEntry(9, SimpleNamespace(command="wake", model="cold-model"),
                                         "wake cold-model", "wake", "cold-model", "cold-model"))
            app.render_snapshot()  # Refreshes the still-open menu live.
            options = {option.id: option for option in menu_options(menu(app))}
            assert not options["cancel-queue"].disabled
            assert app.cancel_queue("cold-model") == 1
            assert not app._queue
    asyncio.run(scenario())
