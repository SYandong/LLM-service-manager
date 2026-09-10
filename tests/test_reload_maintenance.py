# Generated-By: Codex / gpt-6-astra
"""Pure transition proof validation and temporary queue hook state transitions."""
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from llmsvc.reload import InstanceTransitionProof, RecoveryProof, ReloadError, ReloadQueue
from llmsvc.reload_witness import CandidateBinding, InstanceIdentity
from llmsvc.state import Pin
from test_reload import harness, make_quiet

OLD = InstanceIdentity(123, '456')
NEW = InstanceIdentity(123, '789')  # PID reuse is a distinct start identity.


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def setup(harness):
    queue, quiet, clock, calls, logs = harness
    original = queue.path.read_bytes()
    candidate = original+b'# candidate\n'
    descriptor = {'mode':'maintenance', 'transaction_id':'a'*32,
                  'base_sha256':digest(original), 'old_scope_sha256':'b'*64}
    binding = CandidateBinding('http://127.0.0.1:1/api/mcp', 'gen_'+'1'*32, OLD, digest(candidate))
    trace = []
    def before(job, raw, *, deadline):
        assert job.external_effects_started and queue.fenced
        assert queue.path.read_bytes() == original and queue.marker.read_bytes() == raw
        trace.append(('before', deadline))
    def after(job, raw, *, deadline):
        assert job.config_committed and queue.path.read_bytes() == candidate
        assert queue.marker.read_bytes() == raw
        trace.append(('after', deadline))
    adapter = SimpleNamespace(blockers=lambda job:[], before_replace=before, after_replace=after)
    queue.maintenance_adapter = adapter
    return queue, descriptor, binding, original, candidate, trace, adapter


def enqueue(context, **kwargs):
    queue, descriptor, binding, _, candidate, *_ = context
    return queue.enqueue(lambda raw:candidate, description={'kind':'fixture_transition'},
                         maintenance=descriptor, witness_binding=binding, **kwargs)


def proof(raw, current=None, **changes):
    record = ReloadQueue._parse_marker(raw)
    p = InstanceTransitionProof(digest(raw), OLD, NEW, 'b'*64, 'c'*64,
        record['sha256'] if current is None else current, True, True, True, True, True, True)
    return replace(p, **changes)


def failed_receipt(harness, *, committed=True):
    context = setup(harness)
    queue, _, _, original, candidate, trace, adapter = context
    def fail(*args, **kwargs): raise OSError('fixture external phase failed')
    if committed: adapter.after_replace = fail
    else: adapter.before_replace = fail
    result = enqueue(context)
    result = queue.process_once()
    assert result['status'] == 'reconciliation_required'
    return context, queue.marker.read_bytes()


def test_explicit_hooks_replace_only_quiet_and_notifier(harness):
    context = setup(harness)
    queue, _, _, _, candidate, trace, _ = context
    prechecks = []
    enqueue(context, precheck=lambda:prechecks.append(True) or [],
            after_apply=lambda **kw:trace.append(('cleanup', kw['deadline'])))
    assert queue.quiet.blockers()  # No synthetic quiet sample is installed.
    assert queue.process_once()['status'] == 'applied'
    assert [phase for phase, _ in trace] == ['before', 'after', 'cleanup']
    assert len({deadline for _, deadline in trace}) == 1 and len(prechecks) >= 3
    assert queue.path.read_bytes() == candidate and not queue.fenced
    assert 'reload' not in [kind for kind, _ in harness[3]]


@pytest.mark.parametrize('gate', ['pin','ram','precheck'])
def test_maintenance_retains_normal_protection_and_admission(harness, gate):
    context = setup(harness);queue, _, _, original, _, trace, _ = context
    snapshot = queue.snapshot
    if gate == 'pin': queue.snapshot = lambda:replace(snapshot(), pins=(Pin('base', 20000, 'fixture'),))
    elif gate == 'ram': queue.snapshot = lambda:replace(snapshot(), memory=replace(snapshot().memory, budget_gb=1))
    enqueue(context, precheck=(lambda:[{'reason':'fixture_precheck'}]) if gate == 'precheck' else None)
    result = queue.process_once()
    assert result['status'] == 'queued' and result['blocked_by']
    assert trace == [] and queue.path.read_bytes() == original and not queue.marker.exists()


@pytest.mark.parametrize('bad', [None, {}, {'mode':'hot_reload'}, {'mode':'maintenance','transaction_id':'bad','base_sha256':'a'*64,'old_scope_sha256':'b'*64}])
def test_bad_descriptor_or_missing_adapter_never_falls_back(harness, bad):
    context = setup(harness);queue, _, binding, original, candidate, _, _ = context
    if bad is None:
        queue.maintenance_adapter = None
        bad = context[1]
    with pytest.raises(ReloadError):
        queue.enqueue(lambda _:candidate, description={}, maintenance=bad, witness_binding=binding)
    assert queue.path.read_bytes() == original and not queue._jobs


