#!/usr/bin/env python3
# Generated-By: Codex / gpt-6-astra
"""Measure pinned v252 watcher/MCP visibility using owned CPU fixtures only."""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

import yaml

# Support direct execution without changing the installed application package.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deploy.reload_smoke import (Deadline, HarnessError, UpstreamState,
                                kill_owned_session, owned_session_members,
                                request_stream, reserve_loopback_server,
                                summarize, timestamp_text)

PINNED_SHA256 = "32aea60b5c1be987c27dde6ea4aaa84f9be7ad93eaede011295fad1e276e80ea"
PINNED_COMMIT = "e31a1adee494bb7a578e2a97ec891b3e809899dc"
GENERATION_PATH = "macros.llmsvc_reload_generation"
SCENARIOS = ("adopt", "delayed-stop", "stranded-stop", "failed-stop",
             "invalid-candidate", "same-stat", "missing-witness", "restart", "deadline")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def identity(pid):
    fields = (Path("/proc") / str(pid) / "stat").read_text().rsplit(") ", 1)[1].split()
    return {"pid": pid, "start_ticks": fields[19], "state": fields[0]}


def same_process(left, right):
    return (left["pid"], left["start_ticks"]) == (right["pid"], right["start_ticks"])


def parse_generation(envelope, request_id):
    """Accept only the complete pinned native scalar response, never HTTP alone."""
    if (not isinstance(envelope, dict) or envelope.get("jsonrpc") != "2.0"
            or envelope.get("id") != request_id or "error" in envelope):
        raise HarnessError("invalid or mismatched JSON-RPC response")
    result = envelope.get("result")
    if not isinstance(result, dict) or result.get("isError", False) is not False:
        raise HarnessError("native tool error")
    content = result.get("content")
    if (not isinstance(content, list) or len(content) != 1
            or not isinstance(content[0], dict) or content[0].get("type") != "text"):
        raise HarnessError("unexpected native content shape")
    text = content[0].get("text", "")
    prefix = 'Current llama-swap configuration at "' + GENERATION_PATH + '" (credentials redacted, values resolved):\n\n```yaml\n'
    if not isinstance(text, str) or not text.startswith(prefix) or not text.endswith("\n```\n"):
        raise HarnessError("missing, truncated or unexpected YAML envelope")
    value = yaml.safe_load(text[len(prefix):-len("\n```\n")])
    if not isinstance(value, str) or not re.fullmatch(r"gen_[0-9a-f]{32}", value):
        raise HarnessError("missing or invalid generation scalar")
    return value


def mcp_read(url, deadline, *, protocol="2026-07-28", name="config__get_config"):
    request_id = uuid.uuid4().hex
    body = {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
            "params": {"name": name, "arguments": {"path": GENERATION_PATH}}}
    headers = {"Content-Type": "application/json", "Mcp-Protocol-Version": protocol,
               "Mcp-Method": "tools/call", "Mcp-Name": name}
    record = {"at": timestamp_text(), "monotonic": time.monotonic(),
              "endpoint": "/api/mcp", "method": "POST", "headers": headers, "request": body}
    request = urllib.request.Request(url + "/api/mcp", data=json.dumps(body).encode(), headers=headers)
    try:
        # No proxies or redirects: the witness must come from the owned listener.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=deadline.timeout(.5)) as response:
            record["http_status"] = response.status
            data = response.read(65537)
        if len(data) > 65536:
            raise HarnessError("oversized native response")
        record["response"] = json.loads(data)
        record["generation"] = parse_generation(record["response"], request_id)
    except urllib.error.HTTPError as exc:
        record["http_status"] = exc.code
        record["error"] = str(exc)
        # Do not chase redirects or unbounded error bodies.
        with exc:
            try:
                record["error_body"] = exc.read(4096).decode("utf-8", errors="replace")
            except (OSError, http.client.HTTPException) as body_error:
                record["error_body_read_error"] = str(body_error)
    except (OSError, ValueError, HarnessError, yaml.YAMLError, http.client.HTTPException) as exc:
        record["error"] = str(exc)
    record["response_received_at"] = timestamp_text()
    record["response_received_mono"] = time.monotonic()
    return record


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def binding_state(baseline, current, expected_generation, witness, expected_hash, actual_hash,
                  *, expired=False, settlement=False):
    """Evidence classification only: never authorizes a production transaction."""
    reasons = []
    if not same_process(baseline, current):
        reasons.append("service_identity_changed")
    if actual_hash != expected_hash:
        reasons.append("candidate_file_digest_changed")
    if witness.get("generation") != expected_generation or witness.get("error"):
        reasons.append("native_generation_not_confirmed")
    if expired:
        reasons.append("deadline_expired")
    visible = not reasons
    if not settlement:
        reasons.append("independent_old_server_settlement_unavailable")
    return {"candidate_generation_visible": visible, "settlement_confirmed": settlement,
            "reconciliation_required": bool(reasons), "reasons": reasons,
            "barrier_must_remain": bool(reasons)}


