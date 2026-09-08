#!/usr/bin/env python3
# Generated-By: Codex / gpt-6-astra
"""Isolated reload interruption smoke harness."""
import argparse
import contextlib
import datetime as _dt
import http.server
import json
import math
import signal
import shlex
import subprocess
import sys
import os
from pathlib import Path
import shutil
import socket
import socketserver
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid


SCHEMA_VERSION = 1
MAX_DEADLINE_SECONDS = 300.0
DEFAULT_TIMEOUT_SECONDS = 30.0


class HarnessError(RuntimeError):
    pass


def utc_now():
    return _dt.datetime.now(_dt.timezone.utc)


def timestamp_text(value=None):
    return (value or utc_now()).isoformat().replace("+00:00", "Z")


class Deadline:
    def __init__(self, seconds, clock=time.monotonic):
        seconds = float(seconds)
        if not math.isfinite(seconds) or seconds <= 0 or seconds > MAX_DEADLINE_SECONDS:
            raise HarnessError("deadline_seconds must be >0 and <=300")
        self.clock = clock
        self.start = clock()
        self.end = self.start + seconds

    def remaining(self):
        left = self.end - self.clock()
        if left <= 0:
            raise TimeoutError("hard deadline exceeded")
        return left

    def timeout(self, requested):
        return max(0.001, min(float(requested), self.remaining()))


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class UpstreamState:
    def __init__(self, chunks, chunk_delay, fail_after_chunks=None):
        self.chunks = chunks
        self.chunk_delay = chunk_delay
        self.fail_after_chunks = fail_after_chunks
        self.lock = threading.Lock()
        self.active = 0
        self.calls = []
        self.generation = 0
        self.stopping = threading.Event()

    def begin(self, path):
        with self.lock:
            self.active += 1
            generation = self.generation
            self.calls.append({"path": path, "started_at": timestamp_text(), "generation": generation})
            return generation

    def finish(self, generation, completed):
        with self.lock:
            self.active -= 1
            self.calls.append({"event": "finish", "completed": completed, "generation": generation,
                               "ended_at": timestamp_text()})

    def reload(self, mode, deadline):
        started = timestamp_text()
        if mode == "wait":
            while True:
                with self.lock:
                    if self.active == 0:
                        break
                if deadline.remaining() <= 0:
                    raise TimeoutError("wait-mode reload exceeded deadline")
                time.sleep(min(0.01, deadline.timeout(0.01)))
        with self.lock:
            self.generation += 1
            generation = self.generation
        return {"mode": mode, "started_at": started, "adopted_at": timestamp_text(),
                "generation": generation}


def make_handler(state):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            if self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")
                return
            self.send_error(404)

        def do_POST(self):
            if self.path.startswith("/sleep"):
                length = int(self.headers.get("Content-Length", "0") or "0")
                if length:
                    self.rfile.read(length)
                mode = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("mode", ["abort"])[0]
                with state.lock:
                    state.calls.append({"event": "sleep", "path": self.path, "query_mode": mode, "at": timestamp_text()})
                state.reload(mode, getattr(state, "deadline", Deadline(5)))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                body = b'{"sleeping":true}'
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path != "/v1/chat/completions":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length:
                self.rfile.read(length)
            generation = state.begin(self.path)
            completed = False
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                for index in range(state.chunks):
                    if state.stopping.wait(state.chunk_delay):
                        self.close_connection = True
                        return
                    with state.lock:
                        stale = generation != state.generation
                    if stale:
                        self.close_connection = True
                        return
                    if state.fail_after_chunks is not None and index >= state.fail_after_chunks:
                        self.close_connection = True
                        return
                    chunk = {"id": "fixture", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": "chunk-%d" % index}, "finish_reason": None}]}
                    self.wfile.write(("data: "+json.dumps(chunk)+"\n\n").encode("utf-8"))
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                completed = True
                self.close_connection = True
            except (BrokenPipeError, ConnectionResetError):
                return
            finally:
                state.finish(generation, completed)

    return Handler


def reserve_loopback_server(state):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, name="reload-smoke-upstream", daemon=True)
    thread.start()
    return server, thread, server.server_address[1]


