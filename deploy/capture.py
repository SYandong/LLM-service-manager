#!/usr/bin/env python3
# Generated-By: Codex / gpt-6-astra
"""Capture a bounded, read-only deployment snapshot."""
import argparse
import datetime as _dt
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


SCHEMA_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_MAX_OUTPUT_BYTES = 262144
KILL_GRACE_SECONDS = 1.0
REDACTED = "<redacted>"
SECRET_PATTERN = re.compile(r"(?i)(token|secret|password|passwd|apikey|api_key|authorization|bearer|credential)")


class CaptureError(Exception):
    pass


def utc_now():
    return _dt.datetime.now(_dt.timezone.utc)


def timestamp_text(value):
    return value.isoformat().replace("+00:00", "Z")


def file_timestamp(value):
    return value.strftime("%Y%m%dT%H%M%S.%fZ")


def redacted_text(value):
    text = str(value)
    if SECRET_PATTERN.search(text):
        return REDACTED
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+", r"\1" + REDACTED, text)
    text = re.sub(r"(?i)((?:token|secret|password|passwd|apikey|api_key)=)[^&\s]+", r"\1" + REDACTED, text)
    return text


def redacted_url(value):
    parts = urllib.parse.urlsplit(value)
    host = parts.hostname or ""
    if parts.port:
        host += ":" + str(parts.port)
    if parts.username or parts.password:
        host = REDACTED + "@" + host
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    safe_query = urllib.parse.urlencode([(key, REDACTED if SECRET_PATTERN.search(key) else val) for key, val in query])
    return urllib.parse.urlunsplit((parts.scheme, host, parts.path, safe_query, parts.fragment))


def bounded_decode(data):
    return data.decode("utf-8", errors="replace")


