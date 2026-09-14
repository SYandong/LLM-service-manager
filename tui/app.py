# Generated-By: Codex / gpt-6-astra
# Generated-By: Codex / gpt-5.6-luna
# Generated-By: Claude Code / claude-fable-5-1
"""Scheduler dashboard; command semantics come from the standalone CLI."""

import argparse
import asyncio
from datetime import datetime, timezone
import json
import math
import os
import shlex
import threading
import time

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from .event_view import EventDetails, EventPresentation


# Item menu order is fixed; the identifier of the first four is the CLI verb the
# entry submits through the ordinary command path.
MENU_ITEMS = (
    ("preload", "Load into memory"),
    ("wake", "Bring online"),
    ("sleep", "Sleep to memory"),
    ("stop", "Free from memory"),
    ("copy-name", "Copy name"),
    ("copy-row", "Copy status line"),
    ("insert", "Insert into command line"),
)

# UI-layer actions only. Model commands stay in cli/llm's parser.
SLASH_COMMANDS = ("/clear", "/copy", "/events", "/help", "/quit", "/refresh", "/usage")

HINT = "Enter run · Tab complete · Ctrl+O menu · /help"

INTERRUPT_WINDOW = 2.0


class CommandMessage(Exception):
    """Parser output that belongs inside the UI, not on the terminal stream."""


class UIParser(argparse.ArgumentParser):
    def _print_message(self, message, file=None):
        self.output = getattr(self, "output", "") + (message or "")

    def exit(self, status=0, message=None):
        raise CommandMessage(getattr(self, "output", "") + (message or ""))

    def error(self, message):
        raise CommandMessage(message)


class CommandInput(Input):
    """The only focusable dashboard widget; UI keys never reach it as text."""

    async def on_key(self, event):
        handler = getattr(self.app, "handle_command_key", None)
        if handler is None:
            return
        if handler(event.key, event.is_printable, event.character):
            event.stop()
            event.prevent_default()


class ModelTable(DataTable, can_focus=False):
    """A click selects a row and opens its menu without taking focus."""

    def on_click(self, event):
        # Runs before DataTable's own handler, which still moves its cursor after
        # this; the menu takes the clicked row from the click metadata directly.
        meta = event.style.meta
        row = meta.get("row")
        opener = getattr(self.app, "open_model_menu", None)
        if opener is not None and isinstance(row, int) and row >= 0 and not meta.get("out_of_bounds", False):
            opener(row)


class ConfirmRam(ModalScreen):
    """A separate explicit confirmation; Enter on the command never authorizes stop."""
    DEFAULT_CSS = """
    ConfirmRam { align: center middle; }
    #ram-dialog { width: 90%; max-width: 70; height: auto; padding: 1; background: $surface; }
    #ram-dialog Static { height: auto; }
    #ram-dialog Horizontal { height: 3; }
    #ram-dialog Button { width: 1fr; min-width: 0; }
    """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, command):
        super().__init__()
        self.command = command

    def compose(self):
        with Vertical(id="ram-dialog"):
            yield Static("Confirm host RAM reclamation\n" + self.command +
                         "\nEligible models may be stopped and need a cold start later.", markup=False)
            with Horizontal():
                yield Button("Cancel", id="ram-cancel")
                yield Button("Confirm stop", id="ram-confirm", variant="warning")

    def on_mount(self):
        self.query_one("#ram-cancel", Button).focus()

    def action_cancel(self):
        self.dismiss(False)

    def on_button_pressed(self, event):
        self.dismiss(event.button.id == "ram-confirm")


class EventsChanged(Message):
    """A coalesced notification; the reader thread never touches widgets."""


