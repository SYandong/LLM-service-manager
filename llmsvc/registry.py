# Generated-By: Codex / gpt-6-astra
"""Pure helpers for temporary full-weight model registry updates."""

from __future__ import annotations

import copy
import json
import math
import re
import shlex
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

from llmsvc.reload import ReloadQueue
from urllib.parse import urlsplit, urlunsplit

from llmsvc.state import Action, Activity, Blocker, ModelState, StateSnapshot


SAFE_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
DEFAULT_EXPIRY_SECONDS = 7 * 24 * 60 * 60
WEIGHT_SUFFIXES = (".safetensors", ".bin", ".pt")
LORA_WEIGHT_NAMES = {"adapter_model.bin", "adapter_model.safetensors"}
SHELL_TOKENS = {";", "|", "||", "&&", "<", ">", ">>"}
SHARD_PATTERN = re.compile(r"^.+-(\d{5})-of-(\d{5})\.(safetensors|bin|pt)$")


class RegistryError(ValueError):
    """Raised when a registry request cannot be safely applied."""


@dataclass(frozen=True)
class ModelPathInfo:
    path: str
    config: Mapping[str, Any]
    weight_files: tuple[str, ...]


@dataclass(frozen=True)
class RegistryAddResult:
    config: dict[str, Any]
    records: dict[str, dict[str, Any]]
    record: dict[str, Any]


@dataclass(frozen=True)
class RemovalPlan:
    allowed: bool
    blockers: tuple[Blocker, ...] = ()
    actions: tuple[Action, ...] = ()
    last_used_at: float | None = None


@dataclass(frozen=True)
class RegistryRemoveResult:
    config: dict[str, Any]
    records: dict[str, dict[str, Any]]
    record: dict[str, Any]
    actions: tuple[Action, ...]


def validate_safe_model_name(name: str) -> str:
    """Return a validated model name safe for llama-swap ids and unit suffixes."""
    if not isinstance(name, str) or not SAFE_MODEL_NAME.fullmatch(name):
        raise RegistryError("model name must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}")
    if name in {".", ".."} or name.startswith(("vllm-", "-")):
        raise RegistryError("model name uses a reserved or unsafe prefix")
    return name


def validate_full_weight_model_dir(path: str | Path, shared_roots: Sequence[str | Path]) -> ModelPathInfo:
    """Validate a readable full-weight model directory confined to shared roots."""
    try:
        model_path = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RegistryError("model path is not accessible") from exc
    if any(token in str(model_path) for token in ("$", "`", "\n", "\r")):
        raise RegistryError("model path contains command macro characters")
    try:
        roots = tuple(Path(root).expanduser().resolve(strict=True) for root in shared_roots)
    except (OSError, RuntimeError) as exc:
        raise RegistryError("configured shared root is not accessible") from exc
    if not roots:
        raise RegistryError("at least one shared root is required")
    if not model_path.is_dir():
        raise RegistryError("model path must be a directory")
    if not _is_under_any(model_path, roots):
        raise RegistryError("model path is outside configured shared roots")

    config_path = model_path / "config.json"
    if not config_path.is_file():
        raise RegistryError("model directory must contain config.json")
    _reject_symlink_escapes(model_path, roots)

    try:
        with config_path.open("r", encoding="utf-8") as handle:
            model_config = json.load(handle)
    except OSError as exc:
        raise RegistryError("config.json is not readable") from exc
    except json.JSONDecodeError as exc:
        raise RegistryError("config.json is not valid JSON") from exc
    if not isinstance(model_config, dict):
        raise RegistryError("config.json must contain a JSON object")

    weight_files = _find_weight_files(model_path)
    if not weight_files:
        if (model_path / "adapter_config.json").exists() or any((model_path / name).exists() for name in LORA_WEIGHT_NAMES):
            raise RegistryError("LoRA adapter directories are not full-weight models")
        raise RegistryError("model directory must contain full weights or shard files")
    if set(weight_files).issubset(LORA_WEIGHT_NAMES):
        raise RegistryError("LoRA adapter weights are not full model weights")
    if str(model_config.get("peft_type", "")).strip():
        raise RegistryError("LoRA/PEFT config is not a full-weight model")
    return ModelPathInfo(path=str(model_path), config=model_config, weight_files=weight_files)


def add_full_weight_model(
    config: Mapping[str, Any],
    temporary_records: Mapping[str, Mapping[str, Any]],
    *,
    name: str,
    model_path: str | Path,
    base_model: str,
    shared_roots: Sequence[str | Path],
    daemon_port_range: tuple[int, int],
    reserved_ports: Sequence[int] = (),
    created_at: float,
) -> RegistryAddResult:
    """Return detached config and temporary records with one full-weight model added."""
    name = validate_safe_model_name(name)
    base_model = validate_safe_model_name(base_model)
    created_at = _finite_float(created_at, "created_at")
    path_info = validate_full_weight_model_dir(model_path, shared_roots)

    new_config = copy.deepcopy(dict(config))
    new_records = copy.deepcopy({key: dict(value) for key, value in temporary_records.items()})
    models = _models_mapping(new_config)
    if name in _reserved_model_ids(new_config, new_records):
        raise RegistryError("model name already exists")
    if base_model in new_records:
        raise RegistryError("base model must be a permanent llama-swap model")
    if base_model not in models:
        raise RegistryError("base model is not present in llama-swap config")

    base_block = copy.deepcopy(models[base_model])
    if not isinstance(base_block, dict):
        raise RegistryError("base model config block must be a mapping")

    daemon_port = _choose_daemon_port(new_config, new_records, daemon_port_range, reserved_ports)
    new_block = _clone_model_block(base_model, name, base_block, path_info.path, daemon_port)
    models[name] = new_block
    groups = _append_group_membership(new_config, base_model, name)

    record = {
        "name": name,
        "kind": "full_weight",
        "base": base_model,
        "path": path_info.path,
        "created_at": created_at,
        "daemon_port": daemon_port,
        "groups": groups,
    }
    new_records[name] = copy.deepcopy(record)
    return RegistryAddResult(config=new_config, records=new_records, record=record)


