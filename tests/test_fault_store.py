# Generated-By: Codex / gpt-6-astra
"""Lazy fault fencing migration, pin retention and restart-safe account stages."""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from llmsvc.state import FaultClaim, Lease, Pin
from llmsvc.store import IntentStore


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "state.sqlite"
    store = IntentStore(str(path), action_lock=threading.RLock())
    lease = Lease("lease", "model", 0, .4, 20000, 80)
    store.create_lease(lease, "vllm-model.service")
    store.transition_lease(lease.lease_id, "confirmed")
    store.put_pin(Pin("model", 20000, "owner"))
    claim = FaultClaim("lease", "model", "vllm-model.service", "a"*32, 0, "unexpected_unit_exit", 100, proxy_origin_hash="f"*64)
    yield store, path, claim
    store.close()


def version(path):
    with sqlite3.connect(path) as db:
        return db.execute("PRAGMA user_version").fetchone()[0]


def test_fault_dry_run_and_readonly_do_not_migrate_v2_or_change_any_bytes(database):
    store, path, claim = database
    before = path.read_bytes()
    result = store.claim_fault(claim, dry_run=True)
    assert result["would"][0]["kind"] == "fault_claim"
    assert not store.faults() and version(path) == 2 and path.read_bytes() == before
    readonly = IntentStore(str(path), action_lock=threading.RLock(), read_only=True)
    try:
        assert not readonly.faults()
        readonly.claim_fault(claim, dry_run=True)
        with pytest.raises(PermissionError):
            readonly.claim_fault(claim)
    finally:
        readonly.close()
    assert path.read_bytes() == before


def test_real_claim_lazily_migrates_and_survives_readonly_restart(database):
    store, path, claim = database
    store.claim_fault(claim)
    assert version(path) == 3 and store.fault("model") == claim
    assert store.lease("lease")[0].status == "confirmed"
    readonly = IntentStore(str(path), action_lock=threading.RLock(), read_only=True)
    try:
        assert readonly.faults() == (claim,)
        assert readonly.active(100)[0] == (Pin("model", 20000, "owner"),)
        before = path.read_bytes()
        readonly.advance_fault(claim, stage="released", dry_run=True)
        assert path.read_bytes() == before and readonly.lease("lease")[0].status == "confirmed"
    finally:
        readonly.close()


def test_release_and_pending_proxy_fence_are_one_durable_transition(database):
    store, path, claim = database
    store.claim_fault(claim)
    released = store.advance_fault(claim, stage="released")
    assert store.lease("lease")[0].status == "released" and store.fault("model") == released
    reopened = IntentStore(str(path), action_lock=threading.RLock())
    try:
        assert reopened.fault("model") == released
        assert reopened.active(100)[0][0].by == "owner"
        submitted = reopened.advance_fault(released, stage="released", proxy_submitted=True)
        acknowledged = reopened.advance_fault(submitted, stage="released", proxy_acknowledged=True)
        reopened.advance_fault(acknowledged, stage="complete")
        assert not reopened.faults() and reopened.active(100)[0][0].model == "model"
    finally:
        reopened.close()


def test_concurrent_claims_have_one_owner_and_never_release_budget(database):
    store, _, claim = database
    def attempt():
        try:
            store.claim_fault(claim)
            return "claimed"
        except sqlite3.IntegrityError:
            return "conflict"
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(lambda _: attempt(), range(2))) == ["claimed", "conflict"]
    assert store.faults() == (claim,) and store.lease("lease")[0].budget_gb == 80
    assert store.lease("lease")[0].status == "confirmed"


@pytest.mark.parametrize("change", [{"lease_id": "other"}, {"unit": "vllm-other.service"}, {"gpu": 1}])
def test_mismatched_initial_account_cannot_create_a_claim_or_schema(database, change):
    store, path, claim = database
    before = path.read_bytes()
    with pytest.raises(ValueError, match="account changed"):
        store.claim_fault(replace(claim, **change))
    assert version(path) == 2 and not store.faults() and path.read_bytes() == before


def test_unknown_effect_error_preserves_claim_and_stale_transition_cannot_overwrite_it(database):
    store, _, claim = database
    store.claim_fault(claim)
    updated = store.advance_fault(claim, stage="claimed", error="fault_exit_unconfirmed")
    assert store.lease("lease")[0].status == "confirmed"
    with pytest.raises(ValueError, match="claim changed"):
        store.advance_fault(claim, stage="released")
    assert store.fault("model") == updated


def test_fault_fences_cannot_be_bypassed_by_ordinary_store_lease_methods(database):
    store, _, claim = database
    store.claim_fault(claim)
    with pytest.raises(ValueError, match="fault recovery pending"):
        store.transition_lease("lease", "released")
    released = store.advance_fault(claim, stage="released")
    with pytest.raises(ValueError, match="fault recovery pending"):
        store.create_lease(Lease("new", "model", 0, .4, 20000, 80), claim.unit)
    assert store.fault("model") == released


@pytest.mark.parametrize("phase", ["migration", "release"])
def test_fault_transactions_roll_back_both_schema_claim_and_account_stage(database, phase, monkeypatch):
    store, path, claim = database
    if phase == "release":
        store.claim_fault(claim)
    actual = store._db
    class Interrupted:
        def __getattr__(self, name):
            return getattr(actual, name)
        def __enter__(self):
            return actual.__enter__()
        def __exit__(self, *args):
            return actual.__exit__(*args)
        def execute(self, sql, *args):
            if ((phase == "migration" and sql == "PRAGMA user_version = 3")
                    or (phase == "release" and sql.startswith("UPDATE llmsvc_faults"))):
                raise sqlite3.OperationalError("injected transaction interruption")
            return actual.execute(sql, *args)
    monkeypatch.setattr(store, "_db", Interrupted())
    with pytest.raises(sqlite3.OperationalError, match="interruption"):
        if phase == "migration":
            store.claim_fault(claim)
        else:
            store.advance_fault(claim, stage="released")
    assert store.lease("lease")[0].status == "confirmed"
    assert store.active(100)[0] == (Pin("model", 20000, "owner"),)
    if phase == "migration":
        assert version(path) == 2 and not store.faults()
        assert actual.execute("SELECT name FROM sqlite_master WHERE name='llmsvc_faults'").fetchone() is None
    else:
        assert version(path) == 3 and store.fault("model") == claim


def test_proxy_submission_cannot_be_forgotten_or_claim_cleared_without_ack(database):
    store, _, claim = database
    store.claim_fault(claim)
    released = store.advance_fault(claim, stage="released")
    with pytest.raises(ValueError):
        store.advance_fault(released, stage="complete")
    with pytest.raises(ValueError, match="prior durable"):
        store.advance_fault(released, stage="released", proxy_submitted=True, proxy_acknowledged=True)
    sent = store.advance_fault(released, stage="released", proxy_submitted=True)
    with pytest.raises(ValueError):
        store.advance_fault(sent, stage="released", proxy_submitted=False)
    with pytest.raises(ValueError):
        store.advance_fault(sent, stage="complete")
    assert store.fault("model") == sent
