#!/usr/bin/env python3
# Generated-By: Codex / gpt-6.1-sol
# Generated-By: Codex / unknown model
"""Bounded, observation-only host fleet export. Python 3.10, standard library."""

import argparse
import csv
import datetime
import hashlib
import ipaddress
import json
import math
import os
import re
import selectors
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import unicodedata
from dataclasses import dataclass
from pathlib import Path


MIB = 1024 * 1024
MAX_SNAPSHOT = 2 * MIB
MAX_RESPONSE = 4 * MIB
MAX_CMDLINE_BYTES = 256 * 1024
MAX_ARGC = 4096
MAX_PASSWD_BYTES = 64 * 1024
MAX_UID = 2 ** 32 - 1
COMMAND_CLEANUP_SECONDS = 0.25
DEFAULT_RULES = [
    {"engine": "vllm", "all": ["vllm", "serve"]},
    {"engine": "vllm", "all": ["vllm.entrypoints.openai.api_server"]},
    {"engine": "sglang", "all": ["sglang.launch_server"]},
    {"engine": "llama-server", "all": ["llama-server"]},
    {"engine": "ollama", "all": ["ollama", "serve"]},
]
DEFAULTS = {
    "proc_root": "/proc",
    "host_passwd_path": "/etc/passwd",
    "output_path": "/var/lib/llmsvc-host-export/fleet.json",
    "nvidia_smi_path": "/usr/bin/nvidia-smi",
    "nsenter_path": "/usr/bin/nsenter",
    "python_path": "/usr/bin/python3",
    "managed_container": "llmsvc",
    "sample_interval_seconds": 60,
    "scan_budget_seconds": 20,
    "target_timeout_seconds": 2,
    "gpu_query_timeout_seconds": 5,
    "max_response_bytes": MAX_RESPONSE,
    "max_snapshot_bytes": MAX_SNAPSHOT,
    "max_processes": 65536,
    "max_services": 256,
    "max_gpu_processes": 4096,
    "max_proc_bytes": 64 * MIB,
    "match_rules": DEFAULT_RULES,
}
METRICS = {
    "vllm:generation_tokens_total": "generation_tokens_total",
    "vllm:prompt_tokens_total": "prompt_tokens_total",
    "vllm:prompt_tokens_cached_total": "prompt_tokens_cached_total",
    "vllm:e2e_request_latency_seconds_count": "requests_total",
    "vllm:num_requests_running": "num_requests_running",
    "vllm:num_requests_waiting": "num_requests_waiting",
    "vllm:kv_cache_usage_perc": "kv_cache_usage_perc",
    "vllm:engine_sleep_state": "engine_sleep_state",
    "vllm:generation_tokens_created": "generation_tokens_created",
    "vllm:prompt_tokens_created": "prompt_tokens_created",
    "vllm:prompt_tokens_cached_created": "prompt_tokens_cached_created",
    "vllm:e2e_request_latency_seconds_created": "requests_created",
}
SECRET = re.compile(r"(?i)(?:hf_[a-z0-9_-]+|sk-[a-z0-9_-]+)")
SENSITIVE = re.compile(r"(?i)(?:token|key|secret|password|authorization|credential)")
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
SAMPLE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{([^}]*)\})?\s+([^\s]+)(?:\s+[^\s]+)?$")
LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\[\\"n])*)"')
URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


class ScanError(Exception):
    """An export-safe error code; never include untrusted exception text."""


class BudgetExceeded(ScanError):
    pass


class Budget:
    def __init__(self, seconds, clock=time.monotonic):
        self.clock = clock
        self.started = clock()
        self.deadline = self.started + seconds

    def remaining(self):
        return max(0.0, self.deadline - self.clock())

    def check(self):
        if self.remaining() <= 0.05:
            raise BudgetExceeded("scan_budget_exceeded")


def redact_url(match):
    try:
        parsed = urllib.parse.urlsplit(match.group(0))
        hostname = parsed.hostname
        if not hostname or "%" in hostname or len(hostname) > 253:
            return parsed.scheme + "://[REDACTED]" + parsed.path
        authority = f"[{hostname}]" if ":" in hostname else hostname
        if parsed.port is not None:
            authority += ":" + str(parsed.port)
        return urllib.parse.urlunsplit((parsed.scheme, authority, parsed.path, "", ""))
    except ValueError:
        return "[REDACTED-URL]"


def safe_text(value, limit=256):
    if not isinstance(value, str):
        return ""
    value = ANSI.sub("", value[: max(limit * 4, 1024)])
    value = CONTROL.sub("", value)
    value = "".join(char for char in value if unicodedata.category(char) not in {"Cc", "Cf", "Cs"})
    # Rebuild URLs from their parsed scheme/host/path. Userinfo, including
    # encoded credentials, and the entire query/fragment never survive export.
    value = URL.sub(redact_url, value)
    value = SECRET.sub("[REDACTED]", value)
    value = re.sub(r"(?i)([a-z0-9_-]*(?:token|key|secret|password)[a-z0-9_-]*=)[^\s&]+", r"\1[REDACTED]", value)
    return value[:limit]


def redact_argv(argv):
    result = []
    redact_next = False
    for arg in argv[:256]:
        if redact_next:
            if not arg.startswith("-"):
                result.append("[REDACTED]")
                continue
            redact_next = False
        name = arg.split("=", 1)[0]
        if arg.startswith("-") and SENSITIVE.search(name):
            result.append(safe_text(name, 128) + ("=[REDACTED]" if "=" in arg else ""))
            # API-key flags can have multiple values. Redact positional values
            # until the next option rather than guessing an argument's arity.
            redact_next = True
        else:
            result.append(safe_text(arg, 512))
    return " ".join(result)[:1024]


def load_config(path=None):
    config = dict(DEFAULTS)
    if path is not None:
        with open(path, "rb") as handle:
            raw = handle.read(65537)
        if len(raw) > 65536:
            raise ScanError("config_too_large")
        try:
            supplied = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ScanError("invalid_config") from None
        if not isinstance(supplied, dict):
            raise ScanError("invalid_config")
        if any(key not in DEFAULTS and key not in {"_comments", "_generated_by"} for key in supplied):
            raise ScanError("unknown_config_field")
        config.update({key: value for key, value in supplied.items() if key in DEFAULTS})
    for key in ("proc_root", "host_passwd_path", "output_path", "nvidia_smi_path", "nsenter_path", "python_path"):
        value = config[key]
        if not isinstance(value, str) or not value.startswith("/") or CONTROL.search(value) or ".." in Path(value).parts:
            raise ScanError("invalid_config_path")
    bounds = {
        "sample_interval_seconds": (30, 300), "scan_budget_seconds": (0.1, 120),
        "target_timeout_seconds": (0.01, 2), "max_response_bytes": (1, MAX_RESPONSE),
        "gpu_query_timeout_seconds": (0.01, 30),
        "max_snapshot_bytes": (1, MAX_SNAPSHOT), "max_processes": (1, 65536),
        "max_services": (1, 256), "max_gpu_processes": (1, 4096),
        "max_proc_bytes": (1024, 64 * MIB),
    }
    for key, (low, high) in bounds.items():
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ScanError("invalid_config_bound")
        if key.startswith("max_") and not isinstance(value, int):
            raise ScanError("invalid_config_bound")
    if not isinstance(config["managed_container"], str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", config["managed_container"]):
        raise ScanError("invalid_managed_container")
    rules = config["match_rules"]
    if not isinstance(rules, list) or not 1 <= len(rules) <= 32:
        raise ScanError("invalid_match_rules")
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {"engine", "all"} or rule["engine"] not in {"vllm", "sglang", "llama-server", "ollama"}:
            raise ScanError("invalid_match_rules")
        if not isinstance(rule["all"], list) or not 1 <= len(rule["all"]) <= 16 or any(not isinstance(token, str) or not 1 <= len(token) <= 128 or CONTROL.search(token) for token in rule["all"]):
            raise ScanError("invalid_match_rules")
    return config


class ProcReader:
    def __init__(self, root, budget, max_bytes, host_passwd_path="/etc/passwd"):
        self.root = Path(root)
        self.budget = budget
        self.bytes_left = max_bytes
        self.host_passwd_path = Path(host_passwd_path)
        self.host_users = None

    def read(self, relative, limit, regular=False):
        self.budget.check()
        if self.bytes_left <= 0:
            raise ScanError("proc_byte_limit")
        flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | (os.O_NOFOLLOW if regular else 0)
        fd = os.open(self.root / relative, flags)
        try:
            if regular:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                    raise ScanError("passwd_file_unavailable")
            chunks = []
            size = 0
            while size <= limit and size <= self.bytes_left:
                self.budget.check()
                chunk = os.read(fd, min(65536, limit + 1 - size, self.bytes_left + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            data = b"".join(chunks)
        finally:
            os.close(fd)
        self.bytes_left -= len(data)
        self.budget.check()
        if len(data) > limit or self.bytes_left < 0:
            raise ScanError("proc_file_limit")
        return data

    def text(self, relative, limit=8192):
        return self.read(relative, limit).decode("utf-8", "replace")

    def net_inode(self, pid):
        self.budget.check()
        return os.stat(self.root / str(pid) / "ns/net").st_ino

    def namespace_ids(self, pid):
        identities = []
        for name in ("pid", "user", "mnt"):
            self.budget.check()
            info = os.stat(self.root / str(pid) / "ns" / name)
            identities.append((info.st_dev, info.st_ino))
        self.budget.check()
        return identities

    def host_user(self, uid):
        if self.host_users is None:
            self.host_users = {}
            try:
                data = self.read(self.host_passwd_path, MAX_PASSWD_BYTES, regular=True).decode("utf-8", "strict")
                ambiguous = set()
                for line in data.splitlines():
                    self.budget.check()
                    fields = line.split(":")
                    if len(fields) != 7 or not re.fullmatch(r"[0-9]{1,10}", fields[2]):
                        continue
                    owner = int(fields[2])
                    if owner > MAX_UID:
                        continue
                    if owner in self.host_users:
                        ambiguous.add(owner)
                    self.host_users[owner] = safe_text(fields[0], 128) or None
                for owner in ambiguous:
                    self.host_users.pop(owner, None)
            except BudgetExceeded:
                raise
            except (OSError, ScanError, ValueError, UnicodeError):
                self.host_users = {}
        return self.host_users.get(uid)


def stat_identity(text):
    # comm can contain spaces and parentheses; fields start after the last ')'.
    suffix = text.rsplit(")", 1)[1].split()
    return int(suffix[1]), int(suffix[19])


@dataclass
class Process:
    pid: int
    ppid: int
    start: int
    argv: list
    cgroup: str
    container: str | None
    comm: str
    uid: int | None = None


def real_uid(status):
    rows = re.findall(r"^Uid:[ \t]*([^\n]+)$", status, re.M)
    if len(rows) != 1:
        return None
    fields = rows[0].split()
    if len(fields) != 4 or any(not re.fullmatch(r"[0-9]{1,10}", value) or int(value) > MAX_UID for value in fields):
        return None
    return int(fields[0])


def container_from_cgroup(cgroup):
    matches = re.findall(r"(?:^|/)lxc\.payload\.([^/\n]+)(?:/|$)", cgroup)
    if any(not re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", name) for name in matches) or len(set(matches)) > 1:
        raise ScanError("container_identity_unavailable")
    return matches[0] if matches else None


def discover(reader, config):
    processes = {}
    complete = True
    errors = set()
    with os.scandir(reader.root) as entries:
        for entry in entries:
            try:
                reader.budget.check()
                if not entry.name.isascii() or not entry.name.isdigit():
                    continue
                if len(processes) >= config["max_processes"]:
                    raise ScanError("proc_process_limit")
                pid = int(entry.name)
                ppid, start = stat_identity(reader.text(f"{pid}/stat"))
                status = reader.text(f"{pid}/status")
                status_ppid = re.search(r"^PPid:\s*(\d+)\s*$", status, re.M)
                if status_ppid is None or int(status_ppid.group(1)) != ppid:
                    raise ScanError("proc_identity_changed")
                raw = reader.read(f"{pid}/cmdline", MAX_CMDLINE_BYTES)
                argv = raw.rstrip(b"\0").decode("utf-8", "replace").split("\0") if raw else []
                if len(argv) > MAX_ARGC:
                    raise ScanError("proc_argv_limit")
                cgroup = reader.text(f"{pid}/cgroup")
                comm = reader.text(f"{pid}/comm", 512).strip()
                if stat_identity(reader.text(f"{pid}/stat")) != (ppid, start):
                    raise ScanError("proc_identity_changed")
                processes[pid] = Process(pid, ppid, start, argv, cgroup, container_from_cgroup(cgroup), safe_text(comm, 64), real_uid(status))
            except (BudgetExceeded, ScanError) as exc:
                complete = False
                errors.add(str(exc))
                if isinstance(exc, BudgetExceeded) or str(exc) in {"proc_byte_limit", "proc_file_limit", "proc_process_limit"}:
                    break
            except (OSError, ValueError, IndexError):
                # An inaccessible/racing PID prevents proof of complete discovery.
                complete = False
                errors.add("proc_process_unavailable")
    return processes, complete, errors


def engine_for(process, rules):
    tokens = {token.casefold() for token in process.argv}
    tokens.update(Path(token).name.casefold() for token in process.argv[:2])
    for rule in rules:
        if all(token.casefold() in tokens for token in rule["all"]):
            return rule["engine"]
    return None


def ancestor_service(pid, processes, service_pids, budget):
    visited = set()
    while pid in processes and pid not in visited:
        budget.check()
        if pid in service_pids:
            return pid
        visited.add(pid)
        parent = processes[pid].ppid
        if parent in processes:
            # A recycled parent PID or a different container cannot own this child.
            if processes[parent].start > processes[pid].start or processes[parent].container != processes[pid].container:
                return None
        pid = parent
    return None


def option(argv, name):
    for index, token in enumerate(argv):
        if token.startswith(name + "="):
            return token[len(name) + 1:]
        if token == name and index + 1 < len(argv) and not argv[index + 1].startswith("-"):
            return argv[index + 1]
    return None


def service_row(process, engine, config, boot_time, clock_ticks):
    model_path = option(process.argv, "--model")
    if model_path is None and "serve" in process.argv:
        index = process.argv.index("serve") + 1
        if index < len(process.argv) and not process.argv[index].startswith("-"):
            model_path = process.argv[index]
    model = option(process.argv, "--served-model-name") or model_path
    identity = f"{process.container or ''}|{process.pid}|{process.start}".encode()
    return {
        "id": hashlib.sha256(identity).hexdigest()[:16], "engine": engine,
        "engine_version": None, "container": process.container, "host": process.container is None,
        "managed_by": "llmsvc" if process.container == config["managed_container"] and re.search(r"(?:^|/)vllm-[^/\n]+\.service(?:/|$)", process.cgroup) else None,
        "pid": process.pid, "started_at": boot_time + process.start / clock_ticks if boot_time is not None else None,
        "bind": None, "port": None, "listener_observation_complete": False, "listener_ipv6_only": None,
        "model": safe_text(model) if model else None,
        "model_path": safe_text(model_path, 512) if model_path else None,
        "gpus": [], "gpu_observation_complete": False,
        "argv_redacted": redact_argv(process.argv), "metrics": {}, "ollama": None,
        "scrape": {"ok": False, "error": "scrape_skipped", "duration_ms": 0},
    }


def run_bounded(argv, timeout, max_output, pass_fds=()):
    """Drain bounded stdout; never invoke a shell or inherit proxy/secret env."""
    if timeout <= 0:
        raise BudgetExceeded("scan_budget_exceeded")
    deadline = time.monotonic() + timeout
    try:
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 shell=False, close_fds=True, pass_fds=pass_fds, start_new_session=True,
                                 env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}, bufsize=0)
    except OSError:
        raise ScanError("command_unavailable") from None
    output = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ScanError("command_timeout")
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fileobj.fileno(), min(65536, max_output + 1 - len(output)))
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        output.extend(chunk)
                        if len(output) > max_output:
                            raise ScanError("command_output_limit")
            code = child.wait(timeout=max(0.001, deadline - time.monotonic()))
        if code != 0:
            helper_errors = {20: "redirect_denied", 21: "http_error", 22: "response_limit", 23: "http_timeout", 24: "http_unavailable", 25: "invalid_endpoint"}
            raise ScanError(helper_errors.get(code, "command_failed"))
        return bytes(output)
    except subprocess.TimeoutExpired:
        raise ScanError("command_timeout") from None
    finally:
        if child.poll() is None:
            try:
                child.kill()
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=COMMAND_CLEANUP_SECONDS)
            except subprocess.TimeoutExpired:
                # A killed query can remain in an uninterruptible kernel wait.
                # Keep the original failure; it provides no usable observation.
                pass
        child.stdout.close()


def number(raw, maximum=2 ** 63 - 1):
    value = float(raw)
    if not math.isfinite(value) or value < 0 or maximum is not None and value > maximum:
        raise ValueError("invalid_number")
    return int(value) if value.is_integer() else value


def gpu_inventory(config, budget, runner):
    errors = set()
    gpus, apps = [], []
    gpu_ok, apps_ok = False, False
    for query, target in (("index,uuid,memory.total,memory.used,utilization.gpu", "gpus"), ("pid,gpu_uuid,used_memory", "apps")):
        try:
            budget.check()
            flag = "--query-gpu=" if target == "gpus" else "--query-compute-apps="
            data = runner([config["nvidia_smi_path"], flag + query, "--format=csv,noheader,nounits"],
                          min(config["gpu_query_timeout_seconds"], max(0.001, budget.remaining() - 0.05)), config["max_response_bytes"])
            if len(data) > config["max_response_bytes"]:
                raise ScanError("gpu_output_limit")
            seen = set()
            seen_uuids = set()
            for fields in csv.reader(data.decode("utf-8", "strict").splitlines()):
                budget.check()
                if not fields:
                    continue
                if len(fields) != (5 if target == "gpus" else 3):
                    raise ValueError("invalid_gpu_row")
                fields = [field.strip() for field in fields]
                if target == "gpus":
                    if len(gpus) >= 128 or not re.fullmatch(r"GPU-[a-zA-Z0-9-]{1,128}", fields[1]):
                        raise ValueError("invalid_gpu_identity")
                    index = number(fields[0], 65535)
                    if not isinstance(index, int) or index in seen or fields[1] in seen_uuids:
                        raise ValueError("invalid_gpu_index")
                    seen.add(index)
                    seen_uuids.add(fields[1])
                    gpus.append({"index": index, "uuid": fields[1], "total_mib": number(fields[2]), "used_mib": number(fields[3]), "util_percent": number(fields[4], 100)})
                else:
                    if len(apps) >= config["max_gpu_processes"]:
                        raise ScanError("gpu_process_limit")
                    pid = number(fields[0], 2 ** 31 - 1)
                    if not isinstance(pid, int) or pid < 1 or not re.fullmatch(r"GPU-[a-zA-Z0-9-]{1,128}", fields[1]):
                        raise ValueError("invalid_gpu_process")
                    if (pid, fields[1]) in seen:
                        raise ValueError("duplicate_gpu_process")
                    seen.add((pid, fields[1]))
                    apps.append((pid, fields[1], number(fields[2])))
            if target == "gpus":
                gpu_ok = bool(gpus)
                if not gpu_ok:
                    errors.add("gpu_inventory_unavailable")
            else:
                apps_ok = True
        except (ScanError, ValueError, UnicodeError):
            errors.add("gpu_inventory_unavailable" if target == "gpus" else "gpu_processes_unavailable")
            if target == "gpus":
                gpus = []
            else:
                apps = []
    return gpus, apps, gpu_ok, apps_ok, errors


def current_process(reader, process):
    return stat_identity(reader.text(f"{process.pid}/stat")) == (process.ppid, process.start) and reader.text(f"{process.pid}/cgroup") == process.cgroup


def host_metadata(reader, process):
    unknown = {"host": None, "host_uid": None, "host_user": None}
    if process.container is not None:
        return dict(unknown, host=False)
    if process.uid is None:
        return unknown
    try:
        init_identity = stat_identity(reader.text("1/stat"))
        namespaces = reader.namespace_ids(1)
        if not current_process(reader, process) or reader.namespace_ids(process.pid) != namespaces:
            return unknown
        user = reader.host_user(process.uid)
        if (reader.namespace_ids(1) != namespaces or reader.namespace_ids(process.pid) != namespaces
                or stat_identity(reader.text("1/stat")) != init_identity
                or real_uid(reader.text(f"{process.pid}/status")) != process.uid or not current_process(reader, process)):
            return unknown
        return {"host": True, "host_uid": process.uid, "host_user": user}
    except BudgetExceeded:
        raise
    except (OSError, ValueError, IndexError, ScanError):
        return unknown


def attribute_gpus(reader, processes, services, gpus, apps, complete):
    other = []
    gpu_indices = {gpu["uuid"]: gpu["index"] for gpu in gpus}
    usage = {pid: {} for pid in services}
    for pid, uuid, used in apps:
        unknown = {"container": None, "pid": pid, "gpu": gpu_indices.get(uuid), "used_mib": used, "comm": "unknown",
                   "host": None, "host_uid": None, "host_user": None}
        try:
            reader.budget.check()
            process = processes.get(pid)
            if process is None or not current_process(reader, process) or uuid not in gpu_indices:
                complete = False
                if uuid in gpu_indices:
                    other.append(unknown)
                continue
            owner = ancestor_service(pid, processes, services, reader.budget)
            ancestor = pid
            visited = set()
            while owner is not None and ancestor not in visited:
                reader.budget.check()
                visited.add(ancestor)
                if not current_process(reader, processes[ancestor]):
                    owner = None
                    complete = False
                    break
                if ancestor == owner:
                    break
                ancestor = processes[ancestor].ppid
            if owner is not None and not current_process(reader, processes[owner]):
                owner = None
                complete = False
            gpu = gpu_indices[uuid]
            if owner is None:
                metadata = host_metadata(reader, process)
                if not current_process(reader, process):
                    raise ScanError("proc_identity_changed")
                other.append(dict(unknown, container=process.container, comm=process.comm, **metadata))
            else:
                usage[owner][gpu] = usage[owner].get(gpu, 0) + used
        except (OSError, ValueError, IndexError, ScanError):
            complete = False
            if uuid in gpu_indices:
                other.append(unknown)
    for pid, service in services.items():
        service["gpus"] = [{"index": index, "used_mib": value} for index, value in sorted(usage[pid].items())]
        service["gpu_observation_complete"] = complete
    return other, complete


def decode_address(raw, family):
    address, port = raw.split(":")
    data = bytes.fromhex(address)
    if family == socket.AF_INET:
        data = data[::-1]
    else:
        data = b"".join(data[i:i + 4][::-1] for i in range(0, 16, 4))
    return socket.inet_ntop(family, data), int(port, 16)


def owned_listeners(reader, pid):
    owned = set()
    with os.scandir(reader.root / str(pid) / "fd") as entries:
        for index, entry in enumerate(entries):
            reader.budget.check()
            if index >= 4096:
                raise ScanError("socket_fd_limit")
            try:
                link = os.readlink(entry.path)
            except FileNotFoundError:
                continue
            match = re.fullmatch(r"socket:\[(\d+)\]", link)
            if match:
                owned.add(match.group(1))
    listeners = []
    for filename, family in (("tcp", socket.AF_INET), ("tcp6", socket.AF_INET6)):
        try:
            data = reader.text(f"{pid}/net/{filename}", MIB)
        except FileNotFoundError:
            continue
        for line in data.splitlines()[1:]:
            reader.budget.check()
            if len(line) > 8192:
                raise ScanError("socket_row_limit")
            fields = line.split()
            if len(fields) >= 10 and fields[3] == "0A" and fields[9] in owned:
                bind, port = decode_address(fields[1], family)
                listeners.append((bind, port, fields[9]))
    return listeners


def choose_listener(reader, process, engine):
    port = option(process.argv, "--port")
    bind = option(process.argv, "--host")
    if engine == "ollama":
        # Read a bounded environment, retain only this variable, export none of it.
        try:
            for item in reader.read(f"{process.pid}/environ", 65536).split(b"\0"):
                reader.budget.check()
                if item.startswith(b"OLLAMA_HOST="):
                    value = item.split(b"=", 1)[1].decode("ascii", "strict")
                    parsed = urllib.parse.urlsplit(value if "://" in value else "http://" + value)
                    if parsed.scheme != "http" or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
                        raise ScanError("invalid_endpoint")
                    bind, port = parsed.hostname, parsed.port or 11434
                    break
        except FileNotFoundError:
            pass
    if port is not None:
        try:
            port = int(port)
            if not 1 <= port <= 65535:
                raise ValueError()
        except (TypeError, ValueError):
            raise ScanError("invalid_endpoint") from None
    if bind is not None:
        try:
            bind = str(ipaddress.ip_address(bind))
        except ValueError:
            raise ScanError("invalid_endpoint") from None
    candidates = owned_listeners(reader, process.pid)
    preferred = port or (11434 if engine == "ollama" else 8000)
    # Ollama can configure an IPv4 wildcard but own an IPv6 wildcard socket.
    # Keep the configured port and actual owned address; ambiguity still fails.
    wildcard_bind = engine == "ollama" and port is not None and bind in {"0.0.0.0", "::"}
    matching = [
        item for item in candidates
        if (port is None or item[1] == port)
        and (bind is None or item[0] == bind or (wildcard_bind and item[0] in {"0.0.0.0", "::"}))
    ]
    defaults = [item for item in matching if item[1] == preferred]
    if defaults:
        matching = defaults
    if len(matching) != 1:
        raise ScanError("listener_identity_unavailable")
    return matching[0]


def parse_metrics(data, budget, with_identity=False):
    values = {}
    samples = {}
    identities = set()
    text = data.decode("utf-8", "strict")
    for line in text.splitlines():
        budget.check()
        if len(line) > 8192:
            raise ScanError("metrics_line_limit")
        if not line or line.startswith("#"):
            continue
        # Ignore non-whitelisted names, including arbitrary attacker-controlled labels.
        name = re.split(r"[\s{]", line, maxsplit=1)[0]
        if name not in METRICS:
            continue
        match = SAMPLE.fullmatch(line)
        if match is None:
            raise ScanError("invalid_metrics")
        labels = match.group(2)
        normalized_labels = []
        if labels is not None:
            if len(labels) > 2048:
                raise ScanError("metrics_label_limit")
            position = 0
            while position < len(labels):
                budget.check()
                label = LABEL.match(labels, position)
                if label is None or len(label.group(2)) > 512:
                    raise ScanError("invalid_metrics_labels")
                normalized_labels.append((label.group(1), label.group(2)))
                if len({name for name, _ in normalized_labels}) != len(normalized_labels):
                    raise ScanError("ambiguous_metrics_labels")
                position = label.end()
                if position < len(labels):
                    if labels[position] != ",":
                        raise ScanError("invalid_metrics_labels")
                    position += 1
        value = number(match.group(3))
        key = METRICS[name]
        identity = (name, tuple(sorted(normalized_labels)))
        if identity in identities:
            raise ScanError("ambiguous_metrics_series")
        identities.add(identity)
        samples.setdefault(key, []).append(value)
        if key.endswith("_created"):
            values[key] = max(values.get(key, value), value)
        elif key in {"kv_cache_usage_perc", "engine_sleep_state"}:
            if key == "kv_cache_usage_perc" and value > 1:
                raise ScanError("invalid_metrics_gauge")
        else:
            values[key] = values.get(key, 0) + value
            if not math.isfinite(values[key]):
                raise ScanError("invalid_metrics")
    if not values:
        raise ScanError("metrics_unavailable")
    if "kv_cache_usage_perc" in samples:
        values["kv_cache_usage_perc"] = sum(samples["kv_cache_usage_perc"]) / len(samples["kv_cache_usage_perc"])
    if "engine_sleep_state" in samples and len(set(samples["engine_sleep_state"])) == 1:
        values["engine_sleep_state"] = samples["engine_sleep_state"][0]
    counter_identities = sorted(identity for identity in identities if METRICS[identity[0]].endswith("_total"))
    fingerprint = hashlib.sha256(json.dumps(counter_identities, separators=(",", ":")).encode()).hexdigest()[:32]
    return (values, fingerprint) if with_identity else values


def parse_ollama(data, budget):
    budget.check()
    payload = json.loads(data)
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list) or len(payload["models"]) > 256:
        raise ScanError("invalid_ollama")
    models = []
    for model in payload["models"]:
        budget.check()
        if not isinstance(model, dict) or not isinstance(model.get("name"), str) or not isinstance(model.get("expires_at"), str):
            raise ScanError("invalid_ollama")
        expiry = model["expires_at"]
        if len(expiry) > 64 or CONTROL.search(expiry):
            raise ScanError("invalid_ollama")
        timestamp = datetime.datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ScanError("invalid_ollama")
        models.append({"name": safe_text(model["name"]), "size_vram": number(model.get("size_vram", 0)), "expires_at": expiry})
    return {"models": models}


