# Generated-By: Codex / gpt-6.1-sol
# Generated-By: Codex / unknown model
"""Optional fleet dashboard using the standalone CLI's HTTP and event client."""

import asyncio
from datetime import datetime, timezone
import math
import sys
import threading
from types import SimpleNamespace
from urllib.parse import quote, urlencode

from rich.text import Text
from textual import work
from textual.app import App
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Sparkline, Static

from .fleet_format import api_label, count, duration, gib, status_label, total_tokens
from .fleet_gpu import (MEASURED_KEY, account_gpu, compact_gib, detail_lines, expanded_header,
                        fit, render_expanded, render_overview)
from .fleet_selection import SelectableStatic


STATUS_STYLE = {"active": "green", "idle": "", "over_limit": "bold yellow",
                "claimed": "cyan", "unknown": "dim"}
HINT = "↑↓ scroll  J/K service  S sort  M mine  C claim  U revoke  G GPU  ? help Q quit"
GPU_HINT = "↑↓ scroll  J/K GPU  ←→ owner  Enter details  Z compact  P people  ? help Q quit"


def numeric(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def amount(value):
    return "?" if not numeric(value) else ("%.1f" % value).rstrip("0").rstrip(".")


def timestamp(value):
    try:
        return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def activity_bar(values):
    """Keep unknown hours distinct from observed zero activity."""
    return "".join("·" if not numeric(value) else "▁▂▃▄▅▆▇█"[
        min(7, int(min(value, 60) * 7 / 60))] for value in values)


def owner_name(service):
    return service.get("container") or ("host" if service.get("host") else "unknown")


def total_memory(services):
    values = [service.get("gpu_gb") for service in services]
    return sum(values) if all(numeric(value) for value in values) else None


class RowLabel(Text):
    """A visible label carrying its stable row identity through table sorting."""
    def __init__(self, text, key, style=""):
        super().__init__(text, style=style)
        self.key = key


class FleetEventsChanged(Message):
    pass


class FleetHelpDialog(ModalScreen):
    DEFAULT_CSS = """
    FleetHelpDialog { align: center middle; }
    #fleet-help-dialog { width: 76; max-width: 96%; height: 90%;
        background: #181f28; border: round #8192a8; padding: 0 1; }
    #fleet-help-title { height: 1; text-style: bold; color: #eaf1f7; }
    #fleet-help-scroll { height: 1fr; }
    #fleet-help-text { height: auto; }
    #fleet-help-close { height: 3; width: 100%; }
    """
    BINDINGS = [("escape", "close", "Close"), ("q", "app.quit", "Quit"),
                Binding("up", "scroll_contents(-1)", show=False, priority=True),
                Binding("down", "scroll_contents(1)", show=False, priority=True),
                Binding("pageup", "scroll_contents(-1, True)", show=False, priority=True),
                Binding("pagedown", "scroll_contents(1, True)", show=False, priority=True)]

    def __init__(self, owner):
        super().__init__()
        self.owner = owner

    def compose(self):
        settings = (self.owner.snapshot or {}).get("config", {})
        active_window = settings.get("active_window_seconds")
        idle_limit = settings.get("idle_limit_hours")
        idle_help = ("Idle: no inference activity in the last %s.\n" % duration(active_window)
                     if numeric(active_window) else "Idle: no recent inference activity.\n")
        inactive_help = ("Running · inactive: still running, idle for at least %s.\n" %
                         duration(idle_limit * 3600) if numeric(idle_limit)
                         else "Running · inactive: still running, past the idle reminder.\n")
        with Vertical(id="fleet-help-dialog"):
            yield SelectableStatic("Fleet help", id="fleet-help-title")
            with VerticalScroll(id="fleet-help-scroll"):
                yield SelectableStatic(
                    "G  GPU panels     P  People / containers\n"
                    "Z  Expanded / compact GPU view\n"
                    "J / K  Select next / previous GPU or service\n"
                    "Up / Down, wheel / trackpad  Scroll contents\n"
                    "Page Up / Down  Scroll one page\n"
                    "Left / Right  Select owner; keep the chart in place\n"
                    "Enter  Details and history\n"
                    "Drag to select; copy with your terminal\n"
                    "Tab / Shift+Tab  Focus controls     Escape  Close     Q  Quit\n\n"
                    "/  Search     M  Mine     R  Refresh\n"
                    "S  Sort: State, Idle time, Memory, Output tokens\n"
                    "C  Claim service     U  Revoke claim\n\n"
                    "LLM: inference models, solid fill\n"
                    "Other: other GPU jobs, dotted fill\n"
                    "Unattributed: used VRAM with no matched workload\n"
                    "Free: available VRAM\n\n"
                    + idle_help + inactive_help +
                    "Local only means loopback access. Shared APIs have no idle reminder.\n"
                    "Activity and tokens cover the whole service, across all its GPUs.\n"
                    "Dots mean unknown hours; coverage is observed time.\n"
                    "Search and Mine filter services; bars show all allocations.\n"
                    "Refresh every 15s and on service changes.",
                    id="fleet-help-text", markup=False)
            yield Button("Close", id="fleet-help-close")

    def action_close(self):
        self.owner.clear_text_selections()
        self.dismiss()
        self.owner.focus_view()

    def on_button_pressed(self, event):
        event.stop()
        self.action_close()

    def action_scroll_contents(self, direction, page=False):
        self.owner.clear_text_selections()
        viewport = self.query_one("#fleet-help-scroll", VerticalScroll)
        viewport._scroll_to(y=viewport.scroll_y + direction * (viewport.size.height if page else 1),
                            animate=False)


class FleetGpuScroll(VerticalScroll):
    """Route wheel gestures from the GPU viewport's padding as well as its text."""

    def on_mouse_scroll_up(self, event):
        self.app.handle_gpu_wheel(event, -1)

    def on_mouse_scroll_down(self, event):
        self.app.handle_gpu_wheel(event, 1)

    def scroll_now(self, y):
        # Both supported Textual versions use this synchronous scroll primitive.
        self._scroll_to(y=y, animate=False)

    def watch_scroll_y(self, old_value, new_value):
        super().watch_scroll_y(old_value, new_value)
        if (self.is_attached and round(old_value) != round(new_value)
                and not self.app._gpu_scroll_pending):
            self.call_after_refresh(self.app.follow_gpu_scroll)


class GpuOverview(SelectableStatic):
    """Selection, heading styles and click targets share one displayed snapshot."""
    def __init__(self, *args, **kwargs):
        self.hits = []
        self.service_hits = {}
        self.anchors = {}
        self.heading_rows = {}
        self.heading_gpu = None
        self.bar_rows = 1
        self.marker_bright = True
        self._pending_view = None
        super().__init__(*args, **kwargs)

    def update_view(self, content, hits, service_hits, anchors, headings, selected, bar_rows):
        payload = (content.copy(), list(hits), dict(service_hits), dict(anchors),
                   dict(headings), selected, bar_rows)
        if self.dragging or self.has_selection:
            self._pending_view = payload
            return
        text, self.hits, self.service_hits, self.anchors, self.heading_rows, self.heading_gpu, self.bar_rows = payload
        if self._source_text != text:
            self.update(text)
        else:
            self.refresh()
        if self.is_attached:
            self.app._gpu_anchors = self.anchors

    def clear_selection(self, *, apply_pending=True):
        payload = self._pending_view if apply_pending else None
        if apply_pending:
            self._pending_view = None
        super().clear_selection(apply_pending=apply_pending)
        if payload is not None and not self._selection_closed:
            self.update_view(*payload)

    def render(self):
        text = super().render()
        heading = self.heading_rows.get(self.heading_gpu)
        if heading is not None:
            row, count_rows = heading
            lines = text.plain.splitlines(keepends=True)
            start = sum(map(len, lines[:row]))
            end = sum(map(len, lines[:row + count_rows]))
            text.stylize("bold #f2f8ff on #325573", start, end)
            text.stylize("bold #ffffff on " + ("#67c8ef" if self.marker_bright else "#3a7995"),
                         start, min(end, start + 2))
        return text

    def pulse_marker(self):
        self.marker_bright = not self.marker_bright
        self.refresh()

    def on_mouse_scroll_up(self, event):
        self.app.handle_gpu_wheel(event, -1)

    def on_mouse_scroll_down(self, event):
        self.app.handle_gpu_wheel(event, 1)

    def on_click(self, event):
        if self.consume_selection_click(event):
            return
        if self.app.view != "gpu":
            return
        x = event.screen_offset.x - self.content_region.x
        y = event.screen_offset.y - self.content_region.y
        for row, left, right, index, key in self.hits:
            if row == y and left <= x < right:
                event.stop()
                event.prevent_default()
                self.app.select_gpu(index, key, self.service_hits.get(row))
                self.focus(scroll_visible=False)
                if key is not None:
                    self.app.open_gpu_details()
                break


class GpuDetailDialog(ModalScreen):
    DEFAULT_CSS = """
    GpuDetailDialog { align: center middle; }
    #gpu-detail-dialog { width: 94; max-width: 96%; height: 94%;
        background: #181f28; border: round #8192a8; padding: 0 1; }
    #gpu-allocation-scroll { height: 1fr; }
    #gpu-allocation-text { height: auto; }
    #gpu-detail-services { height: 6; min-height: 3; }
    #gpu-service-details { height: auto; }
    #gpu-service-history, #gpu-service-history-bars { height: auto; }
    #gpu-active-chart, #gpu-token-chart { height: 1; }
    #gpu-detail-buttons { height: 3; }
    #gpu-detail-buttons Button { width: 1fr; min-width: 0; }
    """
    BINDINGS = [("escape", "close", "Close"), ("q", "app.quit", "Quit"),
                Binding("up", "scroll_contents(-1)", show=False, priority=True),
                Binding("down", "scroll_contents(1)", show=False, priority=True),
                Binding("pageup", "scroll_contents(-1, True)", show=False, priority=True),
                Binding("pagedown", "scroll_contents(1, True)", show=False, priority=True)]

    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self._rows = None

    def compose(self):
        with Vertical(id="gpu-detail-dialog"):
            with VerticalScroll(id="gpu-allocation-scroll"):
                yield SelectableStatic("", id="gpu-allocation-text", markup=False)
                yield DataTable(id="gpu-detail-services", cursor_type="row", cell_padding=1)
                yield SelectableStatic("", id="gpu-service-details", markup=False)
                yield SelectableStatic("", id="gpu-service-history", markup=False)
                yield SelectableStatic("", id="gpu-service-history-bars", markup=False)
                yield Sparkline([], id="gpu-active-chart")
                yield Sparkline([], id="gpu-token-chart")
            with Horizontal(id="gpu-detail-buttons"):
                yield Button("Claim", id="gpu-detail-claim")
                yield Button("Revoke", id="gpu-detail-revoke")
                yield Button("Close", id="gpu-detail-close")

    def on_mount(self):
        self.owner.gpu_detail = self
        table = self.query_one("#gpu-detail-services", DataTable)
        table.add_columns("OWNER / SERVICE", "GiB", "STATE")
        self.refresh_contents()
        table.focus()

    def on_unmount(self):
        if self.owner.gpu_detail is self:
            self.owner.gpu_detail = None

    def action_close(self):
        self.owner.clear_text_selections()
        self.dismiss()
        self.owner.focus_view()

    def refresh_contents(self):
        if not self.is_attached:
            return
        account = self.owner.selected_gpu_account()
        self.query_one("#gpu-allocation-text", Static).update(
            detail_lines(account, self.owner.selected_segment, self.owner.clean)
            if account else "GPU no longer observed")
        services = self.owner.gpu_services()
        amounts = {}
        if account:
            for allocation in account.allocations:
                for ident, value in allocation.members:
                    if ident is not None:
                        amounts.setdefault(ident, []).append(value)
        rows = [(service["id"], self.owner.clean(owner_name(service)) + " · " +
                 self.owner.clean(service.get("model") or "unknown"),
                 gib(sum(amounts[service["id"]])) if all(numeric(value) for value in
                     amounts.get(service["id"], [None])) else "?",
                 status_label(self.owner.service_status(service))) for service in services]
        table = self.query_one("#gpu-detail-services", DataTable)
        if rows != self._rows:
            selected = self.owner.selected_service_id()
            # Rebuilding rows must not masquerade as deliberate service navigation.
            with table.prevent(DataTable.RowHighlighted):
                table.clear()
                for ident, label, memory, status in rows:
                    table.add_row(label, memory, status, key=ident)
                self._rows = rows
                position = next((index for index, row in enumerate(rows) if row[0] == selected), 0)
                table.move_cursor(row=position, animate=False)
        self.sync_history()

    def sync_history(self):
        for name, source in (("gpu-service-details", "fleet-detail-text"),
                             ("gpu-service-history", "fleet-history-text"),
                             ("gpu-service-history-bars", "fleet-history-bars")):
            self.query_one("#" + name, Static).update(self.owner._rendered.get(source, ""))
        for name, source in (("gpu-active-chart", "fleet-active-chart"),
                             ("gpu-token-chart", "fleet-token-chart")):
            original = self.owner.dashboard.query_one("#" + source, Sparkline)
            chart = self.query_one("#" + name, Sparkline)
            chart.display = original.display
            if list(chart.data or []) != list(original.data or []):
                chart.data = list(original.data or [])
        service = self.owner.selected_service()
        allowed = bool(service and self.owner.claim_allowed(service["id"], service.get("container")))
        self.query_one("#gpu-detail-claim", Button).disabled = not allowed
        self.query_one("#gpu-detail-revoke", Button).disabled = not allowed or not (service.get("claim") or {}).get("id")

    def on_data_table_row_highlighted(self, event):
        if event.data_table.id == "gpu-detail-services":
            event.stop()
            selected = event.row_key.value
            if selected != self.owner._gpu_detail_service:
                self.owner.clear_text_selections()
            self.owner._gpu_detail_service = selected
            self.owner.render_details()
            self.sync_history()

    def on_button_pressed(self, event):
        event.stop()
        if event.button.id == "gpu-detail-close":
            self.action_close()
        else:
            self.owner.clear_text_selections()
            self.owner.action_claim(revoke=event.button.id == "gpu-detail-revoke")

    def on_key(self, event):
        key = event.key.lower()
        if key in ("j", "k"):
            event.stop()
            event.prevent_default()
            self.owner.clear_text_selections()
            table = self.query_one("#gpu-detail-services", DataTable)
            if table.row_count:
                table.move_cursor(row=max(0, min(table.row_count - 1,
                                  table.cursor_row + (1 if key == "j" else -1))), animate=False)
        elif key in ("c", "u"):
            event.stop()
            event.prevent_default()
            self.owner.clear_text_selections()
            self.owner.action_claim(revoke=key == "u")

    def action_scroll_contents(self, direction, page=False):
        self.owner.clear_text_selections()
        viewport = self.query_one("#gpu-allocation-scroll", VerticalScroll)
        viewport._scroll_to(y=viewport.scroll_y + direction * (viewport.size.height if page else 1),
                            animate=False)


class ClaimDialog(ModalScreen):
    DEFAULT_CSS = """
    ClaimDialog { align: center middle; }
    #claim-dialog { width: 76; max-width: 96%; height: auto; max-height: 96%;
                    background: #232323; border: round #ad8c63; padding: 0 1; }
    #claim-title, #claim-until-label, #claim-reason-label { height: auto; }
    #claim-status { height: auto; min-height: 3; max-height: 8; }
    #claim-buttons { height: 3; }
    #claim-buttons Button { width: 1fr; min-width: 0; }
    """
    BINDINGS = [("escape", "close", "Close"), ("q", "app.quit", "Quit")]

    def __init__(self, owner, service, revoke=False):
        super().__init__()
        self.owner = owner
        self.service_id = service["id"]
        self.container = service.get("container")
        self.model = service.get("model") or "unknown"
        self.revoke = revoke
        self.claim = dict(service.get("claim") or {})
        self.busy = False
        self.submitted = False
        self.preview_payload = None

    def compose(self):
        with Vertical(id="claim-dialog"):
            yield SelectableStatic(("Revoke claim" if self.revoke else "Claim service") + " · " +
                         self.owner.clean(self.model), id="claim-title", markup=False)
            if not self.revoke:
                yield SelectableStatic("Until (+3d or YYYY-MM-DDTHH:MM):", id="claim-until-label")
                yield Input(value="+1d", id="claim-until")
                yield SelectableStatic("Reason (1–200 characters):", id="claim-reason-label")
                yield Input(placeholder="Why this service is needed", max_length=200,
                            id="claim-reason")
            else:
                yield SelectableStatic("Until %s · %s" % (timestamp(self.claim.get("until")),
                             self.owner.clean(self.claim.get("reason", ""))), markup=False)
            yield SelectableStatic("Preview first. A preview does not save or revoke a claim.",
                         id="claim-status", markup=False)
            with Horizontal(id="claim-buttons"):
                yield Button("Preview", id="claim-preview")
                yield Button("Revoke" if self.revoke else "Submit", id="claim-submit",
                             disabled=True, variant="warning" if self.revoke else "primary")
                yield Button("Close", id="claim-close")

    def say(self, message):
        if self.owner.alive() and self.is_attached:
            self.query_one("#claim-status", Static).update(self.owner.clean(message))

    def on_input_changed(self, event):
        self.preview_payload = None
        if self.is_mounted:
            self.query_one("#claim-submit", Button).disabled = True

    def action_close(self):
        if self.busy:
            self.say("Request pending; wait for its result.")
        else:
            self.owner.clear_text_selections()
            self.dismiss()

    def on_button_pressed(self, event):
        event.stop()
        if event.button.id == "claim-close":
            self.action_close()
            return
        if self.busy or self.submitted:
            return
        preview = event.button.id == "claim-preview"
        if not preview and self.preview_payload is None:
            return
        if not self.owner.claim_allowed(self.service_id, self.container, self.revoke,
                                        self.claim.get("id")):
            self.say("Refresh and select your current service before making a claim.")
            self.query_one("#claim-submit", Button).disabled = True
            return
        try:
            if self.revoke:
                payload = None
            elif preview:
                reason = self.query_one("#claim-reason", Input).value.strip()
                if not 1 <= len(reason) <= 200:
                    raise ValueError("Enter a reason of 1–200 characters.")
                payload = {"service_id": self.service_id, "reason": reason,
                           "until": self.owner.api.claim_until(
                               self.query_one("#claim-until", Input).value.strip())}
            else:
                payload = dict(self.preview_payload)
        except Exception as exc:
            self.say(str(exc))
            return
        # Set synchronously so two key presses cannot dispatch two writes.
        self.busy = True
        self.submitted = not preview
        for button in self.query(Button):
            button.disabled = True
        for field in self.query(Input):
            field.disabled = True
        self.say("Checking preview…" if preview else "Request submitted; waiting for its result…")
        self.send_request(preview, payload)

    @work
    async def send_request(self, preview, payload):
        method = "DELETE" if self.revoke else "POST"
        path = ("/v1/fleet/claims/" + quote(str(self.claim["id"]), safe="")
                if self.revoke else "/v1/fleet/claims")
        if preview:
            path += "?dry_run=1"
        success = False
        try:
            result = await asyncio.to_thread(self.owner.client.request, method, path, payload)
            if not self.owner.alive():
                return
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise ValueError("The service returned an unrecognized claim result.")
            if result.get("dry_run", False) is not preview:
                raise ValueError("The service did not confirm the requested preview or submission.")
            claim = result.get("claim")
            if not isinstance(claim, dict) or claim.get("instance_id") != self.service_id:
                raise ValueError("The claim result does not identify this service.")
            if claim.get("service_id", self.service_id) != self.service_id:
                raise ValueError("The claim result identifies a different service.")
            if (not numeric(claim.get("until")) or claim["until"] <= 0
                    or timestamp(claim["until"]) == "?"):
                raise ValueError("The claim result does not provide a valid deadline.")
            if self.revoke:
                if (not isinstance(claim.get("id"), str) or not claim["id"]
                        or claim["id"] != self.claim.get("id")
                        or not numeric(claim.get("revoked_at")) or claim["revoked_at"] <= 0
                        or timestamp(claim["revoked_at"]) == "?"):
                    raise ValueError("The service did not confirm this claim's revocation.")
            elif (claim.get("until") != payload["until"] or claim.get("reason") != payload["reason"]
                  or (not preview and (not isinstance(claim.get("id"), str) or not claim["id"]))
                  or (preview and "id" in claim)):
                raise ValueError("The service did not confirm the submitted claim.")
            if preview:
                self.preview_payload = {} if self.revoke else dict(payload)
                message = ("Preview: revoke this claim. Select Revoke to confirm." if self.revoke else
                           "Preview: until %s\nReason: %s\nSelect Submit to save this claim." %
                           (timestamp(payload["until"]), payload["reason"]))
            else:
                success = True
                message = "Claim revoked." if self.revoke else "Claim saved."
                self.owner.notice(message)
                self.owner.refresh_fleet()
        except Exception as exc:
            status = getattr(exc, "status", None)
            if status == 403:
                message = "403: Only your own container's services can be claimed or revoked."
            elif preview:
                message = "Preview failed; no claim submitted: " + str(exc)
            elif status is not None and 400 <= status < 500:
                message = "Request rejected: " + str(exc)
            else:
                self.owner.uncertain_services.add(self.service_id)
                message = "Result unknown. Read the current claim state before further action; this request will not be retried. " + str(exc)
            if not preview:
                self.owner.notice(message)
                self.owner.refresh_fleet()
        finally:
            self.busy = False
            if self.owner.alive() and self.is_attached:
                self.say(message)
                self.query_one("#claim-close", Button).disabled = False
                self.query_one("#claim-preview", Button).disabled = self.submitted
                self.query_one("#claim-submit", Button).disabled = self.submitted or self.preview_payload is None
                for field in self.query(Input):
                    field.disabled = self.submitted
                if success:
                    self.query_one("#claim-close", Button).focus()


class FleetApp(App):
    """GPU/People views with bounded reads and explicit self-service claims."""
    CSS = """
    Screen { background: #181818; color: #d4d4d4; }
    #fleet-title { height: 1; color: #eaf1f7; text-style: bold; }
    #fleet-banner { height: auto; max-height: 2; color: yellow; }
    #fleet-gpu-scroll { height: auto; max-height: 8; scrollbar-size-vertical: 1; }
    #fleet-gpus { height: auto; }
    #fleet-controls { height: 1; color: #b0b0b0; }
    #fleet-filter { height: 3; display: none; }
    #fleet-table { height: 1fr; min-height: 3; background: #181818; }
    #fleet-details { height: 8; border-top: solid #4c4439; }
    .narrow #fleet-details { height: 6; }
    #fleet-detail-text, #fleet-history-text, #fleet-history-bars { height: auto; }
    #fleet-active-chart, #fleet-token-chart { height: 1; }
    #fleet-notice { height: auto; min-height: 1; max-height: 3; color: #d8ae7b; }
    #fleet-footer { height: 1; color: #aaa; }
    .gpu #fleet-gpu-scroll { height: 1fr; max-height: 100%; }
    .gpu.compact #fleet-gpu-scroll { overflow-y: hidden; }
    .gpu #fleet-table, .gpu #fleet-details { display: none; }
    .gpu #fleet-banner, .gpu #fleet-notice { max-height: 1; }
    DataTable > .datatable--header { background: #2b2823; color: #d8ae7b; }
    """
    BINDINGS = [Binding("ctrl+c", "copy_selection", "Copy", show=False, priority=True),
                Binding("q", "quit", "Quit", show=False)]

    def __init__(self, client, api, event_reader=None, **kwargs):
        if sys.platform != "win32" and "driver_class" not in kwargs:
            from .fleet_terminal import FleetTerminalDriver
            kwargs["driver_class"] = FleetTerminalDriver
        super().__init__(**kwargs)
        self.client, self.api = client, api
        self.event_reader = event_reader if event_reader is not None else api.EventReader(client)
        self.refresh_seconds = 15
        self.snapshot = None
        self.fetching = False
        self._refresh_pending = False
        self.read_error = None
        self.view = "gpu"
        self.compact_gpus = False
        self._gpu_anchors = {}
        self._gpu_scroll_pending = True
        self._selection_widget = None
        self.selected_gpu = None
        self.selected_segment = None
        self._gpu_detail_service = None
        self.gpu_accounts = []
        self.gpu_detail = None
        self.sort_mode = "priority"
        self.filter_text = ""
        self.mine_only = False
        self.uncertain_services = set()
        self._rendered = {}
        self._rows = {}
        self.row_keys = []
        self.row_services = {}
        self._columns = None
        self.history = None
        self.history_error = None
        self._history_generation = 0
        self._history_signature = None
        self._history_timer = None
        self._ui_closed = False
        self._ui_timers = []
        self._event_notice = threading.Event()
        self._event_driven = hasattr(self.event_reader, "set_notify")
        self.event_generation = 0
        self.event_cursor = 0
        self.connection = "Live changes connecting"

    def run(self, *, mouse=False, **kwargs):
        return super().run(mouse=mouse, **kwargs)

    async def run_async(self, *, mouse=False, **kwargs):
        return await super().run_async(mouse=mouse, **kwargs)

    @property
    def dashboard(self):
        return self.screen_stack[0]

    def alive(self):
        return self.is_running and not self._ui_closed

    def clean(self, value):
        return self.api.clean_text(value)

    def compose(self):
        yield SelectableStatic("GPU inference services · loading…", id="fleet-title", markup=False)
        yield SelectableStatic("", id="fleet-banner", markup=False)
        yield SelectableStatic("", id="fleet-controls", markup=False)
        with FleetGpuScroll(id="fleet-gpu-scroll"):
            yield GpuOverview("GPU observations unavailable", id="fleet-gpus", markup=False)
        yield Input(placeholder="Filter container, model, engine or status", id="fleet-filter")
        yield DataTable(id="fleet-table", cursor_type="row", zebra_stripes=False, cell_padding=1)
        with VerticalScroll(id="fleet-details"):
            yield SelectableStatic("Select a service to see its last 7 days.", id="fleet-detail-text", markup=False)
            yield SelectableStatic("", id="fleet-history-text", markup=False)
            yield SelectableStatic("", id="fleet-history-bars", markup=False)
            yield Sparkline([], id="fleet-active-chart")
            yield Sparkline([], id="fleet-token-chart")
        yield SelectableStatic("Drag to select; copy with your terminal", id="fleet-notice", markup=False)
        yield SelectableStatic(HINT, id="fleet-footer", markup=False)

    def on_mount(self):
        self._table = self.dashboard.query_one("#fleet-table", DataTable)
        self.dashboard.set_class(True, "gpu")
        self.focus_view()
        self._ui_timers.append(self.set_interval(self.refresh_seconds, self.refresh_fleet))
        self._event_timer = self.set_interval(0.25, self.update_events, pause=self._event_driven)
        self._ui_timers.append(self._event_timer)
        self._ui_timers.append(self.set_interval(.6, self.pulse_gpu_marker))
        if self._event_driven:
            self.event_reader.set_notify(self.notify_events)
        self.event_reader.start()
        self.refresh_fleet()
        self.update_events()

    async def on_unmount(self):
        if self._ui_closed:
            return
        self._ui_closed = True
        self.clear_text_selections()
        if self._event_driven:
            self.event_reader.set_notify(None)
        for timer in self._ui_timers:
            timer.stop()
        if self._history_timer is not None:
            self._history_timer.stop()
        self._history_generation += 1
        await asyncio.to_thread(self.event_reader.close)

    def on_resize(self, event):
        self.dashboard.set_class(event.size.width < 100, "narrow")
        if self.snapshot is not None and self.alive():
            self.render_snapshot()

    def focus_view(self):
        if self.alive():
            self.dashboard.query_one("#fleet-gpus" if self.view == "gpu" else "#fleet-table").focus(scroll_visible=False)

    def clear_text_selections(self, except_widget=None):
        for screen in self.screen_stack:
            for panel in screen.query(SelectableStatic):
                if panel is not except_widget and (panel.dragging or panel.has_selection):
                    panel.clear_selection()
        if self._selection_widget is not except_widget:
            self._selection_widget = None

    def on_selectable_static_selection_changed(self, event):
        panel = event.selection
        if panel.dragging or panel.has_selection:
            self.clear_text_selections(except_widget=panel)
            self._selection_widget = panel
        elif self._selection_widget is panel:
            self._selection_widget = None
            if self.view == "gpu" and self.screen is self.dashboard:
                self.call_after_refresh(self.follow_gpu_scroll)

    def action_copy_selection(self):
        panel = self._selection_widget
        if panel is not None and panel.is_attached and panel.has_selection:
            self.copy_to_clipboard(panel.selected_text)
        elif isinstance(self.focused, Input):
            selected = getattr(self.focused, "selected_text", "")
            if selected:
                self.copy_to_clipboard(selected)
        elif isinstance(self.focused, DataTable) and self.focused.display:
            table = self.focused
            if 0 <= table.cursor_row < table.row_count:
                cells = table.get_row_at(table.cursor_row)
                self.copy_to_clipboard("\t".join(
                    cell.plain if isinstance(cell, Text) else self.clean(cell) for cell in cells))

    def pulse_gpu_marker(self):
        if self.alive() and self.view == "gpu":
            self.dashboard.query_one("#fleet-gpus", GpuOverview).pulse_marker()

    def handle_gpu_wheel(self, event, step):
        if self.view != "gpu" or self.screen is not self.dashboard:
            return
        event.stop()
        event.prevent_default()
        viewport = self.dashboard.query_one("#fleet-gpu-scroll", FleetGpuScroll)
        viewport.scroll_now(viewport.scroll_y + step)

    def follow_gpu_scroll(self):
        if (not self.alive() or self.view != "gpu" or self.screen is not self.dashboard
                or self.compact_gpus or self._gpu_scroll_pending):
            return
        overview = self.dashboard.query_one("#fleet-gpus", GpuOverview)
        if overview.dragging or overview.has_selection:
            return
        viewport = self.dashboard.query_one("#fleet-gpu-scroll", VerticalScroll)
        middle = viewport.scroll_y + viewport.content_size.height / 2
        visible = [account.index for account in self.gpu_accounts
                   if self._gpu_anchors.get(account.index, float("inf")) <= middle]
        if visible and visible[-1] != self.selected_gpu:
            self.select_gpu(visible[-1], scroll=False)

    def scroll_gpu_selection(self):
        try:
            if self.alive() and self.view == "gpu":
                target = 0 if self.compact_gpus else self._gpu_anchors.get(self.selected_gpu, 0)
                self.dashboard.query_one("#fleet-gpu-scroll", FleetGpuScroll).scroll_now(target)
        finally:
            self._gpu_scroll_pending = False

    def selected_gpu_account(self):
        return next((account for account in self.gpu_accounts if account.index == self.selected_gpu), None)

    def gpu_services(self):
        account = self.selected_gpu_account()
        if account is None:
            return []
        selected = self.selected_segment
        allocations = [allocation for allocation in account.allocations
                       if selected is None or selected in (MEASURED_KEY, allocation.key)]
        ids = {ident for allocation in allocations for ident, _ in allocation.members if ident is not None}
        return sorted((service for service in self.services() if service["id"] in ids), key=self.sort_key)

    def select_gpu(self, index, segment=None, service_id=None, *, scroll=True):
        changed = index != self.selected_gpu
        self.clear_text_selections()
        self.selected_gpu, self.selected_segment = index, segment
        self._gpu_detail_service = service_id
        self._gpu_scroll_pending = self._gpu_scroll_pending or (changed and scroll)
        self.render_snapshot()

    def move_gpu(self, step):
        indices = [account.index for account in self.gpu_accounts]
        if indices:
            current = indices.index(self.selected_gpu) if self.selected_gpu in indices else 0
            self.select_gpu(indices[max(0, min(len(indices) - 1, current + step))])

    def move_segment(self, step):
        account = self.selected_gpu_account()
        keys = account.selection_keys() if account else []
        if keys:
            current = keys.index(self.selected_segment) if self.selected_segment in keys else (-1 if step > 0 else 0)
            self.select_gpu(self.selected_gpu, keys[(current + step) % len(keys)])

    def open_gpu_details(self):
        if self.selected_gpu_account() is not None:
            self.clear_text_selections()
            self.push_screen(GpuDetailDialog(self))

    def update_static(self, name, value):
        if self._rendered.get(name) != value:
            self.dashboard.query_one("#" + name, Static).update(value)
            self._rendered[name] = value

    def notice(self, message):
        if self.alive():
            self.update_static("fleet-notice", self.clean(message))

    def unreliable(self):
        return bool(self.read_error or (self.snapshot or {}).get("stale") or
                    (self.snapshot or {}).get("errors"))

    @work
    async def refresh_fleet(self):
        if not self.alive():
            return
        if self.fetching:
            self._refresh_pending = True
            return
        self.fetching = True
        try:
            snapshot = await asyncio.to_thread(self.client.request, "GET", "/v1/fleet")
            validator = getattr(self.api, "validate_fleet", None)
            if callable(validator):
                validator(snapshot)
            if (not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1 or
                    not isinstance(snapshot.get("services"), list) or
                    not isinstance(snapshot.get("gpus"), list) or
                    not isinstance(snapshot.get("errors", []), list) or
                    not isinstance(snapshot.get("config", {}), dict)):
                raise ValueError("Fleet observations have an unsupported format.")
            ids = set()
            for service in snapshot["services"]:
                if (not isinstance(service, dict) or not isinstance(service.get("id"), str)
                        or not service["id"] or service["id"] in ids
                        or (service.get("container") is not None and not isinstance(service["container"], str))
                        or not isinstance(service.get("gpus", []), list)
                        or not isinstance(service.get("hourly_active_24h") or [], list)
                        or any(not isinstance(service.get(name) or {}, dict)
                               for name in ("window_24h", "window_7d", "claim"))):
                    raise ValueError("Fleet service observations have an unsupported format.")
                ids.add(service["id"])
            for gpu in snapshot["gpus"]:
                if (not isinstance(gpu, dict) or type(gpu.get("index")) is not int
                        or not isinstance(gpu.get("occupants", []), list)
                        or any(not isinstance(item, dict) for item in gpu.get("occupants", []))):
                    raise ValueError("GPU observations have an unsupported format.")
                account_gpu(gpu, snapshot["services"])
            if not self.alive():
                return
            self.snapshot, self.read_error = snapshot, None
            self.render_snapshot()
        except Exception as exc:
            if self.alive():
                self.read_error = self.clean(exc)
                self.render_snapshot()
        finally:
            self.fetching = False
            if self.alive() and self._refresh_pending:
                self._refresh_pending = False
                self.refresh_fleet()

    def service_status(self, service):
        status = service.get("status", "unknown")
        return "unknown" if self.unreliable() or status not in STATUS_STYLE else status

    def services(self):
        values = (self.snapshot or {}).get("services", [])
        needle = self.filter_text.casefold()
        return [service for service in values
                if (not self.mine_only or service.get("mine") is True)
                and (not needle or needle in " ".join(self.clean(service.get(key, ""))
                     for key in ("container", "model", "engine", "status")).casefold())]

    def sort_key(self, service):
        mem = service.get("gpu_gb")
        mem = mem if numeric(mem) else -1
        idle = service.get("idle_seconds")
        idle = idle if numeric(idle) and self.service_status(service) != "unknown" else -1
        tokens = (service.get("window_24h") or {}).get("gen_tokens")
        tokens = tokens if numeric(tokens) else -1
        leading = {"priority": -(self.service_status(service) == "over_limit"),
                   "idle": -idle, "mem": -mem, "tokens": -tokens}[self.sort_mode]
        return leading, -mem, self.clean(owner_name(service)), service["id"]

    def service_cells(self, service, key, narrow):
        status = self.service_status(service)
        window = service.get("window_24h") or {}
        ratio = window.get("active_ratio")
        values = ["  " + self.clean(service.get("model") or "unknown"),
                  ",".join(str(gpu) for gpu in service.get("gpus", [])) or "?",
                  gib(service.get("gpu_gb")),
                  activity_bar(service.get("hourly_active_24h") or [None] * 24),
                  "?" if status == "unknown" else duration(service.get("idle_seconds")),
                  "Inactive" if status == "over_limit" else status_label(status)]
        if not narrow:
            values += ["—" if not numeric(ratio) else "%d%%" % (ratio * 100), count(window.get("gen_tokens"))]
        cells = [RowLabel(values[0], key)]
        for index, value in enumerate(values[1:], 1):
            cells.append(Text(value, style=STATUS_STYLE[status] if index == 5 else "",
                              justify="right" if index in (1, 2, 4, 6, 7) else "left"))
        return tuple(cells)

    def display_rows(self, narrow):
        services = sorted(self.services(), key=self.sort_key)
        groups = {}
        result, metadata = [], {}
        if self.view == "person":
            for service in services:
                groups.setdefault(owner_name(service), []).append(service)
            ordered = sorted(groups, key=lambda name: (min(self.sort_key(item)[0] for item in groups[name]),
                             -(total_memory(groups[name]) if total_memory(groups[name]) is not None else -1), name))
            for name in ordered:
                members = groups[name]
                key = "person:" + name
                over = sum(self.service_status(item) == "over_limit" for item in members)
                values = [name + " · %dsvc" % len(members), "", gib(total_memory(members)),
                          "", "", "%d inactive" % over if over else ""] + ([] if narrow else ["", ""])
                result.append((key, tuple([RowLabel(self.clean(values[0]), key, "bold #d8ae7b")] +
                                         [Text(value) for value in values[1:]])))
                for service in members:
                    key = "service:" + service["id"]
                    result.append((key, self.service_cells(service, key, narrow)))
                    metadata[key] = service["id"]
        else:
            for gpu in sorted((self.snapshot or {}).get("gpus", []), key=lambda gpu: gpu["index"]):
                index = gpu["index"]
                key = "gpu:" + str(index)
                values = ["GPU" + str(index), "", gib(gpu.get("used_gb")),
                          "%s/%s GiB · %s%%" % (gib(gpu.get("used_gb")), gib(gpu.get("total_gb")),
                                                amount(gpu.get("util_percent"))), "", ""] + ([] if narrow else ["", ""])
                result.append((key, tuple([RowLabel(values[0], key, "bold #d8ae7b")] +
                                         [Text(value) for value in values[1:]])))
                for service in services:
                    if index in service.get("gpus", []):
                        key = "service:%s:%s" % (index, service["id"])
                        cells = list(self.service_cells(service, key, narrow))
                        cells[0] = RowLabel(self.clean(owner_name(service)) + " · " +
                                            self.clean(service.get("model") or "unknown"), key)
                        # Per-card occupants are measured independently of a service's total.
                        occupant = next((item for item in gpu.get("occupants", [])
                                         if item.get("service_id") == service["id"]), None)
                        cells[2] = Text(gib(occupant.get("used_gb")) if occupant else "?")
                        result.append((key, tuple(cells)))
                        metadata[key] = service["id"]
                if not self.mine_only:
                    for number, occupant in enumerate(gpu.get("occupants", [])):
                        if occupant.get("service_id") is not None:
                            continue
                        name = self.clean(occupant.get("container") or "unknown")
                        if self.filter_text and self.filter_text.casefold() not in name.casefold():
                            continue
                        key = "other:%s:%s:%s" % (index, name, number)
                        values = [name + " (other workload)", str(index), gib(occupant.get("used_gb")),
                                  "", "", ""] + ([] if narrow else ["", ""])
                        result.append((key, tuple([RowLabel(values[0], key)] + [Text(value) for value in values[1:]])))
        return result, metadata

    def selected_service_id(self):
        if self.view == "gpu":
            services = self.gpu_services()
            return next((service["id"] for service in services if service["id"] == self._gpu_detail_service),
                        services[0]["id"] if services else None)
        if not hasattr(self, "_table"):
            return None
        row = self._table.cursor_row
        return self.row_services.get(self.row_keys[row]) if 0 <= row < len(self.row_keys) else None

    def selected_service(self):
        ident = self.selected_service_id()
        return next((service for service in (self.snapshot or {}).get("services", [])
                     if service["id"] == ident), None)

    def render_snapshot(self):
        if not self.alive() or not self._table.is_attached:
            return
        snapshot = self.snapshot or {}
        age = snapshot.get("snapshot_age_seconds")
        self.dashboard.set_class(self.view == "gpu", "gpu")
        self.dashboard.set_class(self.compact_gpus, "compact")
        self.gpu_accounts = [account_gpu(gpu, snapshot.get("services", []))
                             for gpu in sorted(snapshot.get("gpus", []), key=lambda item: item["index"])]
        indices = [account.index for account in self.gpu_accounts]
        if self.selected_gpu not in indices:
            self.selected_gpu = indices[0] if indices else None
            self.selected_segment, self._gpu_detail_service = None, None
        account = self.selected_gpu_account()
        if account and self.selected_segment not in (None, MEASURED_KEY, *account.selection_keys()):
            self.selected_segment, self._gpu_detail_service = None, None
        used = [account.used_gb for account in self.gpu_accounts]
        totals = [account.total_gb for account in self.gpu_accounts]
        frees = [account.free_gb for account in self.gpu_accounts]
        summary = "VRAM %s used of %s GiB · free %s GiB" % (
            compact_gib(sum(used)) if used and all(numeric(value) for value in used) else "?",
            compact_gib(sum(totals)) if totals and all(numeric(value) for value in totals) else "?",
            compact_gib(sum(frees)) if frees and all(numeric(value) for value in frees) else "?")
        age_label = "%ds" % age if numeric(age) and age < 60 else duration(age)
        self.update_static("fleet-title", fit("%s · %s" % (
            "GPU fleet" if self.view == "gpu" else "People / containers", summary), self.size.width))
        errors = []
        if self.read_error:
            errors.append("Read failed: " + self.read_error)
        if snapshot.get("stale"):
            errors.append("Snapshot stale")
        if snapshot.get("errors"):
            errors.append("Collection incomplete: " +
                          "; ".join(self.clean(error) for error in snapshot["errors"]))
        self.update_static("fleet-banner", " · ".join(errors))
        self.dashboard.query_one("#fleet-banner").display = bool(errors)
        overview = self.dashboard.query_one("#fleet-gpus", GpuOverview)
        if self.view == "gpu":
            stale = bool(snapshot.get("stale") or self.read_error)
            if self.compact_gpus:
                available = self.size.height - 4 - bool(errors) - (3 if self.dashboard.query_one("#fleet-filter").display else 0)
                bar_rows = 2 if self.size.width >= 100 and available >= len(self.gpu_accounts) * 4 else 1
                view, hits = render_overview(self.gpu_accounts, self.size.width, bar_rows,
                    self.selected_gpu, self.selected_segment, stale, self.clean,
                    show_legends=available >= len(self.gpu_accounts) * 3)
                service_hits, anchors = {}, {}
                headings = {index: (row, 1) for row, _, _, index, key in hits if key is None}
            else:
                bar_rows = 3
                width = max(1, self.size.width - 1)
                view, hits, anchors, service_hits = render_expanded(
                    self.gpu_accounts, sorted(self.services(), key=self.sort_key), self.size.width - 1,
                    bar_rows, self.selected_gpu, self.selected_segment, stale, self.clean,
                    {service["id"]: self.service_status(service) for service in self.services()})
                headings = {account.index: (anchors[account.index], len(expanded_header(
                    account, account.index == self.selected_gpu, stale).wrap(
                        self.console, width, overflow="fold", no_wrap=False))) for account in self.gpu_accounts}
            overview.update_view(view, hits, service_hits, anchors, headings, self.selected_gpu, bar_rows)
            self._rendered["fleet-gpus"] = view
            if self._gpu_scroll_pending and not (overview.dragging or overview.has_selection):
                self.call_after_refresh(self.scroll_gpu_selection)
        else:
            overview.update_view(Text(summary), [], {}, {}, {}, None, 1)
            self._rendered["fleet-gpus"] = summary
        if self.view == "gpu":
            controls = Text("LLM · Other · Unattributed · Free", style="#98a4b4")
            controls.append(" · Updated " + age_label + " ago", style="#98a4b4")
            if self.mine_only:
                controls.append(" · mine", style="#c5ced8")
            if self.filter_text:
                controls.append(" · search: " + self.clean(self.filter_text), style="#c5ced8")
        else:
            controls = "People / containers · sort %s%s%s" % (
                {"priority": "State", "idle": "Idle time", "mem": "Memory", "tokens": "Output"}[self.sort_mode],
                " · mine" if self.mine_only else "",
                " · filter: " + self.clean(self.filter_text) if self.filter_text else "")
        self.update_static("fleet-controls", fit(controls, self.size.width))
        self.update_static("fleet-footer", fit(
            GPU_HINT.replace("Z compact", "Z expand") if self.compact_gpus and self.view == "gpu"
            else GPU_HINT if self.view == "gpu" else HINT, self.size.width))
        narrow = self.size.width < 100
        columns = [("service", "OWNER / SERVICE", 16 if narrow else 19), ("gpu", "GPU", 4),
                   ("mem", "GiB", 5), ("activity", "ACTIVITY · 24h", 24),
                   ("idle", "IDLE", 8), ("status", "STATE", 8)]
        if not narrow:
            columns += [("ratio", "24h", 4), ("tokens", "OUT TOKENS", 10)]
        previous_id = self.selected_service_id()
        previous_key = self.row_keys[self._table.cursor_row] if self.row_keys and self._table.cursor_row < len(self.row_keys) else None
        with self._table.prevent(DataTable.RowHighlighted):
            if columns != self._columns:
                self._table.clear(columns=True)
                self._rows.clear()
                self.row_keys = []
                for key, label, width in columns:
                    self._table.add_column(label, key=key, width=width)
                self._columns = columns
            desired, metadata = self.display_rows(narrow)
            desired_keys = [key for key, _ in desired]
            for key in list(self._rows):
                if key not in desired_keys:
                    self._table.remove_row(key)
                    del self._rows[key]
            for key, cells in desired:
                if key not in self._rows:
                    self._table.add_row(*cells, key=key)
                    self._rows[key] = cells
                else:
                    old = list(self._rows[key])
                    for index, cell in enumerate(cells):
                        if old[index] != cell:
                            self._table.update_cell(key, columns[index][0], cell, update_width=False)
                            old[index] = cell
                    self._rows[key] = tuple(old)
            if desired_keys != self.row_keys:
                ranks = {key: index for index, key in enumerate(desired_keys)}
                self._table.sort("service", key=lambda label: ranks[label.key])
            self.row_keys, self.row_services = desired_keys, metadata
            selected = previous_key if previous_key in metadata else next(
                (key for key in desired_keys if previous_id is not None and metadata.get(key) == previous_id), None)
            selected = selected or next((key for key in desired_keys if key in metadata), None)
            if selected is not None and self._table.cursor_row != desired_keys.index(selected):
                row = desired_keys.index(selected)
                self._table.move_cursor(row=row, animate=False)
                # New rows obtain their cursor bounds on the next layout pass.
                if self._table.cursor_row != row:
                    self.call_after_refresh(self.restore_service_cursor, selected)
        self.render_details()
        if self.gpu_detail is not None:
            self.gpu_detail.refresh_contents()

    def restore_service_cursor(self, key):
        if self.alive() and key in self.row_services:
            with self._table.prevent(DataTable.RowHighlighted):
                self._table.move_cursor(row=self.row_keys.index(key), animate=False)
            self.render_details()

    def on_data_table_row_highlighted(self, event):
        if self.alive() and event.data_table is self._table:
            if self._history_signature is not None and self.selected_service_id() != self._history_signature[0]:
                self.clear_text_selections()
            self.render_details()

    def render_details(self):
        service = self.selected_service()
        if service is None:
            self._history_generation += 1
            self._history_signature = None
            self.update_static("fleet-detail-text", "Select a service to see its last 7 days.")
            self.history, self.history_error = None, None
            self.render_history()
            return
        lines = ["%s · %s · %s · online %s · %s" % (self.clean(service.get("model") or "unknown"),
                 self.clean(owner_name(service)), self.clean(service.get("engine", "?")),
                 duration(service.get("uptime_seconds")), status_label(self.service_status(service))),
                 "API: " + api_label(service, self.clean, fresh=not bool(
                     self.read_error or (self.snapshot or {}).get("stale")))]
        for name, label in (("window_24h", "24h"), ("window_7d", "7d")):
            window = service.get(name) or {}
            ratio, coverage = window.get("active_ratio"), window.get("coverage_ratio")
            lines.append("%s active %s (%s) · requests %s · input %s tokens · output %s tokens · total %s tokens · observed %s" %
                         (label, duration(None if window.get("active_minutes") is None else window["active_minutes"] * 60),
                          "—" if not numeric(ratio) else "%d%%" % (ratio * 100), count(window.get("requests")),
                          count(window.get("prompt_tokens")), count(window.get("gen_tokens")), count(total_tokens(window)),
                          "?" if not numeric(coverage) else "%d%%" % (coverage * 100)))
        claim = service.get("claim")
        lines.append("Started %s · claim %s" % (timestamp(service.get("started_at")),
                     ("until %s: %s" % (timestamp(claim.get("until")), self.clean(claim.get("reason", "")))) if claim else "none"))
        self.update_static("fleet-detail-text", "\n".join(lines))
        signature = service["id"], (self.snapshot or {}).get("generated_at")
        if signature != self._history_signature:
            changed = self._history_signature is None or signature[0] != self._history_signature[0]
            self._history_signature = signature
            self._history_generation += 1
            if changed:
                self.history, self.history_error = None, "Loading 7d history…"
                self.render_history()
            if self._history_timer is not None:
                self._history_timer.stop()
            generation = self._history_generation
            self._history_timer = self.set_timer(0.1, lambda: self.fetch_history(service["id"], generation))

    @work
    async def fetch_history(self, ident, generation):
        if not self.alive() or generation != self._history_generation or self.selected_service_id() != ident:
            return
        try:
            result = await asyncio.to_thread(self.client.request, "GET", "/v1/fleet/history?" +
                                            urlencode({"service": ident, "hours": 168}))
            validator = getattr(self.api, "validate_fleet_history", None)
            if callable(validator):
                validator(SimpleNamespace(service=ident, hours=168), result)
            if (not isinstance(result, dict) or result.get("schema_version") != 1 or
                    result.get("service_id") != ident or result.get("hours") != 168 or
                    result.get("resolution") != "hourly" or
                    not isinstance(result.get("samples"), list) or len(result["samples"]) > 169 or
                    any(not isinstance(sample, dict) for sample in result["samples"]) or
                    not isinstance(result.get("service", {}), dict)):
                raise ValueError("Unrecognized service history.")
            error = None
        except Exception as exc:
            result, error = None, "7d history unavailable: " + self.clean(exc)
        if self.alive() and generation == self._history_generation and self.selected_service_id() == ident:
            self.history, self.history_error = result, error
            self.render_history()

    def render_history(self):
        samples = (self.history or {}).get("samples", [])
        summary = (self.history or {}).get("service") or {}
        self.update_static("fleet-history-text", self.history_error or
                           ("7d hourly active / output tokens · %d hour records · parameters: %s" %
                            (len(samples), self.clean(summary.get("argv_redacted") or "unavailable")) if self.history else ""))
        fallback = []
        for name, field, label in (("fleet-active-chart", "active_minutes", "Active"),
                                   ("fleet-token-chart", "gen_tokens", "Output")):
            values = [sample.get(field) for sample in samples]
            complete = bool(values) and all(numeric(value) for value in values)
            chart = self.dashboard.query_one("#" + name, Sparkline)
            chart.display = complete
            if complete and list(chart.data or []) != values:
                chart.data = values
            if values and not complete:
                maximum = max((value for value in values if numeric(value)), default=0)
                scaled = values if field == "active_minutes" else [
                    None if not numeric(value) else (value / maximum * 60 if maximum else 0) for value in values]
                fallback.append(label + " " + activity_bar(scaled) + " · unknown hours remain dots")
        self.update_static("fleet-history-bars", "\n".join(fallback))
        if self.gpu_detail is not None:
            self.gpu_detail.sync_history()

    def notify_events(self):
        if not self._ui_closed and not self._event_notice.is_set():
            self._event_notice.set()
            self.post_message(FleetEventsChanged())

    def on_fleet_events_changed(self, message):
        if self.alive():
            self._event_timer.resume()

    def update_events(self):
        if not self.alive() or not self._table.is_attached:
            return
        if self._event_driven:
            self._event_timer.pause()
        self._event_notice.clear()
        update = self.event_reader.drain()
        generation = update.get("generation", 0)
        reset = generation != self.event_generation
        if reset:
            self.event_generation, self.event_cursor = generation, 0
        status = update.get("status", "unknown")
        connected = "connected" in status and "disconnected" not in status
        old_connection = self.connection
        self.connection = "Live changes connected" if connected else "Live changes reconnecting; timed refresh continues"
        missing = bool(update.get("dropped") or update.get("missed"))
        if missing:
            self.connection += " · changes may be missing"
        relevant = False
        for item in update.get("events", []):
            ident = item.get("id", 0)
            if ident <= self.event_cursor:
                continue
            self.event_cursor = ident
            relevant |= item.get("kind") == "fleet_status_changed"
        if self.snapshot is not None and self.connection != old_connection:
            self.render_snapshot()
        if relevant or reset or missing or (connected and old_connection != self.connection and self.snapshot is not None):
            self.refresh_fleet()

    def claim_allowed(self, ident, container, revoke=False, claim_id=None):
        service = next((item for item in (self.snapshot or {}).get("services", []) if item["id"] == ident), None)
        return bool(service and service.get("mine") is True and service.get("container") == container
                    and not self.unreliable() and ident not in self.uncertain_services
                    and (not revoke or (service.get("claim") or {}).get("id") == claim_id))

    def action_claim(self, revoke=False):
        service = self.selected_service()
        if service and service["id"] in self.uncertain_services:
            self.notice("An earlier claim result is unknown; this session will not repeat it. Read the current claim state.")
        elif service is None or service.get("mine") is not True:
            self.notice("Select a service owned by your container to claim it.")
        elif self.unreliable():
            self.notice("Refresh observations before changing a claim.")
        elif revoke and not (service.get("claim") or {}).get("id"):
            self.notice("This service has no claim to revoke.")
        else:
            self.clear_text_selections()
            self.push_screen(ClaimDialog(self, service, revoke=revoke))

    def on_key(self, event):
        if self.screen is not self.dashboard or isinstance(self.focused, Input):
            if event.key == "escape" and self.screen is self.dashboard:
                self.dashboard.query_one("#fleet-filter", Input).display = False
                self.focus_view()
                self.render_snapshot()
            return
        key = event.key.lower()
        if key in ("up", "down", "pageup", "pagedown"):
            event.stop()
            event.prevent_default()
            self.clear_text_selections()
            viewport = (self.dashboard.query_one("#fleet-gpu-scroll", FleetGpuScroll)
                        if self.view == "gpu" else self._table)
            if self.view == "person" and self.focused is not None:
                details = self.dashboard.query_one("#fleet-details", VerticalScroll)
                if self.focused is details or details in self.focused.ancestors:
                    viewport = details
            step = (-1 if key in ("up", "pageup") else 1) * (
                viewport.size.height if key in ("pageup", "pagedown") else 1)
            viewport._scroll_to(y=viewport.scroll_y + step, animate=False)
            if self.view == "gpu":
                self.call_after_refresh(self.follow_gpu_scroll)
            return
        if self.view == "person" and key in ("j", "k"):
            event.stop()
            event.prevent_default()
            self.clear_text_selections()
            self._table.move_cursor(row=max(0, min(len(self.row_keys) - 1,
                                    self._table.cursor_row + (-1 if key == "k" else 1))), animate=False)
            return
        gpu_key = self.view == "gpu" and key in ("j", "k", "left", "right", "enter")
        if not gpu_key and key not in ("p", "g", "z", "s", "m", "c", "u", "r", "q", "slash", "question_mark"):
            return
        event.stop()
        event.prevent_default()
        if key not in ("r", "q"):
            self.clear_text_selections()
        if gpu_key:
            if key in ("j", "k"):
                self.move_gpu(-1 if key == "k" else 1)
            elif key in ("left", "right"):
                self.move_segment(-1 if key == "left" else 1)
            else:
                self.open_gpu_details()
        elif key in ("p", "g"):
            self.view = "person" if key == "p" else "gpu"
            self._gpu_scroll_pending = key == "g"
            self.render_snapshot()
            self.focus_view()
        elif key == "z":
            if self.view == "gpu":
                self.compact_gpus = not self.compact_gpus
                self._gpu_scroll_pending = True
                self.render_snapshot()
        elif key == "s":
            options = ["priority", "idle", "mem", "tokens"]
            self.sort_mode = options[(options.index(self.sort_mode) + 1) % len(options)]
            self.render_snapshot()
        elif key == "m":
            self.mine_only = not self.mine_only
            self.render_snapshot()
        elif key in ("c", "u"):
            self.action_claim(revoke=key == "u")
        elif key == "r":
            self.refresh_fleet()
        elif key == "q":
            self.exit()
        elif key == "slash":
            field = self.dashboard.query_one("#fleet-filter", Input)
            field.display = True
            self.render_snapshot()
            field.focus()
        else:
            self.push_screen(FleetHelpDialog(self))

    def on_input_changed(self, event):
        if event.input.id == "fleet-filter":
            self.filter_text = event.value
            if self.snapshot is not None:
                self.render_snapshot()

    def on_input_submitted(self, event):
        if event.input.id == "fleet-filter":
            event.stop()
            event.input.display = False
            self.focus_view()
            self.render_snapshot()
