# Generated-By: Codex / gpt-6-astra
"""Offline reload safety and interrupted-transaction regression tests."""
from dataclasses import replace
from pathlib import Path
import threading

import pytest

from llmsvc.reload import CommandValidator, QuietPeriod, ReloadError, ReloadQueue, ValidationError, reload_blockers
from llmsvc.state import Activity, Lease, MemoryState, ModelState, Pin, StateSnapshot


class Clock:
    value = 1000.0

    def __call__(self):
        return self.value

    def advance(self, seconds=1):
        self.value += seconds


def snapshot(clock, **kwargs):
    values = dict(sampled_at=clock(), models=(ModelState('base', state='awake', weights_gb=20),),
                  activity=(Activity('base', in_flight=0),), memory=MemoryState(500, 0), read_only=False)
    values.update(kwargs)
    return StateSnapshot(**values)


@pytest.fixture
def harness(tmp_path):
    clock = Clock()
    quiet = QuietPeriod(clock)
    path = tmp_path / 'config.yaml'
    path.write_bytes(b'models: {}\n')
    path.chmod(0o640)
    calls, logs = [], []
    queue = ReloadQueue(path, action_lock=threading.RLock(), quiet=quiet,
                        snapshot=lambda: snapshot(clock), validate=lambda p: calls.append(('validate', p.read_bytes())),
                        notify_reload=lambda **kwargs: calls.append(('reload', path.read_bytes())), log=logs.append,
                        clock=clock, wall_clock=clock)
    return queue, quiet, clock, calls, logs


def make_quiet(quiet, clock):
    quiet.observe(0)
    for _ in range(5):
        clock.advance()
        quiet.heartbeat()


def add(queue, suffix=b'# added\n', **kwargs):
    return queue.enqueue(lambda value: value + suffix, description={'kind': 'add_model', 'model': 'new'}, **kwargs)


def test_continuous_quiet_not_two_samples():
    clock = Clock()
    quiet = QuietPeriod(clock)
    quiet.observe(0)
    clock.advance(5)
    quiet.observe(0)
    assert quiet.blockers() == [{'reason': 'quiet_period'}]
    for _ in range(5):
        clock.advance()
        quiet.heartbeat()
    assert quiet.blockers() == []
    quiet.observe(1)
    quiet.observe(0)
    assert quiet.blockers()


@pytest.mark.parametrize('count', [None, -1, True, 0.5])
def test_bad_or_disconnected_stream_resets(count):
    clock = Clock()
    quiet = QuietPeriod(clock)
    make_quiet(quiet, clock)
    quiet.observe(count)
    assert quiet.blockers() == [{'reason': 'inflight_stream_unknown'}]
    make_quiet(quiet, clock)
    quiet.observe(0, connected=False)
    assert quiet.blockers()


def test_stale_heartbeat_cannot_hide_gap():
    clock = Clock()
    quiet = QuietPeriod(clock)
    make_quiet(quiet, clock)
    clock.advance(3)
    quiet.heartbeat()
    assert quiet.blockers() == [{'reason': 'inflight_stream_unknown'}]


def test_default_may_sleep_but_awake_pin_blocks():
    clock = Clock()
    state = snapshot(clock, models=(ModelState('base', state='awake', weights_gb=20, is_default=True),))
    assert reload_blockers(state, clock()) == []
    assert reload_blockers(replace(state, pins=(Pin('base', clock()+60, 'test'),)), clock())[0]['reason'] == 'pinned_until'
    state = replace(state, models=(replace(state.models[0], state='sleeping'),), pins=(Pin('base', clock()+60, 'test'),))
    assert reload_blockers(state, clock()) == []


def test_ram_admission_is_for_entire_batch():
    clock = Clock()
    state = snapshot(clock, models=(ModelState('base', state='awake', weights_gb=40),
                                    ModelState('other', state='awake', weights_gb=40)),
                     activity=(Activity('base', in_flight=0), Activity('other', in_flight=0)),
                     memory=MemoryState(210, 0, 200, 150))
    assert reload_blockers(state, clock()) == [{'reason': 'memory_budget', 'awake_weights_gb': 80}]
    assert reload_blockers(replace(state, memory=MemoryState(500, 150, 200, 150)), clock())


@pytest.mark.parametrize('change,reason', [
    ({'read_only': True}, 'read_only'),
    ({'sampled_at': None}, 'state_unknown_or_stale'),
    ({'sampled_at': 1}, 'state_unknown_or_stale'),
    ({'errors': ('missing_probe',)}, 'state_unknown_or_stale'),
    ({'activity': ()}, 'activity_unknown'),
    ({'activity': (Activity('base', in_flight=1),)}, 'in_flight'),
    ({'models': (ModelState('base'),)}, 'state_unknown'),
    ({'models': (ModelState('base', state='awake'),)}, 'weights_unknown'),
    ({'memory': MemoryState()}, 'memory_unknown'),
    ({'memory': MemoryState(float('nan'), 0)}, 'memory_unknown'),
    ({'leases': (Lease('x', 'base', 0, .3, 1200, 30),)}, 'active_lease'),
])
def test_unknown_safety_fails_closed(change, reason):
    clock = Clock()
    assert reason in [item['reason'] for item in reload_blockers(snapshot(clock, **change), clock())]