def plan_temporary_model_removal(
    name: str,
    temporary_records: Mapping[str, Mapping[str, Any]],
    snapshot: StateSnapshot,
    *,
    now: float,
    max_snapshot_age_seconds: float = 60.0,
) -> RemovalPlan:
    """Check whether a temporary model can be removed without violating protections."""
    name = validate_safe_model_name(name)
    now = _finite_float(now, "now")
    if name not in temporary_records:
        return RemovalPlan(False, (Blocker(name, "not_temporary"),))

    model = _model_by_name(snapshot, name)
    activity = _activity_by_name(snapshot, name)
    blockers = list(_snapshot_blockers(name, snapshot, now, max_snapshot_age_seconds))
    blockers.extend(_removal_blockers(name, model, activity, snapshot, now))
    last_used_at = _last_used_at(temporary_records[name], activity)
    if blockers:
        return RemovalPlan(False, tuple(blockers), last_used_at=last_used_at)

    actions: tuple[Action, ...] = ()
    if model and model.state in {"awake", "sleeping"}:
        actions = (Action(kind="stop", model=name, reason="temporary_model_remove", gpu=model.gpu),)
    return RemovalPlan(True, actions=actions, last_used_at=last_used_at)


def remove_temporary_model(
    config: Mapping[str, Any],
    temporary_records: Mapping[str, Mapping[str, Any]],
    *,
    name: str,
    snapshot: StateSnapshot,
    now: float,
) -> RegistryRemoveResult:
    """Return detached config/records after removing a safe temporary model."""
    plan = plan_temporary_model_removal(name, temporary_records, snapshot, now=now)
    if not plan.allowed:
        reasons = ", ".join(blocker.reason for blocker in plan.blockers)
        raise RegistryError(f"model cannot be removed: {reasons}")

    new_config = copy.deepcopy(dict(config))
    new_records = copy.deepcopy({key: dict(value) for key, value in temporary_records.items()})
    models = _models_mapping(new_config)
    record = copy.deepcopy(new_records.pop(name))
    models.pop(name, None)
    _remove_group_membership(new_config, name)
    return RegistryRemoveResult(config=new_config, records=new_records, record=record, actions=plan.actions)


def expired_temporary_models(
    temporary_records: Mapping[str, Mapping[str, Any]],
    snapshot: StateSnapshot,
    *,
    now: float,
    max_unused_seconds: float = DEFAULT_EXPIRY_SECONDS,
) -> tuple[str, ...]:
    """Return temporary model names that are safe to expire after seven idle days."""
    expired: list[str] = []
    for name in sorted(temporary_records):
        plan = plan_temporary_model_removal(name, temporary_records, snapshot, now=now)
        if not plan.allowed or plan.last_used_at is None:
            continue
        if now - plan.last_used_at >= max_unused_seconds:
            expired.append(name)
    return tuple(expired)


def _models_mapping(config: dict[str, Any]) -> dict[str, Any]:
    models = config.get("models")
    if not isinstance(models, dict):
        raise RegistryError("llama-swap config must contain a models mapping")
    return models


def _clone_model_block(base_model: str, name: str, base_block: dict[str, Any], model_path: str, daemon_port: int) -> dict[str, Any]:
    block = copy.deepcopy(base_block)
    block.pop("aliases", None)
    block.pop("alias", None)
    block.pop("setParamsByID", None)
    if isinstance(block.get("filters"), dict):
        block["filters"].pop("setParamsByID", None)
    block["useModelName"] = name
    if "cmd" not in block or "cmdStop" not in block:
        raise RegistryError("base model config must contain cmd and cmdStop")
    cmd_argv = _command_argv(base_block["cmd"])
    stop_argv = _command_argv(base_block["cmdStop"])
    cmd_port = _extract_vllm_url_port(cmd_argv)
    stop_port = _extract_vllm_url_port(stop_argv)
    if cmd_port is None or stop_port is None or cmd_port != stop_port:
        raise RegistryError("cmd and cmdStop must use the same upstream daemon port")
    block["cmd"] = _same_shape_command(
        base_block["cmd"],
        _transform_cmd(cmd_argv, base_model, name, model_path, daemon_port),
    )
    block["cmdStop"] = _same_shape_command(
        base_block["cmdStop"],
        _transform_cmd_stop(stop_argv, daemon_port),
    )
    return block


