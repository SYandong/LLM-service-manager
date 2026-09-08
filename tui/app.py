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
from textual.screen import ModalScreen
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
                ("ctrl+r", "reset_events", "Reset events"), ("u", "usage", "Usage"),
                ("f", "prepare_free", "Free"), ("p", "prepare_pin", "Pin"),
                ("w", "prepare_wake", "Wake")]

    def __init__(self, client, api, event_reader=None, **kwargs):
        super().__init__(**kwargs)
        self.client = client
        self.api = api
        self.snapshot = None
        self.fetching = False
        self._write_busy = False
        self._shortcut_target = None
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

    @property
    def dashboard(self):
        # Textual 0.70 App.query_one searches only the active modal screen.
        # Background observations always belong to the original dashboard.
        return self.screen_stack[0]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="summary"):
            yield Static("Loading GPU observations…", id="gpus", markup=False)
            yield Static("Loading RAM observations…", id="memory", markup=False)
        with Horizontal(id="content"):
            yield DataTable(id="models", cursor_type="row")
            with Vertical(id="event-panel"):
                yield Static("Events via scheduler (last 200)", id="event-title", markup=False)
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
        command = Input(placeholder="status | free --gpu 0 | wake MODEL | pin MODEL --for 8h", id="command")
        # Newer Textual selects all on focus; typing a parameter must append at
        # the requested cursor instead of replacing the prefilled command.
        if hasattr(command, "select_on_focus"):
            command.select_on_focus = False
        yield command
        yield Footer()

    def on_mount(self):
        self.dashboard.query_one("#models", DataTable).focus()
        self._event_status = self.dashboard.query_one("#event-status", Static)
        self._event_log = self.dashboard.query_one("#events", RichLog)
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
        self.dashboard.set_class(event.size.width < 100, "narrow")
        if self.snapshot is not None:
            self.render_snapshot()
        if self.usage_active:
            self.render_usage()

    @work
    async def refresh_state(self, args=None):
        # A slow HTTP request must not start overlapping polls or freeze keyboard input.
        if not self.is_running or self.fetching or self._write_busy:
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
                if getattr(args, "model", None) in self.model_names:
                    self.dashboard.query_one("#models", DataTable).move_cursor(row=self.model_names.index(args.model))
                    self.update_details()
            except Exception as exc:
                message += "\nState refresh failed: " + str(exc)
            if self.is_running:
                self.show_result(message)
        except Exception as exc:
            if self.is_running:
                self.show_result("%s request failed: %s" % (args.command, exc))
        finally:
            self._write_busy = False

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

    def action_usage(self):
        if self.usage_active:
            self.show_status()
        else:
            self.show_usage(self.usage_args)

    def show_status(self):
        self.usage_active = False
        self.usage_generation += 1
        self.dashboard.remove_class("usage")
        self.dashboard.query_one("#models", DataTable).focus()
        self.refresh_state()

    def show_usage(self, args):
        self.usage_args = args
        self.usage_active = True
        self.usage_generation += 1
        self.usage_snapshot = None
        self.usage_error = "Loading usage…"
        self.dashboard.add_class("usage")
        self.render_usage()
        self.dashboard.query_one("#usage-7", Button).focus()
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
        if event.button.id == "usage-status":
            self.show_status()
        elif event.button.id in ("usage-7", "usage-30"):
            days = event.button.id.removeprefix("usage-")
            args = self.api.build_parser(UIParser).parse_args(["usage", "--days", days, "--by", self.usage_args.by])
            self.show_usage(args)

    def show_result(self, text):
        self.dashboard.query_one("#result", Static).update(self.api.clean_text(text))

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
        self.dashboard.query_one("#gpus", Static).update("\n".join(lines))
        memory = state.get("memory", {})
        self.dashboard.query_one("#memory", Static).update("RAM %s/%s GiB budget\nHost available %s GiB" % (
            self.api.number(memory.get("sleeping_weights_gb")), self.api.number(memory.get("budget_gb")),
            self.api.number(memory.get("host_available_gb"))))
        table = self.dashboard.query_one("#models", DataTable)
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
        row = self.dashboard.query_one("#models", DataTable).cursor_row
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
        self.dashboard.query_one("#details", Static).update(self.api.clean_text(text))

    def on_data_table_row_highlighted(self, event):
        if self.snapshot is not None and event.data_table.is_attached:
            self.update_details()

    def action_command(self):
        self.dashboard.query_one("#command", Input).focus()

    def shortcut_model_present(self, name):
        return self.snapshot is not None and name is not None and sum(
            item.get("name") == name for item in self.snapshot.get("models", [])) == 1

    def prepare_command(self, command):
        # Printable keys belong to focused inputs. A modal owns its interaction
        # until dismissed; no shortcut may edit the command hidden underneath it.
        if not self.is_running or self.screen is not self.dashboard or isinstance(self.focused, Input):
            return
        if self._write_busy:
            self.show_result("An operation is already running; wait for its result (no request queued)")
            return
        entry = self.dashboard.query_one("#command", Input)
        if entry.value.strip():
            entry.focus()
            self.show_result("Existing command draft retained; edit or clear it before choosing another shortcut")
            return
        name = self.selected_model() if command != "free" else None
        if command != "free":
            if not self.shortcut_model_present(name):
                self.show_result("No current model selection; refresh and select a model first")
                return
            try:
                self.api.model_name(name)
            except argparse.ArgumentTypeError as exc:
                self.show_result(str(exc))
                return
            self._shortcut_target = (command, name)
        else:
            self._shortcut_target = None
        if command == "pin":
            # Empty duration intentionally fails the shared parser on Enter.
            # -- ends options before every name, including leading-dash names.
            entry.value = "pin --for  -- " + shlex.quote(name)
            cursor = len("pin --for ")
            message = "Enter a positive pin duration at the cursor (for example 1h), then press Enter; nothing sent"
        else:
            entry.value = "free " if command == "free" else "wake -- " + shlex.quote(name)
            cursor = len(entry.value)
            message = "Review/edit the command, then press Enter; nothing sent"
        entry.focus()
        entry.cursor_position = cursor
        # 0.70 moves the cursor to the end when the queued Focus arrives.
        # Restore the parameter position after focus/layout, without overwriting
        # text typed meanwhile or touching widgets after shutdown.
        self.call_after_refresh(self.position_prefill_cursor, entry, entry.value, cursor)
        self.show_result(message)

    def position_prefill_cursor(self, entry, value, cursor):
        if self.is_running and entry.is_attached and entry.has_focus and entry.value == value:
            entry.cursor_position = cursor

    def action_prepare_free(self):
        self.prepare_command("free")

    def action_prepare_pin(self):
        self.prepare_command("pin")

    def action_prepare_wake(self):
        self.prepare_command("wake")

    def action_help(self):
        self.show_result("f prefill free · p prefill selected pin (duration required) · w prefill selected wake · Enter submits · free --ram confirms separately · free [--gpu N] [--need 80G] [--ram] · wake MODEL [--wait 930] · pin MODEL --for 8h · unpin MODEL · all operations accept --dry-run · status · usage --days 7|30 · u usage · / command · Ctrl+R reset events after known restart · q quit")

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
            log.write(self.format_event(item))

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
        line = "%s [%s] #%s %s %s %s %s" % (
            timestamp, "llama-swap" if relayed else "scheduler", item["id"],
            kind, item.get("model") or "", note, payload)
        if len(line) > 2048:
            line = line[:2048] + " …"
        return Text(self.api.clean_text(line), style=color)

    def on_input_submitted(self, event):
        try:
            args = self.api.build_parser(UIParser).parse_args(shlex.split(event.value))
            if args.url or args.config or args.timeout:
                raise CommandMessage("Connection settings are fixed for this session; restart llm to change them")
            if self._shortcut_target == (args.command, getattr(args, "model", None)):
                if not self.shortcut_model_present(args.model):
                    raise CommandMessage("Shortcut model is no longer in the snapshot; refresh and select it again")
            if args.command == "usage":
                self.show_usage(args)
            elif args.command in ("pin", "unpin", "free", "wake"):
                if self._write_busy:
                    self.show_result("An operation is already running; wait for its result (no request queued)")
                elif args.command == "free" and args.ram and not args.dry_run:
                    self.confirm_ram(args, event.value)
                else:
                    self.run_write(args)
            else:
                self.usage_active = False
                self.usage_generation += 1
                self.dashboard.remove_class("usage")
                self.refresh_state(args)
        except (CommandMessage, ValueError) as exc:
            self.show_result(str(exc))
        self._shortcut_target = None
        event.input.value = ""
