# Generated-By: Codex / gpt-6.1-sol
"""Real PTY protocol and keyboard coverage; no OS clipboard is simulated."""

import asyncio
import copy
import errno
import json
import os
from pathlib import Path
import re
import select
import struct
import subprocess
import sys
import time

import pytest
from rich.cells import cell_len

pytest.importorskip("textual")
from textual.containers import VerticalScroll
from textual.widgets import Button, DataTable
from textual import events

from test_tui_fleet import make_app, ready
from tui.fleet_app import FleetApp, FleetHelpDialog, GpuDetailDialog, GpuOverview
from tui.fleet_terminal import FleetReplyFilter, FleetTerminalDriver, FleetXTermParser


ROOT = Path(__file__).parents[1]


@pytest.fixture
def native_snapshot():
    return json.loads((ROOT / "tests/fixtures/fleet_gpu_overview.json").read_text())


@pytest.mark.parametrize("size", [(100, 30), (80, 24)])
def test_physical_arrows_select_gpu_and_wheel_keeps_selection(native_snapshot, size):
    async def scenario():
        app, _ = make_app(native_snapshot)
        async with app.run_test(size=size) as pilot:
            await ready(app, pilot)
            viewport = app.query_one("#fleet-gpu-scroll", VerticalScroll)
            overview = app.query_one("#fleet-gpus", GpuOverview)
            for expected in (1, 2, 3, 4):
                await app.on_event(events.Key("fleet_scroll_down", None))
                await pilot.pause()
                assert viewport.scroll_y == expected
                assert app.selected_gpu == 0
            await app.on_event(events.Key("fleet_scroll_up", None))
            await pilot.pause()
            assert viewport.scroll_y == 3
            await pilot.press("down")
            assert app.selected_gpu == 1
            assert viewport.scroll_y == app._gpu_anchors[1]
            start = viewport.scroll_y
            await app.on_event(events.Key("fleet_scroll_down", None))
            await pilot.pause()
            assert viewport.scroll_y == start + 1
            await pilot.press("right", "left")
            assert viewport.scroll_y == start
            previous = app.selected_gpu - 1
            await pilot.press("up")
            assert app.selected_gpu == previous
            assert viewport.scroll_y == app._gpu_anchors[previous]
            assert overview.heading_gpu == previous
    asyncio.run(scenario())


def wheel_parser(enabled=True):
    parser = (FleetXTermParser(False) if hasattr(FleetXTermParser, "tick")
              else FleetXTermParser(lambda: True, False))
    parser.wheel_keys = enabled
    return parser


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("chunks", [
    ["a\x1bOB\x1b[A\x1bOAb\x1b[C\x1bOD"],
    ["a\x1b", "O", "B\x1b[", "A\x1bO", "A", "b\x1b[C\x1bOD"],
    list("a\x1bOB\x1b[A\x1bOAb\x1b[C\x1bOD"),
])
def test_raw_wheel_pipeline_preserves_chunk_boundaries_and_event_order(enabled, chunks):
    from codecs import getincrementaldecoder

    parser = wheel_parser(enabled)
    replies = FleetReplyFilter()
    decoder = getincrementaldecoder("utf-8")()
    messages = []
    for chunk in chunks:
        decoded = decoder.decode(replies.feed(chunk.encode("utf-8")))
        if decoded:
            messages.extend(parser.feed(decoded))
    assert [message.key for message in messages] == [
        "a", "fleet_scroll_down" if enabled else "down", "up",
        "fleet_scroll_up" if enabled else "up", "b", "right", "left"]
    assert all(message.character is None for message in messages if "scroll" in message.key)


@pytest.mark.parametrize("enabled", [False, True])
def test_raw_wheel_parser_keeps_bracketed_paste_and_modified_arrows(enabled):
    parser = wheel_parser(enabled)
    messages = []
    for chunk in ["\x1b[20", "0~pasted é\n", "text\x1b[201~", "x\x1b[1;2A\x1bOB"]:
        messages.extend(parser.feed(chunk))
    assert isinstance(messages[0], events.Paste)
    assert messages[0].text == "pasted é\ntext"
    assert [message.key for message in messages[1:]] == [
        "x", "shift+up", "fleet_scroll_down" if enabled else "down"]


@pytest.mark.parametrize("chunk_size", [1, 2, 7, 4096])
def test_late_terminal_replies_do_not_become_keys_or_corrupt_utf8(chunk_size):
    from codecs import getincrementaldecoder

    replies = FleetReplyFilter()
    parser = wheel_parser()
    decoder = getincrementaldecoder("utf-8")()
    packet = (b"a\xc3\x1bP>|iTerm2 3.7.3\x1b\\\xa9\x1b[\x1b[?1007;1$yC"
              b"\x1b[?31u\x1b[?1;2$y\x1bOBb")
    messages = []
    for offset in range(0, len(packet), chunk_size):
        decoded = decoder.decode(replies.feed(packet[offset:offset + chunk_size]))
        if decoded:
            messages.extend(parser.feed(decoded))
    assert [message.key for message in messages] == ["a", "é", "right", "fleet_scroll_down", "b"]
    assert not replies.pending


def test_startup_partial_reply_stays_quarantined_until_delayed_terminator(monkeypatch):
    from tui import fleet_terminal

    clock = [10.0]
    monkeypatch.setattr(fleet_terminal.time, "monotonic", lambda: clock[0])
    replies = FleetReplyFilter()
    assert replies.feed(b"a\x1bP>|iTer") == b"a"
    clock[0] += 20
    assert replies.tick() == b""
    assert replies.feed(b"m2 3.7.3\x1b") == b""
    assert replies.feed(b"\\b\x1b[?") == b"b"
    clock[0] += 20
    assert replies.tick() == b""
    assert replies.feed(b"1007;1$y\x1b[?1u") == b""
    assert not replies.pending