def request_stream(url, request_id, timeout, model="fake"):
    started_mono = time.monotonic()
    result = {"id": request_id, "started_at": timestamp_text(), "started_mono": started_mono}
    body = json.dumps({"model": model, "stream": True, "messages": [{"role": "user", "content": "ping"}]}).encode()
    request = urllib.request.Request(url + "/v1/chat/completions", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
    seen_done = False
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result["http_status"] = response.status
            while True:
                line = response.readline(8192)
                if not line:
                    break
                if b"data: [DONE]" in line:
                    seen_done = True
                    break
            result["status"] = "completed" if seen_done else "truncated_stream"
    except urllib.error.HTTPError as exc:
        result["http_status"] = exc.code
        result["status"] = "http_5xx" if 500 <= exc.code <= 599 else "http_error"
        result["detail"] = "HTTPError"
        result["error_body"] = exc.read(4096).decode("utf-8", errors="replace")
    except socket.timeout:
        result["status"] = "timeout"
    except Exception as exc:
        result["status"] = "truncated_stream"
        result["detail"] = type(exc).__name__
    result["ended_mono"] = time.monotonic()
    result["ended_at"] = timestamp_text()
    result["duration_seconds"] = round(result["ended_mono"] - started_mono, 6)
    return result


def write_initial_config(directory, run_id, port):
    path = directory / "llama-swap.yaml"
    path.write_text("models:\n  fake-%s:\n    proxy: http://127.0.0.1:%d\n" % (run_id, port),
                    encoding="utf-8")
    return path


def atomic_candidate_rename(config_path, run_id):
    candidate = config_path.with_name(config_path.name + ".next")
    candidate.write_text(config_path.read_text(encoding="utf-8") + "metadata:\n  run_id: %s\n" % run_id,
                         encoding="utf-8")
    before = time.monotonic()
    os.replace(candidate, config_path)
    return {"renamed_at_mono": before, "renamed_at": timestamp_text()}


def load_config(path):
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise HarnessError("cannot read config: " + str(exc))
    except json.JSONDecodeError as exc:
        raise HarnessError("invalid config JSON: " + str(exc))
    if not isinstance(data, dict):
        raise HarnessError("config must be a JSON object")
    return data


def merged_settings(args):
    config = load_config(args.config)
    settings = {
        "deadline_seconds": config.get("deadline_seconds", args.deadline_seconds),
        "mode": config.get("mode", args.mode),
        "request_count": config.get("request_count", args.request_count),
        "chunks": config.get("chunks", args.chunks),
        "chunk_delay_seconds": config.get("chunk_delay_seconds", args.chunk_delay_seconds),
        "reload_after_seconds": config.get("reload_after_seconds", args.reload_after_seconds),
        "client_timeout_seconds": config.get("client_timeout_seconds", args.client_timeout_seconds),
        "fail_after_chunks": config.get("fail_after_chunks", args.fail_after_chunks),
        "llama_swap_binary": config.get("llama_swap_binary", args.llama_swap_binary),
        "wrapper_binary": config.get("wrapper_binary", args.wrapper_binary),
    }
    if settings["mode"] not in ("abort", "wait"):
        raise HarnessError("mode must be abort or wait")
    for key in ("request_count", "chunks"):
        settings[key] = int(settings[key])
        if settings[key] <= 0:
            raise HarnessError(key + " must be positive")
    for key in ("deadline_seconds", "chunk_delay_seconds", "reload_after_seconds", "client_timeout_seconds"):
        settings[key] = float(settings[key])
        if not math.isfinite(settings[key]) or settings[key] <= 0:
            raise HarnessError(key + " must be finite and positive")
    if settings["request_count"] > 64 or settings["chunks"] > 10000:
        raise HarnessError("request/chunk resource cap exceeded")
    if settings["deadline_seconds"] > MAX_DEADLINE_SECONDS:
        raise HarnessError("deadline_seconds must be <=300")
    if settings["fail_after_chunks"] is not None:
        settings["fail_after_chunks"] = int(settings["fail_after_chunks"])
        if settings["fail_after_chunks"] < 0:
            raise HarnessError("fail_after_chunks must be >=0")
    return settings


def real_binary_status(binary):
    if not binary:
        return {"status": "not_configured", "detail": "no llama-swap binary path supplied"}
    path = Path(binary)
    if not path.exists():
        return {"status": "not_executed", "binary": str(path), "detail": "configured path does not exist"}
    if not os.access(str(path), os.X_OK):
        return {"status": "not_executed", "binary": str(path), "detail": "configured path is not executable"}
    return {"status": "not_executed", "binary": str(path),
            "detail": "this harness records the path but does not certify real v252 reload or wrapper behavior"}


def summarize(results):
    counts = {"completed": 0, "http_5xx": 0, "truncated_stream": 0, "timeout": 0, "http_error": 0}
    for item in results:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return counts


def run_smoke(settings, output_dir):
    deadline = Deadline(settings["deadline_seconds"])
    run_id = settings.get("_run_id", uuid.uuid4().hex)
    tmpdir = Path(settings["_owned_tmpdir"]) if settings.get("_owned_tmpdir") else Path(tempfile.mkdtemp(prefix="llmsvc-reload-smoke-" + run_id + "-"))
    server = None
    real_process = None
    state = None
    model = "fake"
    real_status = real_binary_status(settings["llama_swap_binary"])
    try:
        state = UpstreamState(settings["chunks"], settings["chunk_delay_seconds"], settings["fail_after_chunks"])
        state.deadline = deadline
        server, thread, port = reserve_loopback_server(state)
        url = "http://127.0.0.1:%d" % port
        with urllib.request.urlopen(url + "/health", timeout=deadline.timeout(2)) as ready:
            ready.read()
        config_path = write_initial_config(tmpdir, run_id, port)
        if settings["llama_swap_binary"]:
            if not os.access(settings["llama_swap_binary"], os.X_OK):
                raise HarnessError("real baseline needs an executable llama-swap binary")
            if settings["mode"] == "wait" and not settings.get("wrapper_binary"):
                raise HarnessError("real wait mode requires the explicit wrapper transport fixture")
            model = "fake-" + run_id
            with socket.socket() as reserve:
                reserve.bind(("127.0.0.1", 0))
                swap_port = reserve.getsockname()[1]
            command = shlex.join([sys.executable, "-c", "import time;time.sleep(max(0," + repr(deadline.end) + "-time.monotonic()))"])
            model_config = {"cmd": command, "proxy": url, "useModelName": "fake"}
            if settings.get("wrapper_binary"):
                wrapper = settings["wrapper_binary"]
                if not os.access(wrapper, os.X_OK):
                    raise HarnessError("wrapper binary is not executable")
                upstream_url = url
                with socket.socket() as reservation:
                    reservation.bind(("127.0.0.1", 0)); wrapper_port = reservation.getsockname()[1]
                model_config["cmd"] = shlex.join([wrapper, "serve", "--vllm-url", upstream_url, "--listen", "127.0.0.1:"+str(wrapper_port), "--wait-timeout", "5s", "--", sys.executable, "-c", "import time;time.sleep(max(0,"+repr(deadline.end)+"-time.monotonic()))"])
                model_config["proxy"] = "http://127.0.0.1:"+str(wrapper_port)
                if settings["mode"] == "abort":
                    model_config["cmdStop"] = shlex.join([wrapper, "sleep", "--vllm-url", upstream_url, "--stop-pid", "${PID}"])
                else:
                    stop_code = "import os,signal,time,urllib.request; r=urllib.request.urlopen(urllib.request.Request("+repr(upstream_url+"/sleep?level=1&mode=wait")+",data=b'{}'),timeout=max(.05,"+repr(deadline.end)+"-time.monotonic()));r.read();r.close();os.kill(int('${PID}'),signal.SIGTERM)"
                    model_config["cmdStop"] = shlex.join([sys.executable, "-c", stop_code])
            actual_config = {"globalTTL": 0, "healthCheckTimeout": 15, "store": {"path": str(tmpdir/"activity.sqlite")}, "models": {model: model_config}}
            config_path.write_text(json.dumps(actual_config))
            subprocess.run([settings["llama_swap_binary"], "-config", str(config_path), "-validate"], check=True, capture_output=True, timeout=deadline.timeout(3))
            real_log = (tmpdir / "llama-swap.log").open("w")
            try:
                real_process = subprocess.Popen([settings["llama_swap_binary"], "-config", str(config_path), "-listen", "127.0.0.1:"+str(swap_port)], stdout=real_log, stderr=real_log, cwd=tmpdir)
            finally:
                real_log.close()
            url = "http://127.0.0.1:"+str(swap_port)
            while True:
                if real_process.poll() is not None:
                    raise HarnessError("isolated llama-swap exited before readiness")
                try:
                    with urllib.request.urlopen(url+"/v1/models", timeout=deadline.timeout(.3)) as reply:
                        if model in [m["id"] for m in json.load(reply).get("data", [])]:
                            break
                except (urllib.error.URLError, TimeoutError):
                    pass
                time.sleep(deadline.timeout(.02))
            real_status = {"status": "executed_isolated_baseline", "binary": settings["llama_swap_binary"], "pid": real_process.pid, "port": swap_port, "wrapper_executed": bool(settings.get("wrapper_binary")), "vllm_backend_mock": True}

        last_zero = {"last_safe_check_mono": time.monotonic(), "last_safe_check_at": timestamp_text(),
                     "source": "owned_mock_counter_zero_before_fixture_requests", "certified_quiet": False,
                     "limitation": "not a #53 continuous production quiet source"}
        results = []
        lock = threading.Lock()
        threads = []

        def worker(index):
            result = request_stream(url, "req-%03d" % index, deadline.timeout(settings["client_timeout_seconds"]), model=model)
            with lock:
                results.append(result)

        for index in range(settings["request_count"]):
            thread = threading.Thread(target=worker, args=(index,), daemon=True)
            thread.start()
            threads.append(thread)

        if real_process is not None:
            while sum("path" in row for row in state.calls) < settings["request_count"]:
                if all(not worker.is_alive() for worker in threads):
                    raise HarnessError("fixture requests failed before reaching upstream: "+json.dumps(results))
                time.sleep(deadline.timeout(.01))
        time.sleep(min(settings["reload_after_seconds"], deadline.timeout(settings["reload_after_seconds"])))
        last_safe = {**last_zero, "active_at_reload": state.active}
        if real_process is not None and state.active == 0:
            raise HarnessError("real reload case has no in-flight fixture stream")
        if real_process is None:
            rename = atomic_candidate_rename(config_path, run_id)
            trigger = {"trigger": "mock_generation_reload", "triggered_at_mono": time.monotonic(), "triggered_at": timestamp_text()}
            adoption = state.reload(settings["mode"], deadline)
        else:
            adopted_model = "adopted-"+run_id
            actual_config["models"][adopted_model] = dict(actual_config["models"][model])
            candidate = config_path.with_suffix(".next")
            candidate.write_text(json.dumps(actual_config))
            rename = {"renamed_at_mono": time.monotonic(), "renamed_at": timestamp_text()}
            os.replace(candidate, config_path)
            trigger = {"trigger": "SIGHUP_to_owned_process", "triggered_at_mono": time.monotonic(), "triggered_at": timestamp_text()}
            os.kill(real_process.pid, signal.SIGHUP)
            while True:
                with urllib.request.urlopen(url+"/v1/models", timeout=deadline.timeout(.5)) as reply:
                    visible = [m["id"] for m in json.load(reply).get("data", [])]
                if adopted_model in visible:
                    break
                time.sleep(deadline.timeout(.02))
            adoption = {"mode": "real_swap_baseline", "adopted_at": timestamp_text(), "adopted_mono": time.monotonic(), "verified_model_id": adopted_model}

        for thread in threads:
            thread.join(deadline.timeout(settings["client_timeout_seconds"]))
        if settings.get("wrapper_binary"):
            until = min(deadline.end, time.monotonic()+1)
            while sum(row.get("event")=="sleep" for row in state.calls)<2 and time.monotonic()<until:
                time.sleep(deadline.timeout(.01))

        evidence = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "status": "ok",
            "dry_run": False,
            "mock_vs_real": {
                "mock_upstream": "executed",
                "real_llama_swap": real_status,
            },
            "scope": {
                "production_endpoint": False,
                "production_config": False,
                "certifies_v252_continuous_quiet": False,
                "certifies_real_wrapper_reload": False,
            },
            "settings": settings,
            "resources": {"tmpdir": str(tmpdir), "upstream_url": url, "upstream_port": port,
                          "config_path": str(config_path)},
            "reload_timestamps": {**last_safe, **rename, **trigger, **adoption},
            "request_summary": summarize(results),
            "requests": sorted(results, key=lambda item: item["id"]),
            "upstream_calls": state.calls,
        }
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / ("reload-smoke-%s.json" % run_id)
        output.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
        evidence["output"] = str(output)
        return evidence
    finally:
        if state is not None:
            state.stopping.set()
        if real_process is not None and real_process.poll() is None:
            real_process.terminate()
            try:
                real_process.wait(timeout=.5)
            except subprocess.TimeoutExpired:
                real_process.kill();real_process.wait(timeout=.5)
        if server is not None:
            with contextlib.suppress(Exception):
                server.shutdown()
                server.server_close()
        if not settings.get("_owned_tmpdir"):
            shutil.rmtree(tmpdir, ignore_errors=True)


