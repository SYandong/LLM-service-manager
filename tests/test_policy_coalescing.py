# Generated-By: Codex / gpt-6-astra
"""Action-level regression oracle independent of policy's RAM projection."""

from dataclasses import replace

from llmsvc.policy import PolicySettings, plan_free
from llmsvc.policy.common import Projection
from llmsvc.state import Action, GPUState, Lease, MemoryState, Pin
from test_policy_intents import model, snapshot


def simulate(source, actions):
    """Interpret physical transfers and final reservations, asserting protection."""
    models = {m.name: m for m in source.models}
    activity = {a.model: a for a in source.activity}
    state = {m.name: m.state for m in source.models}
    available = source.memory.host_available_gb
    sleeping = source.memory.sleeping_weights_gb
    transferred = 0
    for action in actions:
        m = models[action.model]
        assert activity[m.name].in_flight == 0
        assert not any(p.model == m.name and p.until > source.sampled_at for p in source.pins)
        if action.kind == "sleep":
            assert state[m.name] == "awake"
            available -= m.weights_gb
            sleeping += m.weights_gb
            transferred += m.weights_gb
            state[m.name] = "sleeping"
            assert available >= source.memory.host_min_available_gb
            assert sleeping <= source.memory.budget_gb
        else:
            assert action.kind == "stop" and not m.is_default
            if state[m.name] == "sleeping":
                available += m.weights_gb
                sleeping -= m.weights_gb
            state[m.name] = "stopped"
    reserved = sum(m.budget_gb for m in source.models if state[m.name] != "stopped")
    reserved += sum(l.budget_gb for l in source.leases if l.status in ("pending", "stale"))
    return (state, available, sleeping, reserved), transferred


def test_coalesced_free_preserves_protected_final_accounting_without_victim_transfer():
    s = snapshot(model("a"), model("default", is_default=True),
                 model("pinned", budget_gb=20, weights_gb=20, resident_gb=20),
                 model("busy", budget_gb=20, weights_gb=20, resident_gb=20),
                 gpus=(GPUState(0, total_gb=200, free_gb=80, external_gb=0),),
                 memory=MemoryState(210, 0), pins=(Pin("pinned", 20000, "owner"),),
                 leases=(Lease("L", "loading", 0, 0.3, 1, 30, "stale"),))
    s = replace(s, activity=tuple(replace(a, in_flight=1) if a.model == "busy" else a for a in s.activity))
    old_sequence = (Action("sleep", "a", "free", 0), Action("stop", "a", "sleep_memory_admission", 0),
                    Action("sleep", "default", "free", 0))
    before = s.to_dict()
    expected, old_transfers = simulate(s, old_sequence)
    actual, transfers = simulate(s, plan_free(s).actions)
    assert actual == expected == ({"a": "stopped", "default": "sleeping", "pinned": "awake", "busy": "awake"},
                                  170, 40, 110)
    assert old_transfers == 80
    assert transfers == 40  # Only the surviving default sleeper transfers weights.
    assert [(a.kind, a.model) for a in plan_free(s).actions] == [("stop", "a"), ("sleep", "default")]
    assert s.to_dict() == before


def test_coalescing_keeps_other_actions_in_order_and_projection_accounting_equivalent():
    s = snapshot(model("a"), model("other"))
    p = Projection(s, PolicySettings())
    p.sleep(s.models[0], "free")
    p.sleep(s.models[1], "free")
    p.stop(p.models["a"], "sleep_memory_admission")
    assert [(a.kind, a.model) for a in p.actions] == [("sleep", "other"), ("stop", "a")]
    final, transferred = simulate(s, p.actions)
    assert final == ({"a": "stopped", "other": "sleeping"}, p.available, p.sleeping, 40)
    assert transferred == 40


def test_already_sleeping_victim_releases_ram_once_without_removing_other_sleep():
    s = snapshot(model("sleeper", state="sleeping"), model("awake"), memory=MemoryState(500, 40))
    p = Projection(s, PolicySettings())
    p.sleep(s.models[1], "free")
    p.stop(s.models[0], "memory_pressure")
    assert [(a.kind, a.model) for a in p.actions] == [("sleep", "awake"), ("stop", "sleeper")]
    final, transferred = simulate(s, p.actions)
    assert final == ({"sleeper": "stopped", "awake": "sleeping"}, p.available, p.sleeping, 40)
    assert p.available == 500 and p.sleeping == 40 and transferred == 40
