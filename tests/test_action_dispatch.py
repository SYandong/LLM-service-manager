# Generated-By: Codex / gpt-6-astra
"""Single-action transport preparation; all external operations are injected spies."""

import threading
from dataclasses import replace

import pytest

from llmsvc.actions import ActionDispatchError, ModelActionDispatcher
from llmsvc.state import Action, Activity, MemoryState, ModelState, Pin, StateSnapshot


def observation():
    return StateSnapshot(sampled_at=10000, read_only=False,
        models=(ModelState("model", state="awake", gpu=0, unit="vllm-model.service", unit_active=True,
                           weights_gb=40, budget_gb=60),),
        activity=(Activity("model", in_flight=0),), memory=MemoryState(500, 0))


def dispatcher(snapshot=None, **changes):
    state = [observation() if snapshot is None else snapshot]
    requests = []
    now = [5.0]
    def http(method, path, *, deadline):
        requests.append((method, path, deadline))
        return 204
    def stop(unit, *, deadline):
        requests.append(("stop", unit, deadline))
        return 0
    arguments = dict(action_lock=threading.RLock(), snapshot=lambda: state[0],
                     http_request=http, stop_unit=stop, timeout_seconds=2,
                     max_snapshot_age_seconds=30, enabled=True,
                     monotonic=lambda: now[0], wall_clock=lambda: 10000)
    arguments.update(changes)
    return ModelActionDispatcher(**arguments), state, requests, now


def action(kind="sleep", **changes):
    return replace(Action(kind, "model", "test", 0), **changes)


@pytest.mark.parametrize("kind", ["sleep", "stop"])
def test_dry_run_uses_no_transport_and_changes_no_snapshot(kind, tmp_path):
    executor, state, requests, _ = dispatcher(enabled=False)
    state[0] = replace(state[0], read_only=True)
    before = state[0].to_dict()
    result = executor.execute(action(kind), dry_run=True)
    assert result["would"][0]["kind"] == kind
    assert not requests
    assert state[0].to_dict() == before
    assert list(tmp_path.iterdir()) == []


def test_sleep_quotes_model_and_reports_submission_not_release():
    snapshot = observation()
    snapshot = replace(snapshot, models=(replace(snapshot.models[0], name="group/model"),),
                       activity=(Activity("group/model", in_flight=0),))
    executor, state, requests, _ = dispatcher(snapshot)
    before = state[0]
    result = executor.execute(action(model="group/model"), dry_run=False, deadline=6)
    assert requests == [("POST", "/api/models/unload/group%2Fmodel", 6)]
    assert result["status"] == "submitted" and result["confirmed"] is False
    assert "freed_gb" not in result and "budget_gb" not in result
    assert state[0] == before


def test_direct_stop_has_no_implicit_sleep_or_accounting_change():
    executor, state, requests, _ = dispatcher()
    before = state[0]
    assert executor.execute(action("stop"), dry_run=False)["confirmed"] is False
    assert requests == [("stop", "vllm-model.service", 7.0)]
    assert state[0] == before


@pytest.mark.parametrize("kind", ["wake", "place"])
def test_reentrant_cold_start_and_placement_are_not_mounted_here(kind):
    executor, _, requests, _ = dispatcher()
    with pytest.raises(ActionDispatchError, match="operation_not_enabled"):
        executor.execute(action(kind), dry_run=False)
    assert not requests


def test_disabled_and_read_only_modes_do_not_call_transports():
    executor, state, requests, _ = dispatcher(enabled=False)
    with pytest.raises(ActionDispatchError, match="executor_disabled"):
        executor.execute(action(), dry_run=False)
    executor.enabled = True
    state[0] = replace(state[0], read_only=True)
    with pytest.raises(ActionDispatchError, match="read_only"):
        executor.execute(action(), dry_run=False)
    assert not requests


@pytest.mark.parametrize("change,reason", [
    ({"sampled_at": 9900}, "unknown_or_stale_snapshot"),
    ({"sampled_at": 10001}, "unknown_or_stale_snapshot"),
    ({"sampled_at": None}, "unknown_or_stale_snapshot"),
    ({"errors": ("probe unavailable",)}, "unknown_or_stale_snapshot"),
    ({"pins": (Pin("model", 11000, "owner"),)}, "pinned"),
    ({"activity": (Activity("model", in_flight=1),)}, "in_flight"),
    ({"activity": (Activity("model", in_flight=None),)}, "unknown_in_flight"),
    ({"activity": (Activity("model", in_flight=False),)}, "unknown_in_flight"),
])
def test_fresh_protection_blocks_before_any_request(change, reason):
    executor, _, requests, _ = dispatcher(replace(observation(), **change))
    with pytest.raises(ActionDispatchError, match=reason) as error:
        executor.execute(action("stop", reason="fault_cleanup"), dry_run=False)
    assert not error.value.attempted and not requests


@pytest.mark.parametrize("unit", ["ssh.service", "vllm-*.service", "vllm-x.service\n", "../vllm-x.service", None])
def test_stop_restricts_target_to_one_known_vllm_unit(unit):
    snapshot = observation()
    executor, _, requests, _ = dispatcher(replace(snapshot, models=(replace(snapshot.models[0], unit=unit),)))
    with pytest.raises(ActionDispatchError, match="invalid_unit"):
        executor.execute(action("stop"), dry_run=False)
    assert not requests