class SchedulerApp(App):
    TITLE = "LLM service status"
    CSS = """
    Screen { background: #171717; color: #d4d4d4; layers: base overlay; }
    DataTable { background: #171717; }
    DataTable > .datatable--header { background: #242424; color: #b0b0b0; text-style: none; }
    DataTable > .datatable--cursor { background: #34302a; color: #faf3e6; text-style: bold; }
    DataTable > .datatable--hover { background: #242424; }
    RichLog { background: #171717; padding: 0 1; }
    #event-title { color: #ad8c63; padding: 0 1; }
    #event-status { color: #a3a3a3; background: #242424; padding: 0 1; }
    #details { color: #a3a3a3; }
    #summary { height: auto; max-height: 12; }
    #gpus { width: 2fr; height: auto; padding: 0 1; }
    #memory { width: 1fr; height: auto; padding: 0 1; }
    Screen.narrow #summary { layout: vertical; }
    Screen.narrow #gpus, Screen.narrow #memory { width: 1fr; }
    #content { height: 1fr; min-height: 6; }
    #models { width: 3fr; height: 1fr; min-height: 3; }
    #event-panel { width: 2fr; height: 1fr; min-width: 20; }
    #event-heading { height: 1; }
    #event-title { width: 1fr; height: 1; }
    #event-details { width: 9; min-width: 9; height: 1; min-height: 1; border: none; padding: 0; }
    #source-status { height: 2; color: #a3a3a3; padding: 0 1; }
    #event-status { height: 1; }
    #events { height: 1fr; }
    Screen.narrow #content { layout: vertical; }
    Screen.narrow #models { width: 1fr; }
    Screen.narrow #event-panel { width: 1fr; height: 7; }
    #details-view { height: 1; }
    #result-view { height: 2; }
    #details, #result { height: auto; min-height: 1; padding: 0 1; }
    #result { color: $text-muted; }
    #usage-view { display: none; height: 1fr; }
    #usage-controls { height: 3; }
    #usage-controls Button { width: 1fr; min-width: 0; }
    #usage-scroll { height: 1fr; }
    #usage-text { height: auto; padding: 0 1; }
    Screen.usage #summary, Screen.usage #content, Screen.usage #details-view { display: none; }
    Screen.usage #usage-view { display: block; }
    #command-row { height: 1; background: #202020; }
    #prompt { width: 2; height: 1; color: #ad8c63; }
    #command { width: 1fr; height: 1; border: none; padding: 0; background: #202020; }
    #model-menu { layer: overlay; display: none; width: 34; height: auto; max-height: 12;
                  background: #202020; border: tall #ad8c63; }
    """
    # Printable keys belong to the command line; only control chords bind here.
    BINDINGS = [("ctrl+r", "reset_events", "Reset events")]

    def __init__(self, client, api, event_reader=None, refresh=None, **kwargs):
        super().__init__(**kwargs)
        self.client = client
        self.api = api
        self.snapshot = None
        self.fetching = False
        self._write_busy = False
        self._registry_generation = 0
        self._state_generation = 0
        self.model_names = []
        self.terminal_width = 100
        self.refresh_seconds = self.configured_refresh(refresh)
        self.event_reader = event_reader if event_reader is not None else api.EventReader(client)
        self.event_history = []
        self.event_presentation = EventPresentation(api.clean_text, api.format_wake_progress)
        self.event_generation = 0
        self.event_delivery = {}
        self._ui_timers = []
        self._ui_closed = False
        self._rendered = {}
        self._table_columns = None
        self._table_rows = {}
        self._event_notice = threading.Event()
        self._event_driven = False
        self._progress = None
        self.usage_active = False
        self.usage_args = api.build_parser(UIParser).parse_args(["usage"])
        self.usage_snapshot = None
        self.usage_error = "Loading usage…"
        self.usage_generation = 0
        self.usage_fetching = False
        self.history = []
        self.history_index = None
        self.history_draft = ""
        self.menu_model = None
        self.pending_confirm = None
        self._interrupt_at = None
        self._notice = None
        self._observation = None
        self._connection = "SSE connecting"

    @staticmethod
    def configured_refresh(refresh):
        """Seconds between /v1/state polls; LLM_TUI_REFRESH overrides the default."""
        if refresh is None:
            refresh = os.environ.get("LLM_TUI_REFRESH")
        try:
            seconds = float(0.5 if refresh is None else refresh)
        except (TypeError, ValueError):
            return 0.5
        return seconds if math.isfinite(seconds) and seconds > 0 else 0.5

    @property
    def dashboard(self):
        # Textual 0.70 App.query_one searches only the active modal screen.
        # Background observations always belong to the original dashboard.
        return self.screen_stack[0]

    def compose(self) -> ComposeResult:
        with Horizontal(id="summary"):
            yield Static("Loading GPU observations…", id="gpus", markup=False)
            yield Static("Loading RAM observations…", id="memory", markup=False)
        with Horizontal(id="content"):
            yield ModelTable(id="models", cursor_type="row")
            with Vertical(id="event-panel"):
                with Horizontal(id="event-heading"):
                    yield Static("Events via scheduler", id="event-title", markup=False)
                    yield Button("Details", id="event-details")
                yield Static(self.event_presentation.status(), id="source-status", markup=False)
                yield RichLog(id="events", max_lines=500, min_width=1, wrap=True, markup=False, highlight=False)
        with Vertical(id="usage-view"):
            with Horizontal(id="usage-controls"):
                yield Button("7 days", id="usage-7")
                yield Button("30 days", id="usage-30")
                yield Button("Status", id="usage-status")
            with VerticalScroll(id="usage-scroll"):
                yield Static("Loading usage…", id="usage-text", markup=False)
        with VerticalScroll(id="details-view"):
            yield Static("Select a model with Shift+↑/↓", id="details", markup=False)
        with VerticalScroll(id="result-view"):
            yield Static("Status · refresh every %ss · Ctrl+O opens the item menu" % self.api.number(
                self.refresh_seconds), id="result", markup=False)
        with Horizontal(id="command-row"):
            yield Static("› ", id="prompt", markup=False)
            command = CommandInput(placeholder="status | wake MODEL | sleep MODEL | /help", id="command")
            # Newer Textual selects all on focus; typing a parameter must append at
            # the requested cursor instead of replacing the prefilled command.
            if hasattr(command, "select_on_focus"):
                command.select_on_focus = False
            yield command
        yield OptionList(id="model-menu", markup=False)
        yield Static(self._connection + " · " + HINT, id="event-status", markup=False)

    def on_mount(self):
        # Only the command line accepts focus; every panel is click-only.
        for widget in self.dashboard.query("RichLog, Button, VerticalScroll, OptionList"):
            widget.can_focus = False
        self.dashboard.query_one("#command", Input).focus()
        self._event_status = self.dashboard.query_one("#event-status", Static)
        self._event_log = self.dashboard.query_one("#events", RichLog)
        self._ui_timers.append(self.set_interval(self.refresh_seconds, self.refresh_current))
        self.refresh_state()
        self._event_driven = hasattr(self.event_reader, "set_notify")
        self._event_timer = self.set_interval(0.5, self.update_events, pause=self._event_driven)
        self._ui_timers.append(self._event_timer)
        # One coalescing clock also updates elapsed time while a command waits.
        self._progress_timer = self._event_timer
        if self._event_driven:
            self.event_reader.set_notify(self.notify_events)
        self.event_reader.start()
        self.update_events()

    async def on_unmount(self):
        if self._ui_closed:
            return
        self._ui_closed = True
        if self._event_driven:
            self.event_reader.set_notify(None)
        for timer in self._ui_timers:
            timer.stop()
        await asyncio.to_thread(self.event_reader.close)

    def on_resize(self, event):
        self.terminal_width = event.size.width
        self.dashboard.set_class(event.size.width < 100, "narrow")
        self.close_menu()
        if self.snapshot is not None:
            self.render_snapshot()
        if self.usage_active:
            self.render_usage()

    def activity_failure(self):
        """Human presentation only; the complete source diagnostics stay in snapshot."""
        reasons = {
            "deadline": "read budget exceeded", "locked": "database busy",
            "schema": "schema unavailable or unsupported", "parse": "invalid activity data",
            "unavailable": "source unavailable", "corrupt": "database damaged",
            "interrupted": "read interrupted", "read_failed": "read failed",
            "io": "database read I/O failed",
            "round_deadline": "collector round deadline exceeded",
            "previous_probe_running": "previous activity read still running",
            "not configured": "source not configured",
        }
        for error in (self.snapshot or {}).get("errors", []):
            if isinstance(error, str) and error.startswith("activity:"):
                return reasons.get(error.partition(":")[2].strip(), "reason unavailable")
        return None

    @staticmethod
    def activity_sources(stats):
        sources = stats.get("by", [])
        known = [source for source in sources if source and source != "unknown"]
        if not known:
            return "source unavailable"
        return "from " + ", ".join(known) + (" · some sources unavailable" if len(known) < len(sources) else "")

    def snapshot_message(self):
        errors = self.snapshot.get("errors", [])
        failure = self.activity_failure()
        if failure is not None:
            message = "Partial update · activity unavailable: " + failure
            if any(not isinstance(error, str) or not error.startswith("activity:") for error in errors):
                message += " · other observations unavailable (status --json for details)"
        elif errors:
            message = "Partial update · some observations unavailable (status --json for details)"
        else:
            message = "Updated"
        return message + (" · read-only" if self.snapshot.get("read_only") else "")

    @work
    async def refresh_state(self, args=None):
        # A slow HTTP request must not start overlapping polls or freeze keyboard input.
        if not self.is_running or self.fetching or self._write_busy:
            return
        self.fetching = True
        generation = self._state_generation
        # A half-second poll must never overwrite what a command printed; only a
        # typed status command owns the result area.
        typed = args is not None
        try:
            args = args or self.api.build_parser(UIParser).parse_args(["status"])
            snapshot = await asyncio.to_thread(self.api.execute_command, args, self.client)
            if not self.is_running or generation != self._state_generation:
                return
            # Validate the shared response before replacing the last good display.
            self.api.format_status(snapshot)
            self.snapshot = snapshot
            self.render_snapshot()
            if getattr(args, "json", False):
                message = json.dumps(snapshot, ensure_ascii=False)
            else:
                message = self.snapshot_message()
            if typed:
                if not self.usage_active:
                    self.show_result(message)
            else:
                self.show_observation(message)
        except Exception as exc:
            if self.is_running and generation == self._state_generation:
                # Keep the last good snapshot and say when the failed read happened.
                failure = "Refresh failed at %s; last snapshot retained: %s" % (
                    datetime.now(timezone.utc).strftime("%H:%M:%SZ"), exc)
                if typed and not self.usage_active:
                    self.show_result(failure)
                else:
                    self.show_observation(failure)
        finally:
            self.fetching = False

    @work
    async def show_models(self, args):
        if not self.is_running:
            return
        self._registry_generation += 1
        generation, state_generation = self._registry_generation, self._state_generation
        try:
            result = await asyncio.to_thread(self.api.execute_command, args, self.client)
            message = self.api.format_result(args, result, width=self.terminal_width)
        except Exception as exc:
            message = "Registry read failed: " + self.api.format_registry_error(exc)
        if (self.is_running and generation == self._registry_generation
                and state_generation == self._state_generation and not self._write_busy):
            self.show_result(message)

    @work
    async def run_write(self, args):
        if not self.is_running:
            return
        if self._write_busy:
            self.show_result("An operation is already running; wait for its result (no request queued)")
            return
        self._write_busy = True
        self._state_generation += 1
        self.usage_active = False
        self.usage_generation += 1
        self.dashboard.remove_class("usage")
        target = getattr(args, "model", None)
        if target is None:
            target = "GPU %s" % args.gpu if getattr(args, "gpu", None) is not None else "host RAM" if getattr(args, "ram", False) else "eligible models"
        model = next((m for m in (self.snapshot or {}).get("models", []) if m["name"] == target), {})
        estimate = model.get("cold_start_seconds") if args.command == "wake" and model.get("state") == "stopped" else None
        self._progress = {"command": args.command, "target": target, "started": time.monotonic(),
                          "stage": "waiting for observed release" if args.command == "free" else "awaiting scheduler response",
                          "estimate": estimate, "after_id": max((e["id"] for e in self.event_history), default=0),
                          "since": time.time(), "log_epoch": None, "progress_sequence": 0,
                          "retired_epochs": set()}
        self.render_progress()
        self._progress_timer.resume()
        try:
            result = await asyncio.to_thread(self.api.execute_command, args, self.client)
            if not self.is_running:
                return
            message = self.api.format_result(args, result, width=self.terminal_width)
            self._progress["stage"] = "response received; refreshing state"
            try:
                status_args = self.api.build_parser(UIParser).parse_args(["status"])
                snapshot = await asyncio.to_thread(self.api.execute_command, status_args, self.client)
                if not self.is_running:
                    return
                self.api.format_status(snapshot)
                self.snapshot = snapshot
                self.render_snapshot()
                if getattr(args, "model", None) in self.model_names:
                    self.dashboard.query_one("#models", DataTable).move_cursor(row=self.model_names.index(args.model))
                    self.update_details()
            except Exception as exc:
                message += "\nState refresh failed: " + str(exc)
            if self.is_running:
                self.show_result(message)
        except Exception as exc:
            if self.is_running:
                detail = self.api.format_registry_error(exc) if args.command in ("add", "rm") else str(exc)
                self.show_result("%s request failed: %s" % (args.command, detail))
        finally:
            self._write_busy = False
            self._progress = None
            # A pending SSE notification must still be drained after completion.

    def render_progress(self):
        if not self.is_running or not self._progress:
            return
        progress = self._progress
        elapsed = max(0, time.monotonic() - progress["started"])
        estimate = progress["estimate"]
        eta = "ETA unknown"
        if type(estimate) in (int, float) and math.isfinite(estimate) and estimate > 0:
            eta = "estimated total ~%ss (configured), ETA unknown" % round(estimate)
        self.show_result("%s %s · %.0fs elapsed · stage: %s · %s" % (
            progress["command"].capitalize(), progress["target"], elapsed, progress["stage"], eta))

    def confirm_ram(self, args, text):
        def decided(confirmed):
            if not self.is_running:
                return
            if confirmed:
                self.run_write(args)
            else:
                self.show_result("Free --ram cancelled; no request sent")
        self.push_screen(ConfirmRam(self.api.clean_text(text)), decided)

    def refresh_current(self):
        if self.usage_active:
            self.refresh_usage()
        else:
            self.refresh_state()

    def show_status(self):
        self.usage_active = False
        self.usage_generation += 1
        self.dashboard.remove_class("usage")
        self.dashboard.query_one("#command", Input).focus()
        self.refresh_state()

    def show_usage(self, args):
        self.usage_args = args
        self.usage_active = True
        self.usage_generation += 1
        self.usage_snapshot = None
        self.usage_error = "Loading usage…"
        self.close_menu()
        self.dashboard.add_class("usage")
        self.render_usage()
        self.dashboard.query_one("#command", Input).focus()
        self.refresh_usage()

    @work
    async def refresh_usage(self):
        if not self.is_running or self.usage_fetching:
            return
        self.usage_fetching = True
        try:
            while self.usage_active:
                generation, args = self.usage_generation, self.usage_args
                try:
                    result = await asyncio.to_thread(self.api.execute_command, args, self.client)
                except Exception as exc:
                    result, error = None, "Usage unavailable: " + str(exc)
                else:
                    error = ""
                if not self.is_running:
                    return
                if self.usage_active and generation == self.usage_generation:
                    self.usage_snapshot, self.usage_error = result, error
                    self.render_usage()
                    self.show_result("Usage updated" if result and result["known"] else
                                     "Usage unavailable; counts are unknown")
                    break
                # Window changes during a slow query queue only the latest selection.
        finally:
            self.usage_fetching = False

    def render_usage(self):
        if self.usage_snapshot is None:
            text = "Usage: last %s days by %s\n%s" % (
                self.usage_args.days, self.usage_args.by, self.api.clean_text(self.usage_error))
        else:
            text = self.api.format_result(self.usage_args, self.usage_snapshot,
                                          width=max(1, self.terminal_width - 4))
        self.dashboard.query_one("#usage-text", Static).update(text)

    def on_button_pressed(self, event):
        if event.button.id == "event-details":
            self.action_event_details()
        elif event.button.id == "usage-status":
            self.show_status()
        elif event.button.id in ("usage-7", "usage-30"):
            days = event.button.id.removeprefix("usage-")
            args = self.api.build_parser(UIParser).parse_args(["usage", "--days", days, "--by", self.usage_args.by])
            self.show_usage(args)
        self.dashboard.query_one("#command", Input).focus()

    def update_static(self, name, text):
        if self._rendered.get(name) != text:
            self.dashboard.query_one("#" + name, Static).update(text)
            self._rendered[name] = text.copy() if isinstance(text, Text) else text

    def show_result(self, text):
        self.update_static("result", self.api.clean_text(text))

    def show_notice(self, text):
        """Transient UI feedback; a background poll must never overwrite it."""
        self._notice = text
        self.render_event_status()

    def show_observation(self, text):
        """The latest background read; it has its own slot beside user feedback."""
        self._observation = text
        self.render_event_status()

    def gpu_text(self, gpu):
        return "GPU%s %s/%sG  llmsvc %s  ext %s" % (
            gpu["index"], self.api.number(gpu.get("used_gb")), self.api.number(gpu.get("total_gb")),
            self.api.number(gpu.get("managed_gb")), self.api.number(gpu.get("external_gb")))

    @staticmethod
    def gpu_style(gpu):
        total, used = gpu.get("total_gb"), gpu.get("used_gb")
        if not (total is not None and total > 0 and used is not None):
            return "dim"
        percent = min(100, max(0, used / total * 100))
        return "#6f9f6f" if percent < 60 else "#ad8c63" if percent < 85 else "#c46a6a"

    def render_snapshot(self):
        state = self.snapshot
        # The server filters active intents with its own clock.
        reserved = {r["gpu"] for r in state.get("reserves", [])}
        lines = Text()
        for gpu in state.get("gpus", []):
            lines.append(self.gpu_text(gpu), style=self.gpu_style(gpu))
            if gpu["index"] in reserved:
                lines.append(" · reserved for placement", style="cyan")
            lines.append("\n")
        lines.rstrip()
        self.update_static("gpus", lines)
        memory = state.get("memory", {})
        self.update_static("memory", Text("RAM %s/%s GiB budget\nHost free %s GiB" % (
            self.api.number(memory.get("sleeping_weights_gb")), self.api.number(memory.get("budget_gb")),
            self.api.number(memory.get("host_available_gb"))), style="#a3a3a3"))
        table = self.dashboard.query_one("#models", DataTable)
        previous = self.selected_model()
        table_width = self.terminal_width if self.terminal_width < 100 else int(self.terminal_width * 0.6)
        # 65 = the fixed columns plus DataTable's per-cell padding; below that the
        # wide set cannot fit and the compact one is used instead.
        narrow = table_width < 78
        columns = [("MODEL", max(8, min(26, table_width - (33 if narrow else 65)))), ("STATE", 8), ("GPU", 3), ("MEM", 6)]
        if narrow:
            columns.append(("PIN", 3))
        else:
            columns.extend([("BUDGET", 6), ("USED", 6), ("10m", 4), ("PIN", 16)])
        if self._table_columns != columns:
            # Only a schema/terminal-width change rebuilds the table.
            table.clear(columns=True)
            for label, width in columns:
                table.add_column(label, width=width, key=label)
            self._table_columns = columns
            self._table_rows.clear()
            self.model_names = []
        activity = {} if self.activity_failure() is not None else {
            item["model"]: item for item in state.get("activity", [])}
        now = state.get("sampled_at")
        observed_at = self.api.time.time() if now is None else now
        pins = {item["model"]: item for item in state.get("pins", []) if item["until"] > observed_at}
        models = {model["name"]: model for model in state.get("models", [])}
        for name in list(self.model_names):
            if name not in models:
                table.remove_row(name)
                self._table_rows.pop(name, None)
                self.model_names.remove(name)
        for model in state.get("models", []):
            name = model["name"]
            stats, pin = activity.get(name, {}), pins.get(name)
            label = self.api.fit(name, columns[0][1] - 2).rstrip() + " *" if model.get("is_default") else name
            row = [label, model.get("state", "unknown"), "-" if model.get("gpu") is None else str(model["gpu"]),
                   self.api.number(model.get("resident_gb")) + "G"]
            if narrow:
                row.append("yes" if pin else "-")
            else:
                used = None if now is None or stats.get("last_request_at") is None else now - stats["last_request_at"]
                row.extend([self.api.number(model.get("budget_gb")) + "G", self.api.age(used),
                            "?" if stats.get("requests_last_10m") is None else str(stats["requests_last_10m"]),
                            self.api.expiry(pin["until"]) if pin else "-"])
            cells = tuple(Text(self.api.clean_text(value),
                               justify="right" if columns[index][0] in ("GPU", "MEM", "BUDGET", "USED", "10m") else "left",
                               style="dim" if value in ("?", "?G", "-", "unknown") else "")
                          for index, value in enumerate(row))
            old = self._table_rows.get(name)
            if old is None:
                table.add_row(*cells, key=name)
                self.model_names.append(name)
            else:
                for index, value in enumerate(cells):
                    if value != old[index]:
                        table.update_cell(name, columns[index][0], value, update_width=False)
            self._table_rows[name] = cells
        if previous in self.model_names and table.cursor_row != self.model_names.index(previous):
            table.move_cursor(row=self.model_names.index(previous), animate=False)
        self.update_details()

    def selected_model(self):
        row = self.dashboard.query_one("#models", DataTable).cursor_row
        return self.model_names[row] if 0 <= row < len(self.model_names) else None

    def model_status_line(self, name):
        cells = self._table_rows.get(name)
        return None if cells is None else "  ".join(cell.plain.strip() for cell in cells)

    def update_details(self):
        name = self.selected_model()
        text = "No model observations" if name is None else name
        if name:
            stats = next((item for item in self.snapshot.get("activity", []) if item["model"] == name), {})
            now = self.snapshot.get("sampled_at")
            observed_at = self.api.time.time() if now is None else now
            pin = next((item for item in self.snapshot.get("pins", [])
                        if item["model"] == name and item["until"] > observed_at), None)
            failure = self.activity_failure()
            if failure is not None:
                text += " · activity unavailable: " + failure + " · source unavailable"
            else:
                text += " · " + self.activity_sources(stats)
            if pin:
                text += " · pin %s (%s)" % (self.api.expiry(pin["until"]), pin["by"])
        self.update_static("details", self.api.clean_text(text))

    def on_data_table_row_highlighted(self, event):
        if self.snapshot is not None and event.data_table.is_attached:
            self.update_details()

    # ---------------------------------------------------------------- keyboard

    def handle_command_key(self, key, printable, character):
        """Return True when the key is a UI action instead of command-line text."""
        if not self.is_running or self.screen is not self.dashboard:
            return False
        if key != "ctrl+c":
            self._interrupt_at = None
        if self.pending_confirm is not None:
            if printable or key in ("enter", "escape"):
                self.resolve_confirm(character if printable else None)
                return True
            return False
        if self.menu_model is not None:
            if key in ("up", "down", "enter", "escape", "ctrl+o"):
                self.menu_key(key)
                return True
            return False
        entry = self.dashboard.query_one("#command", Input)
        if key == "question_mark":
            if entry.value:
                return False
            self.action_help()
            return True
        if key == "escape":
            entry.value = ""
            return True
        if key == "ctrl+c":
            self.interrupt(entry)
            return True
        if key == "ctrl+d":
            if entry.value:
                return False
            self.exit()
            return True
        if key == "tab":
            self.complete(entry)
            return True
        if key in ("up", "down"):
            self.recall_history(entry, -1 if key == "up" else 1)
            return True
        if key in ("shift+tab", "shift+down", "shift+up"):
            self.move_selection(-1 if key == "shift+up" else 1)
            return True
        if key == "ctrl+o":
            self.open_model_menu()
            return True
        if key == "ctrl+l":
            self._event_log.clear()
            self.event_presentation.reset_display()
            self.show_notice("Event log display cleared; cursor and history kept")
            return True
        return False

    def interrupt(self, entry):
        if entry.value:
            entry.value = ""
            self._interrupt_at = None
            self.show_notice("Input cleared")
            return
        now = time.monotonic()
        if self._interrupt_at is not None and now - self._interrupt_at <= INTERRUPT_WINDOW:
            self.exit()
            return
        self._interrupt_at = now
        self.show_notice("Press Ctrl+C again to exit")

    def remember(self, value):
        if value and (not self.history or self.history[-1] != value):
            self.history.append(value)
        self.history_index = None
        self.history_draft = ""

    def recall_history(self, entry, direction):
        if not self.history:
            return
        if self.history_index is None:
            if direction > 0:
                return
            self.history_draft = entry.value
            self.history_index = len(self.history)
        index = self.history_index + direction
        if index >= len(self.history):
            self.history_index = None
            entry.value = self.history_draft
        else:
            self.history_index = max(0, index)
            entry.value = self.history[self.history_index]
        entry.cursor_position = len(entry.value)

    def command_names(self):
        parser = self.api.build_parser(UIParser)
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                return sorted(action.choices)
        return []

    def complete(self, entry):
        value = entry.value
        head, _, token = value.rpartition(" ")
        first = not head.strip()
        if first and token.startswith("/"):
            candidates = [name for name in SLASH_COMMANDS if name.startswith(token)]
        elif first:
            candidates = [name for name in self.command_names() if name.startswith(token)]
        else:
            candidates = [name for name in self.model_names if name.startswith(token)]
        if not candidates:
            self.show_notice("No completion for %r" % self.api.clean_text(token))
            return
        if len(candidates) == 1:
            entry.value = (head + " " if head else "") + candidates[0] + " "
            entry.cursor_position = len(entry.value)
            return
        self.show_result("Completions: " + " ".join(self.api.clean_text(name) for name in candidates))

    def move_selection(self, delta):
        if not self.model_names:
            self.show_notice("No model rows to select")
            return
        table = self.dashboard.query_one("#models", DataTable)
        row = min(len(self.model_names) - 1, max(0, table.cursor_row + delta))
        table.move_cursor(row=row, animate=False)
        self.update_details()

    def action_help(self):
        self.show_result(
            "Enter run · Tab complete · ↑/↓ history · Shift+↑/↓ or Shift+Tab select model · "
            "Ctrl+O item menu · Ctrl+L clear event log · Ctrl+R reset event cursor · "
            "Esc close menu or clear input · Ctrl+C clear then exit · Ctrl+D exit · "
            "click a row for its menu, a GPU line or an event line to copy it · "
            "/help /quit /usage [7|30] /events /clear /copy [MODEL|gpu N|events] /refresh · "
            "commands use the llm CLI: status · usage --days 7|30 · wake MODEL · sleep MODEL · "
            "stop MODEL · preload MODEL · free [--gpu N] [--need 80G] [--ram] · pin MODEL --for 8h · "
            "unpin MODEL · reserve --gpu N --size 80G --for 4h · unreserve ID · models · registry · "
            "add PATH --name X --base BASE · rm NAME · all operations accept --dry-run")

    def action_reset_events(self):
        self.event_reader.reset_cursor()
        self.update_events()
        self.show_result("Event cursor reset locally; replaying available scheduler history")

    # -------------------------------------------------------------- item menu

    def open_model_menu(self, row=None):
        if not self.is_running or self.screen is not self.dashboard:
            return
        table = self.dashboard.query_one("#models", DataTable)
        if row is not None and 0 <= row < len(self.model_names):
            table.move_cursor(row=row, animate=False)
            self.update_details()
        name = self.selected_model()
        model = next((item for item in (self.snapshot or {}).get("models", []) if item["name"] == name), None)
        if model is None:
            self.show_result("No current model selection; refresh and select a model first")
            return
        state, default = model.get("state"), bool(model.get("is_default"))
        disabled = {"awake": {"wake", "preload"}, "sleeping": {"preload", "sleep"},
                    "stopped": {"sleep", "stop"}}.get(state, set()) | ({"stop"} if default else set())
        menu = self.dashboard.query_one("#model-menu", OptionList)
        menu.clear_options()
        menu.add_options([Option(label + (" (default)" if default and key == "stop" else ""),
                                 id=key, disabled=key in disabled) for key, label in MENU_ITEMS])
        menu.display = True
        menu.highlighted = next((index for index, (key, _) in enumerate(MENU_ITEMS) if key not in disabled), 0)
        menu.styles.offset = self.menu_offset(table, self.model_names.index(name))
        self.menu_model = name
        self.dashboard.query_one("#command", Input).focus()
        self.show_result("Menu for %s · ↑/↓ choose · Enter run · Esc close" % self.api.clean_text(name))

    def menu_offset(self, table, row):
        height, width = len(MENU_ITEMS) + 2, 34
        region = table.content_region
        y = region.y + (table.header_height if table.show_header else 0) + row - int(table.scroll_y) + 1
        return (max(0, min(region.x + 2, self.size.width - width)),
                max(0, min(y, self.size.height - height)))

    def close_menu(self, notice=None):
        if self.menu_model is None:
            return
        self.menu_model = None
        menu = self.dashboard.query_one("#model-menu", OptionList)
        menu.display = False
        menu.clear_options()
        self.dashboard.query_one("#command", Input).focus()
        if notice:
            self.show_notice(notice)

    def menu_key(self, key):
        menu = self.dashboard.query_one("#model-menu", OptionList)
        if key == "up":
            menu.action_cursor_up()
        elif key == "down":
            menu.action_cursor_down()
        elif key == "enter":
            menu.action_select()
        else:
            self.close_menu("Menu closed; nothing sent")

    def on_option_list_option_selected(self, event):
        event.stop()
        if event.option_list.id != "model-menu":
            return
        choice, name = event.option.id, self.menu_model
        self.close_menu()
        if name is not None:
            self.menu_action(choice, name)

    def menu_action(self, choice, name):
        if choice in ("preload", "wake", "sleep"):
            self.submit_command("%s -- %s" % (choice, shlex.quote(name)))
        elif choice == "stop":
            self.pending_confirm = ("stop -- " + shlex.quote(name), name)
            self.show_result("Free %s from memory? [y/N]" % self.api.clean_text(name))
        elif choice == "copy-name":
            self.copy_text(name, "model name")
            self.append_to_command(name)
        elif choice == "copy-row":
            line = self.model_status_line(name)
            if line is None:
                self.show_notice("No status line for this model yet")
            else:
                self.copy_text(line, "status line")
        elif choice == "insert":
            self.append_to_command(name)

    def resolve_confirm(self, character):
        command, name = self.pending_confirm
        self.pending_confirm = None
        if character in ("y", "Y"):
            self.submit_command(command)
        else:
            self.show_result("Free %s from memory cancelled; no request sent" % self.api.clean_text(name))

    def append_to_command(self, text):
        entry = self.dashboard.query_one("#command", Input)
        value = entry.value
        if value and not value.endswith(" "):
            value += " "
        entry.value = value + text
        entry.cursor_position = len(entry.value)
        entry.focus()

    def copy_text(self, text, label):
        """Requesting the terminal clipboard can silently fail; always say so."""
        copier = getattr(self, "copy_to_clipboard", None)
        if not callable(copier) or getattr(self, "_driver", None) is None:
            self.show_notice("Clipboard unavailable here; %s not copied" % label)
            return False
        try:
            copier(text)
        except Exception:
            self.show_notice("Clipboard request failed; %s not copied" % label)
            return False
        self.show_notice("Copy requested: %s (terminals may block clipboard access)" % label)
        return True

    # ------------------------------------------------------------------ mouse

    def on_click(self, event):
        if not self.is_running or self.screen is not self.dashboard:
            return
        widget = getattr(event, "widget", None)
        identifier = getattr(widget, "id", None)
        if identifier == "gpus":
            self.copy_gpu_line(event.screen_offset.y - widget.content_region.y)
        elif identifier == "events":
            self.copy_event_line(event.screen_offset.y - widget.content_region.y + int(widget.scroll_offset.y))
        if identifier != "model-menu":
            self.dashboard.query_one("#command", Input).focus()

    def copy_gpu_line(self, index):
        gpus = (self.snapshot or {}).get("gpus", [])
        if 0 <= index < len(gpus):
            self.copy_text(self.gpu_text(gpus[index]), "gpu %s" % gpus[index]["index"])
        else:
            self.show_notice("No GPU observation on that line")

    def copy_event_line(self, index):
        lines = self._event_log.lines
        if 0 <= index < len(lines):
            self.copy_text(lines[index].text, "event line")
        else:
            self.show_notice("No event on that line")

    # --------------------------------------------------------------- events

    def notify_events(self):
        # Called by the SSE reader thread. post_message is thread-safe; coalesce
        # a burst into one wake-up, leaving bounded delivery to the reader.
        if not self._ui_closed and not self._event_notice.is_set():
            self._event_notice.set()
            self.post_message(EventsChanged())

    def on_events_changed(self, message):
        if self.is_running and not self._ui_closed:
            self._event_timer.resume()

    def render_event_status(self):
        # Connection, then the newest background read, then user feedback; the
        # hint is last because it is the only part safe to clip.
        parts = [self._connection, self._observation, self._notice, HINT]
        status = " · ".join(self.api.clean_text(part) for part in parts if part)
        if self._rendered.get("event-status") != status:
            self._event_status.update(status)
            self._rendered["event-status"] = status

    def update_events(self):
        # Textual marks the app stopped before pruning widgets, but closes the
        # App timers afterwards. A callback in that window must not drain/redraw.
        if not self.is_running:
            return
        if not self._event_status.is_attached or not self._event_log.is_attached:
            return
        if self._event_driven and self._progress is None:
            self._event_timer.pause()
        self._event_notice.clear()
        update = self.event_reader.drain()
        self.event_delivery = {key: value for key, value in update.items() if key != "events"}
        reset = update["generation"] != self.event_generation
        changed = reset or bool(update["events"])
        if update["generation"] != self.event_generation:
            self.event_generation = update["generation"]
            self.event_history.clear()
            self.event_presentation.reset()
            if self._progress is not None:
                self._progress["log_epoch"] = None
                self._progress["progress_sequence"] = 0
                self._progress["retired_epochs"] = set()
        status = update["status"]
        if update.get("missed"):
            status += " · %s events unavailable in server history" % update["missed"]
        if update["dropped"]:
            status += " · %s events dropped from delivery queue" % update["dropped"]
        self._connection = status
        self.render_event_status()
        if not changed:
            self.render_progress()
            return
        key = lambda item: (item["timestamp"], item["id"])
        existing_ids = {item["id"] for item in self.event_history}
        incoming = sorted((item for item in update["events"] if item["id"] not in existing_ids), key=key)
        reordered = bool(self.event_history and incoming and key(incoming[0]) < key(self.event_history[-1]))
        self.event_history = sorted(self.event_history + incoming, key=key)[-200:]
        for item in incoming:
            self.event_presentation.account(item)
        log = self._event_log
        if reset or reordered:
            # Reset and late timestamps are exceptional; preserve the established
            # ordering contract without rewriting normal in-order delivery.
            log.clear()
            self.event_presentation.reset_display()
            incoming = self.event_history
        for item in incoming[-200:]:
            self.observe_progress(item)
            line = self.event_presentation.line(item)
            if line is not None:
                log.write(line)
        self.update_static("source-status", self.event_presentation.status())
        self.render_progress()
        # Any scheduler observation is a reason to re-read state immediately;
        # the in-flight guard merges this with the ordinary poll.
        self.refresh_current()

    def event_export_text(self):
        header = ("Events: frozen copy of retained scheduler history (at most 200 records).\n"
                  "Data-plane observations originate from llama-swap, are not a daemon stop or resource release, "
                  "and are not trusted quiet proof. received_at is local; upstream loss unknown.\n"
                  "Local relay discards separate intentional filtering, invalid schema/framing, buffer overflow "
                  "and source bounds. Connection/inflight events and repeated errors/snapshots remain in raw records.\n")
        metadata = {"source_counters": self.event_presentation.counters(),
                    "delivery": self.event_delivery,
                    "connection_status": self._connection or "unknown",
                    "events": self.event_history}
        return header + "\n" + "\n".join(self.format_event(item).plain for item in self.event_history) + "\n\nRaw JSON:\n" + json.dumps(metadata, ensure_ascii=False, indent=2, allow_nan=False)

    def event_summary_text(self):
        projection = EventPresentation(self.api.clean_text, self.api.format_wake_progress)
        lines = ["Events summary · retained history (up to 200 records)",
                 "Data-plane observations are not daemon state or proof of released resources."]
        for item in self.event_history:
            line = projection.line(item)
            if line is not None:
                lines.append(line.plain)
        return "\n".join(lines) + "\n\n" + self.event_presentation.status()

    def action_event_details(self):
        if self.is_running and self.screen is self.dashboard:
            self.close_menu()
            self.push_screen(EventDetails(self.event_export_text(), self.event_summary_text()))

    def observe_progress(self, item):
        progress = self._progress
        if (not progress or progress["command"] != "wake" or item.get("model") != progress["target"]
                or item["id"] <= progress.get("after_id", 0) or item["timestamp"] < progress.get("since", 0)):
            return
        detail = item.get("detail")
        if not isinstance(detail, dict):
            return
        stage = None
        if item["kind"] == "wake_requested":
            stage = "cold-start request submitted" if detail.get("cold_start") is True else "wake requested"
        elif item["kind"] == "place":
            stage = "placement granted on GPU %s; awaiting unit confirmation" % detail.get("gpu", "?")
        elif item["kind"] == "lease_confirmed":
            stage = "unit/account confirmed; awaiting readiness"
        elif item["kind"] == "wake_progress":
            observed = self.api.parse_wake_progress(
                item, progress["target"], after_id=progress.get("after_id", 0),
                since=progress.get("since"))
            if observed is not None:
                tracker = {"log_epoch": progress.get("log_epoch"),
                           "sequence": progress.get("progress_sequence", 0),
                           "retired_epochs": progress.setdefault("retired_epochs", set())}
                if not self.api.accept_wake_progress(observed, tracker):
                    progress["stage"] = "observed: progress unavailable (source epoch replayed or changed)"
                    return
                progress["log_epoch"] = tracker["log_epoch"]
                progress["progress_sequence"] = tracker["sequence"]
                stage = "%s (source: llama-swap; advisory)" % observed["label"]
            elif "state" in detail or "swap_state" in detail:
                # Preserve the existing scheduler-state progress envelope.
                stage = "state %s, swap %s" % (detail.get("state") or "unknown", detail.get("swap_state") or "unknown")
        if stage is not None:
            # Existing events have no request ID. Label this as a fresh target
            # observation; only the HTTP result can complete our own operation.
            progress["stage"] = "observed: " + stage

    def format_event(self, item):
        kind = item["kind"]
        detail = item.get("detail", {})
        relayed = (kind.startswith("data_plane_") and isinstance(detail, dict)
                   and detail.get("source") == "llama-swap")
        color, note = "white", ""
        if relayed:
            color = "bright_red" if kind == "data_plane_error" else "blue"
            if kind == "data_plane_state":
                note = "data-plane state only; not a daemon stop or resource release"
            elif kind == "data_plane_dropped":
                color = "yellow"
                note = ("local relay discard counts; upstream loss unknown; "
                        "unlisted_model=intentional filtering; "
                        "invalid_event=payload/framing/schema; "
                        "buffer_full=local buffer capacity; limit_exceeded=local source bound")
        elif "error" in kind or "fail" in kind:
            color = "bright_red"
        elif kind in ("stop", "stopped"):
            color = "red"
        elif "evict" in kind:
            color = "magenta"
        elif "sleep" in kind:
            color = "yellow"
        elif "pin" in kind or "reserve" in kind:
            color = "cyan"
        elif "load" in kind or "wake" in kind:
            color = "green"
        try:
            timestamp = datetime.fromtimestamp(item["timestamp"], timezone.utc).strftime("%H:%M:%S")
        except (ValueError, OverflowError, OSError):
            timestamp = "?"
        payload = json.dumps(detail, ensure_ascii=False, sort_keys=True)
        if kind == "wake_progress" and isinstance(detail, dict) and detail.get("progress_source") == "per_model_log":
            observed = self.api.format_wake_progress(detail)
            note = "observed %s; advisory only" % observed
            payload = "{}"
        line = "%s [%s] #%s %s %s %s %s" % (
            timestamp, "llama-swap" if relayed else "scheduler", item["id"],
            kind, item.get("model") or "", note, payload)
        if len(line) > 2048:
            line = line[:2048] + " …"
        return Text(self.api.clean_text(line), style=color)

    # ---------------------------------------------------------------- commands

    def on_input_submitted(self, event):
        if (not self.is_running or self.screen is not self.dashboard
                or event.input.id != "command"):
            return
        value = event.value
        event.input.value = ""
        self.remember(value.strip())
        self.submit_command(value)

    def submit_command(self, text):
        """The single entry point for typed, recalled and menu-issued commands."""
        text = text.strip()
        if not text:
            return
        self.close_menu()
        if text.startswith("/"):
            self.run_slash(text)
            return
        try:
            args = self.api.build_parser(UIParser).parse_args(shlex.split(text))
            if args.url or args.config or args.timeout:
                raise CommandMessage("Connection settings are fixed for this session; restart llm to change them")
            if args.command == "usage":
                self.show_usage(args)
            elif args.command in ("models", "registry"):
                self.show_models(args)
            elif args.command in ("pin", "unpin", "free", "wake", "reserve", "unreserve",
                                  "add", "rm", "sleep", "stop", "preload"):
                if self._write_busy:
                    self.show_result("An operation is already running; wait for its result (no request queued)")
                elif args.command == "free" and args.ram and not args.dry_run:
                    self.confirm_ram(args, text)
                else:
                    self.run_write(args)
            else:
                self.usage_active = False
                self.usage_generation += 1
                self.dashboard.remove_class("usage")
                self.refresh_state(args)
        except (CommandMessage, ValueError) as exc:
            self.show_result(str(exc))

    def run_slash(self, text):
        """UI-layer actions; these are not model commands and have no parser."""
        parts = text.split()
        name, rest = parts[0], parts[1:]
        if name == "/help":
            self.action_help()
        elif name == "/quit":
            self.exit()
        elif name == "/refresh":
            self.refresh_current()
            self.show_notice("Refresh requested")
        elif name == "/clear":
            self._event_log.clear()
            self.event_presentation.reset_display()
            self.show_result("Cleared")
        elif name == "/events":
            self.action_event_details()
        elif name == "/usage":
            days = rest[0] if rest else "7"
            if days not in ("7", "30"):
                self.show_result("/usage accepts 7 or 30")
                return
            self.show_usage(self.api.build_parser(UIParser).parse_args(
                ["usage", "--days", days, "--by", self.usage_args.by]))
        elif name == "/copy":
            self.run_copy(rest)
        else:
            self.show_result("Unknown UI command %s · try %s" % (
                self.api.clean_text(name), " ".join(SLASH_COMMANDS)))

    def run_copy(self, rest):
        if rest and rest[0] == "events":
            self.copy_text(self.event_summary_text(), "event summary")
            return
        if rest and rest[0] == "gpu":
            gpus = (self.snapshot or {}).get("gpus", [])
            index = next((position for position, gpu in enumerate(gpus)
                          if len(rest) > 1 and str(gpu["index"]) == rest[1]), None)
            if index is None:
                self.show_result("/copy gpu N needs an observed GPU index")
            else:
                self.copy_gpu_line(index)
            return
        name = " ".join(rest) if rest else self.selected_model()
        line = None if name is None else self.model_status_line(name)
        if line is None:
            self.show_result("/copy needs a model in the current snapshot, gpu N, or events")
        else:
            self.copy_text(line, "status line")