@pytest.mark.parametrize("frame,prefix_length", [
    (b"\x1bP>|iTerm2 3.7.3\x1b\\", 2),
    (b"\x1bP>|iTerm2 3.7.3\x1b\\", 3),
    (b"\x1b[?31u", 2),
    (b"\x1b[?1;1$y", 2),
    (b"\x1b[?1007;1$y", 2),
], ids=["dcs-introducer", "dcs-header", "kitty-csi", "cursor-csi", "wheel-csi"])
def test_completed_reply_introducers_survive_delayed_headers(monkeypatch, frame, prefix_length):
    from codecs import getincrementaldecoder
    from tui import fleet_terminal

    clock = [10.0]
    monkeypatch.setattr(fleet_terminal.time, "monotonic", lambda: clock[0])
    replies = FleetReplyFilter()
    parser = wheel_parser()
    decoder = getincrementaldecoder("utf-8")()
    messages = list(parser.feed(decoder.decode(replies.feed(b"a\xc3" + frame[:prefix_length]))))
    clock[0] += 1
    assert replies.tick() == b"", "partial control header expired into ordinary input"
    decoded = decoder.decode(replies.feed(frame[prefix_length:] + b"\xa9x"))
    messages.extend(parser.feed(decoded))
    assert [message.key for message in messages] == ["a", "é", "x"]
    assert not replies.pending


@pytest.mark.parametrize("packet,prefix_length", [
    (b"\x1bPother dcs\x1b\\", 2),
    (b"\x1bP>other dcs\x1b\\", 3),
    (b"\x1b[A", 2),
    (b"\x1b[?2026;1$y", 2),
    (b"\x1b[200~pasted \x1bP>|iTerm2 3.7.3\x1b\\\x1b[?31u\x1b[201~", 2),
], ids=["unrelated-dcs", "unrelated-dcs-header", "physical-arrow", "unrelated-mode", "paste"])
def test_delayed_control_headers_preserve_unrelated_controls_and_paste(monkeypatch, packet, prefix_length):
    from tui import fleet_terminal

    clock = [10.0]
    monkeypatch.setattr(fleet_terminal.time, "monotonic", lambda: clock[0])
    replies = FleetReplyFilter()
    assert replies.feed(packet[:prefix_length]) == b""
    clock[0] += 1
    assert replies.tick() == b""
    assert replies.feed(packet[prefix_length:]) == packet
    assert not replies.pending


def test_reply_filter_preserves_bracketed_paste_and_unrelated_control_bytes():
    replies = FleetReplyFilter()
    packet = (b"\x1b[200~pasted \x1bP>|iTerm2 3.7.3\x1b\\ \x1b[?31u\x1b[?1;1$y\x1b[201~"
              b"\x1bPother dcs\x1b\\\x1b[?2026;1$y\x1bOA")
    output = b"".join(replies.feed(bytes([byte])) for byte in packet)
    assert output == packet and not replies.pending


def test_reply_filter_bounds_malformed_frames_and_resynchronizes():
    replies = FleetReplyFilter()
    assert replies.feed(b"a\x1bP>|" + b"p" * 10000) == b"a"
    assert len(replies.pending) <= replies.MAX_REPLY_BYTES
    assert replies.feed(b"r" * 10000) == b"" and len(replies.pending) <= 1
    assert replies.feed(b"\x1b\\b\x1b[?" + b"9" * 10000) == b"b"
    assert len(replies.pending) <= 1
    assert replies.feed(b"uc\x1bP>|broken\x1b[Bd") == b"c\x1b[Bd"
    assert not replies.pending


def test_reply_filter_releases_a_real_escape_with_normal_timeout(monkeypatch):
    from tui import fleet_terminal

    clock = [10.0]
    monkeypatch.setattr(fleet_terminal.time, "monotonic", lambda: clock[0])
    replies = FleetReplyFilter()
    assert replies.feed(b"\x1b") == b""
    clock[0] += 1
    assert replies.tick() == b"\x1b" and not replies.pending


def test_new_reply_prefix_gets_its_own_timeout_after_a_delayed_frame(monkeypatch):
    from codecs import getincrementaldecoder
    from tui import fleet_terminal

    clock = [10.0]
    monkeypatch.setattr(fleet_terminal.time, "monotonic", lambda: clock[0])
    replies = FleetReplyFilter()
    decoder = getincrementaldecoder("utf-8")()
    assert decoder.decode(replies.feed(b"\xc3\x1bP>|iTerm2")) == ""
    clock[0] += 20
    assert replies.tick() == b""
    assert replies.feed(b" 3.7.3\x1b\\\x1b") == b""
    assert replies.tick() == b"", "new reply Escape inherited the old DCS timeout"
    assert decoder.decode(replies.feed(b"[?31u\xa9")) == "é"


def test_input_thread_keeps_reply_escape_while_utf8_is_incomplete(monkeypatch):
    from codecs import getincrementaldecoder
    from threading import Event
    from tui import fleet_terminal

    clock = [10.0]
    monkeypatch.setattr(fleet_terminal.time, "monotonic", lambda: clock[0])
    driver = FleetTerminalDriver.__new__(FleetTerminalDriver)
    driver.fileno = 42
    driver._debug = False
    driver._wheel_keys_enabled = False
    driver._reply_filter = FleetReplyFilter()
    driver._input_decoder = getincrementaldecoder("utf-8")()
    driver._pending_input = bytearray(b"a\xc3\x1bP>|iTerm2 3.7.3\x1b\\\x1b[?1;1$y\x1b")
    driver.exit_event = Event()
    messages = []

    def process(message):
        messages.append(message)
        if message.key == "x":
            driver.exit_event.set()

    driver.process_message = process

    class InputSelector:
        def __init__(self):
            self.calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def register(self, fd, event):
            assert fd == driver.fileno

        def select(self, timeout):
            self.calls += 1
            if self.calls == 1:
                clock[0] += 1
                return []
            return [(None, fleet_terminal.selectors.EVENT_READ)]

    def read(fd, size):
        assert fd == driver.fileno
        return b"[?1007;1$y\xa9x"

    monkeypatch.setattr(fleet_terminal.selectors, "SelectSelector", InputSelector)
    monkeypatch.setattr(fleet_terminal.os, "read", read)
    driver.run_input_thread()
    assert [message.key for message in messages] == ["a", "é", "x"]
    assert not driver._input_decoder.getstate()[0] and not driver._reply_filter.pending


