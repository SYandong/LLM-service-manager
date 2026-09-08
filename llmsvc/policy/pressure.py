# Generated-By: Codex / gpt-6-astra
"""Observation-driven pressure and per-GPU idle TTL (DESIGN §4.1)."""

from llmsvc.state import StateSnapshot
from .common import Decision, PolicySettings, Projection, known_number


def plan_pressure_sleep(
    snapshot: StateSnapshot, *, settings: PolicySettings = PolicySettings(),
) -> Decision:
    """Return one sleep attempt, including its RAM-admission prerequisites.

    Core must execute/recollect/replan until pressure clears, not replay this
    plan on the same observations. Physical free memory is never predicted from
    a budget. Exclusive TTL defaults to 60 minutes, shared TTL to 5 minutes;
    configured values are candidates pending real calibration. The M2 fixed-TTL
    planner must not run alongside this replacement.
    """
    p = Projection(snapshot, settings)
    if p.blockers:
        return p.result()
    gpus = {g.index: g for g in snapshot.gpus}
    triggered = []
    reasons = {}
    for model in snapshot.models:
        if model.state != "awake":
            continue
        gpu = gpus.get(model.gpu)
        if gpu is None or not all(known_number(v) for v in (gpu.total_gb, gpu.free_gb, gpu.external_gb)):
            p.block(model, "unknown_gpu_pressure")
            continue
        shared = gpu.index != settings.exclusive_gpu
        pressure = shared and (bool(gpu.external_processes) or gpu.external_gb > 0 or
                               gpu.free_gb < settings.shared_free_threshold_gb)
        activity = p.activity.get(model.name)
        if activity is None or not known_number(activity.last_request_at) or activity.last_request_at > snapshot.sampled_at:
            p.block(model, "unknown_activity")
            continue
        ttl = settings.shared_ttl_seconds if shared else settings.exclusive_ttl_seconds
        expired = snapshot.sampled_at - activity.last_request_at >= ttl
        if pressure or expired:
            triggered.append(model)
            reasons[model.name] = "shared_gpu_pressure" if pressure else "gpu_idle_ttl"
    for model in p.candidates(triggered):
        if p.sleep(model, reasons[model.name]) or p.actions:
            return p.result()
    return p.result()
