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
        return int(fields[19])
    except (FileNotFoundError, IndexError, ValueError):
        return None


def is_running() -> tuple[bool, Optional[int]]:
    if not PID_FILE.exists():
        return False, None
    data = json.loads(PID_FILE.read_text())
    pid = data["pid"]
    actual_start = _get_start_time(pid)
    if actual_start != data["start_time"]:
        PID_FILE.unlink(missing_ok=True)
        return False, None
    return True, pid


def read_metadata() -> dict:
    if not PID_FILE.exists():
        return {}
    return json.loads(PID_FILE.read_text())


def _tee_stderr(proc, log_file: Path):
    with open(log_file, "a") as log:
        for line in proc.stderr:
            sys.stderr.buffer.write(line)
            sys.stderr.buffer.flush()
            log.write(line.decode(errors="replace"))
            log.flush()


def start(cmd: list[str], log_file: Path, model: str | None = None) -> int:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    log = open(log_file, "a")
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.PIPE)
    threading.Thread(target=_tee_stderr, args=(proc, log_file), daemon=True).start()
    metadata = {"pid": proc.pid, "start_time": _get_start_time(proc.pid)}
    if model is not None:
        metadata["model"] = model
    PID_FILE.write_text(json.dumps(metadata))
    return proc.pid


def stop() -> bool:
    running, pid = is_running()
    if not running:
        return False
    os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        time.sleep(1)
        if _get_start_time(pid) is None:
            PID_FILE.unlink(missing_ok=True)
            return True
    return False