@pytest.mark.parametrize("size", [(100, 30), (80, 24)])
def test_help_keyboard_scroll_from_button_focus_preserves_dashboard(native_snapshot, size):
    async def scenario():
        app, _ = make_app(native_snapshot)
        async with app.run_test(size=size) as pilot:
            await ready(app, pilot)
            await pilot.press("j", "j", "right", "down")
            viewport = app.query_one("#fleet-gpu-scroll", VerticalScroll)
            context = app.selected_gpu, app.selected_segment, viewport.scroll_y
            await pilot.press("question_mark")
            await ready(app, pilot)
            assert isinstance(app.screen, FleetHelpDialog)
            help_scroll = app.screen.query_one("#fleet-help-scroll", VerticalScroll)
            app.screen.query_one("#fleet-help-close", Button).focus(scroll_visible=False)
            assert help_scroll.max_scroll_y > 0
            await pilot.press("down")
            assert help_scroll.scroll_y == 1
            await pilot.press("pagedown")
            assert help_scroll.scroll_y == min(1 + help_scroll.size.height, help_scroll.max_scroll_y)
            await pilot.press("up")
            assert help_scroll.scroll_y == max(0, help_scroll.max_scroll_y - 1)
            await pilot.press("pageup", "escape")
            assert (app.selected_gpu, app.selected_segment, viewport.scroll_y) == context
            assert app.focused is app.query_one("#fleet-gpus", GpuOverview)
    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(100, 30), (80, 24)])
def test_modal_keyboard_scroll_reaches_history_without_changing_service(native_snapshot, size):
    async def scenario():
        native_snapshot["services"][0]["model"] = "Visible service details " * 30
        app, _ = make_app(native_snapshot)
        async with app.run_test(size=size) as pilot:
            await ready(app, pilot)
            dashboard_scroll = app.query_one("#fleet-gpu-scroll", VerticalScroll)
            await pilot.press("right", "enter")
            await ready(app, pilot)
            assert isinstance(app.screen, GpuDetailDialog)
            viewport = app.screen.query_one("#gpu-allocation-scroll", VerticalScroll)
            table = app.screen.query_one("#gpu-detail-services", DataTable)
            assert viewport.max_scroll_y > 0 and table.row_count == 2
            first = app.selected_service_id()
            start = viewport.scroll_y
            await pilot.press("down")
            assert viewport.scroll_y == start + 1
            assert app.selected_service_id() == first and table.cursor_row == 0
            await pilot.press("up", "j")
            await ready(app, pilot)
            assert table.cursor_row == 1 and app.selected_service_id() != first
            await pilot.press("k")
            await ready(app, pilot)
            assert app.selected_service_id() == first
            app.screen.query_one("#gpu-detail-close", Button).focus(scroll_visible=False)
            for _ in range(3):
                await pilot.press("pagedown")
            assert viewport.scroll_y == viewport.max_scroll_y
            history = app.screen.query_one("#gpu-service-history-bars")
            assert viewport.content_region.overlaps(history.region)
            assert app.selected_service_id() == first
            await pilot.press("pageup")
            assert viewport.scroll_y < viewport.max_scroll_y
            await pilot.press("enter")
            assert app.screen is app.dashboard and dashboard_scroll.scroll_y == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(100, 30), (80, 24)])
@pytest.mark.parametrize("focus", ["fleet-details", "fleet-detail-text", "fleet-history-text"])
def test_people_keys_scroll_focused_contents_without_changing_service(native_snapshot, size, focus):
    async def scenario():
        for index in range(8):
            service = copy.deepcopy(native_snapshot["services"][0])
            service.update(id="keyboard-service-%s" % index, gpu_gb=0, gpus=[0])
            native_snapshot["services"].append(service)
            native_snapshot["gpus"][0]["occupants"].append({
                "container": service["container"], "kind": "llm", "used_gb": 0, "service_id": service["id"]})
        for service in native_snapshot["services"]:
            service["model"] = "Visible service details " * 30
        app, _ = make_app(native_snapshot)
        async with app.run_test(size=size) as pilot:
            await ready(app, pilot)
            await pilot.press("p", "j")
            await ready(app, pilot)
            table = app.query_one("#fleet-table", DataTable)
            details = app.query_one("#fleet-details", VerticalScroll)
            assert app.selected_service_id() is not None
            selected = app.selected_service_id(), table.cursor_row
            table.focus(scroll_visible=False)
            table._scroll_to(y=0, animate=False)
            await pilot.pause()
            assert table.max_scroll_y > 0
            await pilot.press("down")
            assert table.scroll_y == 1
            assert (app.selected_service_id(), table.cursor_row) == selected
            details._scroll_to(y=0, animate=False)
            app.query_one("#" + focus).focus(scroll_visible=False)
            await pilot.pause()
            assert app.focused.id == focus and details.max_scroll_y > 0
            table_scroll = table.scroll_y
            await pilot.press("down", "down", "up")
            assert details.scroll_y == 1 and table.scroll_y == table_scroll
            await pilot.press("pagedown")
            assert details.scroll_y == min(1 + details.size.height, details.max_scroll_y)
            await pilot.press("pageup")
            assert details.scroll_y == min(1, max(0, details.max_scroll_y - details.size.height))
            assert (app.selected_service_id(), table.cursor_row) == selected
    asyncio.run(scenario())


