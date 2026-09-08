# Generated-By: Codex / gpt-6-astra
"""Strict parsers for bounded, read-only observations (memory is GiB)."""

import csv
import io
import math
import re
import shlex
from typing import Optional

from llmsvc.state import GPUProcess, GPUState


def number(value: str) -> Optional[float]:
    try:
        result = float(value.strip())
        return result if math.isfinite(result) and result >= 0 else None
    except (TypeError, ValueError):
        return None


def parse_gpus(text: str) -> tuple[GPUState, ...]:
    result = []
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        if len(row) != 6 or not row[0].strip().isdigit():
            raise ValueError("invalid GPU row")
        values = [number(v) for v in row[2:]]
        result.append(GPUState(
            index=int(row[0]), uuid=row[1].strip(),
            total_gb=values[0] / 1024 if values[0] is not None else None,
            used_gb=values[1] / 1024 if values[1] is not None else None,
            free_gb=values[2] / 1024 if values[2] is not None else None,
            utilization_percent=values[3],
        ))
    if len({gpu.index for gpu in result}) != len(result):
        raise ValueError("duplicate GPU index")
    return tuple(sorted(result, key=lambda g: g.index))


def parse_processes(text: str) -> dict[str, tuple[GPUProcess, ...]]:
    result = {}
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        if len(row) != 4 or not row[1].strip().isdigit():
            raise ValueError("invalid GPU process row")
        memory = number(row[2])
        process = GPUProcess(int(row[1]), used_gb=memory / 1024 if memory is not None else None,
                             name=row[3].strip())
        result.setdefault(row[0].strip(), []).append(process)
    return {uuid: tuple(rows) for uuid, rows in result.items()}


def parse_units(text: str) -> dict[str, dict]:
    """Extract only managed service properties, ignoring timers and other units.

    Unknown/non-numeric CUDA selectors (including TP) are left unknown; the
    control plane's current contract supports a single numeric GPU per model.
    """
    result = {}
    for block in re.split(r"\n\s*\n", text.strip()):
        fields = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if fields and "Id" not in fields:
            raise ValueError("unit status missing Id")
        unit = fields.get("Id", "")
        if not re.fullmatch(r"vllm-.+\.service", unit):
            continue
        if unit in result:
            raise ValueError("duplicate unit")
        env = dict(token.split("=", 1) for token in shlex.split(fields.get("Environment", "")) if "=" in token)
        cmd = fields.get("ExecStart", "")
        flags = {}
        for flag in ("port", "gpu-memory-utilization", "tensor-parallel-size"):
            match = re.search(r"--" + flag + r"(?:=|\s+)([0-9.]+)(?=\s|;|$)", cmd)
            flags[flag] = number(match.group(1)) if match else None
        gpu = env.get("CUDA_VISIBLE_DEVICES", "")
        if flags['tensor-parallel-size'] not in (None, 1):
            gpu = ""
        active = fields.get("ActiveState")
        # Activating/deactivating services may still own resources.
        unit_active = True if active in ("active", "activating", "deactivating", "reloading") else (
            False if active in ("inactive", "failed") else None)
        pid = fields.get("MainPID", "")
        port = flags["port"]
        result[unit] = dict(
            unit=unit, model=unit[len("vllm-"):-len(".service")],
            unit_active=unit_active, active_state=active, sub_state=fields.get("SubState"),
            main_pid=int(pid) if pid.isdigit() else None, cgroup=fields.get("ControlGroup"),
            exit_status=fields.get("ExecMainStatus"), result=fields.get("Result"),
            gpu=int(gpu) if gpu.isdigit() else None,
            port=int(port) if port is not None and port.is_integer() and 0 < port < 65536 else None,
            util=flags["gpu-memory-utilization"] if flags["gpu-memory-utilization"] is not None and 0 < flags["gpu-memory-utilization"] <= 1 else None,
        )
    return result


def parse_running(payload: dict) -> dict[str, str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("running"), list):
        raise ValueError("invalid running response")
    result = {}
    for entry in payload["running"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("model"), str):
            raise ValueError("invalid running model")
        state = entry.get("state")
        if state not in ("starting", "ready", "stopping", "stopped"):
            raise ValueError("invalid running state")
        result[entry["model"]] = state
    return result


def parse_sleeping(payload: dict) -> bool:
    if not isinstance(payload, dict) or type(payload.get("is_sleeping")) is not bool:
        raise ValueError("invalid is_sleeping response")
    return payload["is_sleeping"]


def parse_meminfo(text: str) -> float:
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            match = re.fullmatch(r"MemAvailable:\s+(\d+)\s+kB\s*", line)
            if match:
                return int(match.group(1)) / 1024**2
    raise ValueError("MemAvailable unavailable")
