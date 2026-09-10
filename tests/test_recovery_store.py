# Generated-By: Codex / gpt-6-astra
"""Ordinary recovery schema/fence transactions; physical proofs belong to core."""

import sqlite3
import threading
from dataclasses import replace

import pytest

from llmsvc.state import FaultClaim, Lease, Pin, RecoveryClaim
from llmsvc.store import IntentStore


@pytest.fixture
def recovery_store(tmp_path):
    path = tmp_path / "state.sqlite"
    store = IntentStore(path, action_lock=threading.RLock())
    store.put_pin(Pin("protected-other", 20000, "fixture"))
    store.create_lease(Lease("source-lease", "source", 1, .8, 20000, 80), "vllm-source.service")
    store.transition_lease("source-lease", "confirmed")
    claim = RecoveryClaim("a"*32, "source", "source-lease", "vllm-source.service", "1"*32,
                          1, "cannot_wake", 10000, .8, 80, "f"*64, True)
    try:
        yield store, path, claim
    finally:
        store.close()


def version(store):
    return store._db.execute("PRAGMA user_version").fetchone()[0]


def settled(store, claim):
    store.claim_recovery(claim)
    claim = store.advance_recovery(claim, stop_submitted=True)
    claim = store.advance_recovery(claim, stop_acknowledged=True)
    # Tests supply the core's already-verified source absence at this boundary.
    claim = store.advance_recovery(claim, stage="released")
    claim = store.advance_recovery(claim, proxy_submitted=True)
    claim = store.advance_recovery(claim, proxy_acknowledged=True)
    return store.advance_recovery(claim, stage="settled")


def test_claim_dryrun_never_migrates_or_mutates_and_readonly_claim_is_rejected(recovery_store):
    store, path, claim = recovery_store
    before = path.read_bytes()
    assert store.claim_recovery(claim, dry_run=True)["would"]
    assert version(store) == 2 and path.read_bytes() == before and store.recoveries() == ()
    with pytest.raises(PermissionError):
        reader = IntentStore(path, action_lock=threading.RLock(), read_only=True)
        try:
            reader.claim_recovery(claim)
        finally:
            reader.close()
    assert path.read_bytes() == before


def test_real_claim_lazily_migrates_and_fences_ordinary_source_operations(recovery_store):
    store, path, claim = recovery_store
    store.claim_recovery(claim)
    assert version(store) == 4 and store.recovery("source") == claim
    assert store.active(10000)[0] == (Pin("protected-other", 20000, "fixture"),)
    assert store.lease("source-lease")[0].budget_gb == 80
    for status in ("released", "confirmed", "stale"):
        with pytest.raises(ValueError, match="sleeping recovery"):
            store.transition_lease("source-lease", status)
    with pytest.raises(ValueError, match="recovery"):
        store.create_lease(Lease("wrong", "source", 0, .8, 20000, 80), claim.unit)
    with pytest.raises(ValueError, match="recovery"):
        store.claim_fault(FaultClaim("source-lease", "source", claim.unit, claim.invocation_id, 1,
                                     "health", 10000, proxy_origin_hash="f"*64))
    reader = IntentStore(path, action_lock=threading.RLock(), read_only=True)
    try:
        assert reader.recoveries() == (claim,)
    finally:
        reader.close()


def test_model_unique_claim_and_stale_source_preserve_old_ledger(recovery_store):
    store, _, claim = recovery_store
    original = store.lease("source-lease"), store.active(10000)
    # A stale expected source cannot migrate/create an ordinary claim.
    with pytest.raises(ValueError):
        store.claim_recovery(replace(claim, source_gpu=0))
    assert version(store) == 2 and store.recoveries() == ()
    assert (store.lease("source-lease"), store.active(10000)) == original
    store.claim_recovery(claim)
    with pytest.raises(ValueError):
        store.claim_recovery(replace(claim, id="b"*32))
    assert store.recoveries() == (claim,)


def test_failed_first_claim_rolls_back_schema_and_preserves_accounts(recovery_store):
    store, _, claim = recovery_store
    before = store.leases(), store.active(10000)
    store._db.set_authorizer(lambda operation, table, *rest:
        sqlite3.SQLITE_DENY if operation == sqlite3.SQLITE_INSERT and table == "llmsvc_recoveries" else sqlite3.SQLITE_OK)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            store.claim_recovery(claim)
    finally:
        store._db.set_authorizer(lambda *args: sqlite3.SQLITE_OK)
    assert version(store) == 2 and store.recoveries() == ()
    assert store._db.execute("SELECT name FROM sqlite_master WHERE name IN ('llmsvc_recoveries','llmsvc_faults')").fetchall() == []
    assert (store.leases(), store.active(10000)) == before


