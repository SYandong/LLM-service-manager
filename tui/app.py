# Generated-By: Codex / gpt-6-astra
# Generated-By: Codex / gpt-5.6-luna
# Generated-By: Claude Code / claude-fable-5-1
# Generated-By: OpenCode / deepseek-v4.1-flash
"""Scheduler dashboard; command semantics come from the standalone CLI."""

import argparse
import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import inspect
import json
import math
import os
import shlex
import threading
import time
from typing import Optional

from rich.console import Console
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, OptionList, RichLog, Static, TextArea
from textual.widgets.option_list import Option

from .event_view import EventDetails, EventPresentation


# Item menu order is fixed; the identifier of the first four is the CLI verb the
# entry submits through the ordinary command path.
MENU_ITEMS = (
    ("preload", "Load into memory"),
    ("wake", "Bring online"),
    ("sleep", "Sleep to memory"),
    ("stop", "Free from memory"),
    ("cancel-queue", "Cancel queued operations"),
    ("copy-name", "Copy name"),
    ("copy-row", "Copy status line"),
    ("copy-endpoint", "Copy model endpoint address"),
    ("insert", "Insert into command line"),
)

# UI-layer actions only. Model commands stay in cli/llm's parser.
SLASH_COMMANDS = ("/cancel", "/clear", "/copy", "/events", "/help", "/queue", "/quit", "/refresh", "/usage")

HINT = "Enter run · Tab complete · Ctrl+O menu · /help"

INTERRUPT_WINDOW = 2.0

# A bounded current-session FIFO. It only covers management writes typed here;
# the scheduler API remains the single source of truth for execution.
MAX_QUEUED_WRITES = 32

# Map an owned/observed transition to the explicit command it represents, so an
# operation started by another session also greys its action in this UI.
TRANSITION_ACTIONS = {
    "SSDtoMEM": "preload", "SSDtoGPU": "wake", "MEMtoGPU": "wake",
    "GPUtoMEM": "sleep", "GPUtoSSD": "stop", "MEMtoSSD": "stop",
}

# UI-only command intents shown in STATE before the backend observes anything.
# Only a known observed source state picks a target phase; an unknown source
# stays honest ("loading"/"queued") and never invents an SSD start.
INTENT_TARGETS = {
    "wake": {"stopped": "SSDtoGPU", "sleeping": "MEMtoGPU"},
    "preload": {"stopped": "SSDtoMEM"},
    "sleep": {"awake": "GPUtoMEM"},
    "stop": {"awake": "GPUtoSSD", "sleeping": "MEMtoSSD"},
}
OBSERVED_STATES = ("awake", "sleeping", "stopped")

# Distinguishes "not captured yet" from a captured no-op/unknown label (None).
UNSET = object()


def transition_action(label):
    return TRANSITION_ACTIONS.get(label) if isinstance(label, str) else None


@dataclass
class QueueEntry:
    """One not-yet-finished management write owned by this TUI session."""

    id: int
    args: object
    text: str
    command: str
    model: Optional[str]
    target: str
    # Active operations freeze their target phase at dispatch so a changing
    # observed state cannot silently drop or replace it mid-flight.
    transition: object = UNSET


class CommandMessage(Exception):
    """Parser output that belongs inside the UI, not on the terminal stream."""


class UIParser(argparse.ArgumentParser):
    def _print_message(self, message, file=None):
        self.output = getattr(self, "output", "") + (message or "")

    def exit(self, status=0, message=None):
        raise CommandMessage(getattr(self, "output", "") + (message or ""))

    def error(self, message):
        raise CommandMessage(message)


class CommandComposer(TextArea):
    """Multi-line command composer; UI keys are routed through the app first.

    Textual 0.70+ is supported: TextArea exists there and ``text`` is the
    document content.  ``placeholder`` is only passed when the running Textual
    accepts it.  A plain ``value`` alias keeps the single-string command path
    (and copied standalone usage) unchanged.
    """

    _HAS_PLACEHOLDER = "placeholder" in inspect.signature(TextArea.__init__).parameters

    def __init__(self, *args, placeholder=None, **kwargs):
        if placeholder is not None and self._HAS_PLACEHOLDER:
            kwargs["placeholder"] = placeholder
        super().__init__(*args, **kwargs)

    async def on_key(self, event):
        handler = getattr(self.app, "handle_command_key", None)
        if handler is None:
            return
        if handler(event.key, event.is_printable, event.character):
            event.stop()
            event.prevent_default()
            return
        if event.key == "enter":
            submit = getattr(self.app, "submit_composer", None)
            if submit is not None:
                event.stop()
                event.prevent_default()
                submit(self)
        elif event.key in ("shift+enter", "alt+enter", "ctrl+enter"):
            self.insert("\n")
            event.stop()
            event.prevent_default()
        elif event.key == "ctrl+d" and self.text:
            self.action_delete_right()
            event.stop()
            event.prevent_default()

    def on_mouse_down(self, event):
        closer = getattr(self.app, "close_menu_for_click", None)
        if closer is not None:
            closer(self)

    @property
    def value(self):
        return self.text

    @value.setter
    def value(self, text):
        self.load_text("" if text is None else str(text))
        self._cursor_to_end()

    def _cursor_to_end(self):
        lines = self.text.split("\n")
        self.move_cursor((len(lines) - 1, len(lines[-1])))
        self.cursor_blink = True

    async def action_submit(self):
        submit = getattr(self.app, "submit_composer", None)
        if submit is not None:
            submit(self)


