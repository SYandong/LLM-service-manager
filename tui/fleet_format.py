# Generated-By: Codex / gpt-6.1-sol
"""Concise fleet labels without changing the underlying observations."""

import hashlib
import math
import re
import unicodedata


STATUS_LABELS = {"active": "Active", "idle": "Idle", "over_limit": "Running · inactive",
                 "claimed": "Claimed", "unknown": "Unknown"}
FLEET_ERROR_CODES = frozenset(("fleet_disabled", "fleet_claims_disabled", "fleet_store_unavailable",
    "fleet_stopping", "unmapped_container", "invalid_fleet_history_query", "service_not_found",
    "claim_not_found", "forbidden_container", "service_observation_unknown", "invalid_claim",
    "invalid_request", "request_too_large", "not_found"))
DURATION_UNITS = ((365 * 86400, "y"), (30 * 86400, "mo"), (86400, "d"),
                  (3600, "h"), (60, "m"), (1, "s"))


def owner_info(item, show_names=False):
    """Keep canonical owner keys separate from their optional visible names."""
    def clean(value):
        return "".join(character if not unicodedata.category(character).startswith("C") else " "
                       for character in value)

    container = item.get("container")
    if isinstance(container, str) and container:
        identity, name = "container:" + container, clean(container)
    elif item.get("host") is True:
        uid = item.get("host_uid")
        if type(uid) is not int or uid < 0:
            return "host", "Unknown"
        user = item.get("host_user")
        user = clean(user).strip() if isinstance(user, str) else ""
        identity, name = "host:uid:" + str(uid), user or "UID " + str(uid)
    else:
        return "unknown", "Unknown"
    if not show_names:
        digest = hashlib.sha256(identity.encode("utf-8")).digest()
        name = "User %09d" % (int.from_bytes(digest, "big") % 1000000000)
    return identity, name


def model_label(value):
    """Shorten absolute model filesystem paths, preserving repository IDs."""
    value = str(value or "unknown")
    return value.rstrip("/").rsplit("/", 1)[-1] if value.startswith("/") and value.strip("/") else value


def display_text(value, items=(), show_names=False, *, preserve_models=True):
    """Apply the same presentation rules to IDs, parameters and diagnostics."""
    text = "".join(character if not unicodedata.category(character).startswith("C") else " "
                   for character in str(value))
    names, models = {}, set()
    for item in items:
        model = item.get("model")
        if isinstance(model, str) and model.startswith("/"):
            text = text.replace(model, model_label(model))
        if preserve_models and isinstance(model, str) and model:
            models.add(model_label(model))
        if show_names:
            continue
        identity, label = owner_info(item)
        if identity in ("host", "unknown"):
            continue
        names.setdefault(identity, label)
        raw_name = item.get("container") if identity.startswith("container:") else item.get("host_user")
        if isinstance(raw_name, str) and raw_name:
            names.setdefault(raw_name, label)
    if names:
        keep = r"User [0-9]{9}(?!\d)"
        if models:
            keep += r"|(?<![\w./-])(?:" + "|".join(
                re.escape(model) for model in sorted(models, key=len, reverse=True)) + r")(?![\w./:-])"
        pattern = r"(?P<keep>" + keep + r")|(?<![\w.-])(?:" + "|".join(
            re.escape(name) for name in sorted(names, key=len, reverse=True)) + r")(?![\w.-])"
        text = re.sub(pattern, lambda match: match.group() if match.group("keep") else names[match.group()], text)
    return text


def error_label(error, items=(), show_names=False):
    """Retain public fleet reason codes without displaying unbound HTTP bodies."""
    status = getattr(error, "status", None)
    if status is not None and not show_names:
        payload = getattr(error, "payload", None)
        code = payload.get("error") if isinstance(payload, dict) else None
        code = code if isinstance(code, str) and code in FLEET_ERROR_CODES else "Fleet request failed"
        return "HTTP %s: %s" % (status, code)
    return display_text(error, items, show_names)


def numeric(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def gib(value):
    """Limit display precision while keeping a tiny positive amount visible."""
    if not numeric(value):
        return "?"
    if value == 0:
        return "0"
    if 0 < value < .01:
        return "<0.01"
    return ("%.2f" % value).rstrip("0").rstrip(".")


def count(value):
    if not numeric(value):
        return "?"
    return format(int(value), ",")


def duration(seconds):
    """Use at most two integer units, with 30-day months and 365-day years."""
    if not numeric(seconds):
        return "?"
    if 0 < seconds < 1:
        return "<1s"
    remaining = int(seconds)
    parts = []
    for size, label in DURATION_UNITS:
        value, remaining = divmod(remaining, size)
        if value:
            parts.append("%d%s" % (value, label))
            if len(parts) == 2:
                break
    return " ".join(parts) or "0s"


def total_tokens(window):
    prompt, generated = window.get("prompt_tokens"), window.get("gen_tokens")
    if not numeric(prompt) or not numeric(generated):
        return None
    total = prompt + generated
    return total if numeric(total) else None


def status_label(status):
    return STATUS_LABELS.get(status, STATUS_LABELS["unknown"])


def api_label(service, clean=str, *, fresh=True):
    """Describe the supplied listener metadata without inferring reachability."""
    if not fresh:
        return "Unknown"
    access = service.get("api_access")
    if access == "local_only":
        return "Local only"
    address = service.get("api_address")
    address = clean(address) if isinstance(address, str) and address else None
    if access == "shared":
        return (address or "Unknown") + " · Shared"
    if access == "direct":
        return address or "Unknown"
    return address + " · Unknown" if address else "Unknown"