# The child uses the production FleetApp and driver, with fixture-only readers.
# Keys written to its PTY are decoded by the driver's real input thread.
PTY_RUNNER = r'''
import asyncio
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path.cwd() / "tests"))
from test_tui_fleet import make_app, ready
from tui.fleet_app import FleetApp
from textual import events

scenario, entrypoint, report_path = sys.argv[1:]
snapshot = json.loads(Path("tests/fixtures/fleet_gpu_overview.json").read_text())
for service in snapshot["services"]:
    service["model"] = "NATIVE_COPY_BUFFER"
base, client = make_app(snapshot)
observations = {}

class Probe(FleetApp):
    async def on_event(self, event):
        if isinstance(event, events.Paste):
            observations.setdefault("pastes", []).append(event.text)
            return
        if isinstance(event, events.Key) and event.key in ("enter", "ctrl+s", "ctrl+q"):
            # Observe driver decoding without Enter opening a modal or Ctrl+Q quitting.
            self.observed_keys.append(event.key)
            return
        return await super().on_event(event)

    def on_key(self, event):
        self.observed_keys.append(event.key)
        result = super().on_key(event)
        if event.key in ("fleet_scroll_up", "fleet_scroll_down", "up", "down") and self.screen is self.dashboard:
            viewport = self.query_one("#fleet-gpu-scroll")
            observations.setdefault("key_contexts", []).append({
                "key": event.key, "gpu": self.selected_gpu, "scroll": viewport.scroll_y})
        return result

    def _handle_exception(self, error):
        observations["error"] = type(error).__name__
        observations["error_message"] = str(error)
        observations["cause"] = str(error.__cause__) if error.__cause__ else None
        report("error")
        return super()._handle_exception(error)

app = Probe(client, base.api, event_reader=base.event_reader)
app.observed_keys = []

def report(stage):
    Path(report_path).write_text(json.dumps(dict(observations, stage=stage, keys=app.observed_keys)))

from tui.fleet_terminal import FleetTerminalDriver
original_input = FleetTerminalDriver.run_input_thread
def observed_input(driver):
    try:
        original_input(driver)
    except BaseException as error:
        observations["input_error"] = type(error).__name__ + ": " + str(error)
        report("input_error")
        raise
FleetTerminalDriver.run_input_thread = observed_input

def fail():
    raise RuntimeError("synthetic fleet failure")

if scenario in ("start_error", "dual_error"):
    from textual.drivers.linux_driver import LinuxDriver
    original_start = LinuxDriver.start_application_mode
    def failing_start(driver):
        original_start(driver)
        fail()
    LinuxDriver.start_application_mode = failing_start
    if scenario == "dual_error":
        from tui.fleet_terminal import FleetTerminalDriver
        original_restore = FleetTerminalDriver._restore_scroll_mode
        def failing_restore(driver):
            original_restore(driver)
            if not getattr(driver, "synthetic_cleanup_failed", False):
                driver.synthetic_cleanup_failed = True
                raise RuntimeError("synthetic cleanup failure")
        FleetTerminalDriver._restore_scroll_mode = failing_restore
if scenario in ("pre_query_error", "post_push_error"):
    from tui.fleet_terminal import FleetTerminalDriver
    original_query = FleetTerminalDriver._query_terminal_state
    def failing_query(driver, states):
        result = original_query(driver, states)
        if scenario == "pre_query_error" or driver._kitty_protocol_open:
            fail()
        return result
    FleetTerminalDriver._query_terminal_state = failing_query

async def wait_for_late_reply(pilot, cycles):
    deadline = time.monotonic() + 3
    while app.observed_keys.count("w") < cycles and time.monotonic() < deadline:
        await pilot.pause(.02)
    assert observations.get("input_error") is None, observations.get("input_error")
    assert app.observed_keys.count("w") == cycles, "late reply input was not processed"

async def operate(pilot):
    await ready(app, pilot)
    viewport = app.query_one("#fleet-gpu-scroll")
    observations["before_scroll"] = viewport.scroll_y
    observations["wheel_keys_enabled"] = app._driver._wheel_keys_enabled
    observations["gpu_anchors"] = {index: anchor for index, anchor in app._gpu_anchors.items()
                                   if isinstance(index, int)}
    report("ready")
    deadline = time.monotonic() + 3
    while "x" not in app.observed_keys and time.monotonic() < deadline:
        await pilot.pause(.02)
    await pilot.pause(.05)
    observations["after_scroll"] = viewport.scroll_y
    observations["selected_gpu"] = app.selected_gpu
    if "late_reply" in scenario:
        await wait_for_late_reply(pilot, 1)
    if scenario in ("suspend", "suspend_late_reply"):
        with app.suspend():
            report("suspended")
            time.sleep(.06)
        if "late_reply" in scenario:
            await wait_for_late_reply(pilot, 2)
        else:
            await pilot.pause(.1)
    observations["view"] = app.view
    if scenario == "app_error":
        app.call_later(fail)
        return
    app.exit()

if entrypoint == "run_async":
    asyncio.run(app.run_async(size=(100, 30), auto_pilot=operate))
else:
    app.run(size=(100, 30), auto_pilot=operate)
observations.setdefault("error", None)
report("finished")
'''


CSI = re.compile(r"\x1b\[([0-?]*)([ -/]*)([@-~])")
CONTROL = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()][A-Za-z0-9]|.)")


def terminal_frames(output, width=100, height=30):
    """Decode emitted cursor/erase operations into bounded terminal text cells."""
    cells = [[" "] * width for _ in range(height)]
    frames = []
    x = y = 0
    position = 0
    for match in CONTROL.finditer(output):
        for char in output[position:match.start()]:
            if char == "\r":
                x = 0
            elif char == "\n":
                y = min(height - 1, y + 1)
            elif char >= " " and char != "\x7f":
                size = cell_len(char)
                if size == 0 and x:
                    cells[y][x - 1] += char
                elif x < width:
                    cells[y][x] = char
                    for offset in range(1, size):
                        if x + offset < width:
                            cells[y][x + offset] = ""
                    x = min(width, x + size)
        token = match.group()
        control = CSI.fullmatch(token)
        if control:
            params, _, command = control.groups()
            fields = params.split(";")
            values = [int(value) if value else 0 for value in fields] if all(not value or value.isdecimal() for value in fields) else []
            if command in ("H", "f"):
                y = min(height - 1, max(0, (values[0] if values else 1) - 1))
                x = min(width - 1, max(0, (values[1] if len(values) > 1 else 1) - 1))
            elif command in ("G", "`"):
                x = min(width - 1, max(0, (values[0] if values else 1) - 1))
            elif command in ("A", "B", "C", "D"):
                step = values[0] if values and values[0] else 1
                x = max(0, min(width - 1, x + (step if command == "C" else -step))) if command in ("C", "D") else x
                y = max(0, min(height - 1, y + (step if command == "B" else -step))) if command in ("A", "B") else y
            elif command == "K":
                mode = values[0] if values else 0
                left, right = (0, width) if mode == 2 else ((0, x + 1) if mode == 1 else (x, width))
                cells[y][left:right] = [" "] * (right - left)
            elif command == "J" and values and values[0] == 2:
                cells = [[" "] * width for _ in range(height)]
            elif command == "l" and params == "?1049":
                frames.append("\n".join("".join(row) for row in cells))
        position = match.end()
    return frames