def process_fixture(directory, end):
    directory = Path(directory)
    (directory / "old-process.json").write_text(json.dumps(identity(os.getpid())))
    while time.monotonic() < end:
        time.sleep(.05)


def stop_fixture(directory, mode, pid, end):
    directory = Path(directory)
    me = identity(os.getpid())
    me.update(at=timestamp_text(), monotonic=time.monotonic())
    (directory / "stop-enter.json").write_text(json.dumps(me))
    if mode == "failed-stop":
        (directory / "stop-exit.json").write_text(json.dumps({"exit_code": 7, "at": timestamp_text()}))
        return 7
    delay = 40 if mode == "stranded-stop" else 2 if mode == "delayed-stop" else 0
    until = min(end, time.monotonic() + delay)
    while time.monotonic() < until:
        time.sleep(max(0, min(.05, until - time.monotonic())))
    # PID macro is bound back to the fixture's recorded process identity.
    previous = json.loads((directory / "old-process.json").read_text())
    try:
        descriptor = os.pidfd_open(pid)
        try:
            if not same_process(previous, identity(pid)):
                raise HarnessError("refuse reused fixture PID")
            signal.pidfd_send_signal(descriptor, signal.SIGTERM)
        finally:
            os.close(descriptor)
    except ProcessLookupError:
        pass
    (directory / "stop-exit.json").write_text(json.dumps({"exit_code": 0, "at": timestamp_text()}))
    return 0


def fixture_resource(directory, name):
    path = directory / name
    if not path.exists():
        return {"recorded": False, "alive": None}
    record = json.loads(path.read_text())
    try:
        current = identity(record["pid"])
        alive = same_process(record, current) and current["state"] != "Z"
    except FileNotFoundError:
        alive = False
    return {"recorded": True, "identity": record, "alive": alive}


def capture_requests(results, count=2):
    """Freeze this observation; missing clients must not become zero errors."""
    by_id = {row["id"]: dict(row) for row in results}
    return [by_id.get("req-" + str(index),
                      {"id": "req-" + str(index), "status": "pending_at_capture"})
            for index in range(count)]


def replace_once(config, data, preserve_stat=False):
    before = config.stat()
    candidate = config.with_suffix(".next")
    with candidate.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    if preserve_stat:
        if len(data) != before.st_size:
            raise HarnessError("same-stat fixture must preserve size")
        os.utime(candidate, ns=(before.st_atime_ns, before.st_mtime_ns))
    elif (candidate.stat().st_mtime_ns, candidate.stat().st_size) == (before.st_mtime_ns, before.st_size):
        raise HarnessError("candidate is invisible to the watcher; no replacement")
    event = {"at": timestamp_text(), "monotonic": time.monotonic(),
             "prior_mtime_ns": before.st_mtime_ns, "prior_size": before.st_size,
             "candidate_sha256": hashlib.sha256(data).hexdigest(),
             "candidate_mtime_ns": candidate.stat().st_mtime_ns, "candidate_size": len(data)}
    os.replace(candidate, config)
    return event