def parse_ipv6_only(data, port, inode):
    """Read INET_DIAG_SKV6ONLY only for one exact wildcard LISTEN inode."""
    if not 88 <= len(data) <= 65536:
        return None
    length, kind, flags, sequence, _ = struct.unpack_from("=IHHII", data)
    if length != len(data) or kind != 20 or flags & 2 or sequence != 1:
        return None
    if data[16:18] != bytes((socket.AF_INET6, 10)):
        return None
    if struct.unpack_from("!HH", data, 20) != (port, 0) or data[24:56] != bytes(32):
        return None
    if struct.unpack_from("=I", data, 84)[0] != inode:
        return None
    value = None
    offset = 88
    while offset < length:
        if offset + 4 > length:
            return None
        size, attribute = struct.unpack_from("=HH", data, offset)
        if size < 4 or offset + size > length:
            return None
        if (attribute & 0x3fff) == 11:  # INET_DIAG_SKV6ONLY from linux/inet_diag.h.
            if value is not None or size != 5 or data[offset + 4] not in (0, 1):
                return None
            value = bool(data[offset + 4])
        offset += (size + 3) & ~3
    return value if offset == length else None


def socket_ipv6_only(port, inode, timeout):
    """One bounded kernel diagnostic in the already pinned network namespace."""
    if not 1 <= port <= 65535 or not 0 < inode <= 2 ** 32 - 1 or not 0 < timeout <= 2:
        return None
    # inet_diag_req_v2, exact wildcard socket query, no dump or destructive flags.
    identity = struct.pack("!HH", port, 0) + bytes(32) + struct.pack("=III", 0, 0xffffffff, 0xffffffff)
    request = struct.pack("=BBBBI", socket.AF_INET6, socket.IPPROTO_TCP, 0, 0, 1 << 10) + identity
    header = struct.pack("=IHHII", 16 + len(request), 20, 1, 1, 0)
    try:
        with socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM, 4) as diagnostic:
            diagnostic.settimeout(timeout)
            diagnostic.bind((0, 0))
            diagnostic.sendto(header + request, (0, 0))
            response, sender = diagnostic.recvfrom(65537)
            if sender[0] != 0:
                return None
            return parse_ipv6_only(response, port, inode)
    except (OSError, ValueError, OverflowError):
        return None