def test_dry_run_no_files_queue_validation_or_reload(harness):
    queue, quiet, clock, calls, logs = harness
    before = {p: p.read_bytes() for p in queue.path.parent.iterdir()}
    assert add(queue, dry_run=True) == {'would': [{'kind': 'add_model', 'model': 'new'}]}
    assert calls == [] and not queue._pending
    assert before == {p: p.read_bytes() for p in queue.path.parent.iterdir()}
    assert logs[-1]['dry_run'] is True


def test_applies_only_after_quiet_and_preserves_mode(harness):
    queue, quiet, clock, calls, _ = harness
    job = add(queue)
    assert queue.process_once()['status'] == 'queued'
    make_quiet(quiet, clock)
    result = queue.process_once()
    assert result['status'] == 'applied' and result['config_committed']
    assert queue.path.read_bytes().endswith(b'# added\n')
    assert queue.path.stat().st_mode & 0o777 == 0o640
    assert queue.get(job['id']) == result
    assert [call[0] for call in calls] == ['validate', 'validate', 'reload']
    assert list(queue.path.parent.iterdir()) == [queue.path]


def test_bad_validation_never_queues_or_writes(harness):
    queue, _, _, _, _ = harness
    def invalid(path):
        raise ValidationError('invalid')
    queue.validate = invalid
    with pytest.raises(ValidationError):
        add(queue)
    assert queue.path.read_bytes() == b'models: {}\n'
    assert not queue._pending
    assert list(queue.path.parent.iterdir()) == [queue.path]


def test_fifo_rebases_after_failed_head(harness):
    queue, quiet, clock, _, _ = harness
    head = add(queue, b'# first\n')
    tail = add(queue, b'# second\n')
    make_quiet(quiet, clock)
    queue.validate = lambda path: (_ for _ in ()).throw(ValidationError('bad'))
    assert queue.process_once()['status'] == 'failed'
    queue.validate = lambda path: None
    assert queue.process_once()['status'] == 'applied'
    assert b'first' not in queue.path.read_bytes() and b'second' in queue.path.read_bytes()
    assert queue.get(head['id'])['status'] == 'failed'
    assert queue.get(tail['id'])['status'] == 'applied'


def test_timeout_no_forced_apply(harness):
    queue, _, clock, calls, _ = harness
    add(queue)
    clock.advance(600)
    assert queue.process_once()['status'] == 'timed_out'
    assert all(kind != 'reload' for kind, _ in calls)


def test_pin_or_request_arriving_during_validation_blocks(harness):
    queue, quiet, clock, calls, _ = harness
    add(queue)
    make_quiet(quiet, clock)
    queue.validate = lambda path: quiet.observe(1)
    assert queue.process_once()['status'] == 'queued'
    assert queue.path.read_bytes() == b'models: {}\n'
    assert not queue.marker.exists()
    assert all(kind != 'reload' for kind, _ in calls)


def test_wait_returns_action_lock_to_other_operations(harness):
    queue, _, _, _, _ = harness
    add(queue)
    queue.process_once()
    acquired = []
    def other_operation():
        with queue.action_lock:
            acquired.append(True)
    thread = threading.Thread(target=other_operation)
    thread.start()
    thread.join(1)
    assert acquired == [True]