def worker(settings, directory):
    deadline = Deadline(settings["deadline_seconds"])
    scenario = settings["scenario"]
    evidence = {"schema_version": 1, "generated_by": "Codex / gpt-6-astra", "scenario": scenario,
                "status": "ok", "binary_sha256": sha256(settings["binary"]),
                "upstream_commit": PINNED_COMMIT, "started_at": timestamp_text(),
                "scope": {"CPU_mock_backend": True, "production_mutation": False, "GPU": False,
                          "certified_quiet": False, "production_notifier": False},
                "single_trigger": {"mode": "watcher_only", "atomic_replacements": 0,
                                   "reload_signals": 0, "fallback_writes": 0},
                "native_samples": [], "request_results": []}
    state = UpstreamState(200, .02)
    server = None
    process = None
    log = None
    try:
        if evidence["binary_sha256"] != PINNED_SHA256:
            raise HarnessError("binary does not match measured pinned v252 SHA256")
        server, server_thread, port = reserve_loopback_server(state)
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            swap_port = reserve.getsockname()[1]
        url = "http://127.0.0.1:" + str(swap_port)
        old_g, new_g = "gen_" + uuid.uuid4().hex, "gen_" + uuid.uuid4().hex
        script = str(Path(__file__).resolve())
        command = shlex.join([sys.executable, script, "--fixture-process", str(directory), str(deadline.end)])
        stop = shlex.join([sys.executable, script, "--fixture-stop", str(directory), scenario, "${PID}", str(deadline.end)])
        config_data = {"globalTTL": 0, "healthCheckTimeout": 5,
                       "macros": {"llmsvc_reload_generation": old_g},
                       "store": {"path": str(directory / "activity.sqlite")},
                       "models": {"fixture": {"cmd": command, "cmdStop": stop,
                                              "proxy": "http://127.0.0.1:" + str(port)}}}
        if scenario == "missing-witness":
            config_data["macros"] = {}
        config = directory / "config.yaml"
        config.write_text(json.dumps(config_data, sort_keys=True))
        log_path = directory / "swap.log"
        log = log_path.open("w")
        argv = [settings["binary"], "-config", str(config), "-watch-config", "-listen", "127.0.0.1:" + str(swap_port)]
        evidence["argv"] = argv
        subprocess.run([settings["binary"], "-config", str(config), "-validate"], check=True, capture_output=True, timeout=deadline.timeout(3))
        process = subprocess.Popen(argv, stdout=log, stderr=log, cwd=directory)
        evidence["service_identity"] = baseline = identity(process.pid)
        evidence["ports"] = {"mock_backend": port, "swap": swap_port}
        while True:
            if process.poll() is not None:
                raise HarnessError("owned swap failed during startup")
            sample = mcp_read(url, deadline)
            if sample.get("generation") == old_g or (scenario == "missing-witness" and sample.get("http_status") == 200):
                evidence["native_samples"].append(sample)
                break
            time.sleep(deadline.timeout(.05))
        if scenario == "missing-witness":
            evidence["outcome"] = "blocked_before_write_missing_native_generation"
            return evidence
        # Capture actual HTTP200 protocol/tool errors without replacing the config.
        evidence["negative_protocol_samples"] = [mcp_read(url, deadline, protocol="wrong"),
                                                   mcp_read(url, deadline, name="config__missing")]
        # Allow initialization; elapsed time alone does not prove watcher baseline.
        time.sleep(deadline.timeout(2.1))
        evidence["last_fixture_zero"] = {"at": timestamp_text(), "monotonic": time.monotonic(), "active": state.active, "certified_quiet": False}
        requests = []
        for index in range(2):
            thread = threading.Thread(target=lambda index=index: evidence["request_results"].append(
                request_stream(url, "req-" + str(index), deadline.timeout(10), model="fixture")), daemon=True)
            thread.start()
            requests.append(thread)
        while state.active < 2:
            if all(not thread.is_alive() for thread in requests):
                raise HarnessError("fixture requests failed before reaching mock backend")
            time.sleep(deadline.timeout(.01))
        evidence["active_at_replace"] = state.active
        config_data["macros"]["llmsvc_reload_generation"] = new_g
        candidate = json.dumps(config_data, sort_keys=True).encode()
        if scenario == "invalid-candidate":
            candidate = b'models: [unterminated\n'
        evidence["binding"] = {"old_generation": old_g, "new_generation": new_g,
                               "old_file_sha256": sha256(config), "candidate_sha256": hashlib.sha256(candidate).hexdigest(),
                               "service_identity": baseline}
        (directory / "binding.json").write_text(json.dumps(dict(evidence["binding"], config_committed=False, barrier_must_remain=True)))
        evidence["replacement"] = replace_once(config, candidate, scenario == "same-stat")
        (directory / "binding.json").write_text(json.dumps(dict(evidence["binding"], config_committed=True, barrier_must_remain=True)))
        evidence["single_trigger"]["atomic_replacements"] = 1
        verification = Deadline(min(deadline.remaining(), .05 if scenario == "deadline" else 6))
        verify_end = verification.end
        evidence["verification_deadline_mono"] = verify_end
        if scenario == "restart":
            # Deliberate owned-process restart injection, never an adoption fallback.
            process.kill()
            process.wait(timeout=deadline.timeout(1))
            process = subprocess.Popen(argv, stdout=log, stderr=log, cwd=directory)
            evidence["injected_restart"] = True
        while True:
            sample = mcp_read(url, verification)
            sample["file_sha256"] = sha256(config)
            sample["service_identity"] = identity(process.pid)
            sample["old_model_process"] = fixture_resource(directory, "old-process.json")
            sample["old_stop_command"] = fixture_resource(directory, "stop-enter.json")
            evidence["native_samples"].append(sample)
            if sample.get("generation") == new_g or time.monotonic() >= verify_end:
                break
            time.sleep(max(0, min(.1, verify_end - time.monotonic())))
        evidence["classification"] = binding_state(baseline, identity(process.pid), new_g, sample,
            evidence["binding"]["candidate_sha256"], sha256(config), expired=time.monotonic() >= verify_end)
        evidence["outcome"] = "candidate_visible_settlement_unproven" if evidence["classification"]["candidate_generation_visible"] else "reconciliation_required"
        # Later diagnostic phase uses the remaining worker budget, not the expired
        # verification budget. It never upgrades a failed classification.
        evidence["diagnostic_phase_started_mono"] = time.monotonic()
        # Observe generic completion separately; it is not a native settlement ACK.
        observe_seconds = 33 if scenario in ("failed-stop", "stranded-stop") else 3
        observe_end = min(deadline.end, time.monotonic() + observe_seconds)
        while time.monotonic() < observe_end:
            text = log_path.read_text()
            if "configuration reloaded" in text:
                evidence["generic_completion_observed"] = {"at": timestamp_text(), "monotonic": time.monotonic(),
                    "old_model_process": fixture_resource(directory, "old-process.json"),
                    "old_stop_command": fixture_resource(directory, "stop-enter.json"),
                    "is_independent_settlement_witness": False}
                break
            time.sleep(deadline.timeout(.05))
        for thread in requests:
            thread.join(deadline.timeout(10))
        evidence["request_results"] = capture_requests(evidence["request_results"])
        evidence["request_summary"] = summarize(evidence["request_results"])
        evidence["observed_reload_log_count"] = log_path.read_text().count("reloading configuration")
        evidence["stop_exit"] = json.loads((directory / "stop-exit.json").read_text()) if (directory / "stop-exit.json").exists() else None
        evidence["independent_settlement_witness"] = None
        return evidence
    finally:
        evidence["ended_at"] = timestamp_text()
        evidence["log"] = (directory / "swap.log").read_text() if (directory / "swap.log").exists() else ""
        state.stopping.set()
        if server is not None:
            server.shutdown()
            server.server_close()
        if log is not None:
            log.close()
        # Supervisor owns complete session cleanup, including cmdStop's process group.


