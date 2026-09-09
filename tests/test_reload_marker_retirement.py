# Generated-By: Codex / gpt-6-astra
"""Recovery receipt retirement faults; all files/proofs are synthetic fixtures."""
import hashlib
from pathlib import Path

import pytest

from llmsvc.reload import RecoveryProof, ReloadError, ReloadQueue
from test_reload import add, harness, make_quiet


def proof(queue):
    return RecoveryProof(hashlib.sha256(queue.marker.read_bytes()).hexdigest(),
                         True, True, True, True)


def reconstruct(queue):
    return ReloadQueue(queue.path, action_lock=queue.action_lock, quiet=queue.quiet,
        snapshot=queue.snapshot, validate=queue.validate, notify_reload=queue.notify_reload,
        log=queue.log, clock=queue.clock, wall_clock=queue.wall_clock)


def prepare(harness, phase):
    queue, quiet, clock, calls, _ = harness
    first = add(queue)
    second = add(queue, b'# later\n')
    make_quiet(quiet, clock)
    if phase == 'reconcile':
        notify = queue.notify_reload
        queue.notify_reload = lambda **kwargs: (_ for _ in ()).throw(RuntimeError('fixture unknown settlement'))
        assert queue.process_once()['status'] == 'reconciliation_required'
        queue.notify_reload = notify
    return queue, first, second, calls


def inject_fault(queue, monkeypatch, *, fault='fsync', repair='success'):
    raw = []
    failed = []
    unlink, sync, open_file = Path.unlink, queue._sync_directory, Path.open
    def unlink_marker(path, *args, **kwargs):
        if path == queue.marker:
            raw.append(path.read_bytes())
            if fault == 'unlink':
                failed.append(True)
                raise OSError('fixture unlink failed')
        return unlink(path, *args, **kwargs)
    def sync_directory():
        if not queue.marker.exists():
            failed.append(True)
            if repair == 'conflict':
                queue.marker.write_bytes(b'other recovery receipt')
            raise OSError('fixture directory fsync failed after unlink')
        sync()
    def open_marker(path, mode='r', *args, **kwargs):
        if path == queue.marker and mode == 'xb' and failed and repair == 'unavailable':
            raise OSError('fixture repair storage unavailable')
        return open_file(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', unlink_marker)
    monkeypatch.setattr(Path, 'open', open_marker)
    monkeypatch.setattr(queue, '_sync_directory', sync_directory)
    return raw


@pytest.mark.parametrize('phase', ['process', 'reconcile'])
@pytest.mark.parametrize('fault', ['unlink', 'fsync'])
def test_retirement_failure_restores_receipt_and_blocks_until_explicit_recovery(harness, monkeypatch, phase, fault):
    queue, first, second, calls = prepare(harness, phase)
    with monkeypatch.context() as patch:
        raw = inject_fault(queue, patch, fault=fault)
        if phase == 'process':
            result = queue.process_once()
            assert result['status'] == 'reconciliation_required' and result['config_committed']
        else:
            with pytest.raises(OSError):
                queue.reconcile(lambda _: proof(queue))
        assert queue.marker.read_bytes() == raw[0]
        assert queue.fenced and queue.queue_snapshot()['fenced']
        assert reconstruct(queue).inspect_recovery()['fenced']
        before = queue.path.read_bytes(), queue.marker.read_bytes(), list(calls), queue.get(second['id'])
        for dry_run in (False, True):
            with pytest.raises(ReloadError, match='reconciliation'):
                add(queue, dry_run=dry_run)
        assert queue.process_once() is None
        assert (queue.path.read_bytes(), queue.marker.read_bytes(), calls, queue.get(second['id'])) == before
    # Only the pre-existing complete, marker-bound fixture proof can retire it.
    assert queue.reconcile(lambda _: proof(queue)) == {'status': 'reconciled'}
    assert not queue.fenced and not queue.marker.exists()
    assert queue.path.read_bytes().endswith(b'# added\n')


@pytest.mark.parametrize('phase', ['process', 'reconcile'])
@pytest.mark.parametrize('repair', ['unavailable', 'conflict'])
def test_failed_repair_keeps_memory_fence_and_never_overwrites_another_marker(harness, monkeypatch, phase, repair):
    queue, _, second, calls = prepare(harness, phase)
    with monkeypatch.context() as patch:
        inject_fault(queue, patch, repair=repair)
        if phase == 'process':
            assert queue.process_once()['status'] == 'reconciliation_required'
        else:
            with pytest.raises(OSError):
                queue.reconcile(lambda _: proof(queue))
        assert queue.fenced
        inspected = queue.inspect_recovery()
        assert inspected['fenced'] and inspected['status'] == 'reconciliation_required'
        assert inspected['marker_valid'] is False and inspected['settlement_confirmed'] is None
        if repair == 'unavailable':
            assert not queue.marker.exists()
            assert inspected['blocked_by'] == [{'reason': 'recovery_marker_persistence_failed'}]
        else:
            assert queue.marker.read_bytes() == b'other recovery receipt'
        assert queue.queue_snapshot()['fenced']
        before = queue.path.read_bytes(), list(calls), queue.get(second['id'])
        with pytest.raises(ReloadError):
            add(queue)
        with pytest.raises(ReloadError):
            queue.reconcile(lambda _: pytest.fail('missing/mismatched marker reached verifier'))
        assert queue.process_once() is None
        assert (queue.path.read_bytes(), calls, queue.get(second['id'])) == before
    # Storage becoming writable alone is not recovery; no automatic latch clear.
    assert queue.fenced and queue.process_once() is None


def test_dry_run_reconcile_does_not_repair_or_clear_failed_retirement(harness, monkeypatch):
    queue, _, _, _ = prepare(harness, 'process')
    inject_fault(queue, monkeypatch, repair='unavailable')
    assert queue.process_once()['status'] == 'reconciliation_required'
    before = queue._completion_marker, queue.path.read_bytes()
    result = queue.reconcile(lambda _: pytest.fail('dry-run verifier'), dry_run=True)
    assert result == {'would': [{'kind': 'reconcile_config'}]}
    assert queue.fenced and not queue.marker.exists()
    assert before == (queue._completion_marker, queue.path.read_bytes())
