# Generated-By: Codex / gpt-6-astra
"""Deterministic queue -> restart -> inspection -> explicit-proof recovery chains."""
from dataclasses import replace
import hashlib
import json
import os
import threading

import pytest

from llmsvc.registry import ModelRegistry
from llmsvc.reload import RecoveryProof, ReloadError, ReloadQueue
from llmsvc.reload_witness import (BindingObservation, CandidateBinding, InstanceIdentity,
                                  NativeGenerationReader)
from test_reload import harness as harness, make_quiet
from test_reload_witness import fake_http as fake_http, GENERATION

INSTANCE = InstanceIdentity(123, '456')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def reconstruct(queue, clock, logs):
    return ReloadQueue(queue.path, action_lock=queue.action_lock, quiet=queue.quiet,
                       snapshot=queue.snapshot, validate=lambda _: pytest.fail('inspection validated'),
                       notify_reload=lambda **kw: pytest.fail('inspection notified'), log=logs.append,
                       clock=clock, wall_clock=clock)


def submit_failure(harness, binding=None):
    queue, quiet, clock, calls, logs = harness
    queue.notify_reload = lambda **kw: (_ for _ in ()).throw(RuntimeError('settlement unknown'))
    job = queue.enqueue(lambda data: data + b'# candidate\n', description={'kind': 'add_model', 'model': 'candidate'},
                        witness_binding=binding)
    make_quiet(quiet, clock)
    assert queue.process_once()['status'] == 'reconciliation_required'
    return job, reconstruct(queue, clock, logs)


def native_inspection(harness, fake_http):
    queue, _, clock, _, logs = harness
    reader = NativeGenerationReader(fake_http['url'], request_timeout=5, clock=clock)
    binding = CandidateBinding(reader.endpoint, GENERATION, INSTANCE, digest(queue.path.read_bytes() + b'# candidate\n'))
    job, restarted = submit_failure(harness, binding)
    before = BindingObservation(clock(), INSTANCE, digest(queue.path.read_bytes()))
    reading = reader.read(deadline=clock() + 5)
    after = BindingObservation(clock(), INSTANCE, digest(queue.path.read_bytes()))
    assert reading.generation == GENERATION and reading.error is None
    return job, restarted, binding, before, reading, after


def full_fixture_proof(queue, instance=None):
    # Explicit independent fixture attestations. Native reading alone never
    # constructs this proof or supplies settlement/cleanup facts.
    return RecoveryProof(digest(queue.marker.read_bytes()), generation_confirmed=True,
                         instance_confirmed=True, settlement_confirmed=True,
                         cleanup_confirmed=True, instance=instance)


def test_queue_snapshot_projects_block_and_timeout_without_mutating(harness):
    queue, _, clock, calls, logs = harness
    description = {'kind': 'add_model', 'details': {'name': 'candidate'}}
    job = queue.enqueue(lambda data: data + b'# candidate\n', description=description)
    description['details']['name'] = 'external change'
    before = queue.path.read_bytes(), list(calls), list(logs)
    first = queue.queue_snapshot()
    assert first['jobs'][0]['status'] == 'blocked'
    assert first['jobs'][0]['recorded_status'] == 'queued'
    assert first['jobs'][0]['blocked_by'] == [{'reason': 'inflight_stream_unknown'}]
    assert first['pending_ids'] == [job['id']] and not first['fenced']
    first['jobs'][0]['description']['details']['name'] = 'tampered snapshot'
    assert queue.get(job['id'])['description']['details']['name'] == 'candidate'
    clock.advance(600)
    timed = queue.queue_snapshot()
    assert timed['jobs'][0]['status'] == 'timed_out' and timed['pending_ids'] == []
    assert queue.get(job['id'])['status'] == 'queued'  # A read does not consume jobs.
    assert (queue.path.read_bytes(), calls, logs) == before
    assert queue.process_once()['status'] == 'timed_out'
    assert queue.queue_snapshot()['jobs'][0]['recorded_status'] == 'timed_out'


def test_restored_marker_visible_without_resurrecting_other_jobs(harness):
    queue, _, clock, _, logs = harness
    job, restarted = submit_failure(harness)
    before = queue.path.read_bytes(), queue.marker.read_bytes(), list(logs)
    state = restarted.queue_snapshot()
    assert state['fenced'] and state['pending_ids'] == []
    assert state['jobs'][0]['id'] == job['id']
    assert state['jobs'][0]['status'] == 'reconciliation_required'
    assert state['jobs'][0]['source'] == 'recovery_marker'
    assert state['jobs'][0]['config_committed'] is None
    assert state['recovery']['candidate_file_matches'] is True
    assert not state['recovery']['candidate_generation_visible']
    assert {'reason': 'native_binding_not_persisted'} in state['recovery']['blocked_by']
    assert (queue.path.read_bytes(), queue.marker.read_bytes(), logs) == before
    assert restarted.process_once() is None
    with pytest.raises(ReloadError, match='reconciliation'):
        restarted.enqueue(lambda data: data, description={})
    assert restarted.inspect_recovery()['fenced']


