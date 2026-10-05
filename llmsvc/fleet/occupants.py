# Generated-By: Codex / gpt-6.1-sol
"""Read-only fleet context for placement errors; never scheduling evidence."""

import re
import unicodedata

from llmsvc.fleet.ingest import number, read_json, validate_snapshot

MAX_MESSAGE_CHARACTERS = 512
MAX_SUMMARY_GPUS = 16
MAX_SUMMARY_GPU_INDEX = 65535


def _shorten(value, limit):
    if limit <= 0:
        return ""
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _card_summary(index, details, limit):
    occupant = details["occupants"][0]
    other_count = len(details["occupants"]) - 1 + details["occupants_remaining"]
    suffix = f" +{other_count}" if other_count else ""
    prefix = f"GPU{index}:"
    amount = f"{occupant['used_gb']:.3g}G"
    kind = "LLM" if occupant["kind"] == "llm" else "other workload"
    tail = f" {amount} ({kind}){suffix}"
    if len(prefix) + len(tail) + 2 > limit and kind == "other workload":
        kind = "other"
        tail = f" {amount} ({kind}){suffix}"
    if len(prefix) + len(tail) > limit:
        # Extreme amounts or large indices still retain the card and count;
        # complete amounts and metadata remain in the structured occupants.
        amount = ""
        tail = f" ({kind}){suffix}"
    label_budget = max(0, limit - len(prefix) - len(tail) - 1)
    model = occupant.get("model")
    model_budget = label_budget // 2 - 1 if model and label_budget >= 12 else 0
    owner_budget = label_budget - model_budget - (1 if model_budget else 0)
    owner = _shorten(occupant["container"] or "unknown container", owner_budget)
    model = _shorten(model, model_budget) if model_budget else ""
    if model:
        kind += " " + model
    tail = (f" {amount} ({kind})" if amount else f" ({kind})") + suffix
    return prefix + (" " + owner if owner else "") + tail


def _summary(message, cards):
    indices = [index for index in sorted(cards) if 0 <= index <= MAX_SUMMARY_GPU_INDEX][:MAX_SUMMARY_GPUS]
    omitted = len(cards) - len(indices)
    remainder = f"; +{omitted} more GPUs" if omitted else ""
    message = _shorten(message, 96)
    if not indices:
        return message + remainder
    available = MAX_MESSAGE_CHARACTERS - len(message) - 2 - len(remainder) - 2 * (len(indices) - 1)
    # Every visible card gets the same character allowance before any owner or
    # model is rendered. An earlier long label cannot consume a later card.
    per_card = available // len(indices)
    rows = [_card_summary(index, cards[index], per_card) for index in indices]
    return message + "; " + "; ".join(rows) + remainder


def _display_text(value):
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError("invalid_occupant_label")
    value = "".join(char for char in value if unicodedata.category(char) not in {"Cc", "Cf", "Cs"})
    value = re.sub(r"(?i)(?:hf_[a-z0-9_-]+|sk-[a-z0-9_-]+)", "[REDACTED]", value)
    value = re.sub(r"(?i)([a-z0-9_-]*(?:token|key|secret|password)[a-z0-9_-]*=)[^\s&]+", r"\1[REDACTED]", value)
    value = re.sub(r"(?i)(https?://)[^/\s]+@", r"\1[REDACTED]@", value)
    value = re.sub(r"(?i)(https?://[^\s?#]+)[?#][^\s]*", r"\1", value)
    return value[:128] or None


def _uuid(value):
    return (isinstance(value, str) and 0 < len(value) <= 256
            and not any(char.isspace() or ord(char) < 32 for char in value))


def _occupants(snapshot, gpu_uuids, *, now, stale_after):
    if (not number(now) or not 0 <= now - snapshot["generated_at"] <= stale_after
            or any(snapshot.get(flag) is not True for flag in
                   ("inventory_complete", "gpu_inventory_complete", "gpu_attribution_complete"))
            or any(service.get("gpu_observation_complete") is not True for service in snapshot["services"])):
        return {}
    by_uuid = {}
    host_indices = set()
    for gpu in snapshot["gpus"]:
        uuid = gpu.get("uuid")
        if not _uuid(uuid) or uuid in by_uuid:
            return {}
        by_uuid[uuid] = gpu["index"]
        host_indices.add(gpu["index"])
    managed_uuids = list(gpu_uuids.values())
    targets = {index: by_uuid[uuid] for index, uuid in gpu_uuids.items()
               if _uuid(uuid) and managed_uuids.count(uuid) == 1 and uuid in by_uuid}
    if not targets:
        return {}
    occupants = {index: [] for index in host_indices}
    for service in snapshot["services"]:
        for gpu in service["gpus"]:
            if gpu["index"] not in host_indices or not number(gpu.get("used_mib")):
                return {}
            if service.get("managed_by") == "llmsvc" or gpu["used_mib"] == 0:
                continue
            occupants[gpu["index"]].append({
                "container": _display_text(service["container"]), "kind": "llm",
                "used_gb": gpu["used_mib"] / 1024, "service_id": service["id"],
                "model": _display_text(service.get("model")),
            })
    other = {}
    for process in snapshot["other_gpu_processes"]:
        if process["gpu"] not in host_indices or not number(process.get("used_mib")):
            return {}
        if process.get("managed_by") == "llmsvc" or process["used_mib"] == 0:
            continue
        container = process.get("container")
        key = process["gpu"], container
        other[key] = other.get(key, 0) + process["used_mib"] / 1024
    for (gpu, container), used_gb in other.items():
        occupants[gpu].append({"container": _display_text(container), "kind": "other",
                               "used_gb": used_gb, "service_id": None})
    result = {}
    for index, host_index in targets.items():
        rows = sorted(occupants[host_index], key=lambda row:
                      (-row["used_gb"], row["container"] or "", row["service_id"] or ""))
        if rows:
            result[index] = {"occupants": rows[:3], "occupants_remaining": max(0, len(rows) - 3)}
    return result


def enrich_unavailable(error, config, *, gpu_uuids, now):
    """Append fresh external ownership hints without changing the error contract.

    Local and host GPU indices may differ; UUIDs bind the failure's exact local
    observation to cards in the single host export. Missing or partial evidence
    leaves the original response unchanged. No database or worker is opened.
    """
    if (not config.fleet_enabled or error.get("error") != "no_feasible_gpu"
            or not any(blocker.get("reason") == "external_pressure" for blocker in error.get("blockers", []))):
        return error
    try:
        snapshot = validate_snapshot(read_json(config.fleet_snapshot_path))
        occupants = _occupants(snapshot, gpu_uuids, now=now, stale_after=config.fleet_stale_after_seconds)
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return error
    blockers = []
    cards = {}
    for blocker in error.get("blockers", []):
        details = occupants.get(blocker.get("gpu")) if blocker.get("reason") == "external_pressure" else None
        if details:
            blocker = {**blocker, **details}
            cards[blocker["gpu"]] = details
        blockers.append(blocker)
    if not cards:
        return error
    message = error.get("message", "No placement GPU is available")
    return {**error, "blockers": blockers, "message": _summary(message, cards)}
