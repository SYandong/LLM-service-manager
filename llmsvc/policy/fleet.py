# Generated-By: Codex / gpt-6.1-sol
"""Pure fleet presentation policy; these states never authorize process actions."""


def window_summary(counts, *, window_seconds, uptime_seconds):
    counts = counts or {}
    observed = min(counts.get("observed_seconds") or 0, window_seconds, uptime_seconds)
    active_minutes = min(counts.get("active_minutes") or 0, observed / 60)
    eligible = min(window_seconds, uptime_seconds)
    return {
        "active_minutes": active_minutes if observed > 0 else None,
        "requests": counts.get("requests"), "gen_tokens": counts.get("gen_tokens"),
        "prompt_tokens": counts.get("prompt_tokens"), "cached_tokens": counts.get("cached_tokens"),
        "active_ratio": active_minutes * 60 / eligible if eligible > 0 and observed > 0 else None,
        "observed_active_ratio": active_minutes * 60 / observed if observed > 0 else None,
        "observed_seconds": observed,
        "coverage_ratio": observed / eligible if eligible > 0 else None,
    }


def service_status(instance, claim, config, now, *, stale, generated_at, hourly=None, windows=None, mine=False):
    """Unknown observations have priority over a declaration or apparent idleness."""
    meta = instance["metadata"]
    state = instance["state"]
    latest = state.get("latest", {})
    last_active = state.get("last_active_at")
    uptime = max(0, now - instance["started_at"])
    current = instance["last_seen"] == generated_at
    observed_idle = state.get("idle_observed_seconds", 0)
    # Idle is an observed lower bound. Time before first_seen, failed scrapes,
    # missing discovery and gaps cannot be reconstructed from process uptime.
    idle_known = current and not stale and latest.get("active") is not None and not state.get("activity_interval_unknown")
    idle = (0 if latest.get("active") else observed_idle) if idle_known else None
    valid_claim = claim if claim and claim.get("revoked_at") is None and claim["until"] > now else None
    if (stale or not current or not state.get("supported") or not latest.get("scrape_ok")
            or state.get("failure_streak", 0) >= 3 or state.get("activity_interval_unknown")):
        status = "unknown"
    elif valid_claim:
        status = "claimed"
    elif last_active is not None and 0 <= now - last_active <= config.fleet_active_window_seconds:
        status = "active"
    elif idle is not None and idle >= config.fleet_idle_limit_hours * 3600:
        status = "over_limit"
    else:
        status = "idle"
    gpu_known = (current and not stale and meta.get("gpu_observation_complete") is not False
                 and all(gpu.get("used_mib") is not None for gpu in meta["gpus"]))
    windows = windows or {}
    return {
        "id": instance["id"], "container": instance["container"], "engine": instance["engine"],
        "engine_version": instance["engine_version"], "host": bool(instance["host"]),
        "managed_by": instance["managed_by"], "model": instance["model"],
        "gpus": [gpu["index"] for gpu in meta["gpus"]],
        "gpu_gb": sum(gpu["used_mib"] for gpu in meta["gpus"]) / 1024 if gpu_known else None,
        "gpu_observation_complete": gpu_known,
        "bind": instance["bind"], "port": instance["port"], "pid": instance["pid"],
        "started_at": instance["started_at"], "uptime_seconds": uptime,
        "first_seen": instance["first_seen"], "last_seen": instance["last_seen"],
        "status": status, "last_active_at": last_active, "idle_seconds": idle,
        "idle_observed_seconds": observed_idle, "never_active": last_active is None,
        "window_24h": window_summary(windows.get("24h"), window_seconds=86400, uptime_seconds=uptime),
        "window_7d": window_summary(windows.get("7d"), window_seconds=604800, uptime_seconds=uptime),
        "hourly_active_24h": hourly if hourly is not None else [None] * 24,
        "claim": valid_claim, "scrape": meta["scrape"] if current else {"ok": False, "error": "discovery_unknown"},
        "mine": bool(mine),
    }


def container_summary(services):
    buckets = {}
    for service in services:
        container = service["container"]
        bucket = buckets.setdefault(container, {"container": container, "services": 0, "gpu_gb": 0, "over_limit": 0})
        bucket["services"] += 1
        if service["gpu_gb"] is None:
            bucket["gpu_gb"] = None
        elif bucket["gpu_gb"] is not None:
            bucket["gpu_gb"] += service["gpu_gb"]
        bucket["over_limit"] += int(service["status"] == "over_limit")
    return [buckets[key] for key in sorted(buckets, key=lambda value: (value is not None, value or ""))]
