# Generated-By: Codex / gpt-6-astra
"""Scheduler dashboard; command semantics come from the standalone CLI."""

import argparse
import asyncio
from datetime import datetime, timezone
import json
import shlex

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Footer, Header, Input, RichLog, Static


class CommandMessage(Exception):
    """Parser output that belongs inside the UI, not on the terminal stream."""


class UIParser(argparse.ArgumentParser):
    def _print_message(self, message, file=None):
        self.output = getattr(self, "output", "") + (message or "")

    def exit(self, status=0, message=None):
        raise CommandMessage(getattr(self, "output", "") + (message or ""))

    def error(self, message):
        raise CommandMessage(message)


class SchedulerApp(App):
    TITLE = "LLM service status"
    CSS = """
    Screen { background: $surface; }
    #summary { height: auto; max-height: 12; }
    #gpus { width: 2fr; height: auto; padding: 0 1; }
    #memory { width: 1fr; height: auto; padding: 0 1; }
    Screen.narrow #summary { layout: vertical; }
    Screen.narrow #gpus, Screen.narrow #memory { width: 1fr; }
    #content { height: 1fr; min-height: 6; }
    #models { width: 3fr; height: 1fr; min-height: 3; }
    #event-panel { width: 2fr; height: 1fr; min-width: 20; }
    #event-title, #event-status { height: 1; }
    #events { height: 1fr; }
    Screen.narrow #content { layout: vertical; }
    Screen.narrow #models { width: 1fr; }
    Screen.narrow #event-panel { width: 1fr; height: 6; }
    #details-view, #result-view { height: 2; }
    #details, #result { height: auto; min-height: 2; padding: 0 1; }
    #result { color: $text-muted; }
    #usage-view { display: none; height: 1fr; }
    #usage-controls { height: 3; }
    #usage-controls Button { width: 1fr; min-width: 0; }
    #usage-scroll { height: 1fr; }
    #usage-text { height: auto; padding: 0 1; }
    Screen.usage #summary, Screen.usage #content, Screen.usage #details-view { display: none; }
    Screen.usage #usage-view { display: block; }
    #command { height: 3; }
    """
    BINDINGS = [("q", "quit", "Quit"), ("r", "refresh_state", "Refresh"),
                ("slash", "command", "Command"), ("question_mark", "help", "Help"),
                ("ctrl+r", "reset_events", "Reset events"), ("u", "usage", "Usage")]

    def __init__(self, client, api, event_reader=None, **kwargs):
        super().__init__(**kwargs)
        self.client = client
        self.api = api
        self.snapshot = None
        self.fetching = False
        self._pin_busy = False
        self._state_generation = 0
        self.model_names = []
        self.terminal_width = 100
        self.event_reader = event_reader if event_reader is not None else api.EventReader(client)
        self.event_history = []
        self.event_generation = 0
        self._ui_timers = []
        self._ui_closed = False
        self.usage_active = False
        self.usage_args = api.build_parser(UIParser).parse_args(["usage"])
        self.usage_snapshot = None
        self.usage_error = "Loading usage…"
        self.usage_generation = 0
        self.usage_fetching = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="summary"):
            yield Static("Loading GPU observations…", id="gpus", markup=False)
            yield Static("Loading RAM observations…", id="memory", markup=False)
        with Horizontal(id="content"):
            yield DataTable(id="models", cursor_type="row")
            with Vertical(id="event-panel"):
                yield Static("Scheduler events (last 200)", id="event-title", markup=False)
                yield RichLog(id="events", max_lines=500, wrap=True, markup=False, highlight=False)
                yield Static("SSE connecting", id="event-status", markup=False)
        with Vertical(id="usage-view"):
            with Horizontal(id="usage-controls"):
                yield Button("7 days", id="usage-7")
                yield Button("30 days", id="usage-30")
                yield Button("Status", id="usage-status")
            with VerticalScroll(id="usage-scroll"):
                yield Static("Loading usage…", id="usage-text", markup=False)
        with VerticalScroll(id="details-view"):
            yield Static("Select a model with ↑/↓", id="details", markup=False)
        with VerticalScroll(id="result-view"):
            yield Static("Status · refresh every 5 seconds", id="result", markup=False)
        yield Input(placeholder="status | pin MODEL --for 8h | usage --days 30", id="command")
        yield Footer()

    def on_mount(self):
        self.query_one("#models", DataTable).focus()
        self._event_status = self.query_one("#event-status", Static)
        self._event_log = self.query_one("#events", RichLog)
        self._ui_timers.append(self.set_interval(5, self.refresh_current))
        self.refresh_state()
        self.event_reader.start()
        self._ui_timers.append(self.set_interval(0.1, self.update_events))

    async def on_unmount(self):
        if self._ui_closed:
            return
        self._ui_closed = True
        for timer in self._ui_timers:
            timer.stop()
        await asyncio.to_thread(self.event_reader.close)

    def on_resize(self, event):
        self.terminal_width = event.size.width
        self.screen.set_class(event.size.width < 100, "narrow")
        if self.snapshot is not None:
            self.render_snapshot()
        if self.usage_active:
            self.render_usage()

    @work
    async def refresh_state(self, args=None):
        # A slow HTTP request must not start overlapping polls or freeze keyboard input.
        if not self.is_running or self.fetching or self._pin_busy:
            return
        self.fetching = True
        generation = self._state_generation
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
                errors = snapshot.get("errors", [])
                message = "Updated" + (" · " + "; ".join(errors) if errors else
                                       " · read-only" if snapshot.get("read_only") else "")
            if not self.usage_active:
                self.show_result(message)
        except Exception as exc:
            if self.is_running and not self.usage_active and generation == self._state_generation:
                self.show_result("Refresh failed; last snapshot retained: " + str(exc))
        finally:
            self.fetching = False

    def action_refresh_state(self):
        self.refresh_current()

    @work
    async def run_pin(self, args):
        if not self.is_running:
            return
        if self._pin_busy:
            self.show_result("A pin/unpin request is already running; wait for its result")
            return
        self._pin_busy = True
        self._state_generation += 1
        self.usage_active = False
        self.usage_generation += 1
        self.screen.remove_class("usage")
        self.show_result("%s request in progress…" % args.command.capitalize())
        try:
            result = await asyncio.to_thread(self.api.execute_command, args, self.client)
            if not self.is_running:
                return
            message = self.api.format_result(args, result, width=self.terminal_width)
            try:
                status_args = self.api.build_parser(UIParser).parse_args(["status"])
                snapshot = await asyncio.to_thread(self.api.execute_command, status_args, self.client)
                if not self.is_running:
                    return
                self.api.format_status(snapshot)
                self.snapshot = snapshot
                self.render_snapshot()
                if args.model in self.model_names:
                    self.query_one("#models", DataTable).move_cursor(row=self.model_names.index(args.model))
                    self.update_details()
            except Exception as exc:
                message += "\nState refresh failed: " + str(exc)
            if self.is_running:
                self.show_result(message)
        except Exception as exc:
            if self.is_running:
                self.show_result("%s request failed: %s" % (args.command, exc))
        finally:
            self._pin_busy = False

    def refresh_current(self):
        if self.usage_active:
            self.refresh_usage()
        else:
            self.refresh_state()

    def action_usage(self):
        if self.usage_active:
            self.show_status()
        else:
            self.show_usage(self.usage_args)

    def show_status(self):
        self.usage_active = False
        self.usage_generation += 1
        self.screen.remove_class("usage")
        self.query_one("#models", DataTable).focus()
        self.refresh_state()

    def show_usage(self, args):
        self.usage_args = args
        self.usage_active = True
        self.usage_generation += 1
        self.usage_snapshot = None
        self.usage_error = "Loading usage…"
        self.screen.add_class("usage")
        self.render_usage()
        self.query_one("#usage-7", Button).focus()
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
        self.query_one("#usage-text", Static).update(text)

    def on_button_pressed(self, event):
        if event.button.id == "usage-status":
            self.show_status()
        elif event.button.id in ("usage-7", "usage-30"):
            days = event.button.id.removeprefix("usage-")
            args = self.api.build_parser(UIParser).parse_args(["usage", "--days", days, "--by", self.usage_args.by])
            self.show_usage(args)

    def show_result(self, text):
        self.query_one("#result", Static).update(self.api.clean_text(text))

    def render_snapshot(self):
        state = self.snapshot
        lines = []
        for gpu in state.get("gpus", []):
            total = gpu.get("total_gb")
            managed, external = gpu.get("managed_gb"), gpu.get("external_gb")
            bar = "?" * 12
            free = gpu.get("free_gb")
            if total and managed is not None and external is not None and free is not None:
                own = max(0, min(12, round(12 * managed / total)))
                other = max(0, min(12 - own, round(12 * external / total)))
                available = max(0, min(12 - own - other, round(12 * free / total)))
                bar = "M" * own + "E" * other + "." * available + "?" * (12 - own - other - available)
            lines.append("GPU%s [%s] %s/%s GiB" % (
                gpu["index"], bar, self.api.number(gpu.get("used_gb")), self.api.number(total)))
        lines.append("M service · E external · . free · ? unknown")
        self.query_one("#gpus", Static).update("\n".join(lines))
        memory = state.get("memory", {})
        self.query_one("#memory", Static).update("RAM %s/%s GiB budget\nHost available %s GiB" % (
            self.api.number(memory.get("sleeping_weights_gb")), self.api.number(memory.get("budget_gb")),
            self.api.number(memory.get("host_available_gb"))))
        table = self.query_one("#models", DataTable)
        previous = self.selected_model()
        table.clear(columns=True)
        table_width = self.terminal_width if self.terminal_width < 100 else int(self.terminal_width * 0.6)
        narrow = table_width < 100
        columns = [("MODEL", max(8, min(22, table_width - (33 if narrow else 27)))), ("STATE", 8), ("GPU", 3), ("MEM", 6)]
        if narrow:
            columns.append(("PIN", 3))
        else:
            columns.extend([("USED", 6), ("10m", 4), ("FROM", 10), ("PIN", 16)])
        for label, width in columns:
            table.add_column(label, width=width)
        activity = {item["model"]: item for item in state.get("activity", [])}
        now = state.get("sampled_at")
        observed_at = self.api.time.time() if now is None else now
        pins = {item["model"]: item for item in state.get("pins", []) if item["until"] > observed_at}
        self.model_names = []
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
                row.extend([self.api.age(used), "?" if stats.get("requests_last_10m") is None else str(stats["requests_last_10m"]),
                            ",".join(stats.get("by", [])) or "-", self.api.expiry(pin["until"]) if pin else "-"])
            table.add_row(*(Text(self.api.clean_text(value)) for value in row), key=name)
            self.model_names.append(name)
        if previous in self.model_names:
            table.move_cursor(row=self.model_names.index(previous))
        self.update_details()

    def selected_model(self):
        row = self.query_one("#models", DataTable).cursor_row
        return self.model_names[row] if 0 <= row < len(self.model_names) else None

    def update_details(self):
        name = self.selected_model()
        text = "No model observations" if name is None else name
        if name:
            stats = next((item for item in self.snapshot.get("activity", []) if item["model"] == name), {})
            now = self.snapshot.get("sampled_at")
            observed_at = self.api.time.time() if now is None else now
            pin = next((item for item in self.snapshot.get("pins", [])
                        if item["model"] == name and item["until"] > observed_at), None)
            text += " · from " + (", ".join(stats.get("by", [])) or "-")
            if pin:
                text += " · pin %s (%s)" % (self.api.expiry(pin["until"]), pin["by"])
        self.query_one("#details", Static).update(self.api.clean_text(text))

    def on_data_table_row_highlighted(self, event):
        if self.snapshot is not None and event.data_table.is_attached:
            self.update_details()

    def action_command(self):
        self.query_one("#command", Input).focus()

    def action_help(self):
        self.show_result("pin MODEL --for 8h [--dry-run] · unpin MODEL [--dry-run] · status · usage --days 7|30 · u toggle usage · r refresh · / command · Ctrl+R reset events after a known restart · q quit")

    def action_reset_events(self):
        self.event_reader.reset_cursor()
        self.update_events()
        self.show_result("Event cursor reset locally; replaying available scheduler history")

    def update_events(self):
        # Textual marks the app stopped before pruning widgets, but closes the
        # App timers afterwards. A callback in that window must not drain/redraw.
        if not self.is_running:
            return
        if not self._event_status.is_attached or not self._event_log.is_attached:
            return
        update = self.event_reader.drain()
        changed = update["generation"] != self.event_generation or bool(update["events"])
        if update["generation"] != self.event_generation:
            self.event_generation = update["generation"]
            self.event_history.clear()
        status = update["status"]
        if update.get("missed"):
            status += " · %s events unavailable in server history" % update["missed"]
        if update["dropped"]:
            status += " · %s events dropped from delivery queue" % update["dropped"]
        self._event_status.update(self.api.clean_text(status))
        if not changed:
            return
        self.event_history.extend(update["events"])
        self.event_history.sort(key=lambda item: (item["timestamp"], item["id"]))
        self.event_history = self.event_history[-200:]
        log = self._event_log
        log.clear()
        for item in self.event_history:
            kind = item["kind"]
            color = "white"
            if "error" in kind or "fail" in kind:
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
            detail = json.dumps(item.get("detail", {}), ensure_ascii=False, sort_keys=True)
            line = "%s %s %s %s" % (timestamp, kind, item.get("model") or "", detail)
            if len(line) > 2048:
                line = line[:2048] + " …"
            log.write(Text(self.api.clean_text(line), style=color))

    def on_input_submitted(self, event):
        try:
            args = self.api.build_parser(UIParser).parse_args(shlex.split(event.value))
            if args.url or args.config or args.timeout:
                raise CommandMessage("Connection settings are fixed for this session; restart llm to change them")
            if args.command == "usage":
                self.show_usage(args)
            elif args.command in ("pin", "unpin"):
                self.run_pin(args)
            else:
                self.usage_active = False
                self.usage_generation += 1
                self.screen.remove_class("usage")
                self.refresh_state(args)
        except (CommandMessage, ValueError) as exc:
            self.show_result(str(exc))
        event.input.value = ""
