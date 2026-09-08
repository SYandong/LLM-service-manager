# Generated-By: Codex / gpt-6-astra
"""Read-only scheduler dashboard; command semantics come from the standalone CLI."""

import argparse
import asyncio
import json
import shlex

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header, Input, Static


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
    #models { height: 1fr; min-height: 3; }
    #details { height: 2; padding: 0 1; overflow-y: auto; }
    #result { height: 2; padding: 0 1; color: $text-muted; overflow-y: auto; }
    #command { height: 3; }
    """
    BINDINGS = [("q", "quit", "Quit"), ("r", "refresh_state", "Refresh"),
                ("slash", "command", "Command"), ("question_mark", "help", "Help")]

    def __init__(self, client, api, **kwargs):
        super().__init__(**kwargs)
        self.client = client
        self.api = api
        self.snapshot = None
        self.fetching = False
        self.model_names = []
        self.terminal_width = 100

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="summary"):
            yield Static("Loading GPU observations…", id="gpus", markup=False)
            yield Static("Loading RAM observations…", id="memory", markup=False)
        yield DataTable(id="models", cursor_type="row")
        yield Static("Select a model with ↑/↓", id="details", markup=False)
        yield Static("Read-only · refresh every 5 seconds", id="result", markup=False)
        yield Input(placeholder="status | status --json | --help", id="command")
        yield Footer()

    def on_mount(self):
        self.query_one("#models", DataTable).focus()
        self.set_interval(5, self.refresh_state)
        self.refresh_state()

    def on_resize(self, event):
        self.terminal_width = event.size.width
        self.screen.set_class(event.size.width < 100, "narrow")
        if self.snapshot is not None:
            self.render_snapshot()

    @work
    async def refresh_state(self, args=None):
        # A slow HTTP request must not start overlapping polls or freeze keyboard input.
        if self.fetching:
            return
        self.fetching = True
        try:
            args = args or self.api.build_parser(UIParser).parse_args(["status"])
            snapshot = await asyncio.to_thread(self.api.execute_command, args, self.client)
            # Validate the shared response before replacing the last good display.
            self.api.format_status(snapshot)
            self.snapshot = snapshot
            self.render_snapshot()
            if getattr(args, "json", False):
                message = json.dumps(snapshot, ensure_ascii=False)
            else:
                errors = snapshot.get("errors", [])
                message = "Updated · " + ("; ".join(errors) if errors else "read-only")
            self.show_result(message)
        except Exception as exc:
            self.show_result("Refresh failed; last snapshot retained: " + str(exc))
        finally:
            self.fetching = False

    def action_refresh_state(self):
        self.refresh_state()

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
        narrow = self.terminal_width < 100
        columns = [("MODEL", max(8, min(22, self.terminal_width - 27))), ("STATE", 8), ("GPU", 3), ("MEM", 6)]
        if not narrow:
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
            if not narrow:
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
        self.show_result("status [--json] · r refresh · / command · ↑/↓ select · q quit")

    def on_input_submitted(self, event):
        try:
            args = self.api.build_parser(UIParser).parse_args(shlex.split(event.value))
            if args.url or args.config or args.timeout:
                raise CommandMessage("Connection settings are fixed for this session; restart llm to change them")
            self.refresh_state(args)
        except (CommandMessage, ValueError) as exc:
            self.show_result(str(exc))
        event.input.value = ""