def test_concurrent_submissions_do_not_lose_updates(harness):
    queue, quiet, clock, _, _ = harness
    threads = [threading.Thread(target=add, args=(queue, f'# {i}\n'.encode())) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
    make_quiet(quiet, clock)
    for _ in threads:
        assert queue.process_once()['status'] == 'applied'
    data = queue.path.read_text()
    assert all(data.count(f'# {i}\n') == 1 for i in range(8))


def test_notify_failure_blocks_even_after_restart(harness):
    queue, quiet, clock, _, logs = harness
    add(queue)
    make_quiet(quiet, clock)
    queue.notify_reload = lambda **kwargs: (_ for _ in ()).throw(RuntimeError('unconfirmed'))
    result = queue.process_once()
    assert result['status'] == 'reconciliation_required' and queue.marker.exists()
    restarted = ReloadQueue(queue.path, action_lock=queue.action_lock, quiet=quiet,
                            snapshot=queue.snapshot, validate=queue.validate, notify_reload=lambda **kwargs: None, log=logs.append)
    with pytest.raises(ReloadError, match='reconciliation'):
        add(restarted)
    assert restarted.reconcile(lambda _: True, dry_run=True)['would']
    assert queue.marker.exists()
    with pytest.raises(ReloadError, match='not confirmed'):
        restarted.reconcile(lambda _: False)
    assert restarted.reconcile(lambda _: True) == {'status': 'reconciled'}
    assert not queue.marker.exists()


def test_cleanup_failure_not_retried_as_reload(harness):
    queue, quiet, clock, calls, _ = harness
    add(queue, after_apply=lambda **kwargs: (_ for _ in ()).throw(RuntimeError('unit still present')))
    make_quiet(quiet, clock)
    assert queue.process_once()['status'] == 'reconciliation_required'
    assert queue.process_once() is None
    assert sum(kind == 'reload' for kind, _ in calls) == 1


def test_symlink_config_refused(harness, tmp_path):
    queue, _, _, _, _ = harness
    original = tmp_path / 'original'
    queue.path.rename(original)
    queue.path.symlink_to(original)
    with pytest.raises(OSError):
        add(queue)
    assert original.read_bytes() == b'models: {}\n'


def test_command_validator_argv_and_failure(tmp_path, monkeypatch):
    import subprocess
    seen = []
    def run(argv, **kwargs):
        seen.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 1)
    monkeypatch.setattr(subprocess, 'run', run)
    validator = CommandValidator('/configured/llama-swap')
    with pytest.raises(ValidationError):
        validator(tmp_path / 'staged.yaml')
    assert seen[0][0] == ['/configured/llama-swap', '-config', str(tmp_path / 'staged.yaml'), '-validate']
    assert 'shell' not in seen[0][1]


def test_validate_cannot_mutate_candidate(harness):
    queue, _, _, _, _ = harness
    queue.validate = lambda path: path.write_bytes(b'changed')
    with pytest.raises(ValidationError, match='modified'):
        add(queue)
    assert queue.path.read_bytes() == b'models: {}\n'


def test_external_config_change_during_validation_not_overwritten(harness):
    queue, quiet, clock, _, _ = harness
    add(queue)
    make_quiet(quiet, clock)
    queue.validate = lambda path: queue.path.write_bytes(b'# external edit\n')
    assert queue.process_once()['status'] == 'failed'
    assert queue.path.read_bytes() == b'# external edit\n'


def test_timeout_during_validation_cannot_commit(harness):
    queue, quiet, clock, _, _ = harness
    add(queue)
    make_quiet(quiet, clock)
    def slow_validation(path):
        clock.advance(600)
        make_quiet(quiet, clock)
    queue.validate = slow_validation
    assert queue.process_once()['status'] == 'timed_out'
    assert queue.path.read_bytes() == b'models: {}\n'


def test_replace_failure_keeps_old_config_and_no_marker(harness, monkeypatch):
    import os
    queue, quiet, clock, _, _ = harness
    add(queue)
    make_quiet(quiet, clock)
    monkeypatch.setattr(os, 'replace', lambda *args: (_ for _ in ()).throw(OSError('disk error')))
    assert queue.process_once()['status'] == 'failed'
    assert queue.path.read_bytes() == b'models: {}\n'
    assert list(queue.path.parent.iterdir()) == [queue.path]


def test_second_candidate_contains_first_and_revalidates(harness):
    queue, quiet, clock, calls, _ = harness
    add(queue, b'# first\n')
    add(queue, b'# second\n')
    assert calls[-1][1].endswith(b'# first\n# second\n')
    make_quiet(quiet, clock)
    queue.process_once()
    queue.process_once()
    assert queue.path.read_bytes().endswith(b'# first\n# second\n')


def test_plain_lock_rejected_instead_of_deadlocking(harness):
    queue, quiet, clock, _, logs = harness
    with pytest.raises(ValueError, match='RLock'):
        ReloadQueue(queue.path, action_lock=threading.Lock(), quiet=quiet, snapshot=queue.snapshot,
                    validate=queue.validate, notify_reload=queue.notify_reload, log=logs.append)


def test_adoption_overrun_retains_recovery_marker(harness):
    queue, quiet, clock, _, _ = harness
    add(queue)
    make_quiet(quiet, clock)
    deadlines = []
    def slow_notify(*, deadline):
        deadlines.append(deadline)
        clock.advance(1000)
    queue.notify_reload = slow_notify
    result = queue.process_once()
    assert result['status'] == 'reconciliation_required'
    assert result['config_committed'] and queue.marker.exists()
    assert deadlines == [1015]


def test_cleanup_deadline_is_same_as_adoption(harness):
    queue, quiet, clock, _, _ = harness
    deadlines = []
    queue.notify_reload = lambda *, deadline: deadlines.append(deadline)
    def slow_cleanup(*, deadline):
        deadlines.append(deadline)
        clock.advance(11)
    add(queue, after_apply=slow_cleanup)
    make_quiet(quiet, clock)
    assert queue.process_once()['status'] == 'reconciliation_required'
    assert deadlines == [1015, 1015]
    assert queue.marker.exists()