def _command_argv(command: Any) -> list[str]:
    if isinstance(command, str):
        argv = shlex.split(command)
    elif isinstance(command, list) and all(isinstance(item, str) for item in command):
        argv = list(command)
    else:
        raise RegistryError("command template must be a string or argv list")
    if not argv:
        raise RegistryError("command template is empty")
    if any(token in SHELL_TOKENS or "`" in token or "$(" in token for token in argv):
        raise RegistryError("shell command templates are not supported")
    return argv


def _same_shape_command(original: Any, argv: Sequence[str]) -> Any:
    if isinstance(original, list):
        return list(argv)
    return shlex.join(argv)


def _transform_cmd(argv: list[str], base_model: str, name: str, model_path: str, daemon_port: int) -> list[str]:
    delimiters = [index for index, token in enumerate(argv) if token == "--"]
    if len(delimiters) != 2 or len(argv) < 2 or argv[1] != "serve":
        raise RegistryError("unsupported wrapper command template")
    launch = argv[delimiters[0] + 1:delimiters[1]]
    expected_units = {f"vllm-{base_model}", "vllm-${MODEL_ID}"}
    if len(launch) != 3 or launch[2] not in expected_units:
        raise RegistryError("launcher must target the base model unit")
    journal = [argv[i + 1] for i, token in enumerate(argv[:delimiters[0]])
               if token == "--journal-unit" and i + 1 < delimiters[0]]
    if len(journal) != 1 or journal[0] not in {unit + ".service" for unit in expected_units}:
        raise RegistryError("wrapper journal must target the base model unit")
    old_port = _extract_vllm_url_port(argv)
    if old_port is None:
        raise RegistryError("command template must include --vllm-url")
    result = _replace_unit_tokens(_rewrite_daemon_ports(argv, old_port, daemon_port), base_model, name)

    vllm_start = delimiters[-1] + 1
    try:
        serve_index = result.index("serve", vllm_start)
    except ValueError as exc:
        raise RegistryError("vLLM command must include serve") from exc
    positional_index = serve_index + 1
    if positional_index >= len(result) or result[positional_index].startswith("-"):
        raise RegistryError("vLLM serve command must include a positional model path")
    old_model_path = result[positional_index]
    result[positional_index] = model_path

    _replace_or_append_option(result, "--served-model-name", name, start=vllm_start)
    _rewrite_speculative_config(result, old_model_path, model_path, start=vllm_start)
    return result


def _transform_cmd_stop(argv: list[str], daemon_port: int) -> list[str]:
    old_port = _extract_vllm_url_port(argv)
    if old_port is None:
        raise RegistryError("cmdStop template must include --vllm-url")
    return _rewrite_daemon_ports(argv, old_port, daemon_port, rewrite_vllm_port=False)


def _extract_vllm_url_port(argv: Sequence[str]) -> int | None:
    for index, token in enumerate(argv):
        value: str | None = None
        if token == "--vllm-url" and index + 1 < len(argv):
            value = argv[index + 1]
        elif token.startswith("--vllm-url="):
            value = token.split("=", 1)[1]
        if value:
            return _parse_url_port(value, "--vllm-url")
    return None


def _rewrite_daemon_ports(argv: Sequence[str], old_port: int, new_port: int, *, rewrite_vllm_port: bool = True) -> list[str]:
    result = list(argv)
    saw_url = False
    saw_port = not rewrite_vllm_port
    for index, token in enumerate(result):
        if token == "--vllm-url":
            if index + 1 >= len(result):
                raise RegistryError("--vllm-url requires a value")
            result[index + 1] = _replace_url_port(result[index + 1], old_port, new_port)
            saw_url = True
        elif token.startswith("--vllm-url="):
            result[index] = "--vllm-url=" + _replace_url_port(token.split("=", 1)[1], old_port, new_port)
            saw_url = True
        elif rewrite_vllm_port and token == "--port":
            if index + 1 >= len(result):
                raise RegistryError("--port requires a value")
            if result[index + 1] != str(old_port):
                raise RegistryError("vLLM --port must match wrapper upstream port")
            result[index + 1] = str(new_port)
            saw_port = True
        elif rewrite_vllm_port and token.startswith("--port="):
            value = token.split("=", 1)[1]
            if value != str(old_port):
                raise RegistryError("vLLM --port must match wrapper upstream port")
            result[index] = f"--port={new_port}"
            saw_port = True
    if not saw_url:
        raise RegistryError("command template must include --vllm-url")
    if not saw_port:
        raise RegistryError("vLLM --port must match wrapper upstream port")
    return result


def _replace_url_port(value: str, expected_port: int, new_port: int) -> str:
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.hostname:
        raise RegistryError("vLLM upstream URL must include scheme and host")
    current_port = _parse_url_port(value, "vLLM upstream URL")
    if current_port != expected_port:
        raise RegistryError("vLLM upstream URL port is inconsistent")
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{new_port}"
    if parsed.username or parsed.password:
        raise RegistryError("vLLM upstream URL must not include credentials")
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _parse_url_port(value: str, label: str) -> int:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise RegistryError(f"{label} must contain a numeric port") from exc
    if port is None:
        raise RegistryError(f"{label} must contain a numeric port")
    return port


