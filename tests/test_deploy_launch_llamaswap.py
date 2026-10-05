# Generated-By: Codex / gpt-6.1-sol
"""Opt-in CPU smoke for the built proxy, unmodified wrapper and real launcher."""

import contextlib
import io
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


@pytest.fixture
def proxy_binaries():
    names = ("LLMSVC_TEST_LLAMA_SWAP", "LLMSVC_TEST_VLLM_WRAPPER")
    paths = [os.environ.get(name) for name in names]
    if not all(paths):
        pytest.skip("set LLMSVC_TEST_LLAMA_SWAP and LLMSVC_TEST_VLLM_WRAPPER to isolated built binaries")
    binaries = [Path(path).resolve() for path in paths]
    assert all(binary.is_file() for binary in binaries)
    assert binaries[1].name == "vllm-wrapper", "launcher verifies the wrapper's process command name"
    return binaries


@contextlib.contextmanager
def http_server(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def placement_server_factory(tmp_path, placement_status):
    """Use real-controller 503 evidence when requested, with simulated controls."""
    if os.environ.get("LLMSVC_TEST_REAL_PLACEMENT") == "1" and placement_status == 503:
        return lambda handler, requests: real_placement_http_server(tmp_path, requests)
    return lambda handler, requests: http_server(handler)


@contextlib.contextmanager
def real_placement_http_server(tmp_path, requests):
    """Real A1 controller/sampler with synthetic observations and no actuators."""
    from llmsvc.actions import ManagedModelTransport
    from llmsvc.config import SchedulerConfig
    from llmsvc.leases import PlacementController
    from llmsvc.scheduler import Scheduler
    from llmsvc.server import SchedulerHTTPServer, SchedulerHandler
    from llmsvc.state import GPUState, MemoryState, ModelState, StateSnapshot
    from llmsvc.store import IntentStore

    if "placement_unplaceable_grace_seconds" not in SchedulerConfig.__dataclass_fields__:
        pytest.skip("LLMSVC_TEST_REAL_PLACEMENT requires the A1 no_feasible_gpu controller")
    config = SchedulerConfig("127.0.0.1", free_port(), read_only=False, placement_enabled=True,
                             state_db_path=str(tmp_path / "real-placement.sqlite"),
                             placement_gpus=[0], shared_external_threshold_gb=30,
                             placement_retry_after_seconds=17)
    store = IntentStore(config.state_db_path, action_lock=threading.RLock())

    def collect():
        sampled_at = time.time()
        time.sleep(0.05)
        return StateSnapshot(sampled_at=sampled_at,
                             gpus=(GPUState(0, total_gb=143.1, free_gb=82.7, external_gb=60.4),),
                             models=(ModelState("smoke", state="stopped", weights_gb=10, unit_active=False),),
                             memory=MemoryState(500, 0))

    actuators = []

    def forbidden_action(*args, **kwargs):
        actuators.append((args, kwargs))
        raise AssertionError("no actuator or unit probe is permitted for external-pressure refusal")

    scheduler = Scheduler(config, collect, store=store)
    transport = ManagedModelTransport(swap_url="http://127.0.0.1:1", systemctl="forbidden-systemctl",
                                      models={"smoke": {"util": 0.5, "weights_gb": 10}}, run=forbidden_action)
    scheduler.placement = PlacementController(scheduler, transport, probe=forbidden_action)
    request_threads = []
    responses = []

    class RecordingHandler(SchedulerHandler):
        def do_POST(self):
            request_threads.append(threading.current_thread())
            data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append((self.path, json.loads(data)))
            self.rfile = io.BytesIO(data)
            super().do_POST()

        def _json(self, status, payload, headers=None):
            responses.append((status, payload, headers))
            return super()._json(status, payload, headers=headers)

    server = SchedulerHTTPServer((config.listen_host, config.listen_port), scheduler)
    server.RequestHandlerClass = RecordingHandler
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    scheduler.start(sampling_only=True)
    thread.start()
    try:
        scheduler.await_initial_sample(time.monotonic() + 2)
        yield f"http://127.0.0.1:{server.server_port}"
        assert scheduler._sample_published >= 2 and not store.leases() and not actuators
        assert len(responses) == 1 and responses[0][0] == 503
        assert responses[0][1]["error"] == "no_feasible_gpu"
        assert responses[0][2]["Retry-After"] == "17"
        print(json.dumps({"real_placement": True, "published_samples": scheduler._sample_published,
                          "scheduler_http_status": responses[0][0], "leases": 0, "actuators": 0}))
    finally:
        server.shutdown()
        scheduler.stop()
        server.server_close()
        thread.join(timeout=3)
        for request_thread in request_threads:
            request_thread.join(timeout=3)
            assert not request_thread.is_alive(), "real scheduler request thread did not stop"
        assert not thread.is_alive() and not scheduler._thread.is_alive()
        store.close()


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def request(url, body=None, timeout=20):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        response = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.status, dict(response.headers), response.read().decode()


@pytest.mark.parametrize("placement_status,stream,journal", [
    (503, False, False), (409, False, False), (200, False, False),
    (503, True, False), (409, True, False), (200, True, False),
    (503, False, True), (503, True, True),
])
def test_built_proxy_launcher_startup_errors(tmp_path, proxy_binaries, placement_server_factory,
                                           placement_status, stream, journal, record_property):
    proxy, wrapper = proxy_binaries
    if journal:
        assert hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"), "journal smoke requires pidfd support"
        # Reject an unsupported runtime before creating a helper that needs
        # identity-bound cleanup even if a later request assertion fails.
        os.close(os.pidfd_open(os.getpid()))
    started = tmp_path / "simulated-backend-started"
    journal_identity = tmp_path / "journal-identity.json"
    scheduler_requests = []

    class QuietHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, status, payload):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    class Scheduler(QuietHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            scheduler_requests.append((self.path, json.loads(self.rfile.read(length))))
            if self.path == "/v1/place":
                if placement_status == 503:
                    # Include the scheduler's default grace in the measured
                    # request so the complete refusal still fits 20 seconds.
                    time.sleep(10)
                    self.respond(503, {"error": "no_feasible_gpu", "retry_after_seconds": 17,
                                       "message": "Placement GPUs are occupied",
                                       "gpus": [{"index": 0, "external_gb": 60.4, "free_gb": 82.7}]})
                elif placement_status == 409:
                    self.respond(409, {"error": "placement_timeout", "blockers": []})
                else:
                    self.respond(200, {"gpu": 0, "lease_id": "smoke-lease"})
            elif self.path == "/v1/place/smoke-lease/confirm":
                self.respond(200, {"ok": True})
            else:
                self.respond(404, {"error": "unexpected_path"})

    class Backend(QuietHandler):
        def do_GET(self):
            self.respond(200 if started.exists() else 503, {"ok": started.exists()})

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length)) if length else {}
            if self.path == "/v1/chat/completions" and started.exists():
                if body.get("stream"):
                    data = b'data: {"choices":[{"delta":{"content":"cpu-smoke-ok"}}]}\n\ndata: [DONE]\n\n'
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self.respond(200, {"choices": [{"message": {"content": "cpu-smoke-ok"}}]})
            else:
                self.respond(200 if started.exists() else 503, {"ok": started.exists()})

    tools = tmp_path / "fake-systemd"
    tools.mkdir()
    for name, program in {
        "systemctl": "import sys\nprint('not-found' if sys.argv[1] == 'show' else 'active')\n",
        "systemd-run": "import os\nfrom pathlib import Path\nPath(os.environ['LLMSVC_SMOKE_STARTED']).touch()\n",
    }.items():
        script = tools / name
        script.write_text(f"#!{sys.executable}\n" + program)
        script.chmod(0o700)
    if journal:
        journalctl = tools / "journalctl"
        journalctl.write_text(f"#!{sys.executable}\n" +
                             "import ctypes, json, os, signal, time\nfrom pathlib import Path\n"
                             "ctypes.CDLL(None).prctl(15, b'journalctl', 0, 0, 0)\n"
                             "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                             "pid = os.getpid()\n"
                             "ticks = Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()[19]\n"
                             "Path(os.environ['LLMSVC_SMOKE_JOURNAL']).write_text(json.dumps({'pid': pid, 'start_ticks': ticks}))\n"
                             "while True: time.sleep(1)\n")
        journalctl.chmod(0o700)
    launcher = Path(__file__).resolve().parents[1] / "deploy" / "vllm-launch"
    with placement_server_factory(Scheduler, scheduler_requests) as scheduler_url, http_server(Backend) as backend_url:
        launcher_config = tmp_path / "launcher.json"
        launcher_config.write_text(json.dumps({
            "scheduler_url": scheduler_url, "lock_dir": str(tmp_path / "locks"),
            "health_url_template": backend_url + "/health", "health_poll_seconds": 0.05,
            "startup_timeout_seconds": 10, "request_timeout_seconds": 15,
        }))
        wrapper_port, proxy_port = free_port(), free_port()
        command = [str(wrapper), "serve", "--listen", f"127.0.0.1:{wrapper_port}",
                   "--vllm-url", backend_url, "--wait-timeout", "120s"]
        if journal:
            command += ["--journal-unit", "vllm-smoke.service"]
        command += ["--",
                   sys.executable, str(launcher), "0.5", "vllm-smoke", "--config", str(launcher_config),
                   "--", "simulated-vllm", "--port", backend_url.rsplit(":", 1)[1]]
        config_path = tmp_path / "swap.yaml"
        config_path.write_text(json.dumps({
            "logToStdout": "both", "globalTTL": 0, "sendLoadingState": True,
            "healthCheckTimeout": 30, "models": {"smoke": {
                "cmd": shlex.join(command), "proxy": f"http://127.0.0.1:{wrapper_port}",
                "checkEndpoint": "/health",
                "env": [f"PATH={tools}:{os.environ.get('PATH', '')}", f"LLMSVC_SMOKE_STARTED={started}",
                        f"LLMSVC_SMOKE_JOURNAL={journal_identity}"],
            }},
        }))
        environment = dict(os.environ)
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            environment.pop(name, None)
        environment["NO_PROXY"] = "127.0.0.1,localhost"
        proxy_url = f"http://127.0.0.1:{proxy_port}"
        with (tmp_path / "proxy.log").open("w") as output:
            process = subprocess.Popen([str(proxy), "-config", str(config_path), "-listen", f"127.0.0.1:{proxy_port}"],
                                       stdout=output, stderr=subprocess.STDOUT, env=environment)
            try:
                deadline = time.monotonic() + 10
                while True:
                    assert process.poll() is None, (tmp_path / "proxy.log").read_text()
                    try:
                        if request(proxy_url + "/health", timeout=1)[0] == 200:
                            break
                    except urllib.error.URLError:
                        pass
                    assert time.monotonic() < deadline, "isolated proxy did not become ready"
                    time.sleep(0.05)
                begin = time.monotonic()
                status, headers, body = request(proxy_url + "/v1/chat/completions", {
                    "model": "smoke", "messages": [{"role": "user", "content": "CPU-only smoke"}], "stream": stream,
                })
                elapsed = time.monotonic() - begin
                assert elapsed <= 20, (elapsed, body)
                for name, value in {"placement_status": placement_status, "stream": stream,
                                    "journal": journal, "http_status": status,
                                    "elapsed_seconds": round(elapsed, 3)}.items():
                    record_property(name, value)
                if placement_status == 200:
                    assert status == 200 and "cpu-smoke-ok" in body
                    assert [path for path, _ in scheduler_requests] == ["/v1/place", "/v1/place/smoke-lease/confirm"]
                else:
                    expected_code = "no_gpu_available" if placement_status == 503 else "internal_error"
                    if stream:
                        assert status == 200 and headers["Content-Type"] == "text/event-stream"
                        frames = [json.loads(line[6:]) for line in body.splitlines()
                                  if line.startswith("data: ") and line != "data: [DONE]"]
                        envelope = next(frame for frame in frames if "error" in frame)
                        assert body.rstrip().endswith("data: [DONE]")
                    else:
                        assert status == (503 if placement_status == 503 else 500)
                        envelope = json.loads(body)
                        if placement_status == 503:
                            assert headers["Retry-After"] == "17"
                    assert envelope["error"]["code"] == expected_code
                    assert envelope["error"]["type"] == "server_error"
                    if placement_status == 503:
                        assert "GPU0: external 60.4 GiB" in envelope["error"]["message"]
                        assert "Retry after 17s." in envelope["error"]["message"]
                    assert not started.exists()
                    assert len(scheduler_requests) == 1
                if journal:
                    assert "wrapper_journal_stopped" in (tmp_path / "proxy.log").read_text()
                    identity = json.loads(journal_identity.read_text())
                    deadline = time.monotonic() + 3
                    while Path(f"/proc/{identity['pid']}").exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    assert not Path(f"/proc/{identity['pid']}").exists(), "owned journal helper survived refusal"
                print(json.dumps({"placement_status": placement_status, "stream": stream,
                                  "journal": journal, "http_status": status, "elapsed_seconds": round(elapsed, 3)}))
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
                # A failed regression must also settle its exact simulated
                # helper, without signalling a reused or unrelated PID.
                if journal_identity.exists():
                    identity = json.loads(journal_identity.read_text())
                    pid = identity["pid"]
                    try:
                        fd = os.pidfd_open(pid)
                    except ProcessLookupError:
                        pass
                    else:
                        try:
                            ticks = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
                            if ticks == identity["start_ticks"]:
                                signal.pidfd_send_signal(fd, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        finally:
                            os.close(fd)
