# Generated-By: Codex / gpt-6.1-sol
"""Concise fleet labels without changing the underlying observations."""

import math


STATUS_LABELS = {"active": "Active", "idle": "Idle", "over_limit": "Running · inactive",
                 "claimed": "Claimed", "unknown": "Unknown"}
DURATION_UNITS = ((365 * 86400, "y"), (30 * 86400, "mo"), (86400, "d"),
                  (3600, "h"), (60, "m"), (1, "s"))


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


def api_label(service, clean=str):
    """Describe the supplied listener metadata without inferring reachability."""
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
