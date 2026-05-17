import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).parent.parent
PID_FILE = _PROJECT_ROOT / "var" / "run" / "vllm.pid"


def _get_start_time(pid: int) -> Optional[int]:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        idx = stat.rfind(")")
        fields = stat[idx + 2:].split()
        if fields[0] == "Z":
            return None
        return int(fields[19])
    except (FileNotFoundError, IndexError, ValueError):
        return None


def _pid_file(pid_file: Path | None = None) -> Path:
    return PID_FILE if pid_file is None else pid_file


def is_running(pid_file: Path | None = None) -> tuple[bool, Optional[int]]:
    pid_file = _pid_file(pid_file)
    if not pid_file.exists():
        return False, None
    data = json.loads(pid_file.read_text())
    pid = data["pid"]
    actual_start = _get_start_time(pid)
    if actual_start is None or actual_start != data["start_time"]:
        pid_file.unlink(missing_ok=True)
        return False, None
    return True, pid


def read_metadata(pid_file: Path | None = None) -> dict:
    pid_file = _pid_file(pid_file)
    if not pid_file.exists():
        return {}
    return json.loads(pid_file.read_text())


def _tee_stderr(proc, log_file: Path):
    with open(log_file, "a") as log:
        for line in proc.stderr:
            sys.stderr.buffer.write(line)
            sys.stderr.buffer.flush()
            log.write(line.decode(errors="replace"))
            log.flush()


def start(
    cmd: list[str],
    log_file: Path,
    model: str | None = None,
    pid_file: Path | None = None,
    tee_stderr: bool = True,
    env: dict[str, str] | None = None,
) -> int:
    pid_file = _pid_file(pid_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    log = open(log_file, "a")
    stderr = subprocess.PIPE if tee_stderr else log
    proc = subprocess.Popen(cmd, stdout=log, stderr=stderr, start_new_session=True, env=env)
    if tee_stderr:
        threading.Thread(target=_tee_stderr, args=(proc, log_file), daemon=True).start()
    metadata = {"pid": proc.pid, "start_time": _get_start_time(proc.pid)}
    if model is not None:
        metadata["model"] = model
    pid_file.write_text(json.dumps(metadata))
    return proc.pid


def stop(pid_file: Path | None = None) -> bool:
    pid_file = _pid_file(pid_file)
    running, pid = is_running(pid_file)
    if not running:
        return False
    os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        time.sleep(1)
        if _get_start_time(pid) is None:
            pid_file.unlink(missing_ok=True)
            return True
    return False
