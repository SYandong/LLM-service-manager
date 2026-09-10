# Generated-By: Codex / gpt-6-astra
"""Internal durable-receipt confirmation plus actual core catalog recovery."""
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from llmsvc.catalog import CatalogRuntime
from llmsvc.reload import RecoveryProof, ReloadError, ReloadQueue
from llmsvc.reload_witness import InstanceIdentity
from llmsvc.state import Lease
from test_catalog_lifecycle import catalog, install
from test_registry_http_preview import request
from test_reload import add, harness, make_quiet


def receipt(harness, *, present=False):
    queue, quiet, clock, _, _ = harness
    queue.notify_reload = lambda **kw: (_ for _ in ()).throw(RuntimeError('fixture unconfirmed adoption'))
    add(queue)
    make_quiet(quiet, clock)
    assert queue.process_once()['status'] == 'reconciliation_required'
    raw = queue.marker.read_bytes()
    if not present:
        queue.marker.unlink()  # Explicit durable core receipt-loss fixture.
    return queue, raw


def full(raw):
    return RecoveryProof(hashlib.sha256(raw).hexdigest(), True, True, True, True)


@pytest.mark.parametrize('present', [False, True])
def test_exact_durable_receipt_confirms_without_config_or_notifier_retry(harness, monkeypatch, present):
    queue, raw = receipt(harness, present=present)
    config = queue.path.read_bytes()
    queue.notify_reload = lambda **kw: pytest.fail('receipt confirmation retriggered reload')
    queue.validate = lambda path: pytest.fail('receipt confirmation staged a config')
    synced = []
    real_sync = queue._sync_directory
    def sync():
        synced.append(queue.marker.exists())
        real_sync()
    monkeypatch.setattr(queue, '_sync_directory', sync)
    assert queue.confirm_retired_receipt(raw, lambda _: full(raw)) == {'status': 'reconciled'}
    assert synced == [False] and not queue.fenced and not queue.marker.exists()
    assert queue.path.read_bytes() == config


@pytest.mark.parametrize('field', ['generation_confirmed', 'instance_confirmed', 'settlement_confirmed', 'cleanup_confirmed', 'marker_sha256'])
def test_partial_or_wrong_hash_receipt_proof_stays_fenced(harness, field):
    queue, raw = receipt(harness)
    value = '0' * 64 if field == 'marker_sha256' else False
    with pytest.raises(ReloadError):
        queue.confirm_retired_receipt(raw, lambda _: replace(full(raw), **{field: value}))
    assert queue.fenced and queue.inspect_recovery()['fenced']
    assert not queue.marker.exists()


def test_exact_instance_binding_is_required(harness):
    queue, raw = receipt(harness)
    data = json.loads(raw)
    data['witness_binding'] = {'endpoint': 'http://127.0.0.1:1/api/mcp', 'generation': 'gen_'+'1'*32,
                              'instance': {'pid': 123, 'start_ticks': '456'}, 'candidate_sha256': data['sha256']}
    raw = json.dumps(data).encode()
    with pytest.raises(ReloadError, match='instance'):
        queue.confirm_retired_receipt(raw, lambda _: replace(full(raw), instance=InstanceIdentity(124, '456')))
    assert queue.fenced
    assert queue.confirm_retired_receipt(raw, lambda _: replace(full(raw), instance=InstanceIdentity(123, '456')))


@pytest.mark.parametrize('fault', ['foreign_before', 'foreign_during_proof', 'foreign_during_sync', 'foreign_during_final_read', 'fsync', 'changed_config', 'missing_config'])
def test_absent_receipt_faults_keep_latch_and_never_erase_foreign_files(harness, monkeypatch, fault):
    queue, raw = receipt(harness)
    original = queue.path.read_bytes()
    if fault == 'foreign_before':
        queue.marker.write_bytes(b'foreign receipt')
    elif fault == 'missing_config':
        queue.path.unlink()
    def confirm(_):
        if fault == 'foreign_during_proof':
            queue.marker.write_bytes(b'foreign receipt')
        elif fault == 'changed_config':
            queue.path.write_bytes(original+b'# changed\n')
        return full(raw)
    sync = queue._sync_directory
    def directory_sync():
        if fault == 'fsync':
            raise OSError('fixture fsync failure')
        if fault == 'foreign_during_sync':
            queue.marker.write_bytes(b'foreign receipt')
        sync()
    monkeypatch.setattr(queue, '_sync_directory', directory_sync)
    if fault == 'foreign_during_final_read':
        read, count = queue._read, []
        def changed_read():
            value = read()
            count.append(1)
            if len(count) == 3:
                queue.marker.write_bytes(b'foreign receipt')
            return value
        monkeypatch.setattr(queue, '_read', changed_read)
    with pytest.raises((ReloadError, OSError)):
        queue.confirm_retired_receipt(raw, confirm)
    assert queue.fenced
    if fault.startswith('foreign'):
        assert queue.marker.read_bytes() == b'foreign receipt'
    if fault == 'missing_config':
        assert not queue.path.exists()


@pytest.mark.parametrize('raw', [b'{}', b'{"sha256":1,"sha256":2}', b'x'*65537, 'not bytes'])
def test_bad_receipt_cannot_replace_existing_latch_or_reach_proof(harness, raw):
    queue, saved = receipt(harness)
    queue._completion_marker = saved
    with pytest.raises(ReloadError):
        queue.confirm_retired_receipt(raw, lambda _: pytest.fail('bad receipt reached proof'))
    assert queue._completion_marker == saved and queue.fenced


