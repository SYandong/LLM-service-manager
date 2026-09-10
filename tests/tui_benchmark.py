# Generated-By: Codex / gpt-6-astra
"""Manual actual-PTY benchmark, synthetic loopback only; not an automatic test.

Run with the installed Textual interpreter:
  python tests/tui_benchmark.py --seconds 60 --output /tmp/llm-tui-benchmark
100% CPU means one logical core. No GPU, model request or deployed config is used.
"""
import argparse
import codecs
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import struct
import subprocess
import sys
import termios
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]


def snapshot():
    now = time.time()
    return {"sampled_at": now, "read_only": True, "errors": [],
            "gpus": [{"index": i, "used_gb": used, "total_gb": 140.4,
                      "free_gb": None if used is None else 140.4-used,
                      "managed_gb": used, "external_gb": 0} for i, used in enumerate([109.7, 35.2, 0, None])],
            "memory": {"budget_gb": 200, "sleeping_weights_gb": 86, "host_available_gb": 823},
            "models": [{"name": name, "state": state, "gpu": gpu, "resident_gb": resident,
                        "is_default": i == 0, "cold_start_seconds": 180 if state == "stopped" else None}
                       for i, (name, state, gpu, resident) in enumerate([
                           ("default-model", "awake", 0, 73), ("research-model", "sleeping", 1, 1.6),
                           ("analysis-model", "stopped", None, 0), ("unknown-model", "unknown", None, None)])],
            "activity": [{"model": "research-model", "last_request_at": now-300,
                          "requests_last_10m": 12, "by": ["lab"]}], "pins": [], "reserves": []}


def child(args):
    import runpy
    from types import SimpleNamespace
    sys.path.insert(0, str(ROOT))
    from textual.widgets import DataTable, Input, RichLog
    from tui.app import SchedulerApp
    api = SimpleNamespace(**runpy.run_path(str(ROOT/'cli/llm')))
    output = Path(args.output)

    class MeasuredApp(SchedulerApp):
        def __init__(self):
            super().__init__(api.SchedulerClient(args.url, timeout=2), api)
            self.frames = []
            self.clears = 0
            self.initial_clears = 0
            self.selected_at_start = None

        def on_mount(self):
            table = self.measured_table = self.query_one('#models', DataTable)
            self.measured_log = self.query_one('#events', RichLog)
            original_clear = table.clear
            def clear(*a, **kw):
                self.clears += 1
                return original_clear(*a, **kw)
            table.clear = clear
            screen = self.screen
            render = screen._compositor_refresh
            def measured():
                started = time.perf_counter()
                try:
                    return render()
                finally:
                    self.frames.append([time.monotonic(), (time.perf_counter()-started)*1000])
            screen._compositor_refresh = measured
            # Textual dispatches the inherited on_mount automatically.
            self.set_timer(1, self.ready)

        async def ready(self):
            await self.workers.wait_for_complete()
            assert self.snapshot is not None
            self.query_one('#models', DataTable).move_cursor(row=1, animate=False)
            self.call_after_refresh(self.capture)

        def capture(self):
            self.update_details()
            self.selected_at_start = self.selected_model()
            self.initial_clears = self.clears
            (output/'screen.svg').write_text(self.export_screenshot(title='llm · synthetic read-only fixture'))
            (output/'ready.json').write_text(json.dumps({'pid': os.getpid(), 'selected': self.selected_at_start}))

        async def on_unmount(self):
            # Capture mounted state before the base class closes the reader.
            metrics = {'frames_ms': self.frames, 'table_clears_after_mount': self.clears-self.initial_clears,
                       'selected_at_start': self.selected_at_start,
                       'selected_at_end': self.model_names[self.measured_table.cursor_row],
                       'log_lines': len(self.measured_log.lines),
                       'event_count': len(self.event_history)}
            await super().on_unmount()
            metrics['reader_closed'] = not self.event_reader.thread.is_alive()
            (output/'child.json').write_text(json.dumps(metrics, indent=2))

    app = MeasuredApp()
    app.run()
    if app._exception:
        raise app._exception


def cpu_seconds(pid):
    fields = Path('/proc/%s/stat' % pid).read_text().rsplit(')', 1)[1].split()
    return (int(fields[11])+int(fields[12])) / os.sysconf('SC_CLK_TCK')