def test_fault_claims_survive_upgrade_and_new_faults_do_not_downgrade_schema(recovery_store):
    store, _, claim = recovery_store
    for name in ("fault-one", "fault-two"):
        store.create_lease(Lease(name, name, 0, .1, 20000, 10), "vllm-"+name+".service")
        store.transition_lease(name, "confirmed")
    def fault(name):
        return FaultClaim(name, name, "vllm-"+name+".service", "3"*32, 0, "health", 10000, proxy_origin_hash="f"*64)
    store.claim_fault(fault("fault-one"))
    assert version(store) == 3
    store.claim_recovery(claim)
    store.claim_fault(fault("fault-two"))
    assert version(store) == 4
    assert set(store.faults()) == {fault("fault-one"), fault("fault-two")}
    assert store.recovery("source") == claim


def test_destination_lease_and_binding_are_atomic_and_source_is_not_charged_twice(recovery_store):
    store, _, claim = recovery_store
    claim = settled(store, claim)
    assert store.lease("source-lease")[0].status == "released" and store.leases() == ()
    claim = store.advance_recovery(claim, stage="waking", wake_submitted=True)
    for lease in (Lease("bad", "source", 1, .8, 20000, 80), Lease("bad", "source", 0, .8, 20000, 40)):
        with pytest.raises(ValueError):
            store.create_lease(lease, claim.unit, recovery_claim=claim)
    destination = Lease("destination", "source", 0, .4, 20000, 80)
    store._db.execute("CREATE TRIGGER reject_binding BEFORE UPDATE ON llmsvc_recoveries WHEN NEW.stage='destination' BEGIN SELECT RAISE(ABORT, 'fixture binding failure'); END")
    store._db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        store.create_lease(destination, claim.unit, recovery_claim=claim)
    assert store.leases() == () and store.recovery("source") == claim
    store._db.execute("DROP TRIGGER reject_binding")
    store._db.commit()
    store.create_lease(destination, claim.unit, recovery_claim=claim)
    bound = store.recovery("source")
    assert bound.destination_lease_id == destination.lease_id and bound.stage == "destination"
    assert sum(lease.budget_gb for lease, _ in store.leases()) == 80
    with pytest.raises(ValueError):
        store.create_lease(replace(destination, lease_id="second"), claim.unit, recovery_claim=claim)
    store.transition_lease(destination.lease_id, "confirmed")
    with pytest.raises(ValueError, match="completion"):
        store.advance_recovery(bound, stage="complete")
    bound = store.advance_recovery(bound, wake_acknowledged=True, destination_invocation_id="2"*32)
    store.advance_recovery(bound, stage="complete")
    assert store.recovery("source") is None and store.lease(destination.lease_id)[0].status == "confirmed"


def test_retirement_and_zero_submission_abort_do_not_delete_protections(recovery_store):
    store, _, claim = recovery_store
    store.claim_recovery(claim)
    store.advance_recovery(claim, stage="complete", error="new_pin_before_submission")
    assert store.recoveries() == () and store.lease("source-lease")[0].status == "confirmed"
    retired = settled(store, replace(claim, id="b"*32, relocate=False))
    store.advance_recovery(retired, stage="complete")
    assert store.recoveries() == () and store.leases() == ()
    assert store.active(10000)[0][0].model == "protected-other"


@pytest.mark.parametrize("changes", [{"stage":"released"}, {"proxy_submitted":True},
    {"stop_acknowledged":True}, {"wake_submitted":True}, {"wake_acknowledged":True}])
def test_progress_cannot_skip_durable_submission_or_exit_stages(recovery_store, changes):
    store, _, claim = recovery_store
    store.claim_recovery(claim)
    with pytest.raises(ValueError):
        store.advance_recovery(claim, **changes)
    assert store.recovery("source") == claim
    assert store.lease("source-lease")[0].status == "confirmed"


def test_unsupported_future_schema_is_rejected_without_writes(recovery_store):
    store, path, claim = recovery_store
    store.claim_recovery(claim)
    store._db.execute("PRAGMA user_version = 6")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="unsupported"):
        IntentStore(path, action_lock=threading.RLock(), read_only=True)
    assert path.read_bytes() == before