def test_default_stop_and_shared_unit_alias_are_blocked():
    snapshot = observation()
    executor, state, requests, _ = dispatcher(replace(snapshot, models=(replace(snapshot.models[0], is_default=True),)))
    with pytest.raises(ActionDispatchError, match="default_or_unknown_role"):
        executor.execute(action("stop"), dry_run=False)
    state[0] = replace(snapshot, models=snapshot.models + (replace(snapshot.models[0], name="other"),))
    with pytest.raises(ActionDispatchError, match="ambiguous_unit"):
        executor.execute(action("stop"), dry_run=False)
    assert not requests


@pytest.mark.parametrize("memory,reason", [(MemoryState(170, 0), "memory_budget"),
    (MemoryState(None, 0), "unknown_memory"), (MemoryState(500, None), "unknown_memory"),
    (MemoryState(500, 190), "memory_budget")])
def test_sleep_readmits_ram_before_transport(memory, reason):
    executor, _, requests, _ = dispatcher(replace(observation(), memory=memory))
    with pytest.raises(ActionDispatchError, match=reason):
        executor.execute(action(), dry_run=False)
    assert not requests


def test_revalidation_reads_new_pin_after_waiting_for_the_same_lock():
    executor, state, requests, _ = dispatcher()
    entered = threading.Event()
    result = []
    def run():
        entered.set()
        try:
            executor.execute(action("stop"), dry_run=False)
        except ActionDispatchError as exc:
            result.append(exc.reason)
    with executor.action_lock:
        worker = threading.Thread(target=run)
        worker.start()
        assert entered.wait(1)
        state[0] = replace(state[0], pins=(Pin("model", 11000, "new-owner"),))
    worker.join(1)
    assert result == ["pinned"] and not requests


def test_guard_and_transport_hold_the_injected_lock():
    lock = threading.RLock()
    checked = []
    def probe_lock():
        acquired = lock.acquire(blocking=False)
        checked.append(acquired)
        if acquired:
            lock.release()
    def stop(unit, *, deadline):
        worker = threading.Thread(target=probe_lock)
        worker.start()
        worker.join(1)
        return 0
    executor, _, _, _ = dispatcher(action_lock=lock, stop_unit=stop)
    assert executor.action_lock is lock
    executor.execute(action("stop"), dry_run=False)
    assert checked == [False]


def test_expired_outer_deadline_makes_no_request():
    executor, _, requests, _ = dispatcher()
    with pytest.raises(ActionDispatchError, match="deadline_exceeded") as error:
        executor.execute(action(), dry_run=False, deadline=5)
    assert not error.value.attempted and not requests


def test_deadline_after_guard_is_checked_before_dispatch():
    executor, state, requests, now = dispatcher()
    def slow_snapshot():
        now[0] = 8
        return state[0]
    executor.snapshot = slow_snapshot
    with pytest.raises(ActionDispatchError, match="deadline_exceeded") as error:
        executor.execute(action(), dry_run=False)
    assert not error.value.attempted and not requests


def test_overrun_after_request_preserves_uncertain_side_effect():
    executor, _, _, now = dispatcher()
    def slow_stop(unit, *, deadline):
        assert deadline == 6
        now[0] = 6
        return 0
    executor.stop_unit = slow_stop
    with pytest.raises(ActionDispatchError, match="deadline_exceeded") as error:
        executor.execute(action("stop"), dry_run=False, deadline=6)
    assert error.value.attempted


@pytest.mark.parametrize("code", [500, True, None])
def test_transport_rejection_is_not_confirmed(code):
    executor, _, _, _ = dispatcher(http_request=lambda *args, **kwargs: code)
    with pytest.raises(ActionDispatchError, match="transport_rejected") as error:
        executor.execute(action(), dry_run=False)
    assert error.value.attempted


def test_transport_exception_is_sanitized_and_marked_attempted(caplog):
    def fail(*args, **kwargs):
        raise OSError("private credentials or response")
    executor, _, _, _ = dispatcher(stop_unit=fail)
    with pytest.raises(ActionDispatchError, match="transport_error") as error:
        executor.execute(action("stop"), dry_run=False)
    assert error.value.attempted
    assert "private" not in caplog.text


def test_lock_timeout_never_reads_snapshot_or_calls_transport():
    executor, _, requests, _ = dispatcher(timeout_seconds=0.02)
    reasons = []
    with executor.action_lock:
        def run():
            try:
                executor.execute(action(), dry_run=False)
            except ActionDispatchError as exc:
                reasons.append(exc.reason)
        thread = threading.Thread(target=run)
        thread.start()
        thread.join(1)
    assert reasons == ["deadline_exceeded"] and not requests


def test_plain_lock_is_rejected_to_prevent_reentrant_snapshot_deadlock():
    with pytest.raises(ValueError, match="scheduler RLock"):
        dispatcher(action_lock=threading.Lock())