def benchmark(args):
    import importlib.metadata
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)  # Never overwrite a previous measurement.
    stopped = threading.Event()
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            calls.append(self.path)
            if self.path == '/v1/state':
                body = json.dumps(snapshot()).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith('/v1/events'):
                self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
                try:
                    self.wfile.write(b': connected\n\n'); self.wfile.flush()
                    for i, kind in enumerate(['pin', 'sleep'], 1):
                        item = {'id': i, 'timestamp': time.time(), 'kind': kind, 'model': 'research-model',
                                'detail': {'fixture': True}}
                        self.wfile.write(('id: %s\ndata: %s\n\n' % (i, json.dumps(item))).encode())
                    self.wfile.flush()
                    while not stopped.wait(15):
                        self.wfile.write(b': heartbeat\n\n'); self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                self.send_error(404)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
    thread.start()
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', args.height, args.width, 0, 0))
    env = dict(os.environ, TERM='xterm-256color', COLORTERM='truecolor')
    env.pop('NO_COLOR', None)
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--child', '--output', str(output),
                                '--url', 'http://127.0.0.1:%s' % server.server_port],
                               stdin=slave, stdout=slave, stderr=slave, env=env, cwd=ROOT)
    os.close(slave)
    origin = time.monotonic()
    decoder = codecs.getincrementaldecoder('utf-8')('replace')
    recording = (output/'session.cast').open('w')
    recording.write(json.dumps({'version': 2, 'width': args.width, 'height': args.height,
                                'timestamp': int(time.time()), 'title': 'Actual PTY, synthetic loopback llm'})+'\n')
    raw = (output/'terminal.raw').open('wb')
    plain = ''
    def pump(timeout=0.05):
        nonlocal plain
        if select.select([master], [], [], timeout)[0]:
            try:
                data = os.read(master, 65536)
            except OSError:
                return
            raw.write(data)
            text = decoder.decode(data)
            recording.write(json.dumps([time.monotonic()-origin, 'o', text])+'\n')
            plain += text
    def wait_for(predicate, seconds=10):
        deadline = time.monotonic()+seconds
        while not predicate():
            if process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('PTY condition failed; inspect terminal.raw')
            pump()
    try:
        wait_for(lambda: (output/'ready.json').exists())
        started = time.monotonic(); cpu_start = cpu_seconds(process.pid)
        while time.monotonic()-started < args.seconds:
            pump(min(0.05, max(0, args.seconds-(time.monotonic()-started))))
        ended = time.monotonic(); cpu_end = cpu_seconds(process.pid)
        # Focus the real Input, then measure one key at a time. Match the actual
        # visible command string in the PTY payload, not an unrelated output byte.
        os.write(master, b'/')
        for _ in range(5): pump()
        os.write(master, b'BENCH')
        ansi = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)')
        wait_for(lambda: 'BENCH' in ansi.sub('', plain))
        visible = 'BENCH'
        latencies = []
        for key in '0123456789':
            plain = ''
            visible += key
            tick = time.monotonic()
            os.write(master, key.encode())
            wait_for(lambda: visible in ansi.sub('', plain), seconds=2)
            latencies.append((time.monotonic()-tick)*1000)
        # Tab moves focus out of the Input, then q uses the normal quit binding.
        os.write(master, b'\t')
        for _ in range(5): pump()
        os.write(master, b'q')
        wait_for(lambda: process.poll() is not None or (output/'child.json').exists())
        process.wait(timeout=5)
        result = json.loads((output/'child.json').read_text())
        frame_times = [duration for at, duration in result['frames_ms'] if started <= at <= ended]
        result.update(python=sys.version.split()[0], textual=importlib.metadata.version('textual'),
                      fixture='synthetic loopback HTTP + real SchedulerClient/EventReader and interactive Textual PTY',
                      idle_seconds=ended-started, cpu_seconds=cpu_end-cpu_start,
                      idle_cpu_one_core_percent=100*(cpu_end-cpu_start)/(ended-started),
                      cpu_definition='100% = one logical CPU core; child process user+system / monotonic wall time',
                      repaint_definition='one real PTY key write to receipt of the changed Input text in terminal output; excludes terminal emulator/remote network display latency',
                      key_to_visible_ms=latencies, idle_frame_count=len(frame_times),
                      idle_frame_max_ms=max(frame_times, default=0), requests=calls,
                      gpu_or_model_actions=False)
        result['acceptance'] = {'idle_cpu_lt_2_percent': result['idle_cpu_one_core_percent'] < 2,
                                'key_repaint_lt_50ms': max(latencies)<50,
                                'no_table_rebuild': result['table_clears_after_mount']==0,
                                'selection_preserved': result['selected_at_start']==result['selected_at_end'],
                                'bounded_log': result['log_lines']<=500, 'reader_closed': result['reader_closed']}
        (output/'metrics.json').write_text(json.dumps(result, indent=2)+'\n')
        print(json.dumps({k: v for k, v in result.items() if k not in ('frames_ms', 'requests')}, indent=2))
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        stopped.set(); server.shutdown(); server.server_close(); thread.join(2)
        os.close(master); recording.close(); raw.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=60)
    parser.add_argument('--width', type=int, default=100)
    parser.add_argument('--height', type=int, default=30)
    parser.add_argument('--output', required=True)
    parser.add_argument('--child', action='store_true')
    parser.add_argument('--url')
    args = parser.parse_args()
    if args.seconds <= 0:
        parser.error('--seconds must be positive')
    child(args) if args.child else benchmark(args)
