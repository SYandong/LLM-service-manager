# Generated-By: Claude Code / claude-fable-5-1
"""Native-maintenance profile rows for models the registry adds or removes.

The native adapter refuses a candidate llama-swap configuration whose models
have no row in its own private profile, and it rebuilds each model's stop
helper from that same profile. An imported model therefore needs its row
written before its candidate is validated, and a removed model's row is dropped
only once the transaction is released: the adapter hashes the profile into
every scope observation, so an edit mid-transaction would invalidate the
instance identity it captured.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from llmsvc.reload import ReloadError, _read_regular_file
from llmsvc.registry import ModelRegistry, RegistryError, _command_argv

PROFILE_MAX_BYTES = 1024 * 1024


def maintenance_profile_path(command: Sequence[str]) -> str | None:
    """Return the ``--profile`` argument of the configured adapter argv."""
    for index, token in enumerate(command):
        if token == "--profile" and index + 1 < len(command):
            return command[index + 1]
        if token.startswith("--profile="):
            return token.split("=", 1)[1]
    return None


def candidate_profile_entries(candidate: bytes, names: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Derive one native profile row per named model of a candidate config."""
    models = ModelRegistry._decode(candidate)[0]["models"]
    entries: dict[str, dict[str, Any]] = {}
    for name in names:
        block = models[name]
        if not isinstance(block, dict):
            raise RegistryError("model block must be a mapping")
        argv = _command_argv(block.get("cmd"))
        entries[name] = {"unit": "vllm-" + name + ".service",
                         "backend_origin": _backend_origin(argv),
                         "process_argv": argv}
    return entries


def _upstream_url(argv: Sequence[str]) -> str | None:
    for index, token in enumerate(argv):
        if token == "--vllm-url" and index + 1 < len(argv):
            return argv[index + 1]
        if token.startswith("--vllm-url="):
            return token.split("=", 1)[1]
    return None


def _backend_origin(argv: Sequence[str]) -> str:
    """Render the wrapper's own upstream URL as a literal-IP HTTP origin."""
    value = _upstream_url(argv)
    if value is None:
        raise RegistryError("model command must include --vllm-url for a native maintenance profile")
    parsed = urlsplit(value)
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
    except ValueError as exc:
        raise RegistryError("native maintenance needs a literal http://IP:PORT --vllm-url, not " + value) from exc
    if parsed.scheme != "http" or port is None:
        raise RegistryError("native maintenance needs a literal http://IP:PORT --vllm-url, not " + value)
    host = "[%s]" % address if address.version == 6 else str(address)
    return "http://%s:%d" % (host, port)


class NativeMaintenanceProfile:
    """Bounded, atomic edits to the adapter's private profile model map.

    Every edit rewrites the whole document through a temporary file and a
    rename, so a reader never sees a half-written profile. Fields outside
    ``models`` and the order of existing keys are preserved.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def add(self, entries: Mapping[str, Mapping[str, Any]]) -> bytes:
        """Write one row per imported model; returns the bytes to restore."""
        raw, data = self._load()
        data["models"].update(copy.deepcopy(dict(entries)))
        self._store(data)
        return raw

    def remove(self, names: Sequence[str]) -> None:
        """Drop the rows of retired models, leaving every other field alone."""
        _, data = self._load()
        if not any(name in data["models"] for name in names):
            return
        for name in names:
            data["models"].pop(name, None)
        self._store(data)

    def restore(self, raw: bytes) -> None:
        """Put back the exact bytes captured before a failed submission."""
        self._write(raw)

    def _load(self) -> tuple[bytes, dict]:
        try:
            raw, _ = _read_regular_file(self.path, PROFILE_MAX_BYTES)
        except (OSError, ReloadError) as exc:
            raise RegistryError("native maintenance profile is unreadable: " + str(self.path)) from exc
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise RegistryError("native maintenance profile is not valid JSON: " + str(self.path)) from exc
        if not isinstance(data, dict) or not isinstance(data.get("models"), dict):
            raise RegistryError("native maintenance profile has no models mapping: " + str(self.path))
        return raw, data

    def _store(self, data: dict) -> None:
        self._write(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n")

    def _write(self, payload: bytes) -> None:
        handle, staged = tempfile.mkstemp(prefix=".llmsvc-profile-", suffix=".json", dir=self.path.parent)
        temporary = Path(staged)
        try:
            with os.fdopen(handle, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        directory = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
