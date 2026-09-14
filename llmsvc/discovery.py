# Generated-By: Claude Code / claude-fable-5-1
"""Read-only discovery of importable model directories under the shared roots.

Discovery is not admission. A scan reads at most one bounded ``llmsvc.json``
per one-level subdirectory and stats weight files; it never writes a file,
allocates a port, touches the llama-swap configuration or starts a unit. Import
stays an explicit action that re-reads the same descriptor through the existing
registry validation path.
"""

from __future__ import annotations

import math
import os
import stat
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from llmsvc.reload import _source_byte_limit
from llmsvc.registry import (
    DEFAULT_MODEL_CONFIG_MAX_BYTES,
    DEFAULT_WEIGHT_INDEX_MAX_BYTES,
    ImportOverrides,
    RegistryError,
    _read_model_json,
    validate_safe_model_name,
)


CONFIG_FILENAME = "llmsvc.json"
MAX_CANDIDATES = 200
MAX_ALIASES = 16
MAX_MODEL_LEN = 2**31 - 1
SUPPORTED_KEYS = ("base", "name", "util", "max_model_len", "aliases", "weights_gb")
NAME_CHARACTERS = "abcdefghijklmnopqrstuvwxyz0123456789._-"
REJECTED_KEYS = {
    "is_default": "llmsvc.json must not set is_default; an imported model is never the default model",
    "cmd": "llmsvc.json must not carry a command; the command is cloned from base",
    "cmdStop": "llmsvc.json must not carry a command; the command is cloned from base",
    "argv": "llmsvc.json must not carry a command; the command is cloned from base",
    "command": "llmsvc.json must not carry a command; the command is cloned from base",
    "env": "llmsvc.json must not carry environment or shell fragments",
    "shell": "llmsvc.json must not carry environment or shell fragments",
}


@dataclass(frozen=True)
class Candidate:
    """One discovered directory; ``status`` never asserts runtime adoption."""

    name: str
    path: str
    base: str | None = None
    status: str = "importable"
    reason: str | None = None
    overrides: ImportOverrides = field(default_factory=ImportOverrides)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "path": self.path, "base": self.base,
                "util": self.overrides.util, "weights_gb": self.overrides.weights_gb,
                "status": self.status, "reason": self.reason}


def display_name(value: str) -> str:
    """Keep an unusable directory name printable without inventing a model id."""
    return "".join(ch if ch.isprintable() else "?" for ch in value)[:128] or "?"


def normalize_model_name(directory_name: str) -> str:
    """Derive the default model name from a directory name, or reject it."""
    lowered = str(directory_name).lower()
    mapped = "".join(ch if ch in NAME_CHARACTERS else "-" for ch in lowered)
    return validate_safe_model_name(mapped.lstrip("-._")[:128])


def parse_import_config(document: Any, *, directory_name: str) -> tuple[str, str, ImportOverrides]:
    """Validate one ``llmsvc.json`` descriptor into a name, base and overrides."""
    if not isinstance(document, Mapping):
        raise RegistryError(CONFIG_FILENAME + " must contain a JSON object")
    for key, message in REJECTED_KEYS.items():
        if key in document:
            raise RegistryError(message)
    unknown = sorted(str(key) for key in document if key not in SUPPORTED_KEYS)
    if unknown:
        raise RegistryError(CONFIG_FILENAME + " has unsupported keys: " + ", ".join(unknown)
                            + "; supported keys are " + ", ".join(SUPPORTED_KEYS))
    base = document.get("base")
    if not isinstance(base, str) or not base:
        raise RegistryError(CONFIG_FILENAME + " requires base, the permanent model to clone")
    base = validate_safe_model_name(base)
    if "name" in document and document["name"] is not None:
        if not isinstance(document["name"], str):
            raise RegistryError(CONFIG_FILENAME + " name must be a string")
        name = validate_safe_model_name(document["name"])
    else:
        name = normalize_model_name(directory_name)
    return name, base, ImportOverrides(util=_optional_util(document.get("util")),
                                       max_model_len=_optional_max_model_len(document.get("max_model_len")),
                                       aliases=_optional_aliases(document.get("aliases")),
                                       weights_gb=_optional_weights_gb(document.get("weights_gb")))