def owned_session_members(session_id):
    members=[]
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            fields=(path/"stat").read_text().rsplit(") ",1)[1].split()
            if int(fields[3])==session_id and fields[0]!="Z":
                members.append((int(path.name),fields[19]))
        except (OSError,ValueError,IndexError):
            pass
    return members


def kill_owned_session(session_id):
    """Also reap descendants moved to other process groups in this session."""
    for pid,started in owned_session_members(session_id):
        descriptor=None
        try:
            descriptor=os.pidfd_open(pid)
            fields=(Path("/proc")/str(pid)/"stat").read_text().rsplit(") ",1)[1].split()
            if int(fields[3])==session_id and fields[19]==started:
                signal.pidfd_send_signal(descriptor,signal.SIGKILL)
        except (ProcessLookupError,FileNotFoundError):
            pass
        finally:
            if descriptor is not None:
                os.close(descriptor)


def bounded_smoke(settings, output_dir):
    """An owned subprocess group makes socket/thread deadlines enforceable."""
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise HarnessError("requires Linux pidfd-enabled Python (the tested target is Python 3.10); refusing before creating workers")
    descriptor = os.pidfd_open(os.getpid())
    os.close(descriptor)
    deadline = Deadline(settings["deadline_seconds"])
    cleanup_reserve = min(2.0, settings["deadline_seconds"] / 4)
    run_id = uuid.uuid4().hex
    tmpdir = Path(tempfile.mkdtemp(prefix="llmsvc-reload-smoke-"+run_id+"-"))
    process = None
    evidence = None
    try:
        worker_settings = dict(settings, _owned_tmpdir=str(tmpdir), _run_id=run_id)
        worker_settings["deadline_seconds"] = max(.001, deadline.remaining()-cleanup_reserve)
        config = tmpdir/"worker.json";config.write_text(json.dumps(worker_settings));config.chmod(0o600)
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker", str(config)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=max(.001, deadline.remaining()-cleanup_reserve))
            if process.returncode:
                raise HarnessError("isolated worker failed: "+stderr[-500:])
            path = tmpdir/("reload-smoke-"+run_id+".json")
            evidence = json.loads(path.read_text())
        except subprocess.TimeoutExpired:
            evidence = {"schema_version": SCHEMA_VERSION, "run_id": run_id, "status": "deadline_exceeded",
                        "request_summary": {}, "requests": [], "resources": {"tmpdir": str(tmpdir)},
                        "mock_vs_real": {"real_llama_swap": {"status": "not_completed"}},
                        "scope": {"production_endpoint": False, "certifies_v252_continuous_quiet": False}}
    finally:
        if process is not None:
            kill_owned_session(process.pid)
            if process.poll() is None:
                process.communicate(timeout=max(.05, deadline.end-time.monotonic()))
            if owned_session_members(process.pid):
                # SIGKILL is asynchronous; reserve a bounded cleanup interval.
                until=min(deadline.end,time.monotonic()+.2)
                while owned_session_members(process.pid) and time.monotonic()<until:
                    time.sleep(.005)
            if owned_session_members(process.pid):
                raise HarnessError("owned test session did not terminate; preserve temporary evidence")
        shutil.rmtree(tmpdir)
    evidence["cleanup"] = {"owned_process_exited": process.returncode is not None, "owned_tmpdir_removed": not tmpdir.exists()}
    evidence["elapsed_seconds"] = time.monotonic()-deadline.start
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    output = output_dir/("reload-smoke-"+run_id+".json")
    with output.open("x") as handle:
        output.chmod(0o600);json.dump(evidence, handle, indent=2, sort_keys=True)
    evidence["output"] = str(output)
    return evidence