def load_config(path):
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CaptureError("cannot read config: " + str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise CaptureError("invalid config JSON: " + str(exc)) from exc
    if not isinstance(config, dict):
        raise CaptureError("config must be a JSON object")
    default_timeout = positive_float(config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS), "timeout_seconds")
    default_limit = positive_int(config.get("max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES), "max_output_bytes")
    config["timeout_seconds"] = default_timeout
    config["max_output_bytes"] = default_limit
    sources = config.get("sources")
    if not isinstance(sources, list) or not sources:
        raise CaptureError("config requires a nonempty sources list")
    config["sources"] = [validate_source(index, source, default_timeout, default_limit) for index, source in enumerate(sources)]
    names = [source["name"] for source in config["sources"]]
    if len(names) != len(set(names)):
        raise CaptureError("source names must be unique")
    return config


def positive_float(value, name):
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise CaptureError(name + " must be a positive number") from exc
    if number <= 0:
        raise CaptureError(name + " must be positive")
    return number


def positive_int(value, name):
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise CaptureError(name + " must be a positive integer") from exc
    if number <= 0:
        raise CaptureError(name + " must be positive")
    return number


def validate_source(index, source, default_timeout, default_limit):
    if not isinstance(source, dict):
        raise CaptureError("source must be an object at index " + str(index))
    name = source.get("name")
    kind = source.get("type")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise CaptureError("source name must contain only letters, digits, dot, underscore, or dash")
    if kind == "command":
        argv = source.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
            raise CaptureError("command source requires nonempty string argv: " + name)
        result = dict(source)
        result["timeout_seconds"] = positive_float(result.get("timeout_seconds", default_timeout), "timeout_seconds")
        result["max_output_bytes"] = positive_int(result.get("max_output_bytes", default_limit), "max_output_bytes")
        return result
    if kind == "http_json":
        method = source.get("method", "GET")
        if method != "GET":
            raise CaptureError("http_json sources only support GET: " + name)
        url = source.get("url")
        parts = urllib.parse.urlsplit(url if isinstance(url, str) else "")
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise CaptureError("http_json source requires an http(s) URL: " + name)
        result = dict(source)
        result["method"] = "GET"
        result["timeout_seconds"] = positive_float(result.get("timeout_seconds", default_timeout), "timeout_seconds")
        result["max_output_bytes"] = positive_int(result.get("max_output_bytes", default_limit), "max_output_bytes")
        return result
    raise CaptureError("unsupported source type at index " + str(index))


def source_timeout(config, source):
    return source["timeout_seconds"]


def source_limit(config, source):
    return source["max_output_bytes"]


def terminate_process(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


def wait_bounded(proc, deadline):
    while proc.poll() is None and time_monotonic() < deadline:
        try:
            proc.wait(timeout=min(0.05, max(0.0, deadline - time_monotonic())))
        except subprocess.TimeoutExpired:
            pass
    return proc.poll()


def time_monotonic():
    return time.monotonic()


def read_available(fileobj, selector, buffers, totals, limit):
    try:
        data = os.read(fileobj.fileno(), 65536)
    except BlockingIOError:
        return False
    if not data:
        selector.unregister(fileobj)
        fileobj.close()
        return True
    key = "stdout" if fileobj is buffers["stdout_file"] else "stderr"
    totals[key] += len(data)
    retained = buffers[key]
    if len(retained) < limit:
        retained.extend(data[:limit - len(retained)])
    return False


def collect_process_output(proc, timeout, limit):
    selector = selectors.DefaultSelector()
    buffers = {"stdout": bytearray(), "stderr": bytearray(), "stdout_file": proc.stdout}
    totals = {"stdout": 0, "stderr": 0}
    for pipe in (proc.stdout, proc.stderr):
        os.set_blocking(pipe.fileno(), False)
        selector.register(pipe, selectors.EVENT_READ)

    deadline = time_monotonic() + timeout
    timed_out = False
    try:
        while selector.get_map():
            now = time_monotonic()
            if now >= deadline:
                timed_out = True
                terminate_process(proc)
                break
            for key, _events in selector.select(min(0.05, deadline - now)):
                read_available(key.fileobj, selector, buffers, totals, limit)

        if timed_out:
            cleanup_deadline = time_monotonic() + KILL_GRACE_SECONDS
            while selector.get_map() and time_monotonic() < cleanup_deadline:
                for key, _events in selector.select(0.05):
                    read_available(key.fileobj, selector, buffers, totals, limit)
            for key in list(selector.get_map().values()):
                try:
                    selector.unregister(key.fileobj)
                except Exception:
                    pass
                key.fileobj.close()
            wait_bounded(proc, cleanup_deadline)
        else:
            proc.wait()
    finally:
        selector.close()
    return bytes(buffers["stdout"]), bytes(buffers["stderr"]), totals, timed_out


def run_command(source, timeout, limit):
    started = utc_now()
    proc = None
    try:
        proc = subprocess.Popen(source["argv"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        stdout, stderr, totals, timed_out = collect_process_output(proc, timeout, limit)
    except OSError as exc:
        return unavailable(source, started, "command_unavailable", str(exc))
    ended = utc_now()
    stdout_too_large = totals["stdout"] > limit
    stderr_too_large = totals["stderr"] > limit
    status = "timeout" if timed_out else ("ok" if proc.returncode == 0 else "unavailable")
    return {
        "name": source["name"],
        "type": "command",
        "status": status,
        "started_at": timestamp_text(started),
        "ended_at": timestamp_text(ended),
        "duration_seconds": round((ended - started).total_seconds(), 6),
        "argv": [redacted_text(item) for item in source["argv"]],
        "returncode": proc.returncode,
        "stdout": bounded_decode(stdout),
        "stderr": bounded_decode(stderr),
        "truncated": {"stdout": stdout_too_large, "stderr": stderr_too_large},
        "captured_bytes": {"stdout": len(stdout), "stderr": len(stderr)},
        "total_bytes": totals,
        "error": "timeout" if timed_out else (None if proc.returncode == 0 else "command_failed"),
    }


def unavailable(source, started, reason, detail):
    ended = utc_now()
    return {
        "name": source["name"],
        "type": source["type"],
        "status": "unavailable",
        "started_at": timestamp_text(started),
        "ended_at": timestamp_text(ended),
        "duration_seconds": round((ended - started).total_seconds(), 6),
        "error": reason,
        "detail": redacted_text(detail),
    }


def read_http_json(source, timeout, limit):
    started = utc_now()
    request = urllib.request.Request(source["url"], headers={"Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(limit + 1)
            status_code = getattr(response, "status", getattr(response, "code", None))
    except urllib.error.URLError as exc:
        return unavailable(source, started, "url_unavailable", str(exc))
    except TimeoutError as exc:
        return unavailable(source, started, "timeout", str(exc))
    ended = utc_now()
    if len(data) > limit:
        return {
            "name": source["name"],
            "type": "http_json",
            "status": "unavailable",
            "started_at": timestamp_text(started),
            "ended_at": timestamp_text(ended),
            "duration_seconds": round((ended - started).total_seconds(), 6),
            "method": "GET",
            "url": redacted_url(source["url"]),
            "http_status": status_code,
            "error": "output_limit_exceeded",
        }
    try:
        payload = json.loads(bounded_decode(data))
    except json.JSONDecodeError as exc:
        return unavailable(source, started, "invalid_json", str(exc))
    return {
        "name": source["name"],
        "type": "http_json",
        "status": "ok",
        "started_at": timestamp_text(started),
        "ended_at": timestamp_text(ended),
        "duration_seconds": round((ended - started).total_seconds(), 6),
        "method": "GET",
        "url": redacted_url(source["url"]),
        "http_status": status_code,
        "json": payload,
    }


def capture_source(config, source):
    timeout = source_timeout(config, source)
    limit = source_limit(config, source)
    if timeout <= 0:
        raise CaptureError("timeout_seconds must be positive")
    if source["type"] == "command":
        return run_command(source, timeout, limit)
    return read_http_json(source, timeout, limit)


def snapshot(config, config_path, dry_run):
    sampled_at = utc_now()
    safe_sources = []
    for source in config["sources"]:
        item = {"name": source["name"], "type": source["type"]}
        if source["type"] == "command":
            item["argv"] = [redacted_text(value) for value in source["argv"]]
        else:
            item["method"] = "GET"
            item["url"] = redacted_url(source["url"])
        safe_sources.append(item)
    result = {
        "schema_version": SCHEMA_VERSION,
        "sampled_at": timestamp_text(sampled_at),
        "read_only": True,
        "dry_run": dry_run,
        "provenance": {
            "tool": "deploy/capture.py",
            "config_path": str(config_path),
            "sources": safe_sources,
            "generated_by": "Codex / gpt-6-astra",
            "command_config_trusted": True,
            "http_timeout_scope": "urllib connect/read socket timeout per operation",
        },
        "sources": [],
    }
    if not dry_run:
        result["sources"] = [capture_source(config, source) for source in config["sources"]]
    return result


def atomic_write_json(path, payload):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    fd, temp_name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.chmod(0o600)
        os.replace(temp_path, path)
    except BaseException:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise
    return hashlib.sha256(data).hexdigest()


def write_outputs(output_dir, snap):
    captured = utc_now()
    base = "snapshot-" + file_timestamp(captured)
    snapshot_path = output_dir / (base + ".json")
    snapshot_sha = atomic_write_json(snapshot_path, snap)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "snapshot": snapshot_path.name,
        "sha256": snapshot_sha,
        "created_at": timestamp_text(captured),
        "provenance": snap["provenance"],
        "generated_by": "Codex / gpt-6-astra",
    }
    manifest_path = output_dir / ("manifest-" + file_timestamp(captured) + ".json")
    manifest_sha = atomic_write_json(manifest_path, manifest)
    return {
        "snapshot": str(snapshot_path),
        "snapshot_sha256": snapshot_sha,
        "manifest": str(manifest_path),
        "manifest_sha256": manifest_sha,
    }


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="JSON capture config")
    parser.add_argument("--output-dir", required=True, type=Path, help="directory for private snapshot JSON files")
    parser.add_argument("--dry-run", action="store_true", help="validate and print the capture plan without command, HTTP, or file mutation")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        config = load_config(args.config)
        snap = snapshot(config, args.config, args.dry_run)
        if args.dry_run:
            print(json.dumps({"dry_run": True, "snapshot": snap}, indent=2, sort_keys=True))
            return 0
        output = write_outputs(args.output_dir, snap)
        print(json.dumps(output, sort_keys=True))
        return 0
    except CaptureError as exc:
        print("capture error: " + str(exc), file=sys.stderr)
        return 2
    except OSError as exc:
        print("capture error: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
