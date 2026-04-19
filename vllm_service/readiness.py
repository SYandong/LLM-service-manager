import time
import urllib.request


def is_ready(host: str, port: int) -> bool:
    try:
        urllib.request.urlopen(f"http://{host}:{port}/v1/models", timeout=5)
        return True
    except Exception:
        return False


def wait_until_ready(host: str, port: int, timeout: int = 600, alive_fn=None) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if alive_fn is not None and not alive_fn():
            return False
        if is_ready(host, port):
            return True
        time.sleep(5)
    return False