def run_pty(tmp_path, scenario, entrypoint, initial_mode, reply_position, query_reply="known",
            *, initial_cursor_mode=False, initial_flags=0, kitty_reply="known",
            cursor_reply="known", identity_reply=b"iTerm2 3.7.3", confirm_reply=None,
            split_replies=False, split_wheel=False, late_identity=False, identity_prefix_bytes=0,
            pause_reply_escape=False):
    import fcntl
    import pty
    import termios

    master, slave = pty.openpty()
    original_attrs = termios.tcgetattr(slave)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
    report_path = tmp_path / "report.json"
    child = subprocess.Popen([sys.executable, "-c", PTY_RUNNER, scenario, entrypoint, str(report_path)],
                             cwd=ROOT, stdin=slave, stdout=slave, stderr=slave,
                             env=dict(os.environ, TERM="xterm-256color", NO_COLOR="1"), close_fds=True)
    output = bytearray()
    queries = 0
    identity_queries = 0
    controls_read = 0
    sent_scroll = False
    active_modes = []
    modes = {1: initial_cursor_mode, 1007: initial_mode}
    kitty_stack = [initial_flags]
    pushes = []
    pops = 0
    screen_exits = []
    scheduled = []
    deadline = time.monotonic() + 12
    try:
        while child.poll() is None:
            assert time.monotonic() < deadline, (
                "synthetic terminal child did not finish: "
                + (report_path.read_text() if report_path.exists() else "no report")
                + repr(CONTROL.sub("", output.decode("utf-8", errors="replace"))[-500:]))
            for due, packet in list(scheduled):
                if time.monotonic() >= due:
                    for offset in range(0, len(packet), 3):
                        chunk = packet[offset:offset + 3]
                        os.write(master, chunk)
                        time.sleep(.15 if pause_reply_escape and chunk.endswith(b"\x1b") else .001)
                    scheduled.remove((due, packet))
            readable, _, _ = select.select([master], [], [], .02)
            if readable:
                output.extend(os.read(master, 65536))
            controls = list(CSI.finditer(output.decode("utf-8", errors="replace")))
            replies = []
            for match in controls[controls_read:]:
                params, intermediate, command = match.groups()
                if params in ("?1", "?1007") and command in ("h", "l"):
                    modes[int(params[1:])] = command == "h"
                elif params.startswith(">") and command == "u":
                    flags = int(params[1:])
                    pushes.append(flags)
                    kitty_stack.append(flags)
                elif params == "<" and command == "u":
                    pops += 1
                    assert len(kitty_stack) > 1, "driver popped an unowned keyboard stack"
                    kitty_stack.pop()
                elif params == "?1049" and command == "l":
                    screen_exits.append(dict(modes, flags=kitty_stack[-1], depth=len(kitty_stack)))
                elif params in ("?1", "?1007") and intermediate == "$" and command == "p":
                    mode_id = int(params[1:])
                    if mode_id == 1007:
                        queries += 1
                    override = query_reply if mode_id == 1007 else cursor_reply
                    state = (1 if modes[mode_id] else 2) if override == "known" else override
                    if len(kitty_stack) > 1 and confirm_reply is not None:
                        state = confirm_reply.get(mode_id, state)
                    if state is not None:
                        replies.append(b"\x1b[?" + str(mode_id).encode("ascii") + b";"
                                       + str(state).encode("ascii") + b"$y")
                elif params == "?" and command == "u":
                    flags = kitty_stack[-1] if kitty_reply == "known" else kitty_reply
                    if len(kitty_stack) > 1 and confirm_reply is not None:
                        flags = confirm_reply.get("kitty", flags)
                    if flags is not None:
                        replies.append(b"\x1b[?" + str(flags).encode("ascii") + b"u")
                elif params == ">0" and command == "q":
                    identity_queries += 1
                    if identity_reply is not None:
                        reply = b"\x1bP>|" + identity_reply + b"\x1b\\"
                        if late_identity:
                            if identity_prefix_bytes:
                                replies.append(b"\xc3" + reply[:identity_prefix_bytes])
                                late = reply[identity_prefix_bytes:]
                            else:
                                late = b"\xc3" + reply
                            late += b"\x1b[?31u\x1b[?1;1$y\x1b[?1007;1$y\xa9w"
                            scheduled.append((time.monotonic() + .3, late))
                        else:
                            replies.append(reply)
            controls_read = len(controls)
            if replies:
                # Reply order is intentionally different from the request order.
                reply = b"".join(reversed(replies))
                if reply_position == "utf8":
                    packet = b"\xc3" + reply + b"\xa9"
                elif reply_position == "escape":
                    packet = b"\x1b[" + reply + b"C"
                elif reply_position == "carriage_return":
                    packet = b"\r" + reply
                elif reply_position == "flow_control":
                    packet = b"\x13" + reply + b"\x11"
                elif reply_position == "paste":
                    packet = reply + b"\x1b[200~pasted \xc3\xa9 text\x1b[201~"
                elif reply_position == "ordered":
                    packet = b"i\x1b[C" + reply + b"\x1b[D"
                else:
                    packet = b"i" + reply if reply_position == "before" else reply + b"i"
                if split_replies:
                    for offset in range(0, len(packet), 3):
                        os.write(master, packet[offset:offset + 3])
                        time.sleep(.001)
                else:
                    os.write(master, packet)
            if not sent_scroll and report_path.exists():
                try:
                    state = json.loads(report_path.read_text())
                except json.JSONDecodeError:
                    continue
                if state.get("stage") == "ready":
                    if late_identity and scheduled:
                        continue
                    active_modes.append(dict(modes, flags=kitty_stack[-1]))
                    # Verified iTerm2 wheel bypasses Kitty; physical arrows use CSI.
                    packet = (b"\x1bOB\x1b[B\x1bOBx" if state["wheel_keys_enabled"]
                              else b"\x1b[Bx")
                    if split_wheel:
                        for byte in packet:
                            os.write(master, bytes([byte]))
                            time.sleep(.002)
                    else:
                        os.write(master, packet)
                    sent_scroll = True
        while select.select([master], [], [], .02)[0]:
            try:
                output.extend(os.read(master, 65536))
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                break
        assert child.returncode == 0
        assert termios.tcgetattr(slave) == original_attrs
        assert report_path.exists(), "terminal child produced no result"
        # Consume final restore writes emitted between poll() and the last drain.
        controls = list(CSI.finditer(output.decode("utf-8", errors="replace")))
        for match in controls[controls_read:]:
            params, _, command = match.groups()
            if params in ("?1", "?1007") and command in ("h", "l"):
                modes[int(params[1:])] = command == "h"
            elif params == "<" and command == "u":
                pops += 1
                assert len(kitty_stack) > 1, "driver popped an unowned keyboard stack"
                kitty_stack.pop()
            elif params == "?1049" and command == "l":
                screen_exits.append(dict(modes, flags=kitty_stack[-1], depth=len(kitty_stack)))
        result = json.loads(report_path.read_text())
        assert result.get("input_error") is None, result.get("input_error")
        result["terminal"] = dict(identity_queries=identity_queries, pushes=pushes, pops=pops,
                                  screen_exits=screen_exits, flags=kitty_stack[-1], modes=modes)
        return bytes(output).decode("utf-8", errors="replace"), result, queries, active_modes
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)
        os.close(master)
        os.close(slave)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX terminal protocol")