def scrape_service(reader, process, row, config, runner):
    started = reader.budget.clock()
    namespace_fd = None
    try:
        reader.budget.check()
        supported = row["engine"] in {"vllm", "ollama"}
        if not current_process(reader, process):
            raise ScanError("process_identity_changed")
        namespace_inode = reader.net_inode(process.pid)
        try:
            listener = choose_listener(reader, process, row["engine"])
        except BudgetExceeded:
            raise
        except ScanError:
            if not supported:
                raise ScanError("unsupported_engine") from None
            raise
        bind, port, _ = listener
        # Pin the namespace FD; nsenter cannot follow a subsequently recycled PID.
        namespace_fd = os.open(reader.root / str(process.pid) / "ns/net", os.O_RDONLY | os.O_CLOEXEC)
        if os.fstat(namespace_fd).st_ino != namespace_inode or reader.net_inode(process.pid) != namespace_inode or not current_process(reader, process) or listener not in owned_listeners(reader, process.pid):
            raise ScanError("process_identity_changed")
        ipv6_only = None
        if bind == "::":
            timeout = min(0.25, config["target_timeout_seconds"], max(0.001, reader.budget.remaining() - 0.05))
            try:
                diagnostic = json.loads(runner([config["nsenter_path"], f"--net=/proc/self/fd/{namespace_fd}", "--",
                    config["python_path"], "-I", "-S", str(Path(__file__).resolve()), "--listener-helper", str(port),
                    listener[2], "--helper-timeout", str(timeout)], timeout,
                    min(256, config["max_response_bytes"]), pass_fds=(namespace_fd,)))
                if (isinstance(diagnostic, dict) and diagnostic.get("bind") == bind and diagnostic.get("port") == port
                        and str(diagnostic.get("inode")) == listener[2] and type(diagnostic.get("ipv6_only")) is bool):
                    ipv6_only = diagnostic["ipv6_only"]
            except (ScanError, OSError, ValueError, UnicodeError):
                pass
            if not current_process(reader, process) or reader.net_inode(process.pid) != namespace_inode or listener not in owned_listeners(reader, process.pid):
                raise ScanError("process_identity_changed")
        if not supported:
            if not current_process(reader, process) or reader.net_inode(process.pid) != namespace_inode or listener not in owned_listeners(reader, process.pid):
                raise ScanError("process_identity_changed")
            row["bind"], row["port"], row["listener_observation_complete"] = bind, port, True
            row["listener_ipv6_only"] = ipv6_only
            raise ScanError("unsupported_engine")
        address = "127.0.0.1" if bind == "0.0.0.0" else "::1" if bind == "::" else bind
        address = f"[{address}]" if ":" in address else address
        path = "/metrics" if row["engine"] == "vllm" else "/api/ps"
        timeout = min(config["target_timeout_seconds"], max(0.001, reader.budget.remaining() - 0.05))
        data = runner([config["nsenter_path"], f"--net=/proc/self/fd/{namespace_fd}", "--", config["python_path"], "-I", "-S",
                       str(Path(__file__).resolve()), "--http-helper", f"http://{address}:{port}{path}",
                       "--helper-limit", str(config["max_response_bytes"]), "--helper-timeout", str(timeout)],
                      timeout, config["max_response_bytes"], pass_fds=(namespace_fd,))
        # Discard the response if ownership changed while the GET was in flight.
        if not current_process(reader, process) or reader.net_inode(process.pid) != namespace_inode or listener not in owned_listeners(reader, process.pid):
            raise ScanError("process_identity_changed")
        row["bind"], row["port"], row["listener_observation_complete"] = bind, port, True
        row["listener_ipv6_only"] = ipv6_only
        if len(data) > config["max_response_bytes"]:
            raise ScanError("response_limit")
        if row["engine"] == "vllm":
            row["metrics"], row["metrics_series_id"] = parse_metrics(data, reader.budget, with_identity=True)
        else:
            row["ollama"] = parse_ollama(data, reader.budget)
        row["scrape"]["ok"], row["scrape"]["error"] = True, None
    except BudgetExceeded:
        row["scrape"]["error"] = "scrape_skipped"
    except ScanError as exc:
        row["scrape"]["error"] = str(exc)
        if str(exc) == "process_identity_changed":
            row["gpus"], row["gpu_observation_complete"] = [], False
    except (OSError, ValueError, UnicodeError, IndexError, RecursionError, OverflowError):
        row["scrape"]["error"] = "scrape_unavailable"
        row["gpus"], row["gpu_observation_complete"] = [], False
    finally:
        if namespace_fd is not None:
            os.close(namespace_fd)
        row["scrape"]["duration_ms"] = round((reader.budget.clock() - started) * 1000)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def http_helper(url, limit, timeout):
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "http" or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"/metrics", "/api/ps"} or not parsed.port or not 1 <= limit <= MAX_RESPONSE or not 0 < timeout <= 2:
            return 25
        ipaddress.ip_address(parsed.hostname)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        request = urllib.request.Request(url, method="GET", headers={"Accept": "text/plain, application/json", "Accept-Encoding": "identity", "Connection": "close"})
        with opener.open(request, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                return 21
            raw = response.read(limit + 1)
            if len(raw) > limit:
                return 22
        sys.stdout.buffer.write(raw)
        return 0
    except urllib.error.HTTPError as exc:
        return 20 if 300 <= exc.code < 400 else 21
    except (TimeoutError, socket.timeout):
        return 23
    except (OSError, urllib.error.URLError):
        return 24
    except (ValueError, TypeError):
        return 25


def scan(config, runner=run_bounded, clock=time.monotonic, wall_clock=time.time):
    budget = Budget(config["scan_budget_seconds"], clock)
    generated_at = wall_clock()
    reader = ProcReader(config["proc_root"], budget, config["max_proc_bytes"], config["host_passwd_path"])
    try:
        match = re.search(r"^btime[ \t]+([0-9]{1,20})[ \t]*$", reader.text("stat", 65536), re.M)
        if match is None:
            raise ScanError("boot_time_unavailable")
        boot_time = int(match.group(1))
        if not 0 < boot_time <= generated_at:
            raise ScanError("boot_time_unavailable") from None
    except (OSError, ValueError, ScanError):
        # Uptime approximations drift between scans and change instance identity.
        raise ScanError("boot_time_unavailable") from None
    processes, complete, errors = discover(reader, config)
    candidates = {}
    for pid, process in processes.items():
        try:
            budget.check()
            engine = engine_for(process, config["match_rules"])
            if engine:
                if len(candidates) >= config["max_services"]:
                    raise ScanError("service_limit")
                candidates[pid] = engine
        except ScanError as exc:
            errors.add(str(exc))
            complete = False
            break
    services = {}
    for pid, engine in candidates.items():
        try:
            if ancestor_service(processes[pid].ppid, processes, candidates, budget) is None:
                services[pid] = service_row(processes[pid], engine, config, boot_time, os.sysconf("SC_CLK_TCK"))
        except BudgetExceeded:
            # Preserve discovered candidates as unknown rows when traversal cannot
            # finish; incomplete inventory forbids consumer-side lifecycle closure.
            complete = False
            errors.add("scan_budget_exceeded")
            services[pid] = service_row(processes[pid], engine, config, boot_time, os.sysconf("SC_CLK_TCK"))
    gpus, apps, gpu_ok, apps_ok, gpu_errors = gpu_inventory(config, budget, runner)
    errors.update(gpu_errors)
    other, attribution_ok = attribute_gpus(reader, processes, services, gpus, apps, gpu_ok and apps_ok and complete)
    if not attribution_ok:
        errors.add("gpu_attribution_incomplete")
    for pid, row in services.items():
        if budget.remaining() <= 0.05:
            errors.add("scan_budget_exceeded")
            continue
        scrape_service(reader, processes[pid], row, config, runner)
        try:
            metadata = host_metadata(reader, processes[pid])
            if metadata["host"] is True:
                row.update(host_uid=metadata["host_uid"], host_user=metadata["host_user"])
        except BudgetExceeded:
            errors.add("scan_budget_exceeded")
    if attribution_ok and any(not row["gpu_observation_complete"] for row in services.values()):
        attribution_ok = False
        errors.add("gpu_attribution_incomplete")
    return {
        "schema_version": 1, "generated_at": generated_at,
        "sample_interval_seconds": config["sample_interval_seconds"],
        "scan_duration_ms": round((clock() - budget.started) * 1000),
        "inventory_complete": complete, "gpu_inventory_complete": gpu_ok,
        "gpu_attribution_complete": attribution_ok,
        "host": {"gpu_count": len(gpus) if gpu_ok else None}, "gpus": gpus,
        "services": sorted(services.values(), key=lambda row: row["id"]),
        "other_gpu_processes": other, "errors": sorted(errors),
    }


def atomic_write(path, snapshot, max_bytes=MAX_SNAPSHOT):
    data = json.dumps(snapshot, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode() + b"\n"
    if len(data) > max_bytes:
        raise ScanError("snapshot_limit")
    path = Path(path)
    # Directory provisioning belongs to installation; a failed scan creates none.
    fd, temporary = tempfile.mkstemp(prefix=".fleet-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o644)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/llmsvc/fleet-scan.json")
    parser.add_argument("--dry-run", action="store_true", help="scan and print JSON without writing the export")
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument("--http-helper", help=argparse.SUPPRESS)
    parser.add_argument("--listener-helper", type=int, nargs=2, metavar=("PORT", "INODE"), help=argparse.SUPPRESS)
    parser.add_argument("--helper-limit", type=int, default=MAX_RESPONSE, help=argparse.SUPPRESS)
    parser.add_argument("--helper-timeout", type=float, default=2, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.http_helper:
        return http_helper(args.http_helper, args.helper_limit, args.helper_timeout)
    if args.listener_helper:
        port, inode = args.listener_helper
        print(json.dumps({"bind": "::", "port": port, "inode": inode,
                          "ipv6_only": socket_ipv6_only(port, inode, args.helper_timeout)}))
        return 0
    try:
        config = load_config(args.config)
        if args.check_config:
            print(json.dumps({"event": "fleet_scan_config_valid", "ok": True}))
            return 0
        started = time.monotonic()
        snapshot = scan(config)
        if time.monotonic() - started >= config["scan_budget_seconds"]:
            raise ScanError("scan_budget_exceeded")
        if args.dry_run:
            data = json.dumps(snapshot, allow_nan=False, ensure_ascii=True)
            if len(data.encode()) > config["max_snapshot_bytes"]:
                raise ScanError("snapshot_limit")
            print(data)
        else:
            atomic_write(config["output_path"], snapshot, config["max_snapshot_bytes"])
        return 0
    except Exception:
        # Keep all private paths, process values, and HTTP bodies out of journal.
        print(json.dumps({"event": "fleet_scan_failed", "error": "scan_failed", "snapshot_retained": True}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
