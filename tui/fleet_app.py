# Generated-By: Codex / gpt-6.1-sol
"""Optional fleet dashboard using the standalone CLI's HTTP and event client."""

import asyncio
from datetime import datetime, timezone
import math
import threading
from types import SimpleNamespace
from urllib.parse import quote, urlencode

from rich.text import Text
from textual import work
from textual.app import App
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Sparkline, Static


STATUS_STYLE = {"active": "green", "idle": "", "over_limit": "bold yellow",
                "claimed": "cyan", "unknown": "dim"}
HINT = "p/g view  / filter  s sort  m mine  c claim  u revoke  r refresh  ? help  q quit"


def numeric(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def amount(value):
    return "?" if not numeric(value) else ("%.1f" % value).rstrip("0").rstrip(".")


def count(value):
    if not numeric(value):
        return "—"
    if value >= 1000000:
        return "%.1fM" % (value / 1000000)
    if value >= 1000:
        return "%.1fk" % (value / 1000)
    return str(int(value))


def duration(seconds):
    if not numeric(seconds):
        return "?"
    if seconds >= 86400:
        return "%dd%dh" % (seconds // 86400, seconds % 86400 // 3600)
    if seconds >= 3600:
        return "%dh%02dm" % (seconds // 3600, seconds % 3600 // 60)
    return "%dm" % (seconds // 60)


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
    BINDINGS = [("escape", "close", "Close")]

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
            yield Static(("Revoke claim" if self.revoke else "Claim service") + " · " +
                         self.owner.clean(self.model), id="claim-title", markup=False)
            if not self.revoke:
                yield Static("Until (+3d or YYYY-MM-DDTHH:MM):", id="claim-until-label")
                yield Input(value="+1d", id="claim-until")
                yield Static("Reason (1–200 characters):", id="claim-reason-label")
                yield Input(placeholder="Why this service is needed", max_length=200,
                            id="claim-reason")
            else:
                yield Static("Until %s · %s" % (timestamp(self.claim.get("until")),
                             self.owner.clean(self.claim.get("reason", ""))), markup=False)
            yield Static("Preview first. A preview does not save or revoke a claim.",
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
            if self.revoke:
                if claim.get("id") != self.claim.get("id") or not numeric(claim.get("revoked_at")):
                    raise ValueError("The service did not confirm this claim's revocation.")
            elif (claim.get("until") != payload["until"] or claim.get("reason") != payload["reason"]
                  or (not preview and not claim.get("id")) or (preview and "id" in claim)):
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
    """Person/GPU views with bounded reads and explicit self-service claims."""
    CSS = """
    Screen { background: #181818; color: #d4d4d4; }
    #fleet-title { height: 1; color: #d8ae7b; text-style: bold; }
    #fleet-banner { height: auto; max-height: 2; color: yellow; }
    #fleet-gpus { height: auto; max-height: 8; }
    #fleet-controls { height: 1; color: #b0b0b0; }
    #fleet-filter { height: 3; display: none; }
    #fleet-table { height: 1fr; min-height: 3; background: #181818; }
    #fleet-details { height: 8; border-top: solid #4c4439; }
    .narrow #fleet-details { height: 6; }
    #fleet-detail-text, #fleet-history-text, #fleet-history-bars { height: auto; }
    #fleet-active-chart, #fleet-token-chart { height: 1; }
    #fleet-notice { height: auto; min-height: 1; max-height: 3; color: #d8ae7b; }
    #fleet-footer { height: 1; color: #aaa; }
    DataTable > .datatable--header { background: #2b2823; color: #d8ae7b; }
    """
    BINDINGS = [("ctrl+c", "quit", "Quit")]

    def __init__(self, client, api, event_reader=None, **kwargs):
        super().__init__(**kwargs)
        self.client, self.api = client, api
        self.event_reader = event_reader if event_reader is not None else api.EventReader(client)
        self.refresh_seconds = 15
        self.snapshot = None
        self.fetching = False
        self._refresh_pending = False
        self.read_error = None
        self.view = "person"
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

    @property
    def dashboard(self):
        return self.screen_stack[0]

    def alive(self):
        return self.is_running and not self._ui_closed

    def clean(self, value):
        return self.api.clean_text(value)

    def compose(self):
        yield Static("GPU inference services · loading…", id="fleet-title", markup=False)
        yield Static("", id="fleet-banner", markup=False)
        yield Static("GPU observations unavailable", id="fleet-gpus", markup=False)
        yield Static("", id="fleet-controls", markup=False)
        yield Input(placeholder="Filter container, model, engine or status", id="fleet-filter")
        yield DataTable(id="fleet-table", cursor_type="row", zebra_stripes=False, cell_padding=1)
        with VerticalScroll(id="fleet-details"):
            yield Static("Select a service to see its last 7 days.", id="fleet-detail-text", markup=False)
            yield Static("", id="fleet-history-text", markup=False)
            yield Static("", id="fleet-history-bars", markup=False)
            yield Sparkline([], id="fleet-active-chart")
            yield Sparkline([], id="fleet-token-chart")
        yield Static("", id="fleet-notice", markup=False)
        yield Static(HINT, id="fleet-footer", markup=False)

    def on_mount(self):
        self._table = self.dashboard.query_one("#fleet-table", DataTable)
        self._table.focus()
        self._ui_timers.append(self.set_interval(self.refresh_seconds, self.refresh_fleet))
        self._event_timer = self.set_interval(0.25, self.update_events, pause=self._event_driven)
        self._ui_timers.append(self._event_timer)
        if self._event_driven:
            self.event_reader.set_notify(self.notify_events)
        self.event_reader.start()
        self.refresh_fleet()
        self.update_events()

    async def on_unmount(self):
        if self._ui_closed:
            return
        self._ui_closed = True
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
                  amount(service.get("gpu_gb")),
                  activity_bar(service.get("hourly_active_24h") or [None] * 24),
                  "?" if status == "unknown" else duration(service.get("idle_seconds")),
                  "OVER" if status == "over_limit" else status]
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
                values = [name + " · %dsvc" % len(members), "", amount(total_memory(members)),
                          "", "", "%d over" % over if over else ""] + ([] if narrow else ["", ""])
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
                values = ["GPU" + str(index), "", amount(gpu.get("used_gb")),
                          "%s/%s GiB · %s%%" % (amount(gpu.get("used_gb")), amount(gpu.get("total_gb")),
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
                        cells[2] = Text(amount(occupant.get("used_gb")) if occupant else "?")
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
                        values = [name + " (training/other)", str(index), amount(occupant.get("used_gb")),
                                  "", "", ""] + ([] if narrow else ["", ""])
                        result.append((key, tuple([RowLabel(values[0], key)] + [Text(value) for value in values[1:]])))
        return result, metadata

    def selected_service_id(self):
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
        self.update_static("fleet-title", "GPU inference services · snapshot %s ago · idle limit %sh" %
                           ("%ds" % age if numeric(age) and age < 60 else duration(age),
                            amount((snapshot.get("config") or {}).get("idle_limit_hours"))))
        errors = []
        if self.read_error:
            errors.append("Read failed: " + self.read_error + "; last observations shown")
        if snapshot.get("stale"):
            errors.append("Snapshot stale; current activity unknown")
        if snapshot.get("errors"):
            errors.append("Collection incomplete; current activity unknown: " +
                          "; ".join(self.clean(error) for error in snapshot["errors"]))
        self.update_static("fleet-banner", " · ".join(errors))
        self.dashboard.query_one("#fleet-banner").display = bool(errors)
        gpu_lines = []
        for gpu in sorted(snapshot.get("gpus", []), key=lambda item: item["index"]):
            used, total = gpu.get("used_gb"), gpu.get("total_gb")
            filled = min(10, int(used / total * 10)) if numeric(used) and numeric(total) and total > 0 else None
            bar = "?" * 10 if filled is None else "█" * filled + "░" * (10 - filled)
            others = ["%s (training/other) %sG" % (self.clean(item.get("container") or "unknown"),
                                                 amount(item.get("used_gb")))
                      for item in gpu.get("occupants", []) if item.get("service_id") is None]
            line = "GPU%s %s %s/%sG %s" % (gpu["index"], bar, amount(used), amount(total), " · ".join(others))
            gpu_lines.append(self.api.usage_truncate(line, self.size.width))
        self.update_static("fleet-gpus", "\n".join(gpu_lines) or "GPU observations unavailable")
        self.update_static("fleet-controls", "By %s · sort %s%s%s · %s" %
                           (self.view, self.sort_mode, " · mine" if self.mine_only else "",
                            " · filter: " + self.clean(self.filter_text) if self.filter_text else "", self.connection))
        narrow = self.size.width < 100
        columns = [("service", "OWNER / SERVICE", 16 if narrow else 19), ("gpu", "GPU", 4),
                   ("mem", "GiB", 5), ("activity", "ACTIVITY · 24h", 24),
                   ("idle", "IDLE", 6), ("status", "STATE", 7)]
        if not narrow:
            columns += [("ratio", "24h", 4), ("tokens", "OUTPUT", 6)]
        previous_id = self.selected_service_id()
        previous_key = self.row_keys[self._table.cursor_row] if self.row_keys and self._table.cursor_row < len(self.row_keys) else None
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
                self.call_after_refresh(self._table.move_cursor, row=row, animate=False)
        self.render_details()

    def on_data_table_row_highlighted(self, event):
        if self.alive():
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
                 duration(service.get("uptime_seconds")), self.service_status(service))]
        for name, label in (("window_24h", "24h"), ("window_7d", "7d")):
            window = service.get(name) or {}
            ratio, coverage = window.get("active_ratio"), window.get("coverage_ratio")
            lines.append("%s active %s (%s) · requests %s · output %s · input %s · observed %s" %
                         (label, duration(None if window.get("active_minutes") is None else window["active_minutes"] * 60),
                          "—" if not numeric(ratio) else "%d%%" % (ratio * 100), count(window.get("requests")),
                          count(window.get("gen_tokens")), count(window.get("prompt_tokens")),
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
                           ("7d hourly active / output · %d hour records · parameters: %s" %
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
            self.push_screen(ClaimDialog(self, service, revoke=revoke))

    def on_key(self, event):
        if self.screen is not self.dashboard or isinstance(self.focused, Input):
            if event.key == "escape" and self.screen is self.dashboard:
                self.dashboard.query_one("#fleet-filter", Input).display = False
                self._table.focus()
            return
        key = event.key.lower()
        if key not in ("p", "g", "s", "m", "c", "u", "r", "q", "slash", "question_mark"):
            return
        event.stop()
        event.prevent_default()
        if key in ("p", "g"):
            self.view = "person" if key == "p" else "gpu"
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
            field.focus()
        else:
            self.notice("15s refresh; live changes prompt reads. Choose person/GPU, filter, sort, or your service's claim. Dots mean unknown activity; training/other rows show memory only.")

    def on_input_changed(self, event):
        if event.input.id == "fleet-filter":
            self.filter_text = event.value
            if self.snapshot is not None:
                self.render_snapshot()

    def on_input_submitted(self, event):
        if event.input.id == "fleet-filter":
            event.stop()
            event.input.display = False
            self._table.focus()