@pytest.mark.parametrize("initial_mode", [False, True], ids=["wheel-originally-off", "wheel-originally-on"])
@pytest.mark.parametrize("scenario,entrypoint,reply_position", [
    ("normal", "run", "before"),
    ("normal", "run_async", "after"),
    ("normal", "run", "utf8"),
    ("normal", "run", "escape"),
    ("normal", "run", "carriage_return"),
    ("normal", "run", "flow_control"),
    ("normal", "run", "paste"),
    ("normal", "run", "ordered"),
    ("suspend", "run", "before"),
    ("app_error", "run", "after"),
    ("start_error", "run", "before"),
])
def test_native_terminal_protocol_restores_wheel_and_keeps_native_selection(tmp_path, initial_mode, scenario, entrypoint, reply_position):
    output, result, queries, active_modes = run_pty(tmp_path, scenario, entrypoint, initial_mode, reply_position)
    controls = [match.groups() for match in CSI.finditer(output)]
    assert not any(command == "h" and params.startswith("?") and
                   {"1000", "1002", "1003"}.intersection(params[1:].split(";"))
                   for params, _, command in controls), "mouse reporting prevents ordinary terminal selection"
    assert queries == (4 if scenario == "suspend" else 2)
    mode = initial_mode
    restores = 0
    for params, _, command in controls:
        if params == "?1007" and command in ("h", "l"):
            mode = command == "h"
        elif params == "?1049" and command == "l":
            assert mode == initial_mode, "wheel mode leaked when application mode ended"
            restores += 1
    assert restores == (2 if scenario == "suspend" else 1)
    assert mode == initial_mode
    terminal = result["terminal"]
    assert terminal["identity_queries"] == restores
    assert terminal["pushes"] == [1] * restores and terminal["pops"] == restores
    assert terminal["flags"] == 0
    assert terminal["screen_exits"] == [
        {1: False, 1007: initial_mode, "flags": 0, "depth": 1}] * restores
    assert "\x1b]52;" not in output, "ordinary terminal copy must not require application OSC52"
    if scenario == "start_error":
        assert result["error"] == "RuntimeError"
    else:
        assert active_modes == [{1: True, 1007: True, "flags": 1}]
        input_keys = {"utf8": ("é",), "escape": ("right",), "carriage_return": ("enter",),
                      "flow_control": ("ctrl+s", "ctrl+q"), "paste": (),
                      "ordered": ("i", "right", "left")}.get(reply_position, ("i",))
        for input_key in input_keys:
            assert result["keys"].count(input_key) == queries, "interleaved non-query input was discarded"
        if reply_position == "paste":
            assert result["pastes"] == ["pasted é text"] * queries
        if reply_position == "ordered":
            query_keys = [key for key in result["keys"] if key in ("i", "right", "left")]
            assert query_keys == ["i", "right", "left"] * queries
        scrolling = [context for context in result["key_contexts"]
                     if context["key"] in ("fleet_scroll_down", "down")]
        assert [context["key"] for context in scrolling] == ["fleet_scroll_down", "down", "fleet_scroll_down"]
        assert scrolling[0] == {"key": "fleet_scroll_down", "gpu": 0,
                                "scroll": result["before_scroll"] + 1}
        assert scrolling[1] == {"key": "down", "gpu": 1,
                                "scroll": result["gpu_anchors"]["1"]}
        assert scrolling[2] == {"key": "fleet_scroll_down", "gpu": 1,
                                "scroll": scrolling[1]["scroll"] + 1}
        assert result["after_scroll"] == scrolling[2]["scroll"] and result["selected_gpu"] == 1
        assert any("NATIVE_COPY_BUFFER" in frame for frame in terminal_frames(output))
        assert result["error"] == ("RuntimeError" if scenario == "app_error" else None)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX terminal protocol")
@pytest.mark.parametrize("query_reply", [0, 3, 4, None], ids=["unrecognized", "permanently-set", "permanently-reset", "timeout"])
def test_unknown_wheel_mode_is_left_unchanged(tmp_path, query_reply):
    initial_mode = query_reply == 3
    output, result, queries, active_modes = run_pty(tmp_path, "normal", "run", initial_mode, "before", query_reply)
    assert queries == 1 and active_modes == [{1: False, 1007: initial_mode, "flags": 0}]
    assert not any(params == "?1007" and command in ("h", "l")
                   for params, _, command in (match.groups() for match in CSI.finditer(output)))
    assert "i" in result["keys"]
    assert result["selected_gpu"] == 1 and result["error"] is None
    assert not result["wheel_keys_enabled"]
    assert not any(key.startswith("fleet_scroll_") for key in result["keys"])
    assert result["terminal"]["pushes"] == [] and result["terminal"]["pops"] == 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX terminal protocol")
def test_startup_and_cleanup_failure_preserve_exception_chain(tmp_path):
    output, result, queries, _ = run_pty(tmp_path, "dual_error", "run", False, "before")
    assert queries == 2
    assert "\x1b[?1007l" in output and "\x1b[?1049l" in output
    assert result["error"] == "RuntimeError" and result["error_message"] == "synthetic fleet failure"
    assert result["cause"] == "synthetic cleanup failure"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX terminal protocol")
@pytest.mark.parametrize("initial_flags", [4, 16, 31])
@pytest.mark.parametrize("initial_cursor_mode", [False, True])
def test_split_protocol_restores_existing_cursor_mode_and_all_keyboard_flags(
        tmp_path, initial_flags, initial_cursor_mode):
    output, result, queries, active_modes = run_pty(
        tmp_path, "normal", "run", True, "utf8", initial_cursor_mode=initial_cursor_mode,
        initial_flags=initial_flags, split_replies=True, split_wheel=True)
    assert queries == 2 and active_modes == [{1: True, 1007: True, "flags": 1}]
    assert result["keys"].count("é") == 2
    assert [key for key in result["keys"] if key in ("fleet_scroll_down", "down")] == [
        "fleet_scroll_down", "down", "fleet_scroll_down"]
    assert result["terminal"]["pushes"] == [1] and result["terminal"]["pops"] == 1
    assert result["terminal"]["flags"] == initial_flags
    assert result["terminal"]["screen_exits"] == [
        {1: initial_cursor_mode, 1007: True, "flags": initial_flags, "depth": 1}]
    assert "\x1b[>25u" not in output


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX terminal protocol")
@pytest.mark.parametrize("options", [
    {"cursor_reply": 0}, {"cursor_reply": 3}, {"cursor_reply": 4}, {"cursor_reply": None},
    {"kitty_reply": None}, {"identity_reply": None},
    {"identity_reply": b"XTerm(398)"}, {"identity_reply": b"iTerm2 invalid"},
], ids=["cursor-unknown", "cursor-permanent-set", "cursor-permanent-reset", "cursor-timeout",
        "keyboard-timeout", "identity-timeout", "other-terminal", "malformed-identity"])