def test_native_visibility_after_restart_never_clears_settlement(harness, fake_http):
    queue, _, clock, _, logs = harness
    job, restarted, binding, before, reading, after = native_inspection(harness, fake_http)
    old = queue.path.read_bytes(), queue.marker.read_bytes(), list(logs)
    state = restarted.inspect_recovery(reading=reading, before=before, after=after)
    assert state['marker_valid'] and state['candidate_file_matches']
    assert state['candidate_generation_visible'] and state['settlement_confirmed'] is None
    assert state['status'] == 'reconciliation_required' and state['fenced']
    assert state['blocked_by'] == [{'reason': 'independent_old_server_settlement_unavailable'}]
    assert len(fake_http['requests']) == 1
    assert (queue.path.read_bytes(), queue.marker.read_bytes(), logs) == old
    with pytest.raises(ReloadError, match='not confirmed'):
        restarted.reconcile(lambda _: state)  # Truthy diagnostic is not proof.
    assert queue.marker.exists()
    with pytest.raises(ReloadError, match='not confirmed'):
        restarted.reconcile(lambda _: True)  # A bare visibility bool is insufficient.
    assert queue.marker.exists()
    assert restarted.reconcile(lambda _: full_fixture_proof(queue, INSTANCE)) == {'status': 'reconciled'}
    assert not queue.marker.exists() and not restarted.queue_snapshot()['fenced']
    assert queue.path.read_bytes() == old[0]


@pytest.mark.parametrize('fault,reason', [('identity','service_identity_changed'),
                                        ('digest','candidate_file_digest_changed'),
                                        ('deadline','deadline_expired')])
def test_invalid_binding_read_stays_fenced(harness, fake_http, fault, reason):
    queue, _, clock, _, _ = harness
    _, restarted, _, before, reading, after = native_inspection(harness, fake_http)
    marker = queue.marker.read_bytes()
    if fault == 'identity':
        after = replace(after, instance=InstanceIdentity(123, '999'))
    elif fault == 'digest':
        queue.path.write_bytes(b'# someone else changed candidate\n')
    else:
        clock.advance(6)
    result = restarted.inspect_recovery(reading=reading, before=before, after=after)
    assert not result['candidate_generation_visible'] and result['fenced']
    assert reason in [row['reason'] for row in result['blocked_by']]
    assert queue.marker.read_bytes() == marker


@pytest.mark.parametrize('fault', ['json','duplicate','oversize','version','digest','job','identity','binding_digest','symlink','hardlink','fifo'])
def test_bad_marker_rejected_readonly_and_cannot_reconcile(harness, tmp_path, fault):
    queue, _, _, _, _ = harness
    _, restarted = submit_failure(harness)
    record = json.loads(queue.marker.read_bytes())
    if fault == 'json': queue.marker.write_bytes(b'{')
    elif fault == 'duplicate': queue.marker.write_text('{"sha256":"x",' + json.dumps(record)[1:])
    elif fault == 'oversize': queue.marker.write_bytes(b' ' * 65537)
    elif fault == 'version': record['schema_version'] = 2
    elif fault == 'digest': record['sha256'] = 'not-a-digest'
    elif fault == 'job': record['job']['id'] = '../unsafe'
    elif fault in ('identity','binding_digest'):
        binding = CandidateBinding('http://127.0.0.1:1/api/mcp', GENERATION, INSTANCE, record['sha256']).to_dict()
        if fault == 'identity': binding['instance']['pid'] = False
        else: binding['candidate_sha256'] = 'a' * 64
        record['witness_binding'] = binding
    elif fault == 'symlink':
        original = tmp_path / 'marker-source'
        queue.marker.rename(original)
        queue.marker.symlink_to(original)
    elif fault == 'hardlink': os.link(queue.marker, tmp_path / 'second-link')
    elif fault == 'fifo':
        queue.marker.unlink()
        os.mkfifo(queue.marker)
    if fault in ('version','digest','job','identity','binding_digest'):
        queue.marker.write_text(json.dumps(record))
    result = restarted.inspect_recovery()
    assert result['fenced'] and not result['marker_valid']
    assert result['status'] == 'reconciliation_required'
    with pytest.raises(ReloadError):
        restarted.reconcile(lambda _: pytest.fail('invalid marker reached verifier'))
    assert queue.marker.exists() or queue.marker.is_symlink()


@pytest.mark.parametrize('field', ['generation_confirmed','instance_confirmed','settlement_confirmed','cleanup_confirmed'])
def test_partial_proof_cannot_clear_marker(harness, field):
    queue, _, _, _, _ = harness
    _, restarted = submit_failure(harness)
    proof = replace(full_fixture_proof(queue), **{field: False})
    with pytest.raises(ReloadError, match='not confirmed'):
        restarted.reconcile(lambda _: proof)
    assert queue.marker.exists()