@pytest.mark.parametrize('fault', ['before','replace','after','deadline_before','descriptor','source','missing_marker'])
def test_external_effects_before_replace_preserve_receipt_and_fence(harness, monkeypatch, fault):
    context = setup(harness);queue, _, _, original, candidate, trace, adapter = context
    before = adapter.before_replace
    def altered(job, raw, *, deadline):
        before(job, raw, deadline=deadline)
        if fault == 'before': raise OSError('fixture stop outcome unknown')
        if fault == 'deadline_before': harness[2].advance(1000)
        if fault == 'descriptor': job.maintenance = None
        if fault == 'source': queue.path.write_bytes(original+b'# external\n')
        if fault == 'missing_marker': queue.marker.unlink()
    adapter.before_replace = altered
    if fault == 'after': adapter.after_replace = lambda *a, **kw:(_ for _ in ()).throw(OSError('fixture start failed'))
    if fault == 'replace': monkeypatch.setattr('llmsvc.reload.os.replace', lambda *a:(_ for _ in ()).throw(OSError('fixture replace failed')))
    enqueue(context)
    result = queue.process_once()
    assert result['status'] == 'reconciliation_required' and queue.fenced
    assert queue._jobs[result['id']].external_effects_started
    assert result['external_effects_started'] is True
    assert result['config_committed'] is (fault == 'after')
    if fault != 'missing_marker':
        raw = queue.marker.read_bytes()
        assert ReloadQueue._parse_marker(raw)['maintenance']['mode'] == 'maintenance'
    else:
        assert queue.inspect_recovery()['fenced']
    assert queue.process_once() is None
    with pytest.raises(ReloadError): enqueue(context)
    assert 'reload' not in [kind for kind, _ in harness[3]]


def test_preflight_failure_has_no_external_effects_and_dryrun_has_no_adapter_calls(harness):
    context = setup(harness);queue, _, _, original, _, trace, adapter = context
    adapter.blockers = lambda job:pytest.fail('dryrun preflight')
    before = list(harness[3])
    assert enqueue(context, dry_run=True)['would']
    assert trace == [] and harness[3] == before and not queue._jobs
    adapter.blockers = lambda job:(_ for _ in ()).throw(OSError('preflight unavailable'))
    enqueue(context)
    result = queue.process_once()
    assert result['status'] == 'queued' and result['external_effects_started'] is False
    assert result['blocked_by'] == [{'reason':'maintenance_preflight_unavailable'}]
    assert queue.path.read_bytes() == original and not queue.marker.exists()


def test_ordinary_job_never_calls_registered_maintenance_adapter(harness):
    queue, quiet, clock, calls, _ = harness
    def forbidden(*a, **kw): pytest.fail('ordinary job entered maintenance adapter')
    queue.maintenance_adapter = SimpleNamespace(blockers=forbidden, before_replace=forbidden, after_replace=forbidden)
    queue.enqueue(lambda data:data+b'# ordinary\n', description={})
    make_quiet(quiet, clock)
    result = queue.process_once()
    assert result['status'] == 'applied' and 'external_effects_started' not in result
    assert [kind for kind, _ in calls].count('reload') == 1


@pytest.mark.parametrize('field', ['generation_confirmed','instance_confirmed','settlement_confirmed','cleanup_confirmed','backends_confirmed','exclusion_confirmed'])
@pytest.mark.parametrize('value', [False, 1, 'true', None])
def test_transition_requires_literal_positive_evidence(harness, field, value):
    context, raw = failed_receipt(harness);queue = context[0]
    with pytest.raises(ReloadError): queue.confirm_retired_receipt(raw, lambda _:proof(raw, **{field:value}))
    assert queue.fenced and queue.marker.read_bytes() == raw


@pytest.mark.parametrize('change', [
    {'marker_sha256':'0'*64}, {'old_instance':InstanceIdentity(124,'456')},
    {'old_instance':InstanceIdentity(True,'456')}, {'new_instance':OLD}, {'new_instance':InstanceIdentity(123,'0456')},
    {'new_instance':InstanceIdentity(True,'789')}, {'new_instance':InstanceIdentity(123,'')},
    {'old_scope_sha256':'d'*64}, {'new_scope_sha256':None}, {'current_sha256':'d'*64},
    {'restored_base':1}, {'attempt_settled':True},
])
def test_wrong_identity_scope_digest_or_mode_cannot_retire(harness, change):
    context, raw = failed_receipt(harness);queue = context[0]
    with pytest.raises(ReloadError): queue.confirm_retired_receipt(raw, lambda _:proof(raw, **change))
    assert queue.fenced


@pytest.mark.parametrize('missing', [False, True])
@pytest.mark.parametrize('rollback', [False, True])
def test_transition_and_rollback_retire_only_matching_current_bytes(harness, missing, rollback):
    context, raw = failed_receipt(harness, committed=not rollback)
    queue, descriptor, _, original, candidate, _, _ = context
    if missing: queue.marker.unlink()
    current = digest(original if rollback else candidate)
    changes = dict(restored_base=True, attempt_settled=True) if rollback else {}
    before = queue.path.read_bytes(), list(harness[3])
    assert queue.confirm_retired_receipt(raw, lambda _:proof(raw, current, **changes)) == {'status':'reconciled'}
    assert not queue.fenced and not queue.marker.exists()
    assert before == (queue.path.read_bytes(), harness[3])