def test_missing_protocol_or_iterm_identity_keeps_ordinary_keyboard_usable(tmp_path, options):
    output, result, queries, active_modes = run_pty(
        tmp_path, "normal", "run", False, "before", initial_flags=16, **options)
    assert queries == 1 and active_modes == [{1: False, 1007: False, "flags": 16}]
    assert result["selected_gpu"] == 1 and result["error"] is None
    assert not result["wheel_keys_enabled"]
    assert result["terminal"]["pushes"] == [] and result["terminal"]["pops"] == 0
    assert result["terminal"]["flags"] == 16
    assert not any(params in ("?1", "?1007") and command in ("h", "l")
                   for params, _, command in (match.groups() for match in CSI.finditer(output)))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX terminal protocol")
@pytest.mark.parametrize("confirm_reply", [
    {1: 2}, {1007: 2}, {"kitty": 0}, {"kitty": None},
], ids=["cursor-not-set", "wheel-not-set", "keyboard-not-set", "keyboard-readback-timeout"])
def test_unconfirmed_protocol_rolls_back_before_input_thread_starts(tmp_path, confirm_reply):
    output, result, queries, active_modes = run_pty(
        tmp_path, "normal", "run", False, "before", initial_flags=31,
        initial_cursor_mode=True, confirm_reply=confirm_reply)
    assert queries == 2 and active_modes == [{1: True, 1007: False, "flags": 31}]
    assert not result["wheel_keys_enabled"]
    assert result["selected_gpu"] == 1 and result["error"] is None
    assert result["terminal"]["pushes"] == [1] and result["terminal"]["pops"] == 1
    assert result["terminal"]["screen_exits"] == [
        {1: True, 1007: False, "flags": 31, "depth": 1}]
    assert "\x1b[>25u" not in output


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX terminal protocol")
@pytest.mark.parametrize("scenario", ["normal_late_reply", "suspend_late_reply"])
@pytest.mark.parametrize("identity_prefix_bytes,pause_reply_escape", [
    (0, False), (2, False), (3, False), (10, False), (3, True),
], ids=["whole-late-frame", "dcs-introducer", "dcs-header", "startup-partial-frame", "utf8-reply-gap"])
def test_late_fragmented_replies_never_trigger_app_keys_at_startup_or_resume(
        tmp_path, scenario, identity_prefix_bytes, pause_reply_escape):
    output, result, queries, active_modes = run_pty(
        tmp_path, scenario, "run", False, "before", initial_flags=16,
        late_identity=True, identity_prefix_bytes=identity_prefix_bytes, pause_reply_escape=pause_reply_escape)
    cycles = 2 if scenario == "suspend_late_reply" else 1
    assert queries == cycles and result["terminal"]["identity_queries"] == cycles
    assert active_modes == [{1: False, 1007: False, "flags": 16}]
    assert result["keys"].count("é") == cycles and result["keys"].count("w") == cycles
    assert set(result["keys"]) == {"i", "é", "w", "down", "x"}
    assert not result["wheel_keys_enabled"] and result["view"] == "gpu"
    assert result["selected_gpu"] == 1 and result["error"] is None
    assert result["terminal"]["pushes"] == [] and result["terminal"]["pops"] == 0
    assert result["terminal"]["flags"] == 16
    assert "\x1b[>25u" not in output


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX terminal protocol")
@pytest.mark.parametrize("scenario", ["pre_query_error", "post_push_error"])
def test_startup_query_failure_restores_only_owned_modes_and_stack(tmp_path, scenario):
    output, result, queries, active_modes = run_pty(
        tmp_path, scenario, "run", True, "before", initial_cursor_mode=True, initial_flags=31)
    assert not active_modes and result["error"] == "RuntimeError"
    assert result["error_message"] == "synthetic fleet failure"
    pushed = scenario == "post_push_error"
    assert queries == (2 if pushed else 1)
    assert result["terminal"]["pushes"] == ([1] if pushed else [])
    assert result["terminal"]["pops"] == int(pushed)
    assert result["terminal"]["screen_exits"] == [
        {1: True, 1007: True, "flags": 31, "depth": 1}]
    assert "\x1b[?1049l" in output and "\x1b[>25u" not in output


def query_driver(monkeypatch, chunks, *, byte_budget=None):
    """Exercise the bounded query reader without an input thread or writer."""
    import termios
    from tui import fleet_terminal

    driver = FleetTerminalDriver.__new__(FleetTerminalDriver)
    driver.input_tty = True
    driver.fileno = 42
    driver._pending_input = bytearray(b"prior:")
    driver._reply_filter = FleetReplyFilter()
    if byte_budget is not None:
        driver.QUERY_BYTES = byte_budget
    original = [0, 0, 0, 0, 0, 0, [0] * 32]
    original_copy = copy.deepcopy(original)
    terminal_attributes = []
    reads = []
    writes = []
    incoming = list(chunks)
    driver.write = writes.append
    driver.flush = lambda: writes.append("flush")
    monkeypatch.setattr(termios, "tcgetattr", lambda fd: original)
    monkeypatch.setattr(termios, "tcsetattr", lambda fd, action, attrs: terminal_attributes.append(attrs))

    class QuerySelector:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def register(self, fd, event):
            assert fd == driver.fileno

        def select(self, timeout):
            return [(None, 1)] if incoming else []

    def read(fd, size):
        reads.append(size)
        chunk = incoming.pop(0)
        data, remainder = chunk[:size], chunk[size:]
        if remainder:
            incoming.insert(0, remainder)
        return data

    monkeypatch.setattr(fleet_terminal.selectors, "SelectSelector", QuerySelector)
    monkeypatch.setattr(fleet_terminal.os, "read", read)
    return driver, writes, terminal_attributes, original, original_copy, reads, incoming