def test_wrong_proof_marker_or_instance_rejected(harness, fake_http):
    queue, _, _, _, _ = harness
    _, restarted, _, _, _, _ = native_inspection(harness, fake_http)
    proof = full_fixture_proof(queue, INSTANCE)
    with pytest.raises(ReloadError, match='not confirmed'):
        restarted.reconcile(lambda _: replace(proof, marker_sha256='b' * 64))
    with pytest.raises(ReloadError, match='instance'):
        restarted.reconcile(lambda _: replace(proof, instance=InstanceIdentity(456, '789')))
    assert queue.marker.exists()


@pytest.mark.parametrize('what', ['marker','config'])
def test_reconcile_rechecks_after_verifier(harness, what):
    queue, _, _, _, _ = harness
    _, restarted = submit_failure(harness)
    proof = full_fixture_proof(queue)
    def confirm(record):
        if what == 'marker':
            record['job']['description']['other'] = 'changed'
            queue.marker.write_text(json.dumps(record))
        else:
            queue.path.write_bytes(b'changed')
        return proof
    with pytest.raises(ReloadError, match='changed'):
        restarted.reconcile(confirm)
    assert queue.marker.exists()


def test_reconcile_dryrun_does_not_read_marker_or_invoke_proof(harness):
    queue, _, _, _, _ = harness
    queue.marker.write_bytes(b'malformed')
    assert queue.reconcile(lambda _: pytest.fail('dryrun verified'), dry_run=True)['would']
    assert queue.marker.read_bytes() == b'malformed'


def test_legacy_marker_supported_as_unproven_recovery(harness):
    queue, _, _, _, _ = harness
    _, restarted = submit_failure(harness)
    record = json.loads(queue.marker.read_bytes())
    del record['schema_version']
    queue.marker.write_text(json.dumps(record))
    assert restarted.inspect_recovery()['marker_valid']
    assert restarted.inspect_recovery()['settlement_confirmed'] is None


def test_binding_mismatch_rejected_before_staging(harness):
    queue, _, _, calls, _ = harness
    binding = CandidateBinding('http://127.0.0.1:1/api/mcp', GENERATION, INSTANCE, '0' * 64)
    with pytest.raises(ReloadError, match='digest'):
        queue.enqueue(lambda data: data + b'# candidate\n', description={}, witness_binding=binding)
    assert calls == [] and queue.queue_snapshot()['jobs'] == []


def test_registry_exposes_same_public_diagnostics(harness):
    queue, _, _, _, _ = harness
    registry = ModelRegistry(queue, shared_roots=(), daemon_port_range=(8000, 8100))
    assert registry.queue_snapshot() == queue.queue_snapshot()
    assert registry.inspect_recovery() == queue.inspect_recovery()


def test_snapshot_reports_precheck_failure_without_leaking_message(harness):
    queue, _, _, _, _ = harness
    queue.enqueue(lambda data: data, description={}, precheck=lambda: (_ for _ in ()).throw(RuntimeError('private details')))
    result = queue.queue_snapshot()
    assert result['jobs'][0]['status'] == 'blocked'
    assert result['jobs'][0]['blocked_by'] == [{'reason': 'inspection_unavailable'}]
    assert 'private details' not in str(result)


def test_oversize_generated_marker_fails_before_config_replacement(harness):
    queue, quiet, clock, _, _ = harness
    original = queue.path.read_bytes()
    queue.enqueue(lambda data: data + b'# candidate\n', description={'large': 'x' * 65536})
    make_quiet(quiet, clock)
    assert queue.process_once()['status'] == 'failed'
    assert queue.path.read_bytes() == original and not queue.marker.exists()


def test_binding_roundtrip_and_invalid_shapes_are_readonly():
    binding = CandidateBinding('http://127.0.0.1:1/api/mcp', GENERATION, INSTANCE, 'a' * 64)
    record = binding.to_dict()
    assert CandidateBinding.from_dict(record) == binding
    record['instance']['pid'] = False
    with pytest.raises(ValueError):
        CandidateBinding.from_dict(record)
    assert binding.instance.pid == 123


def test_binding_rebase_failure_does_not_commit_wrong_candidate(harness):
    queue, quiet, clock, _, _ = harness
    original = queue.path.read_bytes()
    binding = CandidateBinding('http://127.0.0.1:1/api/mcp', GENERATION, INSTANCE, digest(original + b'# candidate\n'))
    queue.enqueue(lambda data: data + b'# candidate\n', description={}, witness_binding=binding)
    queue.path.write_bytes(original + b'# independent edit\n')
    make_quiet(quiet, clock)
    assert queue.process_once()['status'] == 'failed'
    assert queue.path.read_bytes() == original + b'# independent edit\n'
    assert not queue.marker.exists()