def measure_weights_gb(model_path: str | Path, shared_roots: Sequence[str | Path], *,
                       weight_index_max_bytes: int = DEFAULT_WEIGHT_INDEX_MAX_BYTES) -> float | None:
    """Sum deduplicated safetensors sizes; return None when none are present.

    The shard index names the authoritative file set when it exists. Sizes are
    filesystem metadata, not a measured GPU allocation or a residency promise.
    """
    directory = Path(model_path)
    roots = tuple(Path(root) for root in shared_roots)
    indexes = sorted(child for child in directory.iterdir()
                     if child.name.endswith(".safetensors.index.json"))
    if len(indexes) > 1:
        raise RegistryError("model directory contains multiple shard index files")
    if indexes:
        document = _read_model_json(indexes[0], roots, weight_index_max_bytes, "shard index")
        weight_map = document.get("weight_map") if isinstance(document, dict) else None
        if not isinstance(weight_map, dict) or not weight_map:
            raise RegistryError("shard index must contain a weight_map object")
        names = sorted({value for value in weight_map.values()})
        if not all(isinstance(value, str) and value for value in names):
            raise RegistryError("shard index weight_map must name weight files")
        for value in names:
            if Path(value).is_absolute() or ".." in Path(value).parts:
                raise RegistryError("shard index contains an unsafe weight path")
        files = [directory / value for value in names]
    else:
        files = sorted(child for child in directory.iterdir()
                       if child.name.endswith(".safetensors"))
    if not files:
        return None
    total = 0
    for path in files:
        try:
            info = os.lstat(path)
        except OSError as exc:
            raise RegistryError("weight file is not accessible: " + display_name(path.name)) from exc
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
            raise RegistryError("weight files must be non-empty regular files")
        total += info.st_size
    return total / 1024 ** 3