def _replace_unit_tokens(argv: Sequence[str], base_model: str, name: str) -> list[str]:
    base_unit = f"vllm-{base_model}"
    new_unit = f"vllm-{name}"
    return [
        token.replace(f"{base_unit}.service", f"{new_unit}.service")
        if token == f"{base_unit}.service"
        else new_unit
        if token == base_unit
        else token
        for token in argv
    ]


def _ports_in_command(argv: Sequence[str]) -> set[int]:
    ports: set[int] = set()
    url_port = _extract_vllm_url_port(argv)
    if url_port is not None:
        ports.add(url_port)
    for index, token in enumerate(argv):
        value: str | None = None
        if token == "--port" and index + 1 < len(argv):
            value = argv[index + 1]
        elif token.startswith("--port="):
            value = token.split("=", 1)[1]
        if value and value.isdigit():
            ports.add(int(value))
    return ports


def _replace_or_append_option(argv: list[str], option: str, value: str, *, start: int) -> None:
    for index in range(start, len(argv)):
        token = argv[index]
        if token == option:
            if index + 1 >= len(argv):
                raise RegistryError(f"{option} requires a value")
            argv[index + 1] = value
            return
        if token.startswith(f"{option}="):
            argv[index] = f"{option}={value}"
            return
    argv.extend([option, value])


def _rewrite_speculative_config(argv: list[str], old_model_path: str, new_model_path: str, *, start: int) -> None:
    for index in range(start, len(argv)):
        token = argv[index]
        value_index: int | None = None
        value: str | None = None
        if token == "--speculative-config":
            value_index = index + 1
            if value_index >= len(argv):
                raise RegistryError("--speculative-config requires JSON")
            value = argv[value_index]
        elif token.startswith("--speculative-config="):
            value = token.split("=", 1)[1]
        if value is None:
            continue
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise RegistryError("--speculative-config must be valid JSON") from exc
        if isinstance(parsed, dict) and parsed.get("model") == old_model_path:
            parsed["model"] = new_model_path
            rendered = json.dumps(parsed, separators=(",", ":"), sort_keys=True)
            if value_index is None:
                argv[index] = f"--speculative-config={rendered}"
            else:
                argv[value_index] = rendered


def _choose_daemon_port(
    config: Mapping[str, Any],
    temporary_records: Mapping[str, Mapping[str, Any]],
    daemon_port_range: tuple[int, int],
    reserved_ports: Sequence[int],
) -> int:
    start, end = daemon_port_range
    if not isinstance(start, int) or not isinstance(end, int) or start <= 0 or end > 65535 or start > end:
        raise RegistryError("daemon port range is invalid")
    used = {_validate_port(port, "reserved port") for port in reserved_ports}
    models = config.get("models", {})
    if isinstance(models, Mapping):
        for block in models.values():
            if isinstance(block, Mapping):
                for key in ("cmd", "cmdStop"):
                    command = block.get(key)
                    if command is None:
                        continue
                    used.update(_ports_in_command(_command_argv(command)))
    for record in temporary_records.values():
        port = record.get("daemon_port")
        if port is not None:
            used.add(_validate_port(port, "temporary record daemon_port"))
    for port in range(start, end + 1):
        if port not in used:
            return port
    raise RegistryError("no free daemon port in configured range")


def _append_group_membership(config: dict[str, Any], base_model: str, name: str) -> list[str]:
    groups = config.get("groups")
    joined: list[str] = []
    if not isinstance(groups, Mapping):
        return joined
    for group_name, group in groups.items():
        members = _group_members(group)
        if members is None or base_model not in members:
            continue
        if name not in members:
            members.append(name)
        joined.append(str(group_name))
    return joined


def _remove_group_membership(config: dict[str, Any], name: str) -> None:
    groups = config.get("groups")
    if not isinstance(groups, Mapping):
        return
    for group in groups.values():
        members = _group_members(group)
        if members is not None:
            while name in members:
                members.remove(name)


def _group_members(group: Any) -> list[str] | None:
    if isinstance(group, dict):
        members = group.get("members")
        if isinstance(members, list) and all(isinstance(item, str) for item in members):
            return members
    return None


def _snapshot_blockers(name: str, snapshot: StateSnapshot, now: float, max_snapshot_age_seconds: float) -> tuple[Blocker, ...]:
    blockers: list[Blocker] = []
    if snapshot.errors:
        blockers.append(Blocker(name, "snapshot_errors"))
    if snapshot.sampled_at is None:
        blockers.append(Blocker(name, "unknown_snapshot_freshness"))
    elif (not isinstance(snapshot.sampled_at, (int, float)) or not math.isfinite(snapshot.sampled_at)
          or not 0 <= now - snapshot.sampled_at <= max_snapshot_age_seconds):
        blockers.append(Blocker(name, "stale_snapshot"))
    for lease in snapshot.leases:
        if lease.model == name and lease.status in {"pending", "stale"}:
            blockers.append(Blocker(name, "active_lease", gpu=lease.gpu))
    return tuple(blockers)


