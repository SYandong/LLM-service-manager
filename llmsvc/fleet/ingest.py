# Generated-By: Codex / gpt-6.1-sol
"""Bounded host-export reads and counter differencing without external probes."""

import copy
import json
import math
import os
import stat

MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
MAX_SAMPLE_GAP_SECONDS = 300
COUNTERS = {
    "requests": "requests_total",
    "gen_tokens": "generation_tokens_total",
    "prompt_tokens": "prompt_tokens_total",
    "cached_tokens": "prompt_tokens_cached_total",
}


def number(value, *, minimum=0):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and minimum <= value <= 2 ** 63 - 1 and math.isfinite(value))


def read_json(path, max_bytes=MAX_SNAPSHOT_BYTES):
    """Only read a regular, non-writable-by-others local export; never a FIFO."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
            raise ValueError("unsafe_export_file")
        if info.st_size > max_bytes:
            raise ValueError("export_too_large")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError("export_too_large")
        def reject_constant(value):
            raise ValueError("non_finite_export")
        def unique_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate_export_key")
                result[key] = value
            return result
        return json.loads(data, parse_constant=reject_constant, object_pairs_hook=unique_keys)
    finally:
        os.close(fd)


def validate_snapshot(payload):
    if not isinstance(payload, dict) or type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        raise ValueError("unsupported_fleet_schema")
    if not number(payload.get("generated_at")):
        raise ValueError("invalid_fleet_timestamp")
    if "sample_interval_seconds" in payload and (not number(payload["sample_interval_seconds"]) or not 30 <= payload["sample_interval_seconds"] <= MAX_SAMPLE_GAP_SECONDS):
        raise ValueError("invalid_fleet_sample_interval")
    for key, cap in (("services", 256), ("gpus", 128), ("other_gpu_processes", 4096), ("errors", 256)):
        if not isinstance(payload.get(key), list) or len(payload[key]) > cap:
            raise ValueError("invalid_fleet_" + key)
    if any(not isinstance(error, str) or len(error) > 1024 for error in payload["errors"]):
        raise ValueError("invalid_fleet_errors")
    for flag in ("inventory_complete", "gpu_inventory_complete", "gpu_attribution_complete"):
        if flag in payload and type(payload[flag]) is not bool:
            raise ValueError("invalid_fleet_completeness")
    if not isinstance(payload.get("host"), dict):
        raise ValueError("invalid_fleet_host")
    ids = set()
    for service in payload["services"]:
        if not isinstance(service, dict):
            raise ValueError("invalid_fleet_service")
        for key in ("id", "engine"):
            value = service.get(key)
            if not isinstance(value, str) or not value or len(value) > 512 or any(ord(c) < 32 for c in value):
                raise ValueError("invalid_fleet_service_" + key)
        if service["id"] in ids or not number(service.get("started_at")) or service["started_at"] > payload["generated_at"]:
            raise ValueError("invalid_fleet_instance")
        ids.add(service["id"])
        if type(service.get("pid")) is not int or service["pid"] <= 0:
            raise ValueError("invalid_fleet_pid")
        if type(service.get("host")) is not bool:
            raise ValueError("invalid_fleet_host_flag")
        container = service.get("container")
        if container is None:
            if service["host"] is not True:
                raise ValueError("invalid_fleet_container")
        elif not isinstance(container, str) or not container or len(container) > 512 or any(ord(c) < 32 for c in container):
            raise ValueError("invalid_fleet_container")
        model = service.get("model")
        if model is not None and (not isinstance(model, str) or not model or len(model) > 512 or any(ord(c) < 32 for c in model)):
            raise ValueError("invalid_fleet_model")
        port = service.get("port")
        if port is not None and (type(port) is not int or not 1 <= port <= 65535):
            raise ValueError("invalid_fleet_port")
        for key in ("engine_version", "managed_by", "bind", "model_path", "argv_redacted", "metrics_series_id"):
            value = service.get(key)
            if value is not None and (not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value)):
                raise ValueError("invalid_fleet_service_text")
        scrape = service.get("scrape")
        if not isinstance(scrape, dict) or type(scrape.get("ok")) is not bool:
            raise ValueError("invalid_fleet_scrape")
        error = scrape.get("error")
        if error is not None and (not isinstance(error, str) or len(error) > 512):
            raise ValueError("invalid_fleet_scrape_error")
        metrics = service.get("metrics")
        if metrics is not None:
            if not isinstance(metrics, dict) or any(v is not None and not number(v) for v in metrics.values()):
                raise ValueError("invalid_fleet_metrics")
        gpu_rows(service.get("gpus"), "service")
        if "gpu_observation_complete" in service and type(service["gpu_observation_complete"]) is not bool:
            raise ValueError("invalid_fleet_gpu_observation")
        if "listener_observation_complete" in service and type(service["listener_observation_complete"]) is not bool:
            raise ValueError("invalid_fleet_listener_observation")
        if service.get("listener_ipv6_only") is not None and type(service["listener_ipv6_only"]) is not bool:
            raise ValueError("invalid_fleet_listener_ipv6_only")
        ollama = service.get("ollama")
        if ollama is not None and not isinstance(ollama, (dict, list)):
            raise ValueError("invalid_fleet_ollama")
    gpu_rows(payload["gpus"], "global")
    for row in payload["other_gpu_processes"]:
        if (not isinstance(row, dict) or (row.get("container") is not None and not isinstance(row["container"], str))
                or type(row.get("gpu")) is not int or row["gpu"] < 0
                or (row.get("used_mib") is not None and not number(row["used_mib"]))):
            raise ValueError("invalid_fleet_other_process")
    return payload


def gpu_rows(rows, kind):
    if not isinstance(rows, list) or len(rows) > 128:
        raise ValueError("invalid_fleet_gpu_list")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or type(row.get("index")) is not int or row["index"] < 0 or row["index"] in seen:
            raise ValueError("invalid_fleet_gpu_index")
        seen.add(row["index"])
        keys = ("used_mib",) if kind == "service" else ("used_mib", "total_mib", "util_percent")
        if any(row.get(key) is not None and not number(row[key]) for key in keys):
            raise ValueError("invalid_fleet_gpu_value")
        if kind == "global" and row.get("util_percent") is not None and row["util_percent"] > 100:
            raise ValueError("invalid_fleet_gpu_utilization")


def sample(service, previous, ts, nominal_seconds=60):
    """Preserve successful baselines across failed scrapes and bound coverage."""
    state = copy.deepcopy(previous or {})
    baselines = state.setdefault("counters", {})
    last_ts = state.get("ts")
    interval = 0 if last_ts is None else ts - last_ts
    row = {"instance_id": service["id"], "ts": ts, "running": None, "waiting": None,
           "kv_perc": None, "active": None, "scrape_ok": int(service["scrape"]["ok"]),
           "interval_seconds": interval, "observed_seconds": 0, "active_seconds": 0,
           "gap": int(interval > MAX_SAMPLE_GAP_SECONDS), "counter_reset": 0}
    row.update({"d_" + key: None for key in COUNTERS})
    reliable_delta = False
    gauge_active = False
    supported = False
    if row["scrape_ok"] and service["engine"] == "vllm":
        metrics = service.get("metrics") or {}
        row.update(running=metrics.get("num_requests_running"), waiting=metrics.get("num_requests_waiting"),
                   kv_perc=metrics.get("kv_cache_usage_perc"))
        contiguous = bool(state.get("observed") and 0 < interval <= MAX_SAMPLE_GAP_SECONDS)
        for key, metric in COUNTERS.items():
            value = metrics.get(metric)
            if value is None:
                continue
            epoch = [metrics.get(metric.replace("_total", "_created"), metrics.get("counter_created_at")), service.get("metrics_series_id")]
            baseline = baselines.get(key)
            reset = baseline is not None and (value < baseline["value"] or (epoch is not None and epoch != baseline.get("epoch")))
            delta = 0 if baseline is None or reset else value - baseline["value"]
            row["d_" + key] = delta
            row["counter_reset"] |= int(reset)
            if key in ("requests", "gen_tokens"):
                contiguous &= bool(baseline is not None and baseline["ts"] == last_ts and not reset)
                if delta > 0:
                    reliable_delta |= bool(baseline is not None and baseline["ts"] == last_ts and 0 < interval <= MAX_SAMPLE_GAP_SECONDS and not reset)
            baselines[key] = {"value": value, "ts": ts, "epoch": epoch}
        supported = (row["running"] is not None and row["waiting"] is not None
                     and metrics.get("requests_total") is not None)
        reliable_delta &= not bool(row["counter_reset"])
        gauge_active = (row["running"] or 0) > 0 or (row["waiting"] or 0) > 0
        if supported:
            row["active"] = int(gauge_active or (row["d_requests"] or 0) > 0 or (row["d_gen_tokens"] or 0) > 0)
            if contiguous and not row["counter_reset"]:
                row["observed_seconds"] = min(interval, 2 * nominal_seconds)
    elif row["scrape_ok"] and service["engine"] == "ollama":
        # Loaded-model expiry postponement is an activity proxy, not a token counter.
        data = service.get("ollama")
        models = data.get("models") if isinstance(data, dict) else data
        if isinstance(models, list):
            from datetime import datetime
            expiries = {}
            for model in models:
                if not isinstance(model, dict):
                    continue
                expiry = model.get("expires_at")
                try:
                    expiry = float(expiry) if number(expiry) else datetime.fromisoformat(expiry.replace("Z", "+00:00")).timestamp()
                except (ValueError, TypeError, AttributeError, OverflowError):
                    continue
                if number(expiry):
                    expiries[str(model.get("name", model.get("model", "")))] = expiry
            old = state.get("expiries")
            supported = bool(expiries) or models == []
            if supported:
                row["active"] = int(old is not None and any(expiry > old.get(name, expiry) for name, expiry in expiries.items()))
                reliable_delta = bool(state.get("observed") and 0 < interval <= MAX_SAMPLE_GAP_SECONDS)
                if reliable_delta:
                    row["observed_seconds"] = min(interval, 2 * nominal_seconds)
                state["expiries"] = expiries
    if row["active"]:
        row["active_seconds"] = row["observed_seconds"]
        if gauge_active or reliable_delta:
            state["last_active_at"] = ts
        state["idle_since"] = None
        state["idle_observed_seconds"] = 0
    elif row["active"] == 0:
        if row["observed_seconds"] and state.get("idle_since") is not None:
            state["idle_observed_seconds"] = state.get("idle_observed_seconds", 0) + row["observed_seconds"]
        else:
            state["idle_since"] = ts
            state["idle_observed_seconds"] = 0
    else:
        state["idle_since"] = None
        state["idle_observed_seconds"] = 0
    state.update(ts=ts, observed=supported, latest=row,
                 failure_streak=0 if row["scrape_ok"] and supported else state.get("failure_streak", 0) + 1,
                 supported=supported,
                 activity_interval_unknown=bool(row["active"] and not gauge_active and not reliable_delta))
    return row, state