def dry_run_plan(settings, output_dir):
    return {
        "schema_version": SCHEMA_VERSION,
        "dry_run": True,
        "would": [{
            "kind": "isolated_reload_smoke",
            "resources": "new uuid, temporary directory, OS-assigned 127.0.0.1 port, fake streaming upstream",
            "output_dir": str(output_dir),
            "deadline_seconds": settings["deadline_seconds"],
        }],
        "mock_vs_real": {
            "mock_upstream": "not_executed",
            "real_llama_swap": real_binary_status(settings["llama_swap_binary"]),
        },
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="optional JSON settings file")
    parser.add_argument("--output-dir", type=Path, default=Path("var/reload-smoke"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--deadline-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--mode", choices=("abort", "wait"), default="abort")
    parser.add_argument("--request-count", type=int, default=8)
    parser.add_argument("--chunks", type=int, default=12)
    parser.add_argument("--chunk-delay-seconds", type=float, default=0.05)
    parser.add_argument("--reload-after-seconds", type=float, default=0.12)
    parser.add_argument("--client-timeout-seconds", type=float, default=5.0)
    parser.add_argument("--fail-after-chunks", type=int)
    parser.add_argument("--llama-swap-binary")
    parser.add_argument("--wrapper-binary")
    return parser.parse_args(argv)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv)==2 and argv[0]=="--worker":
        settings=json.loads(Path(argv[1]).read_text())
        run_smoke(settings, Path(settings["_owned_tmpdir"]))
        return 0
    args = parse_args(argv)
    try:
        settings = merged_settings(args)
        if args.dry_run:
            print(json.dumps(dry_run_plan(settings, args.output_dir), indent=2, sort_keys=True))
            return 0
        evidence = bounded_smoke(settings, args.output_dir)
        print(json.dumps({"status": evidence["status"], "run_id": evidence["run_id"],
                          "output": evidence["output"], "request_summary": evidence["request_summary"],
                          "mock_vs_real": evidence["mock_vs_real"]}, indent=2, sort_keys=True))
        return 0 if evidence["status"] == "ok" else 2
    except (HarnessError, OSError, TimeoutError, ValueError, subprocess.SubprocessError) as exc:
        print("reload_smoke: " + str(exc), file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