def _removal_blockers(
    name: str,
    model: ModelState | None,
    activity: Activity | None,
    snapshot: StateSnapshot,
    now: float,
) -> tuple[Blocker, ...]:
    blockers: list[Blocker] = []
    if model is None or model.state == "unknown":
        blockers.append(Blocker(name, "unknown_model_state"))
    elif model.is_default:
        blockers.append(Blocker(name, "default_model"))
    active_pin = next((pin for pin in snapshot.pins if pin.model == name and pin.until > now), None)
    if active_pin is not None:
        blockers.append(Blocker(name, "pinned", user=active_pin.by))
    if (activity is None or activity.in_flight is None or isinstance(activity.in_flight, bool)
            or not isinstance(activity.in_flight, int)
            or (activity.last_request_at is None and not _known_never_used(activity))):
        blockers.append(Blocker(name, "unknown_activity"))
    elif activity.in_flight < 0:
        blockers.append(Blocker(name, "invalid_activity", in_flight=activity.in_flight))
    elif activity.in_flight > 0:
        blockers.append(Blocker(name, "in_flight", in_flight=activity.in_flight))
    return tuple(blockers)


def _last_used_at(record: Mapping[str, Any], activity: Activity | None) -> float | None:
    created = record.get("created_at")
    if created is None or activity is None:
        return None
    if activity.last_request_at is None:
        return _finite_float(created, "created_at") if _known_never_used(activity) else None
    return max(_finite_float(created, "created_at"), _finite_float(activity.last_request_at, "last_request_at"))


def _known_never_used(activity: Activity) -> bool:
    # A successful empty history query has known zero aggregates. Missing probes
    # retain None aggregates and must not age an unused model out speculatively.
    return activity.requests_last_hour == 0 and activity.requests_last_10m == 0


def _model_by_name(snapshot: StateSnapshot, name: str) -> ModelState | None:
    return next((model for model in snapshot.models if model.name == name), None)


def _activity_by_name(snapshot: StateSnapshot, name: str) -> Activity | None:
    return next((activity for activity in snapshot.activity if activity.model == name), None)


