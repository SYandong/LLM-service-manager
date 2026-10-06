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

from test_tui_fleet import make_app, ready
from tui.fleet_app import FleetApp, FleetHelpDialog, GpuDetailDialog, GpuOverview


ROOT = Path(__file__).parents[1]


@pytest.fixture
def native_snapshot():
    return json.loads((ROOT / "tests/fixtures/fleet_gpu_overview.json").read_text())


@pytest.mark.parametrize("size", [(100, 30), (80, 24)])
def test_arrow_keys_scroll_rows_and_jk_select_gpu(native_snapshot, size):
    async def scenario():
        app, _ = make_app(native_snapshot)
        async with app.run_test(size=size) as pilot:
            await ready(app, pilot)
            viewport = app.query_one("#fleet-gpu-scroll", VerticalScroll)
            overview = app.query_one("#fleet-gpus", GpuOverview)
            for expected in (1, 2, 3, 4):
                await pilot.press("down")
                assert viewport.scroll_y == expected
                assert app.selected_gpu == 0
            await pilot.press("up")
            assert viewport.scroll_y == 3
            await pilot.press("j")
            assert app.selected_gpu == 1
            assert viewport.scroll_y == app._gpu_anchors[1]
            start = viewport.scroll_y
            await pilot.press("down")
            assert viewport.scroll_y == start + 1
            await pilot.press("right", "left")
            assert viewport.scroll_y == start + 1
            previous = app.selected_gpu - 1
            await pilot.press("k")
            assert app.selected_gpu == previous
            assert viewport.scroll_y == app._gpu_anchors[previous]
            assert overview.heading_gpu == previous
    asyncio.run(scenario())


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
snapshot["services"][0]["model"] = "NATIVE_COPY_BUFFER"
base, client = make_app(snapshot)
observations = {}

class Probe(FleetApp):
    async def on_event(self, event):
        if isinstance(event, events.Key) and event.key in ("enter", "ctrl+s", "ctrl+q"):
            # Observe driver decoding without Enter opening a modal or Ctrl+Q quitting.
            self.observed_keys.append(event.key)
            return
        return await super().on_event(event)

    def on_key(self, event):
        self.observed_keys.append(event.key)
        return super().on_key(event)

    def _handle_exception(self, error):
        observations["error"] = type(error).__name__
        observations["error_message"] = str(error)
        observations["cause"] = str(error.__cause__) if error.__cause__ else None
        return super()._handle_exception(error)

app = Probe(client, base.api, event_reader=base.event_reader)
app.observed_keys = []

def report(stage):
    Path(report_path).write_text(json.dumps(dict(observations, stage=stage, keys=app.observed_keys)))

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

async def operate(pilot):
    await ready(app, pilot)
    viewport = app.query_one("#fleet-gpu-scroll")
    observations["before_scroll"] = viewport.scroll_y
    report("ready")
    deadline = time.monotonic() + 3
    while "down" not in app.observed_keys and time.monotonic() < deadline:
        await pilot.pause(.02)
    await pilot.pause(.05)
    observations["after_scroll"] = viewport.scroll_y
    if scenario == "suspend":
        with app.suspend():
            report("suspended")
            time.sleep(.06)
        await pilot.pause(.1)
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


def run_pty(tmp_path, scenario, entrypoint, initial_mode, reply_position, query_reply="known"):
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
    sent_scroll = False
    active_modes = []
    deadline = time.monotonic() + 12
    try:
        while child.poll() is None:
            assert time.monotonic() < deadline, "synthetic terminal child did not finish"
            readable, _, _ = select.select([master], [], [], .02)
            if readable:
                output.extend(os.read(master, 65536))
            query_count = output.count(b"\x1b[?1007$p")
            while queries < query_count:
                state = (1 if initial_mode else 2) if query_reply == "known" else query_reply
                reply = b"\x1b[?1007;" + str(state).encode("ascii") + b"$y" if state is not None else b""
                if reply_position == "utf8":
                    packet = b"\xc3" + reply + b"\xa9"
                elif reply_position == "escape":
                    packet = b"\x1b[" + reply + b"C"
                elif reply_position == "carriage_return":
                    packet = b"\r" + reply
                elif reply_position == "flow_control":
                    packet = b"\x13" + reply + b"\x11"
                else:
                    packet = b"i" + reply if reply_position == "before" else reply + b"i"
                os.write(master, packet)
                queries += 1
            if not sent_scroll and report_path.exists():
                try:
                    state = json.loads(report_path.read_text())
                except json.JSONDecodeError:
                    continue
                if state.get("stage") == "ready":
                    mode = initial_mode
                    for match in CSI.finditer(output.decode("utf-8", errors="replace")):
                        params, _, command = match.groups()
                        if params == "?1007" and command in ("h", "l"):
                            mode = command == "h"
                    active_modes.append(mode)
                    # DEC1007 translates a terminal wheel tick to this key.
                    os.write(master, b"\x1b[B")
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
        return bytes(output).decode("utf-8", errors="replace"), json.loads(report_path.read_text()), queries, active_modes
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
    assert queries == (2 if scenario == "suspend" else 1)
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
    assert "\x1b]52;" not in output, "ordinary terminal copy must not require application OSC52"
    if scenario == "start_error":
        assert result["error"] == "RuntimeError"
    else:
        assert active_modes == [True], "alternate scroll was not enabled while the app was active"
        input_keys = {"utf8": ("é",), "escape": ("right",), "carriage_return": ("enter",),
                      "flow_control": ("ctrl+s", "ctrl+q")}.get(reply_position, ("i",))
        for input_key in input_keys:
            assert result["keys"].count(input_key) == queries, "interleaved non-query input was discarded"
        assert result["after_scroll"] == result["before_scroll"] + 1
        assert any("NATIVE_COPY_BUFFER" in frame for frame in terminal_frames(output))
        assert result["error"] == ("RuntimeError" if scenario == "app_error" else None)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX terminal protocol")
@pytest.mark.parametrize("query_reply", [0, 3, 4, None], ids=["unrecognized", "permanently-set", "permanently-reset", "timeout"])
def test_unknown_wheel_mode_is_left_unchanged(tmp_path, query_reply):
    initial_mode = query_reply == 3
    output, result, queries, active_modes = run_pty(tmp_path, "normal", "run", initial_mode, "before", query_reply)
    assert queries == 1 and active_modes == [initial_mode]
    assert not any(params == "?1007" and command in ("h", "l")
                   for params, _, command in (match.groups() for match in CSI.finditer(output)))
    assert "i" in result["keys"]
    assert result["after_scroll"] == result["before_scroll"] + 1 and result["error"] is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX terminal protocol")
def test_startup_and_cleanup_failure_preserve_exception_chain(tmp_path):
    output, result, queries, _ = run_pty(tmp_path, "dual_error", "run", False, "before")
    assert queries == 1
    assert "\x1b[?1007l" in output and "\x1b[?1049l" in output
    assert result["error"] == "RuntimeError" and result["error_message"] == "synthetic fleet failure"
    assert result["cause"] == "synthetic cleanup failure"