@pytest.mark.parametrize("chunk_size", [1, 2, 7, 4096])
def test_query_preserves_utf8_escape_input_around_fragmented_out_of_order_replies(monkeypatch, chunk_size):
    packet = (b"\xc3\x1bP>|iTerm2 3.7.3\x1b\\\xa9\x1b[\x1b[?1007;2$yC"
              b"\x13\x1b[?31u\x11\r\x1b[?1;1$y")
    setup = query_driver(monkeypatch, [packet[offset:offset + chunk_size]
                                     for offset in range(0, len(packet), chunk_size)])
    driver, writes, attributes, original, original_copy, _, _ = setup
    replies = driver._query_terminal_state((1, 1007, "kitty", "identity"))
    assert replies == {1: 1, 1007: 2, "kitty": 31, "identity": b"iTerm2 3.7.3"}
    assert driver._pending_input == b"prior:\xc3\xa9\x1b[C\x13\x11\r"
    assert writes == ["\x1b[?1$p\x1b[?1007$p\x1b[?u\x1b[>0q", "flush"]
    assert attributes[-1] is original and original == original_copy
    assert attributes[0][-1] is not original[-1]


@pytest.mark.parametrize("packet", [
    b"a\x1b[?1007;1$", b"b\x1bP>|iTerm2 3.7.3\x1b",
    b"c\x1bP>|iTerm2 3.7.3\x07", b"d\x1b[?1;1$y",
    b"e\x1bP", b"f\x1bP>", b"g\x1b[",
])
def test_query_timeout_keeps_partial_and_unrequested_replies(monkeypatch, packet):
    driver, _, attributes, original, _, _, _ = query_driver(monkeypatch, [packet])
    assert driver._query_terminal_state((1007, "identity")) == {}
    assert driver._pending_input == b"prior:" + packet
    assert attributes[-1] is original


def test_query_keeps_reply_looking_paste_and_matches_only_real_replies(monkeypatch):
    pasted = (b"\x1b[200~\x1b[?1;1$y\x1b[?1007;1$y\x1b[?31u"
              b"\x1bP>|iTerm2 99.0\x1b\\\x1b[201~")
    replies = b"\x1b[?1;2$y\x1b[?1007;2$y\x1b[?0u\x1bP>|iTerm2 3.7.3\x1b\\"
    driver, _, _, _, _, _, _ = query_driver(monkeypatch, [pasted + replies])
    assert driver._query_terminal_state((1, 1007, "kitty", "identity")) == {
        1: 2, 1007: 2, "kitty": 0, "identity": b"iTerm2 3.7.3"}
    assert driver._pending_input == b"prior:" + pasted


def test_query_tracks_split_paste_markers_across_negotiation_rounds(monkeypatch):
    driver, _, _, _, _, _, _ = query_driver(monkeypatch, [b"0~\x1b[?31u\x1b[201~\x1b[?1u"])
    driver._pending_input = bytearray(b"\x1b[20")
    assert driver._query_terminal_state(("kitty",)) == {"kitty": 1}
    assert driver._pending_input == b"\x1b[200~\x1b[?31u\x1b[201~"


def test_resume_query_includes_retained_partial_paste_prefix(monkeypatch):
    driver, _, _, _, _, _, _ = query_driver(monkeypatch, [b"0~\x1b[?31u\x1b[201~\x1b[?1u"])
    driver._pending_input.clear()
    assert driver._reply_filter.feed(b"\x1b[20") == b""
    assert driver._query_terminal_state(("kitty",)) == {"kitty": 1}
    assert driver._reply_filter.feed(driver._pending_input) == b"\x1b[200~\x1b[?31u\x1b[201~"


def test_query_byte_budget_bounds_reads_without_losing_consumed_input(monkeypatch):
    driver, _, _, _, _, reads, incoming = query_driver(monkeypatch, [b"abcdefghijklmnop"], byte_budget=8)
    assert driver._query_terminal_state(("kitty",)) == {}
    assert reads == [8] and incoming == [b"ijklmnop"]
    assert driver._pending_input == b"prior:abcdefgh"


def test_parent_keyboard_suppression_does_not_filter_other_escape_writes(monkeypatch):
    from textual.drivers.linux_driver import LinuxDriver

    writes = []
    monkeypatch.setattr(LinuxDriver, "write", lambda driver, data: writes.append(data))
    driver = FleetTerminalDriver.__new__(FleetTerminalDriver)
    driver._parent_mode_change = True
    for data in FleetTerminalDriver.PARENT_KEYBOARD_WRITES:
        driver.write(data)
    unaffected = ["\x1b[?25l", "\x1b[>2u", "\x1b[>25u\x1b[?25l", "plain text"]
    for data in unaffected:
        driver.write(data)
    assert writes == unaffected
    driver._parent_mode_change = False
    driver.write("\x1b[>1u")
    assert writes[-1] == "\x1b[>1u"


def test_explicit_textual_keyboard_opt_out_keeps_legacy_input(monkeypatch):
    from tui import fleet_terminal

    writes = []
    driver = FleetTerminalDriver.__new__(FleetTerminalDriver)
    driver._scroll_query_done = False
    driver.write = writes.append
    monkeypatch.setattr(fleet_terminal.constants, "DISABLE_KITTY_KEY", True, raising=False)
    driver._query_terminal_state = lambda states: pytest.fail("keyboard opt-out should not negotiate")
    driver._enable_mouse_support()
    assert writes == ["\x1b[?1000l\x1b[?1002l\x1b[?1003l"]


def test_restore_flush_failure_does_not_pop_the_keyboard_stack_twice(monkeypatch):
    from textual.drivers.linux_driver import LinuxDriver

    writes = []
    monkeypatch.setattr(LinuxDriver, "write", lambda driver, data: writes.append(data))
    driver = FleetTerminalDriver.__new__(FleetTerminalDriver)
    driver._wheel_keys_enabled = True
    driver._previous_scroll_mode = False
    driver._previous_cursor_mode = True
    driver._kitty_protocol_open = True

    def fail_flush():
        raise RuntimeError("synthetic flush failure")

    driver.flush = fail_flush
    with pytest.raises(RuntimeError, match="synthetic flush failure"):
        driver._restore_scroll_mode()
    driver._restore_scroll_mode()
    assert writes == ["\x1b[?1007l\x1b[?1h\x1b[<u"]
    assert not driver._wheel_keys_enabled and not driver._kitty_protocol_open