def test_rollback_needs_attempt_settlement_and_cannot_accept_candidate_as_base(harness):
    context, raw = failed_receipt(harness, committed=False);queue, descriptor, *_ = context
    with pytest.raises(ReloadError):
        queue.confirm_retired_receipt(raw, lambda _:proof(raw, descriptor['base_sha256'], restored_base=True))
    assert queue.fenced
    with pytest.raises(ReloadError):
        queue.confirm_retired_receipt(raw, lambda _:proof(raw, restored_base=True, attempt_settled=True))
    assert queue.fenced


def test_proof_kinds_cannot_impersonate_each_other(harness):
    context, raw = failed_receipt(harness);queue = context[0]
    ordinary = RecoveryProof(digest(raw), True, True, True, True, OLD)
    with pytest.raises(ReloadError): queue.confirm_retired_receipt(raw, lambda _:ordinary)
    record = json.loads(raw);record.pop('maintenance');record['schema_version'] = 1
    record['job'].pop('external_effects_started')
    ordinary_raw = json.dumps(record).encode()
    with pytest.raises(ReloadError): ReloadQueue._confirm_proof(record, ordinary_raw, proof(raw))
    assert queue.fenced


def test_malformed_marker_modes_fail_before_verifier(harness):
    _, raw = failed_receipt(harness)
    for change in ('version','descriptor','binding','extra'):
        record = json.loads(raw)
        if change == 'version': record['schema_version'] = 1
        elif change == 'descriptor': record['maintenance']['mode'] = 'automatic'
        elif change == 'binding': record.pop('witness_binding')
        else: record['maintenance']['force'] = True
        with pytest.raises(ReloadError): ReloadQueue._parse_marker(json.dumps(record).encode())


def test_reconstructed_external_effect_status_is_unknown_not_false(harness):
    context, raw = failed_receipt(harness, committed=False)
    queue = context[0]
    saved = ReloadQueue._parse_marker(raw)
    assert saved['job']['external_effects_started'] is False  # Intent written before actuation.
    restarted = ReloadQueue(queue.path, action_lock=queue.action_lock, quiet=queue.quiet,
        snapshot=queue.snapshot, validate=queue.validate, notify_reload=queue.notify_reload,
        log=queue.log, clock=queue.clock, wall_clock=queue.wall_clock)
    snapshot = restarted.queue_snapshot()
    assert snapshot['fenced'] and snapshot['recovery']['external_effects_started'] is None
    assert snapshot['jobs'][0]['config_committed'] is None
    assert snapshot['jobs'][0]['external_effects_started'] is None
    assert restarted.process_once() is None


def test_after_replace_deadline_failure_keeps_external_fence(harness):
    context = setup(harness);queue, _, _, _, _, _, adapter = context
    after = adapter.after_replace
    def overrun(*args, **kwargs):
        after(*args, **kwargs)
        harness[2].advance(1000)
    adapter.after_replace = overrun
    enqueue(context)
    result = queue.process_once()
    assert result['status'] == 'reconciliation_required' and result['config_committed']
    assert result['external_effects_started'] and queue.fenced


def test_maintenance_source_hash_is_revalidated_before_external_effects(harness):
    context = setup(harness);queue, descriptor, binding, original, candidate, trace, _ = context
    with pytest.raises(ReloadError, match='base'):
        queue.enqueue(lambda _:candidate, description={}, maintenance={**descriptor,'base_sha256':'d'*64}, witness_binding=binding)
    enqueue(context)
    queue.path.write_bytes(original+b'# outside change\n')
    result = queue.process_once()
    assert result['status'] == 'failed' and not result['external_effects_started']
    assert trace == [] and not queue.marker.exists()


def test_failed_preflight_cannot_erase_explicit_mode_and_enable_fallback(harness):
    context = setup(harness);queue, _, _, original, _, trace, adapter = context
    def invalid_preflight(job):
        job.maintenance = None
        raise OSError('fixture adapter error after forbidden mutation')
    adapter.blockers = invalid_preflight
    enqueue(context)
    make_quiet(harness[1], harness[2])
    for _ in range(2):
        result = queue.process_once()
        assert result['status'] == 'queued'
        assert result['blocked_by'] == [{'reason':'maintenance_preflight_unavailable'}]
        assert queue._pending[0].maintenance['mode'] == 'maintenance'
    assert trace == [] and queue.path.read_bytes() == original
    assert 'reload' not in [kind for kind, _ in harness[3]]


def test_missing_binding_after_enqueue_stops_before_external_hook(harness):
    context = setup(harness);queue, _, _, original, _, trace, _ = context
    enqueue(context)
    queue._pending[0].witness_binding = None
    result = queue.process_once()
    assert result['status'] == 'failed' and not result['external_effects_started']
    assert trace == [] and queue.path.read_bytes() == original and not queue.marker.exists()