def _is_under_any(path: Path, roots: Sequence[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _reject_symlink_escapes(model_path: Path, roots: Sequence[Path]) -> None:
    for child in model_path.rglob("*"):
        try:
            resolved = child.resolve(strict=True)
        except OSError as exc:
            raise RegistryError(f"path under model directory is not readable: {child.name}") from exc
        if not _is_under_any(resolved, roots):
            raise RegistryError("model directory contains a symlink escape")


def _find_weight_files(model_path: Path) -> tuple[str, ...]:
    index_files = sorted(child for child in model_path.iterdir() if child.is_file() and child.name.endswith(".safetensors.index.json"))
    if len(index_files) > 1:
        raise RegistryError("model directory contains multiple shard index files")
    if index_files:
        return _weights_from_index(model_path, index_files[0])

    files: list[str] = []
    for child in model_path.iterdir():
        if child.is_file() and child.name.endswith(WEIGHT_SUFFIXES):
            _check_readable_nonempty(child)
            files.append(child.name)
    _validate_shard_sequence(files)
    return tuple(sorted(files))


def _weights_from_index(model_path: Path, index_path: Path) -> tuple[str, ...]:
    try:
        with index_path.open("r", encoding="utf-8") as handle:
            index = json.load(handle)
    except OSError as exc:
        raise RegistryError("shard index is not readable") from exc
    except json.JSONDecodeError as exc:
        raise RegistryError("shard index is not valid JSON") from exc
    if not isinstance(index, dict) or not isinstance(index.get("weight_map"), dict):
        raise RegistryError("shard index must contain a weight_map object")
    values = list(index["weight_map"].values())
    if not values or not all(isinstance(name, str) and name for name in values):
        raise RegistryError("shard index weight_map must name weight files")
    files = sorted(set(values))
    for name in files:
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise RegistryError("shard index contains an unsafe weight path")
        path = model_path / name
        if not path.is_file():
            raise RegistryError("shard index references a missing weight file")
        _check_readable_nonempty(path)
    return tuple(files)


def _validate_shard_sequence(files: Sequence[str]) -> None:
    shard_matches = [SHARD_PATTERN.fullmatch(name) for name in files]
    if not any(shard_matches):
        return
    if not all(shard_matches):
        raise RegistryError("model directory mixes shard and non-shard weights without an index")
    totals = {int(match.group(2)) for match in shard_matches if match is not None}
    if len(totals) != 1:
        raise RegistryError("model shard files disagree on total shard count")
    total = totals.pop()
    seen = {int(match.group(1)) for match in shard_matches if match is not None}
    if seen != set(range(1, total + 1)):
        raise RegistryError("model directory is missing shard files")


def _check_readable_nonempty(path: Path) -> None:
    try:
        with path.open("rb") as handle:
            if handle.read(1) == b"":
                raise RegistryError("weight files must be non-empty")
    except OSError as exc:
        raise RegistryError("weight file is not readable") from exc


def _reserved_model_ids(config: Mapping[str, Any], temporary_records: Mapping[str, Mapping[str, Any]]) -> set[str]:
    reserved = set(temporary_records)
    models = config.get("models")
    if isinstance(models, Mapping):
        reserved.update(str(name) for name in models)
        for block in models.values():
            if not isinstance(block, Mapping):
                continue
            aliases = block.get("aliases", ())
            if isinstance(aliases, str):
                reserved.add(aliases)
            elif isinstance(aliases, Sequence):
                reserved.update(alias for alias in aliases if isinstance(alias, str))
            for section in (block, block.get("filters", {})):
                if isinstance(section, Mapping) and isinstance(section.get("setParamsByID"), Mapping):
                    reserved.update(str(key) for key in section["setParamsByID"])
    default_model = config.get("defaultModel")
    if isinstance(default_model, str):
        reserved.add(default_model)
    return reserved


def _validate_port(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > 65535:
        raise RegistryError(f"{label} is invalid")
    return value


def _finite_float(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise RegistryError(f"{label} must be numeric") from exc
    if not math.isfinite(result):
        raise RegistryError(f"{label} must be finite")
    return result


class _RegistryYamlEditor:
    """Patch supported block YAML using parser positions, never re-dump the file.

    PyYAML resolves aliases when composing nodes, so a value whose source marks
    point before its own key is not directly editable. Such layouts, merges,
    duplicate keys and complex flow layouts are rejected rather than normalized.
    A final semantic check also rejects dangling anchors or unintended changes.
    """

    def __init__(self, original: bytes):
        try:
            self.text = original.decode("utf-8")
            self.root = yaml.compose(self.text, Loader=yaml.SafeLoader)
            self.tokens = list(yaml.scan(self.text, Loader=yaml.SafeLoader))
            self.original = yaml.safe_load(self.text)
        except (UnicodeError, yaml.YAMLError) as exc:
            raise RegistryError("unsupported YAML layout: invalid UTF-8/YAML") from exc
        if not self.text.endswith("\n"):
            self._unsupported("a final newline is required")
        self.newline = "\r\n" if "\r\n" in self.text else "\n"
        if "\r" in self.text.replace("\r\n", "") or (self.newline == "\r\n" and "\n" in self.text.replace("\r\n", "")):
            self._unsupported("mixed or unsupported line endings")
        self.fields = self._mapping(self.root)
        self.edits: list[tuple[int, int, str]] = []

    @staticmethod
    def _unsupported(reason: str) -> None:
        raise RegistryError("unsupported YAML layout: " + reason)

    def _mapping(self, node) -> dict:
        if not isinstance(node, yaml.MappingNode) or node.flow_style:
            self._unsupported("edited mappings must use block style")
        fields = {}
        for key, value in node.value:
            if not isinstance(key, yaml.ScalarNode) or key.tag != "tag:yaml.org,2002:str" or key.value in fields:
                self._unsupported("duplicate, merge or complex mapping keys")
            fields[key.value] = (key, value)
        return fields

    def _direct(self, key, value) -> None:
        if value.start_mark.index < key.end_mark.index:
            self._unsupported("edited values must not be aliases")

    def _line_start(self, index: int) -> int:
        return self.text.rfind("\n", 0, index) + 1

    def _line_end(self, index: int) -> int:
        return self.text.index("\n", index) + 1

    def _entry_span(self, key, value) -> tuple[int, int]:
        self._direct(key, value)
        start = self._line_start(key.start_mark.index)
        if self.text[start:key.start_mark.index].strip():
            self._unsupported("mapping entries must start on their own line")
        # BlockEnd tokens are placed after trailing comments; they are not the
        # end of an entry's actual content. Last scalar/flow/alias tokens are.
        content = [token for token in self.tokens
                   if isinstance(token, (yaml.tokens.ScalarToken, yaml.tokens.FlowMappingEndToken,
                                         yaml.tokens.FlowSequenceEndToken, yaml.tokens.AliasToken))
                   and key.start_mark.index <= token.start_mark.index
                   and token.end_mark.index <= value.end_mark.index]
        if not content:
            self._unsupported("cannot determine model entry extent")
        end = self._line_end(max(token.end_mark.index for token in content) - 1)
        return start, end

    def _block_item_span(self, item) -> tuple[int, int, str]:
        start = self._line_start(item.start_mark.index)
        prefix = self.text[start:item.start_mark.index]
        match = re.fullmatch(r"( *)- +", prefix)
        if not match or item.start_mark.line != item.end_mark.line:
            self._unsupported("block members must be single-line scalar entries")
        return start, self._line_end(item.end_mark.index - 1), match.group(1)

    def _members(self, key, sequence, before: list, after: list, name: str, adding: bool) -> None:
        self._direct(key, sequence)
        if not isinstance(sequence, yaml.SequenceNode):
            self._unsupported("group members must be a sequence")
        items = sequence.value
        if any(not isinstance(item, yaml.ScalarNode) or item.tag != "tag:yaml.org,2002:str" for item in items):
            self._unsupported("group members must be strings")
        if len(set(before)) != len(before) or [item.value for item in items] != before:
            self._unsupported("duplicate or indirect group members")
        if any(item.start_mark.index < sequence.start_mark.index for item in items):
            self._unsupported("aliased member entries")
        if any(a.end_mark.index > b.start_mark.index for a, b in zip(items, items[1:])):
            self._unsupported("aliased member entries")
        expected = before + [name] if adding else [member for member in before if member != name]
        if after != expected or (not adding and name not in before):
            self._unsupported("unexpected membership change")
        if sequence.flow_style:
            tokens = [token for token in self.tokens
                      if sequence.start_mark.index <= token.start_mark.index < sequence.end_mark.index]
            opening = next(token for token in tokens if isinstance(token, yaml.tokens.FlowSequenceStartToken))
            closing = next(token for token in reversed(tokens) if isinstance(token, yaml.tokens.FlowSequenceEndToken))
            if "\n" in self.text[opening.start_mark.index:closing.end_mark.index]:
                self._unsupported("flow members must stay on one line")
            if adding:
                at = items[-1].end_mark.index if items else opening.end_mark.index
                self.edits.append((at, at, (", " if items else "") + json.dumps(name)))
            else:
                index = before.index(name)
                item = items[index]
                start, end = item.start_mark.index, item.end_mark.index
                commas = [token for token in tokens if isinstance(token, yaml.tokens.FlowEntryToken)]
                if len(items) > 1:
                    if index == len(items) - 1:
                        start = commas[index - 1].start_mark.index
                    else:
                        end = commas[index].end_mark.index
                self.edits.append((start, end, ""))
        elif adding:
            if not items:
                self._unsupported("empty block member sequence")
            _, end, indent = self._block_item_span(items[-1])
            self.edits.append((end, end, indent + "- " + json.dumps(name) + self.newline))
        else:
            item = items[before.index(name)]
            start, end, indent = self._block_item_span(item)
            replacement = ""
            if len(items) == 1:
                replacement = " " * max(len(indent), key.start_mark.column + 2) + "[]" + self.newline
            self.edits.append((start, end, replacement))

    def render(self, config: dict) -> bytes:
        if "models" not in self.fields:
            self._unsupported("models must be an explicit block mapping")
        key, models = self.fields["models"]
        self._direct(key, models)
        entries = self._mapping(models)
        before, after = self.original["models"], config["models"]
        added, removed = set(after) - set(before), set(before) - set(after)
        if len(added) + len(removed) != 1 or any(before[name] != after[name] for name in set(before) & set(after)):
            self._unsupported("only one added or removed model entry is supported")
        adding = bool(added)
        name = next(iter(added or removed))
        if adding:
            header_end = self._line_end(key.end_mark.index)
            header = self.text[key.end_mark.index:header_end]
            if not re.fullmatch(r":[ \t]*(?:&[\w-]+[ \t]*)?(?:#[^\r\n]*)?\r?\n", header) or not entries:
                self._unsupported("models header or indentation is unsupported")
            columns = {entry_key.start_mark.column for entry_key, _ in entries.values()}
            if len(columns) != 1 or min(columns) <= key.start_mark.column:
                self._unsupported("inconsistent model indentation")
            indent = " " * min(columns)
            block = yaml.safe_dump({name: after[name]}, sort_keys=False, allow_unicode=True)
            rendered = "".join(indent + line if line.strip() else line for line in block.splitlines(keepends=True))
            self.edits.append((header_end, header_end, rendered.replace("\n", self.newline)))
        else:
            start, end = self._entry_span(*entries[name])
            self.edits.append((start, end, ""))
        old_groups, new_groups = self.original.get("groups", {}), config.get("groups", {})
        if old_groups != new_groups:
            group_key, groups = self.fields["groups"]
            self._direct(group_key, groups)
            group_entries = self._mapping(groups)
            if set(old_groups) != set(new_groups):
                self._unsupported("group declarations cannot change")
            for group_name, old in old_groups.items():
                new = new_groups[group_name]
                if old == new:
                    continue
                entry_key, entry = group_entries[group_name]
                self._direct(entry_key, entry)
                fields = self._mapping(entry)
                if "members" not in fields or {k: v for k, v in old.items() if k != "members"} != {k: v for k, v in new.items() if k != "members"}:
                    self._unsupported("only group membership may change")
                self._members(*fields["members"], old["members"], new["members"], name, adding)
        result = self.text
        boundary = len(result)
        for start, end, replacement in sorted(self.edits, reverse=True):
            if not 0 <= start <= end <= boundary:
                self._unsupported("overlapping edit ranges")
            result = result[:start] + replacement + result[end:]
            boundary = start
        try:
            if yaml.safe_load(result) != config:
                self._unsupported("localized edit changed unintended values")
        except (yaml.YAMLError, RecursionError) as exc:
            raise RegistryError("unsupported YAML layout: edit would break anchors or values") from exc
        return result.encode("utf-8")


class ModelRegistry:
    """Core-injected POST/DELETE adapter; all writes go through ReloadQueue.

    stop_model must perform a fresh, guarded core action and raise on failure.
    unit_absent must confirm target unit absence from an authoritative probe.
    Neither callback is invoked during dry-run. No production adapter is defaulted.
    """

    def __init__(self, queue: ReloadQueue, *, shared_roots: Sequence[str | Path],
                 daemon_port_range: tuple[int, int], reserved_ports: Callable[[], Sequence[int]] = lambda: (),
                 stop_model: Callable[..., None] | None = None,
                 unit_absent: Callable[..., bool] | None = None,
                 now: Callable[[], float] = time.time):
        self.queue, self.shared_roots, self.daemon_port_range = queue, tuple(shared_roots), daemon_port_range
        self.reserved_ports, self.stop_model, self.unit_absent, self.now = reserved_ports, stop_model, unit_absent, now
        self._removals: dict[str, str] = {}

    @staticmethod
    def _decode(data: bytes) -> tuple[dict, dict]:
        try:
            config = yaml.safe_load(data)
        except yaml.YAMLError as exc:
            raise RegistryError("invalid YAML configuration") from exc
        if not isinstance(config, dict):
            raise RegistryError("configuration must be a mapping")
        records = {}
        for name, block in _models_mapping(config).items():
            if not isinstance(block, dict):
                raise RegistryError("model block must be a mapping")
            metadata = block.get("metadata", {})
            if not isinstance(metadata, dict):
                raise RegistryError("model metadata must be a mapping")
            record = metadata.get("llmsvc_registry")
            if record is None:
                continue
            if not isinstance(record, dict) or record.get("kind") != "full_weight" or record.get("name") != name:
                raise RegistryError("invalid temporary model metadata")
            validate_safe_model_name(name)
            _finite_float(record.get("created_at"), "created_at")
            _validate_port(record.get("daemon_port"), "daemon_port")
            records[name] = copy.deepcopy(record)
        return config, records

    @staticmethod
    def _encode(original: bytes, config: dict, records: dict) -> bytes:
        for name, record in records.items():
            block = config["models"][name]
            metadata = block.setdefault("metadata", {})
            if not isinstance(metadata, dict):
                raise RegistryError("model metadata must be a mapping")
            metadata["llmsvc_registry"] = copy.deepcopy(record)
        try:
            return _RegistryYamlEditor(original).render(config)
        except (yaml.YAMLError, RecursionError) as exc:
            raise RegistryError("unsupported YAML layout: cannot safely render localized edit") from exc

    def records(self) -> dict:
        with self.queue.action_lock:
            data, _ = self.queue._read()
            return self._decode(data)[1]

    def add(self, body: Mapping[str, Any], *, dry_run: bool = False) -> dict:
        if "lora" in body:
            raise RegistryError("LoRA registration is disabled pending issue #21 measurements")
        if set(body) != {"name", "path", "base"} or not all(isinstance(v, str) and v for v in body.values()):
            raise RegistryError("add requires name, path, and base strings")
        name, path, base = body["name"], body["path"], body["base"]
        created_at = self.now()
        def transform(data: bytes) -> bytes:
            config, records = self._decode(data)
            result = add_full_weight_model(config, records, name=name, model_path=path, base_model=base,
                                           shared_roots=self.shared_roots, daemon_port_range=self.daemon_port_range,
                                           reserved_ports=self.reserved_ports(), created_at=created_at)
            return self._encode(data, result.config, result.records)
        return self.queue.enqueue(transform, description={"kind": "add_model", "model": name, "base": base}, dry_run=dry_run)

    def remove(self, name: str, *, dry_run: bool = False, require_expired: bool = False) -> dict:
        validate_safe_model_name(name)
        with self.queue.action_lock:
            existing = self._removals.get(name)
            if existing and self.queue.get(existing)["status"] == "queued" and not dry_run:
                return self.queue.get(existing)
            def precheck() -> list[dict]:
                records = self.records()
                state = self.queue.snapshot()
                plan = plan_temporary_model_removal(name, records, state, now=self.now())
                blockers = [asdict(item) for item in plan.blockers]
                if require_expired and plan.allowed and name not in expired_temporary_models(records, state, now=self.now()):
                    blockers.append({"model": name, "reason": "recent_activity"})
                return blockers
            blockers = precheck()
            if blockers:
                raise RegistryError("model cannot be removed: " + ", ".join(item["reason"] for item in blockers))
            if not dry_run and (self.stop_model is None or self.unit_absent is None):
                raise RegistryError("core guarded stop and unit-absence callbacks are required")
            def transform(data: bytes) -> bytes:
                config, records = self._decode(data)
                result = remove_temporary_model(config, records, name=name, snapshot=self.queue.snapshot(), now=self.now())
                return self._encode(data, result.config, result.records)
            def cleanup(*, deadline: float) -> None:
                # The routing entry is gone before cleanup. Core must recheck protection
                # against late data-plane activity before touching the target unit.
                assert self.unit_absent is not None and self.stop_model is not None
                self.queue._check_deadline(deadline)
                if not self.unit_absent(name, deadline=deadline):
                    self.queue._check_deadline(deadline)
                    self.stop_model(name, deadline=deadline)
                self.queue._check_deadline(deadline)
                if not self.unit_absent(name, deadline=deadline):
                    raise RegistryError("target unit absence not confirmed")
            result = self.queue.enqueue(transform, description={"kind": "remove_model", "model": name,
                                                               "unit": f"vllm-{name}.service"},
                                        dry_run=dry_run, precheck=precheck, after_apply=cleanup)
            if not dry_run:
                self._removals[name] = result["id"]
            return result

    def expire(self, *, dry_run: bool = False) -> list[dict]:
        with self.queue.action_lock:
            names = expired_temporary_models(self.records(), self.queue.snapshot(), now=self.now())
            return [self.remove(name, dry_run=dry_run, require_expired=True) for name in names]

    def handle(self, method: str, path: str, body: Mapping[str, Any], *, dry_run: bool = False) -> dict:
        if method == "POST" and path == "/v1/models":
            return self.add(body, dry_run=dry_run)
        if method == "DELETE" and path.startswith("/v1/models/"):
            if body:
                raise RegistryError("remove does not accept a request body")
            return self.remove(path[len("/v1/models/"):], dry_run=dry_run)
        raise RegistryError("unsupported registry endpoint")
