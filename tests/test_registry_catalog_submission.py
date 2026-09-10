# Generated-By: Codex / gpt-6-astra
"""Registry's internal submission seam; actual catalog install is core-owned."""
from dataclasses import replace

import pytest

from llmsvc.registry import ModelRegistry
from llmsvc.reload import ReloadError
from llmsvc.state import Activity, ModelState, Pin
from test_registry_api import registry


def saved(registry):
    api, queue, weights, state, clock, calls, units, drain = registry
    api.add({'name': 'fine', 'path': str(weights), 'base': 'base'})
    assert drain()['status'] == 'applied'
    state[0] = replace(state[0], models=state[0].models + (ModelState('fine', state='stopped'),),
                       activity=state[0].activity + (Activity('fine', last_request_at=clock[0], in_flight=0),))
    calls.clear()
    return api, queue, weights, state, clock, calls, units, drain


def test_preview_does_not_call_catalog_submission_even_when_injected(registry):
    api, queue, weights, _, _, calls, _, _ = saved(registry)
    api.submit_change = lambda *a, **kw: pytest.fail('preview invoked catalog submission')
    before = queue.path.read_bytes(), queue.queue_snapshot()
    assert api.preview_add({'name': 'next', 'path': str(weights), 'base': 'base'})['would']
    assert api.preview_remove('fine')['would']
    assert before == (queue.path.read_bytes(), queue.queue_snapshot()) and calls == []


def test_submission_error_never_falls_back_to_plain_queue(registry, monkeypatch):
    api, queue, weights, *_ = registry
    seen = []
    def blocked(transform, **options):
        seen.append(options)
        raise ReloadError('trusted runtime profile or proof unavailable')
    api.submit_change = blocked
    original = queue.path.read_bytes()
    monkeypatch.setattr(queue, 'enqueue', lambda *a, **kw: pytest.fail('fallback bypassed catalog'))
    with pytest.raises(ReloadError, match='profile or proof'):
        api.add({'name': 'fine', 'path': str(weights), 'base': 'base'})
    assert seen == [{'description': {'kind': 'add_model', 'model': 'fine', 'base': 'base'}, 'dry_run': False}]
    assert queue.path.read_bytes() == original and not queue._jobs


def test_remove_passes_late_protection_and_cleanup_to_existing_queue(registry):
    api, queue, _, state, clock, calls, units, drain = saved(registry)
    forwarded = []
    def submission(transform, **options):
        # Component fixture delegates to the actual existing queue, not a fake
        # success receipt. Core supplies catalog prepare/enqueue in integration.
        forwarded.append(options)
        return queue.enqueue(transform, **options)
    api.submit_change = submission
    job = api.remove('fine')
    assert forwarded[0]['description'] == {'kind': 'remove_model', 'model': 'fine', 'unit': 'vllm-fine.service'}
    assert callable(forwarded[0]['precheck']) and callable(forwarded[0]['after_apply'])
    state[0] = replace(state[0], pins=(Pin('fine', clock[0]+100, 'fixture'),))
    assert drain()['status'] == 'queued'  # A pin arriving after enqueue still blocks.
    assert api.records().get('fine') and calls == []
    state[0] = replace(state[0], pins=())
    assert drain()['status'] == 'applied'
    assert 'fine' not in api.records() and not units and calls == ['reload']
    assert queue.get(job['id'])['status'] == 'applied'


def test_expiry_and_manual_remove_use_same_submission(registry):
    api, queue, _, _, clock, _, _, _ = saved(registry)
    forwarded = []
    def submission(transform, **options):
        forwarded.append(options)
        return queue.enqueue(transform, **options)
    api.submit_change = submission
    clock[0] += 8 * 86400
    jobs = api.expire()
    assert len(jobs) == 1 and len(forwarded) == 1
    assert forwarded[0]['description']['model'] == 'fine'
    assert api.remove('fine') == jobs[0] and len(forwarded) == 1


def test_invalid_submission_callback_is_rejected(registry):
    api, queue, *_ = registry
    with pytest.raises(ValueError, match='submit_change'):
        ModelRegistry(queue, shared_roots=api.shared_roots,
                      daemon_port_range=api.daemon_port_range, submit_change=True)


def test_pending_fence_blocks_injected_submission_before_any_callback(registry):
    api, queue, weights, *_ = registry
    queue.marker.write_text('fixture pending transaction')
    api.submit_change = lambda *a, **kw: pytest.fail('pending fence reached submit callback')
    before = queue.path.read_bytes(), queue.marker.read_bytes()
    with pytest.raises(ReloadError, match='reconciliation'):
        api.add({'name': 'fine', 'path': str(weights), 'base': 'base'})
    assert before == (queue.path.read_bytes(), queue.marker.read_bytes()) and not queue._jobs