class ModelDiscovery:
    """Cached one-level scan of the configured shared roots.

    The cheap survey pass stats each subdirectory and its descriptor; the full
    parse and weight measurement only rerun when that fingerprint changes.
    """

    def __init__(self, shared_roots: Sequence[str | Path], *,
                 model_config_max_bytes: int = DEFAULT_MODEL_CONFIG_MAX_BYTES,
                 weight_index_max_bytes: int = DEFAULT_WEIGHT_INDEX_MAX_BYTES,
                 max_candidates: int = MAX_CANDIDATES):
        if not shared_roots:
            raise ValueError("at least one shared root is required")
        if type(max_candidates) is not int or max_candidates < 1:
            raise ValueError("max_candidates must be a positive integer")
        self.roots = tuple(str(Path(root)) for root in shared_roots)
        self.model_config_max_bytes = _source_byte_limit(model_config_max_bytes, "model_config_max_bytes")
        self.weight_index_max_bytes = _source_byte_limit(weight_index_max_bytes, "weight_index_max_bytes")
        self.max_candidates = max_candidates
        self._lock = threading.Lock()
        self._fingerprint: tuple | None = None
        self._candidates: tuple[Candidate, ...] = ()

    def scan(self) -> tuple[Candidate, ...]:
        """Return cached candidates, rescanning only when the survey changed."""
        with self._lock:
            entries, fingerprint = self._survey()
            if fingerprint != self._fingerprint:
                self._candidates = self._build(entries)
                self._fingerprint = fingerprint
            return self._candidates

    def candidates(self, configured_names: Iterable[str] = ()) -> tuple[Candidate, ...]:
        """Overlay ``imported`` for names already present in the configuration."""
        known = {str(name) for name in configured_names}
        return tuple(replace(item, status="imported", reason=None)
                     if item.status == "importable" and item.name in known else item
                     for item in self.scan())

    def resolve(self, name: str, configured_names: Iterable[str] = ()) -> Candidate:
        """Return one importable candidate or explain why it cannot be imported.

        An unusable directory whose displayed name happens to collide never
        shadows a real candidate, so a listed import stays importable.
        """
        matches = [item for item in self.candidates(configured_names) if item.name == name]
        for item in matches:
            if item.status == "importable":
                return item
        for item in matches:
            if item.status == "imported":
                raise RegistryError("model is already configured: " + display_name(name))
            raise RegistryError("model is not importable: " + str(item.reason))
        raise RegistryError("no discovered model named " + display_name(name)
                            + " with an " + CONFIG_FILENAME + " under the configured shared roots")

    def _survey(self) -> tuple[tuple[tuple[str, str, str], ...], tuple]:
        """One cheap metadata pass over each root's direct children."""
        entries: list[tuple[str, str, str]] = []
        fingerprint: list[Any] = []
        for root in self.roots:
            try:
                with os.scandir(root) as listing:
                    names = sorted(item.name for item in listing)
                fingerprint.append((root, os.stat(root).st_mtime_ns))
            except OSError:
                entries.append((os.path.basename(root.rstrip("/")) or root, root, "unreadable_root"))
                fingerprint.append((root, "unreadable"))
                continue
            for name in names:
                path = os.path.join(root, name)
                try:
                    info = os.lstat(path)
                except OSError:
                    continue
                if stat.S_ISLNK(info.st_mode):
                    entries.append((name, path, "symlink"))
                    fingerprint.append((path, "symlink"))
                    continue
                if not stat.S_ISDIR(info.st_mode):
                    continue
                try:
                    descriptor = os.lstat(os.path.join(path, CONFIG_FILENAME))
                except OSError:
                    continue
                if not stat.S_ISREG(descriptor.st_mode):
                    continue
                entries.append((name, path, "directory"))
                fingerprint.append((path, info.st_mtime_ns, descriptor.st_mtime_ns, descriptor.st_size))
        entries.sort()
        return tuple(entries), tuple(fingerprint)

    def _build(self, entries: Sequence[tuple[str, str, str]]) -> tuple[Candidate, ...]:
        candidates: list[Candidate] = []
        claimed: set[str] = set()
        for name, path, kind in entries:
            if len(candidates) >= self.max_candidates:
                break
            if kind == "unreadable_root":
                candidates.append(Candidate(display_name(name), path, status="invalid",
                                            reason="configured shared root is not readable"))
                continue
            if kind == "symlink":
                candidates.append(Candidate(display_name(name), path, status="invalid",
                                            reason="symlinked directories are not scanned"))
                continue
            candidate = self._read(name, path)
            if candidate.status == "importable" and candidate.name in claimed:
                candidate = replace(candidate, status="invalid",
                                    reason="another discovered directory already claims this name")
            if candidate.status == "importable":
                claimed.add(candidate.name)
            candidates.append(candidate)
        candidates.sort(key=lambda item: (item.name, item.path))
        return tuple(candidates)

    def _read(self, directory_name: str, path: str) -> Candidate:
        try:
            document = _read_model_json(Path(path) / CONFIG_FILENAME, tuple(Path(r) for r in self.roots),
                                        self.model_config_max_bytes, CONFIG_FILENAME)
            name, base, overrides = parse_import_config(document, directory_name=directory_name)
            if overrides.weights_gb is None:
                overrides = replace(overrides, weights_gb=measure_weights_gb(
                    path, self.roots, weight_index_max_bytes=self.weight_index_max_bytes))
            return Candidate(name, path, base=base, status="importable", overrides=overrides)
        except (RegistryError, OSError, ValueError) as exc:
            return Candidate(display_name(directory_name), path, status="invalid", reason=str(exc))


def _optional_util(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise RegistryError("util must be a finite number greater than 0 and at most 1")
    if not 0 < float(value) <= 1:
        raise RegistryError("util must be a finite number greater than 0 and at most 1")
    return float(value)


def _optional_max_model_len(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= MAX_MODEL_LEN:
        raise RegistryError("max_model_len must be a positive integer")
    return value


def _optional_aliases(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MAX_ALIASES:
        raise RegistryError("aliases must be a list of at most %d model names" % MAX_ALIASES)
    aliases = tuple(validate_safe_model_name(item) for item in value)
    if len(set(aliases)) != len(aliases):
        raise RegistryError("aliases must be distinct")
    return aliases


def _optional_weights_gb(value: Any) -> float | None:
    if value is None:
        return None
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise RegistryError("weights_gb must be a finite positive number")
    return float(value)