def test_dry_run_reads_nothing_and_cannot_clear_latch(harness, monkeypatch):
    queue, raw = receipt(harness)
    queue._completion_marker = raw
    before = queue.path.read_bytes()
    def forbidden(*args, **kwargs): pytest.fail('dry-run performed receipt I/O or proof')
    monkeypatch.setattr(queue, '_read', forbidden)
    monkeypatch.setattr(queue, '_read_marker', forbidden)
    monkeypatch.setattr(queue, '_sync_directory', forbidden)
    assert queue.confirm_retired_receipt(None, forbidden, dry_run=True) == {'would': [{'kind': 'reconcile_config'}]}
    assert queue._completion_marker == raw and queue.path.read_bytes() == before


@pytest.mark.parametrize('restart', [False, True])
def test_real_catalog_missing_receipt_keeps_global_gate_until_owner_confirmation(catalog, monkeypatch, restart):
    c = catalog
    original_sync = c.q._sync_directory
    def failed_retirement():
        if c.store.catalog_checkpoint() and c.store.catalog_checkpoint()['phase'] == 'published':
            raise OSError('fixture retirement persistence failure')
        original_sync()
    monkeypatch.setattr(c.q, '_sync_directory', failed_retirement)
    result, _ = install(c)
    assert result['status'] == 'reconciliation_required'
    checkpoint = c.store.catalog_checkpoint()
    assert checkpoint['phase'] == 'published' and c.store.catalog_pending()
    c.q.marker.unlink(missing_ok=True)
    # Clear the injected disk fault; do not clear a latch or durable claim.
    monkeypatch.setattr(c.q, '_sync_directory', original_sync)
    if restart:
        from llmsvc.store import IntentStore
        c.store.close()
        c.store = IntentStore(c.cfg.state_db_path, action_lock=c.s.action_lock)
        c.s.store = c.store
        c.q = ReloadQueue(c.path, action_lock=c.s.action_lock, quiet=c.q.quiet,
                          snapshot=c.s.snapshot, validate=lambda p:pytest.fail('restart validation retry'),
                          notify_reload=lambda **kw:pytest.fail('restart reload retry'), log=lambda e:None,
                          clock=c.clock, wall_clock=c.clock)
        c.runtime = CatalogRuntime(c.s, c.q, verifier=c.verify, collector_factory=c.collector_factory,
                                   relay_factory=lambda config:None, transport_factory=c.transport_factory)
    assert c.s.catalog_fenced and c.store.catalog_pending()
    assert request(c.address, 'POST', '/v1/place', {'model':'new','util':.2})[0] == 503
    old_confirm = c.q.confirm_retired_receipt
    phases = []
    def confirm(raw, verifier, **kw):
        assert c.s.catalog_fenced and c.store.catalog_pending()
        result = old_confirm(raw, verifier, **kw)
        phases.append(c.store.catalog_checkpoint()['phase'])
        # The queue may be unfenced now, but durable core admission is still held.
        assert not c.q.fenced and c.s.catalog_fenced and c.store.catalog_pending()
        with pytest.raises(ValueError, match='catalog'):
            c.store.create_lease(Lease('bypass','new',0,.4,20000,40), 'vllm-new.service')
        return result
    monkeypatch.setattr(c.q, 'confirm_retired_receipt', confirm)
    before = c.path.read_bytes(), c.world['calls'].count('adopt')
    assert c.runtime.reconcile()['status'] == 'reconciled'
    assert phases == ['published'] and c.store.catalog_checkpoint()['phase'] == 'released'
    assert not c.s.catalog_fenced and not c.q.fenced
    assert before == (c.path.read_bytes(), c.world['calls'].count('adopt'))
    c.s.sample_once()
    status, placed = request(c.address, 'POST', '/v1/place', {'model':'new','util':.2})
    assert status == 200 and c.store.lease(placed['lease_id'])[0].budget_gb == 40


def test_real_catalog_foreign_marker_after_receipt_confirmation_keeps_global_gate(catalog, monkeypatch):
    c = catalog
    save = c.store.save_catalog
    with monkeypatch.context() as setup:
        def fail_release(expected, record, **kwargs):
            if record['phase'] == 'released':
                raise OSError('fixture release checkpoint failure')
            return save(expected, record, **kwargs)
        setup.setattr(c.store, 'save_catalog', fail_release)
        with pytest.raises(OSError):
            install(c)
    assert c.store.catalog_checkpoint()['phase'] == 'published' and not c.q.marker.exists()
    owner_confirm = c.q.confirm_retired_receipt
    def external_marker_after_confirmation(raw, confirm, **kwargs):
        result = owner_confirm(raw, confirm, **kwargs)
        c.q.marker.write_bytes(b'foreign transaction receipt')
        return result
    monkeypatch.setattr(c.q, 'confirm_retired_receipt', external_marker_after_confirmation)
    with pytest.raises(ReloadError):
        c.runtime.reconcile()
    assert c.q.fenced and c.s.catalog_fenced and c.store.catalog_pending()
    assert c.q.marker.read_bytes() == b'foreign transaction receipt'
    assert request(c.address, 'POST', '/v1/place', {'model':'new','util':.2})[0] == 503
