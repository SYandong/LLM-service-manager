# Generated-By: Codex / gpt-6-astra
"""Read-only system and HTTP probes. No inference or lifecycle endpoints."""

import json
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

from .parsers import parse_gpus, parse_meminfo, parse_processes, parse_running, parse_sleeping, parse_units


class Probes:
    def __init__(self, swap_url, *, timeout=0.5, nvidia_smi="nvidia-smi",
                 systemctl="systemctl", proc_root="/proc", host_meminfo_path=None):
        self.swap_url = swap_url.rstrip("/")
        self.timeout = timeout
        self.nvidia_smi = nvidia_smi
        self.systemctl = systemctl
        self.proc_root = Path(proc_root)
        self.host_meminfo_path = Path(host_meminfo_path) if host_meminfo_path else None
        # Local probes must not depend on inherited HTTP_PROXY configuration.
        self.opener = build_opener(ProxyHandler({}))

    def command(self, args):
        return subprocess.run(args, check=True, capture_output=True, text=True,
                              timeout=self.timeout).stdout

    def gpus(self):
        return parse_gpus(self.command([self.nvidia_smi,
            "--query-gpu=index,uuid,memory.total,memory.used,memory.free,utilization.gpu",
            "--format=csv,noheader,nounits"]))

    def processes(self):
        return parse_processes(self.command([self.nvidia_smi,
            "--query-compute-apps=gpu_uuid,pid,used_gpu_memory,process_name",
            "--format=csv,noheader,nounits"]))

    def units(self):
        return parse_units(self.command([self.systemctl, "show", "vllm-*.service",
            "--property=Id,LoadState,ActiveState,SubState,MainPID,ControlGroup,Environment,ExecStart,ExecMainStatus,Result"]))

    def memory(self):
        if self.host_meminfo_path is None:
            raise ValueError("trusted host meminfo path not configured")
        return parse_meminfo(self.host_meminfo_path.read_text())

    def owner(self, pid, units):
        """Require cgroup evidence; MainPID alone is unsafe across PID namespaces."""
        try:
            groups = [line.split(":", 2)[2] for line in
                      (self.proc_root / str(pid) / "cgroup").read_text().splitlines()]
        except (OSError, IndexError):
            return None
        for unit in units.values():
            group = unit.get("cgroup")
            if group and any(path == group or path.startswith(group + "/") for path in groups):
                return unit["model"]
        return None

    def json(self, url):
        with self.opener.open(url, timeout=self.timeout) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("probe response exceeds limit")
        return json.loads(raw)

    def running(self):
        return parse_running(self.json(self.swap_url + "/running"))

    def sleeping(self, url):
        return parse_sleeping(self.json(url.rstrip("/") + "/is_sleeping"))

    def health(self, url):
        try:
            with self.opener.open(url.rstrip("/") + "/health", timeout=self.timeout) as response:
                return response.status == 200
        except HTTPError as exc:
            # An explicit daemon rejection is a health failure; a failed probe
            # (connection, DNS, timeout) propagates and becomes unknown.
            exc.close()
            return False

    def events(self):
        from .events import EventSnapshot
        snapshot = EventSnapshot()
        deadline = time.monotonic() + self.timeout
        consumed = 0
        frame = []
        with self.opener.open(self.swap_url + "/api/events", timeout=self.timeout) as response:
            while time.monotonic() < deadline:
                line = response.readline(2 * 1024 * 1024 + 1)
                consumed += len(line)
                if not line or consumed > 4 * 1024 * 1024:
                    raise ValueError("incomplete or oversized event snapshot")
                if line.strip() == b"":
                    if frame:
                        snapshot.feed(json.loads(b"\n".join(frame)))
                        frame = []
                    if snapshot.complete:
                        return snapshot
                elif line.startswith(b"data:"):
                    frame.append(line[5:].lstrip())
        raise TimeoutError("event snapshot deadline")