class EventLog(RichLog, can_focus=False):
    """Event log that owns its clicks on every supported Textual."""

    def on_click(self, event):
        handler = getattr(self.app, "click_event_row", None)
        if handler is not None:
            handler(event, self)


class GpuRows(Static, can_focus=False):
    """GPU summary text that owns its clicks on every supported Textual."""

    def on_click(self, event):
        handler = getattr(self.app, "click_gpu_row", None)
        if handler is not None:
            handler(event, self)


class ModelTable(DataTable, can_focus=False):
    """A click selects a row and opens its menu without taking focus."""

    def on_click(self, event):
        # Runs before DataTable's own handler, which still moves its cursor after
        # this; the menu takes the clicked row from the click metadata directly.
        meta = event.style.meta
        row = meta.get("row")
        if isinstance(row, int) and row >= 0 and not meta.get("out_of_bounds", False):
            opener = getattr(self.app, "open_model_menu", None)
            if opener is not None:
                opener(row)
        else:
            # Blank space below/around the rows is an outside click: it closes
            # an open menu instead of leaving it stale.
            closer = getattr(self.app, "close_menu_for_blank_table", None)
            if closer is not None:
                closer()


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
    #summary { height: auto; max-height: 14; }
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
    Screen.narrow #event-panel { width: 1fr; height: 5; }
    #result-view { height: 2; }
    #result { height: auto; min-height: 1; padding: 0 1; color: $text-muted; }
    #usage-view { display: none; height: 1fr; }
    #usage-controls { height: 3; }
    #usage-controls Button { width: 1fr; min-width: 0; }
    #usage-scroll { height: 1fr; }
    #usage-text { height: auto; padding: 0 1; }
    Screen.usage #summary, Screen.usage #content { display: none; }
    Screen.usage #usage-view { display: block; }
    #command-row { height: auto; min-height: 3; max-height: 6; background: #202020;
                   border: tall #3a3a3a; padding: 0 1; align: left top; }
    #prompt { width: 2; height: 1; color: #ad8c63; }
    #command { width: 1fr; height: 3; border: none; padding: 0; background: #202020; }
    #model-menu { layer: overlay; display: none; width: 34; height: auto; max-height: 12;
                  background: #202020; border: tall #ad8c63; }
    """
    # Printable keys belong to the command line; only control chords bind here.
    # ctrl+c is bound so newer/older Textual defaults cannot quit before the
    # composer's own key handler clears the line.
    BINDINGS = [("ctrl+r", "reset_events", "Reset events"),
                ("ctrl+c", "interrupt", "Clear or exit")]

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
        # Bounded current-session FIFO for management writes typed here.
        self._queue = deque()
        self._queue_seq = 0
        self._queue_draining = False
        self._current_write = None
        # UI-only GPU view: preserve known indices across partial/empty probes.
        self._gpu_view = {}
        self._gpu_order = []
        self._gpu_lines = []
        self._gpu_render = []
        # The single CLI version source; the footer shows it even when long
        # connection/queue text clips.
        reader = getattr(api, "app_version", None)
        try:
            self.version = str(reader()) if callable(reader) else "unknown"
        except Exception:
            self.version = "unknown"

    def composer(self):
        return self.dashboard.query_one("#command", CommandComposer)

    def focus_composer(self):
        if not self.is_running:
            return
        try:
            self.composer().focus()
        except Exception:
            pass

    def write_target(self, args):
        target = getattr(args, "model", None)
        if target is None:
            target = "GPU %s" % args.gpu if getattr(args, "gpu", None) is not None else \
                "host RAM" if getattr(args, "ram", False) else "eligible models"
        return target

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

    def version_label(self):
        """Anchored footer prefix: clipped status text must never hide it."""
        return "v" + self.version

    def inference_url(self):
        """The configured shared OpenAI base URL, or None when unset."""
        value = getattr(self.client, "api_url", None)
        return value if isinstance(value, str) and value else None

    @property
    def dashboard(self):
        # Textual 0.70 App.query_one searches only the active modal screen.
        # Background observations always belong to the original dashboard.
        return self.screen_stack[0]

    def compose(self) -> ComposeResult:
        with Horizontal(id="summary"):
            yield GpuRows("Loading GPU observations…", id="gpus", markup=False)
            yield Static("Loading RAM observations…", id="memory", markup=False)
        with Horizontal(id="content"):
            yield ModelTable(id="models", cursor_type="row")
            with Vertical(id="event-panel"):
                with Horizontal(id="event-heading"):
                    yield Static("Events via scheduler", id="event-title", markup=False)
                    yield Button("Details", id="event-details")
                yield Static(self.event_presentation.status(), id="source-status", markup=False)
                yield EventLog(id="events", max_lines=500, min_width=1, wrap=True, markup=False, highlight=False)
        with Vertical(id="usage-view"):
            with Horizontal(id="usage-controls"):
                yield Button("1 day", id="usage-1")
                yield Button("7 days", id="usage-7")
                yield Button("30 days", id="usage-30")
                yield Button("By user", id="usage-user")
                yield Button("By model", id="usage-model")
                yield Button("By day", id="usage-day")
                yield Button("Status", id="usage-status")
            with VerticalScroll(id="usage-scroll"):
                yield Static("Loading usage…", id="usage-text", markup=False)
        with VerticalScroll(id="result-view"):
            yield Static("", id="result", markup=False)
        with Horizontal(id="command-row"):
            yield Static("› ", id="prompt", markup=False)
            command = CommandComposer(placeholder="status | wake MODEL | sleep MODEL | /help",
                                      id="command", soft_wrap=True)
            yield command
        yield OptionList(id="model-menu")
        yield Static(self.version_label() + " · " + self._connection + " · " + HINT,
                     id="event-status", markup=False)

    def on_mount(self):
        # Only the command line accepts focus; every panel is click-only.
        for widget in self.dashboard.query("RichLog, Button, VerticalScroll, OptionList"):
            widget.can_focus = False
        self.composer().focus()
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
        # Teardown discards local, not-yet-dispatched queue entries. It never
        # claims to cancel a remote operation that is already running.
        self._queue.clear()
        self._current_write = None
        self._queue_draining = False
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
        # A slow HTTP request must not start overlapping polls or freeze keyboard
        # input. A queued write must not suppress read-only refresh either: the
        # generation guard stops a pre-write read from overwriting its result.
        if not self.is_running or self.fetching:
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
        """Direct single write; the queue worker reuses the same serialized path."""
        if not self.is_running:
            return
        if self._write_busy:
            self.show_result("An operation is already running; wait for its result (no request queued)")
            return
        await self._perform_write(args)

    async def _perform_write(self, args):
        if not self.is_running:
            return
        self._write_busy = True
        self._state_generation += 1
        self.usage_active = False
        self.usage_generation += 1
        self.dashboard.remove_class("usage")
        target = self.write_target(args)
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
            except Exception as exc:
                message += "\nState refresh failed: " + str(exc)
            if self.is_running:
                self.show_result(message)
        except Exception as exc:
            if self.is_running:
                self.show_result("%s request failed: %s" % (args.command, str(exc)))
        finally:
            self._write_busy = False
            self._progress = None
            # Invalidate any read launched during this write: it shares the
            # generation of the pre-action snapshot and must never overwrite the
            # fresh post-action read that just finished.
            self._state_generation += 1
            # A pending SSE notification must still be drained after completion.

    # ------------------------------------------------------------------ queue

    def enqueue_write(self, args, text, model=None):
        """Register a management write synchronously, then drain in FIFO order.

        Enqueue happens on the UI thread before any worker is scheduled, so a
        burst of commands keeps its submission order.  Only not-yet-dispatched
        entries can be cancelled; the entry currently executing cannot.
        """
        if not self.is_running:
            return None
        if len(self._queue) >= MAX_QUEUED_WRITES:
            self.show_result("Queue full (%d pending); wait for an operation to finish" % MAX_QUEUED_WRITES)
            return None
        self._queue_seq += 1
        target = self.write_target(args)
        entry = QueueEntry(self._queue_seq, args, text, args.command,
                           getattr(args, "model", None), target)
        self._queue.append(entry)
        if self.snapshot is not None:
            self.render_snapshot()
        if not self._queue_draining:
            self._queue_draining = True
            self.drain_queue()
        return entry

    @work
    async def drain_queue(self):
        try:
            while self.is_running and self._queue:
                entry = self._queue.popleft()
                self._current_write = entry
                # The target phase is derived once, from the freshest observed
                # source at dispatch, and then held for the whole ownership
                # lifetime so later samples cannot drop it or leak a queued
                # fallback.  This never edits the snapshot.
                entry.transition = self.intent_label(entry, active=True)
                if self.snapshot is not None:
                    self.render_snapshot()
                try:
                    await self._perform_write(entry.args)
                except Exception as exc:
                    # One failed request never retries and never stops the queue.
                    if self.is_running:
                        self.show_result("%s request failed: %s" % (entry.command, exc))
                finally:
                    self._current_write = None
                if self.is_running and self.snapshot is not None:
                    self.render_snapshot()
        finally:
            self._current_write = None
            self._queue_draining = False
            # An entry appended in the flag-reset window is drained by this kick.
            if self.is_running and self._queue:
                self._queue_draining = True
                self.drain_queue()

    def queued_positions(self):
        positions = {}
        for position, entry in enumerate(self._queue, start=1):
            if entry.model and entry.model not in positions:
                positions[entry.model] = position
        return positions

    def queue_text(self):
        entries = list(self._queue)
        if not entries and self._current_write is None:
            return "Queue empty"
        parts = []
        if self._current_write is not None:
            parts.append("running id%d %s %s" % (self._current_write.id, self._current_write.command,
                                                 self.api.clean_text(self._current_write.target)))
        for position, entry in enumerate(entries, start=1):
            # The immutable id is what /cancel takes; the position is display only.
            parts.append("id%d (#%d) %s %s" % (entry.id, position, entry.command,
                                               self.api.clean_text(entry.target)))
        return "Queue: " + " · ".join(parts)

    def cancel_queue(self, ident):
        ident = (ident or "").strip()
        if not ident:
            self.show_result("Cancel needs a queue id (see /queue) or a model name")
            return False
        # Accept both the numeric id and the displayed "idN" token from /queue.
        numeric = ident
        if numeric[:2].lower() == "id" and numeric[2:].isdigit():
            numeric = numeric[2:]
        # An exact immutable queue id removes exactly that entry.
        if numeric.isdigit():
            entry = next((item for item in list(self._queue) if str(item.id) == numeric), None)
            if entry is not None:
                self._queue.remove(entry)
                self.show_notice("Cancelled queued id%s %s %s (not dispatched)" % (
                    entry.id, entry.command, self.api.clean_text(entry.target)))
                if self.snapshot is not None:
                    self.render_snapshot()
                return True
            current = self._current_write
            if current is not None and str(current.id) == numeric:
                self.show_result("Cannot cancel id%s %s: already dispatched; wait for its result" % (
                    current.id, self.api.clean_text(current.target)))
                return False
            self.show_result("No pending queue entry with id %s (positions are not ids; see /queue)" % numeric)
            return False
        # A model/target name cancels every pending entry for it.
        matches = [item for item in list(self._queue) if item.model == ident or item.target == ident]
        if matches:
            for item in matches:
                self._queue.remove(item)
            self.show_notice("Cancelled %d queued operation(s) for %s (not dispatched)" % (
                len(matches), self.api.clean_text(ident)))
            if self.snapshot is not None:
                self.render_snapshot()
            return True
        current = self._current_write
        if current is not None and (current.model == ident or current.target == ident):
            self.show_result("Cannot cancel %s: already dispatched; wait for its result" % self.api.clean_text(current.target))
        else:
            self.show_result("No pending queue entry for %s" % self.api.clean_text(ident))
        return False

    def cancel_queued_for_model(self, name):
        removed = [entry for entry in list(self._queue) if entry.model == name]
        if not removed:
            self.show_result("No queued operation to cancel for %s" % self.api.clean_text(name))
            return 0
        for entry in removed:
            try:
                self._queue.remove(entry)
            except ValueError:
                pass
        self.show_notice("Cancelled %d queued operation(s) for %s (not dispatched)" % (
            len(removed), self.api.clean_text(name)))
        if self.snapshot is not None:
            self.render_snapshot()
        return len(removed)

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
                self.enqueue_write(args, text)
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
        self.focus_composer()
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
        self.focus_composer()
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
        self.close_menu()
        if event.button.id == "event-details":
            self.action_event_details()
        elif event.button.id == "usage-status":
            self.show_status()
        elif event.button.id in ("usage-1", "usage-7", "usage-30"):
            days = event.button.id.removeprefix("usage-")
            args = self.api.build_parser(UIParser).parse_args(
                ["usage", "--days", days, "--by", self.usage_args.by])
            self.show_usage(args)
        elif event.button.id in ("usage-user", "usage-model", "usage-day"):
            by = event.button.id.removeprefix("usage-")
            args = self.api.build_parser(UIParser).parse_args(
                ["usage", "--days", str(self.usage_args.days), "--by", by])
            self.show_usage(args)
        self.focus_composer()

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

    @staticmethod
    def gpu_bar(used, total, width=10):
        """Same actual total-capacity denominator for used and llmsvc bars."""
        known = (lambda value: isinstance(value, (int, float)) and not isinstance(value, bool)
                 and math.isfinite(value) and value >= 0)
        if not (known(total) and total > 0 and known(used)):
            return None
        filled = int(round(min(1.0, max(0.0, used / total)) * width))
        return "█" * filled + "░" * (width - filled)

    def gpu_text(self, gpu):
        """Plain clipboard/export form; never used for a stale observation."""
        return "GPU%s used %s/%sG  llmsvc %sG  ext %s  free %s" % (
            gpu["index"], self.api.number(gpu.get("used_gb")), self.api.number(gpu.get("total_gb")),
            self.api.number(gpu.get("managed_gb")), self.api.number(gpu.get("external_gb")),
            self.api.number(gpu.get("free_gb")))

    @staticmethod
    def gpu_style(gpu):
        total, used = gpu.get("total_gb"), gpu.get("used_gb")
        if not (total is not None and total > 0 and used is not None):
            return "dim"
        percent = min(100, max(0, used / total * 100))
        return "#6f9f6f" if percent < 60 else "#ad8c63" if percent < 85 else "#c46a6a"

    def gpu_layout(self, ordered, compact):
        """Shared right-aligned widths so every row keeps the same columns.

        ``ordered`` is an iterable of ``(index, gpu, fresh)``.  Each numeric
        field gets its own maximum width so a wide value (a fraction or a
        three-digit total) cannot pad every other field and force a wrap.
        """
        def width(keys):
            values = [len(str(self.api.number(gpu.get(key))))
                      for _, gpu, fresh in ordered if fresh and gpu is not None
                      for key in keys]
            return max(values or [1])

        return {"index": max([len(str(index)) for index, _, _ in ordered] or [1]),
                "pair": width(("used_gb", "total_gb")),
                "managed": width(("managed_gb",)),
                "external": width(("external_gb",)),
                "free": width(("free_gb",))}

    def gpu_line(self, gpu, reserved, compact=False, layout=None):
        """One subdued labelled line: used bar and llmsvc bar over total GiB.

        Numeric fields are right-aligned to a shared per-render width so one-,
        two- and three-digit values (and fractions/unknown) never shift the bars
        or labels.
        """
        if layout is None:
            layout = self.gpu_layout([(gpu.get("index"), gpu, True)], compact)
        index = gpu.get("index")

        def num(key, width):
            return self.api.number(gpu.get(key)).rjust(width)

        bar_width = 6 if compact else 10
        used_label = "u" if compact else "used "
        managed_label = "l" if compact else "llmsvc "
        text = Text()
        text.append("GPU%s " % str(index).rjust(layout["index"]), style="bold #c8c8c8")
        text.append(used_label, style="#8a8a8a")
        bar = self.gpu_bar(gpu.get("used_gb"), gpu.get("total_gb"), bar_width)
        text.append(bar if bar is not None else "?" * bar_width,
                    style=self.gpu_style(gpu) if bar is not None else "dim")
        text.append(" %s/%sG " % (num("used_gb", layout["pair"]), num("total_gb", layout["pair"])),
                    style=self.gpu_style(gpu))
        text.append(managed_label, style="#8a8a8a")
        mbar = self.gpu_bar(gpu.get("managed_gb"), gpu.get("total_gb"), bar_width)
        text.append(mbar if mbar is not None else "?" * bar_width, style="#8a9fb0" if mbar is not None else "dim")
        text.append(" %sG " % num("managed_gb", layout["managed"]), style="#b0c0cc")
        if not compact:
            text.append("ext %s  free %s" % (num("external_gb", layout["external"]),
                                             num("free_gb", layout["free"])), style="#7a7a7a")
        if index in reserved:
            text.append(" · reserved for placement", style="cyan")
        return text

    @staticmethod
    def gpu_stale_line(index, compact=False, layout=None):
        index_text = str(index).rjust((layout or {}).get("index", 1))
        if compact:
            return Text("GPU%s unavailable (stale)" % index_text, style="dim #8a8a8a")
        return Text("GPU%s unavailable · stale observation (probe missing)" % index_text, style="dim #8a8a8a")

    def refresh_gpu_view(self, observed):
        """UI-only merge that preserves known indices across partial/empty probes.

        It never edits the scheduler snapshot.  A missing index keeps its slot
        but is marked unavailable and shows no old numbers as current; a fresh
        observation restores the values.
        """
        for gpu in observed:
            index = gpu.get("index")
            if index is None:
                continue
            if index not in self._gpu_view:
                self._gpu_order.append(index)
            self._gpu_view[index] = {"gpu": gpu, "fresh": True}
        # An empty/partial observation marks every known index unavailable; it
        # must not keep showing old numbers as if they were current.
        seen = {gpu.get("index") for gpu in observed}
        for index in self._gpu_order:
            if index not in seen:
                self._gpu_view[index]["fresh"] = False
        self._gpu_order = sorted(set(self._gpu_order))

    def render_gpus(self, state):
        reserved = {r["gpu"] for r in state.get("reserves", [])}
        observed = state.get("gpus", [])
        # A truly empty observation with probe errors must not erase known rows.
        self.refresh_gpu_view(observed)
        self._gpu_lines = []
        self._gpu_render = []
        lines = Text()
        compact = self.terminal_width < 80
        if not self._gpu_order:
            message = "GPU observations unavailable"
            lines.append(message, style="dim")
            self._gpu_render.append({"index": None, "gpu": None, "fresh": False,
                                     "legend": False, "rich": Text(message, style="dim")})
        elif compact:
            # A legend keeps the shorter u/l labels unambiguous at 40 columns.
            legend = "GPU · u=used l=llmsvc GiB (same total denominator)"
            lines.append(legend + "\n", style="#8a8a8a")
            self._gpu_render.append({"index": None, "gpu": None, "fresh": False,
                                     "legend": True, "rich": Text(legend, style="#8a8a8a")})
        for index in self._gpu_order:
            entry = self._gpu_view.get(index)
            gpu = entry.get("gpu") if entry else None
            fresh = bool(entry and entry.get("fresh"))
            self._gpu_lines.append({"index": index, "gpu": gpu, "fresh": fresh})
        # One shared layout keeps every row's bars/labels in the same columns.
        ordered = [(row["index"], row["gpu"], row["fresh"]) for row in self._gpu_lines]
        layout = self.gpu_layout(ordered, compact)
        for row in self._gpu_lines:
            if row["fresh"] and row["gpu"] is not None:
                rich = self.gpu_line(row["gpu"], reserved, compact, layout)
            else:
                rich = self.gpu_stale_line(row["index"], compact, layout)
            self._gpu_render.append({"index": row["index"], "gpu": row["gpu"],
                                     "fresh": row["fresh"], "legend": False, "rich": rich})
            lines.append_text(rich)
            lines.append("\n")
        lines.rstrip()
        self.update_static("gpus", lines)

    def gpu_visual_line(self, widget, visual_line):
        """Map a rendered visual line (Rich wrapping included) to a GPU entry."""
        width = max(1, int(widget.content_region.width))
        console = Console(width=width, no_color=True, legacy_windows=False, force_terminal=False)
        row = 0
        for item in self._gpu_render:
            span = max(1, len(item["rich"].wrap(console, width)))
            if visual_line < row + span:
                return item
            row += span
        return None

    def copy_gpu_row(self, visual_line, widget):
        item = self.gpu_visual_line(widget, visual_line)
        if item is None:
            self.show_notice("No GPU observation on that line")
            return
        if item.get("legend"):
            self.show_notice("GPU summary legend; no observation to copy")
            return
        if item.get("fresh") and item.get("gpu") is not None:
            gpu = item["gpu"]
            self.copy_text(self.gpu_text(gpu), "gpu %s" % gpu["index"])
        else:
            self.show_notice("No current GPU observation on that line (stale or unavailable)")

    def copy_gpu_index(self, gpu_index):
        """Copy by the stable GPU index, never by a snapshot list position."""
        item = next((entry for entry in self._gpu_render if entry.get("index") == gpu_index), None)
        if item is None or item.get("legend"):
            self.show_notice("No GPU observation with index %s" % self.api.clean_text(str(gpu_index)))
            return
        if item.get("fresh") and item.get("gpu") is not None:
            gpu = item["gpu"]
            self.copy_text(self.gpu_text(gpu), "gpu %s" % gpu["index"])
        else:
            self.show_notice("GPU%s is unavailable (stale); nothing current to copy" % gpu_index)

    def render_snapshot(self):
        state = self.snapshot
        self.render_gpus(state)
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
        queued = self.queued_positions()
        for name in list(self.model_names):
            if name not in models:
                table.remove_row(name)
                self._table_rows.pop(name, None)
                self.model_names.remove(name)
        for model in state.get("models", []):
            name = model["name"]
            stats, pin = activity.get(name, {}), pins.get(name)
            marker = " [q%d]" % queued[name] if name in queued else ""
            width = max(1, columns[0][1] - 2 - len(marker))
            base = self.api.fit(name, width).rstrip()
            label = base + (" *" if model.get("is_default") else "") + marker
            # The local command target outranks a delayed backend transition; the
            # stable observed state returns once the operation is cleaned up.
            # It is dimmed because it is intent, not a measured state.
            transition = self.local_transition(name) or model.get("transition")
            state_label = transition or model.get("state", "unknown")
            row = [label, state_label, "-" if model.get("gpu") is None else str(model["gpu"]),
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
                               style="dim" if (value in ("?", "?G", "-", "unknown")
                                               or (columns[index][0] == "STATE" and transition is not None)) else "")
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
        self.refresh_open_menu()

    def selected_model(self):
        row = self.dashboard.query_one("#models", DataTable).cursor_row
        return self.model_names[row] if 0 <= row < len(self.model_names) else None

    def model_status_line(self, name):
        cells = self._table_rows.get(name)
        return None if cells is None else "  ".join(cell.plain.strip() for cell in cells)

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
        entry = self.composer()
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
            if "\n" in entry.value:
                return False  # Multi-line content: let the composer move the cursor.
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
            return
        self.show_result("Completions: " + " ".join(self.api.clean_text(name) for name in candidates))

    def move_selection(self, delta):
        if not self.model_names:
            self.show_notice("No model rows to select")
            return
        table = self.dashboard.query_one("#models", DataTable)
        row = min(len(self.model_names) - 1, max(0, table.cursor_row + delta))
        table.move_cursor(row=row, animate=False)

    def action_interrupt(self):
        if self.is_running and self.screen is self.dashboard:
            self.interrupt(self.composer())

    def action_help(self):
        self.show_result(
            "Enter run · Tab complete · ↑/↓ history · Shift+↑/↓ or Shift+Tab select model · "
            "Ctrl+O item menu · Ctrl+L clear event log · Ctrl+R reset event cursor · "
            "Esc close menu or clear input · Ctrl+C clear then exit · Ctrl+D exit · "
            "click a row for its menu, a GPU line or an event line to copy it · "
            "menu Copy endpoint copies the configured api_url / LLM_API_URL · "
            "/help /quit /usage [1|7|30] [user|model|day] /events /clear /refresh /copy [MODEL|gpu N|events] "
            "/queue /cancel ID|MODEL · "
            "commands use the llm CLI: status · usage --days N [--by user|model|day] · wake MODEL · sleep MODEL · "
            "stop MODEL · preload MODEL · free [--gpu N] [--need 80G] [--ram] · pin MODEL --for 8h · "
            "unpin MODEL · reserve --gpu N --size 80G --for 4h · unreserve ID · models · registry · "
            "add PATH --name X --base BASE · rm NAME · all operations accept --dry-run")

    def action_reset_events(self):
        self.event_reader.reset_cursor()
        self.update_events()
        self.show_result("Event cursor reset locally; replaying available scheduler history")

    # -------------------------------------------------------------- item menu

    def loading_action(self, model, name):
        """The action currently loading for this model, whether owned here or by
        another session (observed through the backend transition)."""
        current = self._current_write
        if (current is not None and current.model == name
                and current.command in ("wake", "sleep", "stop", "preload")):
            return current.command
        return transition_action(model.get("transition"))

    def observed_state(self, name):
        """The last observed state for a model; None when it is unknown."""
        for model in (self.snapshot or {}).get("models", []):
            if model.get("name") == name:
                state = model.get("state")
                return state if isinstance(state, str) else None
        return None

    def intent_label(self, entry, active):
        """Target phase for one local command, or None for a no-op/dry-run.

        A known observed source picks SSDtoMEM / SSDtoGPU / MEMtoGPU / GPUtoMEM
        (stop: GPUtoSSD / MEMtoSSD).  An unknown source stays honest instead of
        inventing an SSD start.
        """
        table = INTENT_TARGETS.get(entry.command)
        if table is None or getattr(entry.args, "dry_run", False):
            return None
        state = self.observed_state(entry.model)
        if state in OBSERVED_STATES:
            return table.get(state)
        return "loading" if active else "queued"

    def local_transition(self, name):
        """Local active transition first, then the next queued intent for a model.

        An active operation owns the STATE cell for its whole lifetime: its
        target phase is captured at dispatch and returned even when a later
        sample would map to a different phase or to no phase.  A captured no-op
        or unknown label (None) therefore never falls through to a queued intent
        that is not running yet.  Display-only intent; the snapshot is untouched.
        """
        current = self._current_write
        if current is not None and current.model == name:
            if current.transition is UNSET:
                current.transition = self.intent_label(current, active=True)
            return current.transition
        for entry in self._queue:
            if entry.model == name:
                return self.intent_label(entry, active=False)
        return None

    def menu_disabled(self, model, name):
        """Actions greyed out for this model's current state, queue and loading."""
        state, default = model.get("state"), bool(model.get("is_default"))
        disabled = {"awake": {"wake", "preload"}, "sleeping": {"preload", "sleep"},
                    "stopped": {"sleep", "stop"}}.get(state, set()) | ({"stop"} if default else set())
        loading = self.loading_action(model, name)
        if loading is not None:
            # A conflicting immediate operation is not offered while one loads.
            disabled = disabled | {loading}
        if not any(entry.model == name for entry in self._queue):
            disabled = disabled | {"cancel-queue"}
        if self.inference_url() is None:
            # Visibly unavailable rather than a copy that silently does nothing.
            disabled = disabled | {"copy-endpoint"}
        return disabled

    def option_label(self, model, name, key, disabled=None, loading=None):
        """One menu label; an unavailable endpoint explains itself in place.

        The not-configured reason is visible on the disabled entry (and in
        ``/help``) so a user need not invoke an unselectable item to learn it.
        """
        if disabled is None:
            disabled = self.menu_disabled(model, name)
        if loading is None:
            loading = self.loading_action(model, name)
        label = dict(MENU_ITEMS)[key]
        if bool(model.get("is_default")) and key == "stop":
            label += " (default)"
        if key == "copy-endpoint" and self.inference_url() is None:
            label = "Copy endpoint (needs api_url)"
        if key == loading and key in disabled:
            label += " (running…)"
        return label

    def menu_options(self, model, name):
        """Build the ordered menu options, labelling the action that is loading."""
        disabled = self.menu_disabled(model, name)
        loading = self.loading_action(model, name)
        options = []
        for key, _ in MENU_ITEMS:
            options.append(Option(self.option_label(model, name, key, disabled, loading),
                                  id=key, disabled=key in disabled))
        return options

    def refresh_open_menu(self):
        """Keep an open item menu's disabled actions and labels live."""
        if self.menu_model is None:
            return
        name = self.menu_model
        model = next((item for item in (self.snapshot or {}).get("models", []) if item["name"] == name), None)
        if model is None:
            return
        disabled = self.menu_disabled(model, name)
        loading = self.loading_action(model, name)
        menu = self.dashboard.query_one("#model-menu", OptionList)
        changed = False
        for key, _ in MENU_ITEMS:
            option = menu.get_option(key)
            if option is None:
                continue
            text = self.option_label(model, name, key, disabled, loading)
            if str(option.prompt) != text:
                menu.replace_option_prompt(key, text)
                changed = True
            should = key in disabled
            if bool(option.disabled) != should:
                if should:
                    menu.disable_option(key)
                else:
                    menu.enable_option(key)
                changed = True
        if changed:
            options = [menu.get_option(key) for key, _ in MENU_ITEMS]
            if menu.highlighted is not None and 0 <= menu.highlighted < len(options):
                if options[menu.highlighted] is not None and options[menu.highlighted].disabled:
                    menu.highlighted = next(
                        (index for index, option in enumerate(options)
                         if option is not None and not option.disabled), 0)
            refresh = getattr(menu, "refresh", None)
            if callable(refresh):
                refresh()

    def open_model_menu(self, row=None):
        if not self.is_running or self.screen is not self.dashboard:
            return
        table = self.dashboard.query_one("#models", DataTable)
        if row is not None and 0 <= row < len(self.model_names):
            table.move_cursor(row=row, animate=False)
        name = self.selected_model()
        model = next((item for item in (self.snapshot or {}).get("models", []) if item["name"] == name), None)
        if model is None:
            self.show_result("No current model selection; refresh and select a model first")
            return
        menu = self.dashboard.query_one("#model-menu", OptionList)
        if self.menu_model == name and menu.display:
            # Clicking the selected row again toggles its menu closed.
            self.close_menu()
            self.show_notice("Menu closed; nothing sent")
            return
        disabled = self.menu_disabled(model, name)
        menu.clear_options()
        menu.add_options(self.menu_options(model, name))
        menu.display = True
        menu.highlighted = next((index for index, (key, _) in enumerate(MENU_ITEMS) if key not in disabled), 0)
        menu.styles.offset = self.menu_offset(table, self.model_names.index(name))
        self.menu_model = name
        self.focus_composer()

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
        self.focus_composer()
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
        elif choice == "cancel-queue":
            self.cancel_queued_for_model(name)
        elif choice == "copy-name":
            self.copy_text(name, "model name")
            self.append_to_command(name)
        elif choice == "copy-row":
            line = self.model_status_line(name)
            if line is None:
                self.show_notice("No status line for this model yet")
            else:
                self.copy_text(line, "status line")
        elif choice == "copy-endpoint":
            url = self.inference_url()
            if url is None:
                self.show_result(
                    "Inference endpoint not configured: set api_url in ~/.config/llm/config "
                    "or LLM_API_URL (http(s), no credentials) to enable copying it")
            else:
                # The shared OpenAI base URL the caller uses with this model name.
                self.copy_text(url, "model endpoint address")
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
        entry = self.composer()
        value = entry.value
        if value and not value.endswith(" "):
            value += " "
        entry.value = value + text
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

    def close_menu_for_click(self, widget):
        """Dismiss an open menu for a click outside it and the model table.

        Widgets that consume their own click events call this too, so blank,
        composer and panel clicks all close the menu.  A click on the model row
        that opened the menu is owned by the row handler and must not dismiss it
        here, and an option-list click is the menu acting on itself.
        """
        if not self.is_running or self.screen is not self.dashboard:
            return
        if self.menu_model is None:
            return
        if isinstance(widget, ModelTable):
            return
        if getattr(widget, "id", None) == "model-menu":
            return
        self.close_menu()

    def close_menu_for_blank_table(self):
        """A click on table space that is not a model row closes an open menu."""
        if not self.is_running or self.screen is not self.dashboard:
            return
        if self.menu_model is None:
            return
        self.close_menu()

    def on_click(self, event):
        if not self.is_running or self.screen is not self.dashboard:
            return
        widget = getattr(event, "widget", None)
        identifier = getattr(widget, "id", None)
        if identifier == "model-menu":
            return
        self.close_menu_for_click(widget)
        if isinstance(widget, ModelTable):
            self.focus_composer()
            return
        self.focus_composer()

    def click_gpu_row(self, event, widget):
        """Widget-level GPU click; App bubbling differs across Textual versions."""
        if not self.is_running or self.screen is not self.dashboard:
            return
        self.close_menu_for_click(widget)
        self.copy_gpu_row(event.screen_offset.y - widget.content_region.y, widget)
        self.focus_composer()

    def click_event_row(self, event, widget):
        """Widget-level event-log click; App bubbling differs across versions."""
        if not self.is_running or self.screen is not self.dashboard:
            return
        self.close_menu_for_click(widget)
        self.copy_event_line(event.screen_offset.y - widget.content_region.y + int(widget.scroll_offset.y))
        self.focus_composer()

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
        # Version first (never clipped), then connection, the newest background
        # read, user feedback and the queue; the hint is last because it is the
        # only part safe to clip.
        queue = self.queue_text() if (self._queue or self._current_write is not None) else None
        parts = [self.version_label(), self._connection, self._observation, self._notice, queue, HINT]
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

    def submit_composer(self, composer=None):
        """Enter on the composer; the only path that turns text into a command."""
        if not self.is_running or self.screen is not self.dashboard:
            return
        entry = composer if composer is not None else self.composer()
        value = entry.value
        entry.value = ""
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
                                  "sleep", "stop", "preload"):
                if args.command == "free" and args.ram and not args.dry_run:
                    # RAM reclamation is confirmed BEFORE anything is queued, so a
                    # cancelled confirmation leaves no queue entry behind.
                    self.confirm_ram(args, text)
                else:
                    self.enqueue_write(args, text)
            else:
                self.usage_active = False
                self.usage_generation += 1
                self.dashboard.remove_class("usage")
                self.refresh_state(args)
        except (CommandMessage, ValueError) as exc:
            self.show_result(str(exc))

    def run_slash(self, text):
        """UI-layer actions; these are not model commands and have no parser."""
        try:
            parts = shlex.split(text)
        except ValueError:
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
            days, by = "7", "user"
            for token in rest:
                if token in ("1", "7", "30"):
                    days = token
                elif token in ("user", "model", "day"):
                    by = token
                else:
                    by = None
                    break
            if by is None:
                self.show_result("/usage accepts 1|7|30 and user|model|day")
                return
            self.show_usage(self.api.build_parser(UIParser).parse_args(
                ["usage", "--days", days, "--by", by]))
        elif name == "/queue":
            self.show_result(self.queue_text())
        elif name == "/cancel":
            # Quoted model names with spaces survive shlex; an id is one token.
            self.cancel_queue(" ".join(rest) if rest else "")
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
            if len(rest) < 2 or not rest[1].isdigit():
                self.show_result("/copy gpu N needs an observed GPU index")
                return
            index = int(rest[1])
            if not any(entry.get("index") == index for entry in self._gpu_lines):
                self.show_result("/copy gpu N needs an observed GPU index")
                return
            self.copy_gpu_index(index)
            return
        name = " ".join(rest) if rest else self.selected_model()
        line = None if name is None else self.model_status_line(name)
        if line is None:
            self.show_result("/copy needs a model in the current snapshot, gpu N, or events")
        else:
            self.copy_text(line, "status line")