def bounded_run(settings, output_dir):
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise HarnessError("requires Linux pidfd-enabled Python 3.10")
    descriptor = os.pidfd_open(os.getpid())
    os.close(descriptor)
    deadline = Deadline(settings["deadline_seconds"])
    reserve = min(2, settings["deadline_seconds"] / 4)
    directory = Path(tempfile.mkdtemp(prefix="llmsvc-watcher-witness-"))
    process = None
    evidence = {"status": "worker_failed", "scenario": settings["scenario"], "scope": {"production_mutation": False, "GPU": False}}
    try:
        config = directory / "worker.json"
        config.write_text(json.dumps(dict(settings, deadline_seconds=deadline.remaining() - reserve)))
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker", str(config)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=max(.001, deadline.remaining() - reserve))
            if (directory / "result.json").exists():
                evidence = json.loads((directory / "result.json").read_text())
            if process.returncode:
                evidence.update(status="worker_failed", error=stderr[-2000:])
        except subprocess.TimeoutExpired:
            evidence.update(status="deadline_exceeded")
    finally:
        if process is not None:
            kill_owned_session(process.pid)
            if process.poll() is None:
                process.communicate(timeout=max(.05, deadline.end - time.monotonic()))
            until = min(deadline.end, time.monotonic() + .3)
            while owned_session_members(process.pid) and time.monotonic() < until:
                time.sleep(.005)
            if owned_session_members(process.pid):
                raise HarnessError("owned session remains; retaining fixture directory " + str(directory))
        # Keep a bounded diagnostic even if deadline killed the worker before JSON output.
        if (directory / "swap.log").exists():
            evidence["log"] = (directory / "swap.log").read_text()[-65536:]
        if (directory / "binding.json").exists():
            evidence["retained_recovery_state"] = json.loads((directory / "binding.json").read_text())
            evidence["final_file_sha256"] = sha256(directory / "config.yaml")
        import shutil
        shutil.rmtree(directory)
    evidence["cleanup"] = {"owned_session_empty": True, "owned_temp_removed": not directory.exists()}
    evidence["elapsed_seconds"] = time.monotonic() - deadline.start
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    output = output_dir / ("watcher-witness-" + settings["scenario"] + "-" + uuid.uuid4().hex + ".json")
    with output.open("x") as handle:
        output.chmod(0o600)
        json.dump(evidence, handle, indent=2)
    evidence["output"] = str(output)
    return evidence


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--fixture-process":
        process_fixture(argv[1], float(argv[2]))
        return 0
    if argv and argv[0] == "--fixture-stop":
        return stop_fixture(argv[1], argv[2], int(argv[3]), float(argv[4]))
    if argv and argv[0] == "--worker":
        path = Path(argv[1])
        result = worker(json.loads(path.read_text()), path.parent)
        (path.parent / "result.json").write_text(json.dumps(result))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--llama-swap-binary", required=True)
    parser.add_argument("--scenario", choices=SCENARIOS, default="adopt")
    parser.add_argument("--deadline-seconds", type=float, default=45)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        Deadline(args.deadline_seconds)
        settings = {"binary": str(Path(args.llama_swap_binary).resolve()), "scenario": args.scenario,
                    "deadline_seconds": args.deadline_seconds}
        if args.dry_run:
            print(json.dumps({"dry_run": True, "settings": settings, "output_dir": str(args.output_dir),
                              "would": "owned CPU fixture, single watcher replacement, native read, bounded session cleanup"}))
            return 0
        result = bounded_run(settings, args.output_dir)
        print(json.dumps({"status": result["status"], "output": result["output"],
                          "scenario": args.scenario, "outcome": result.get("outcome")}))
        return 0 if result["status"] == "ok" else 2
    except (HarnessError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print("watcher_witness: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
