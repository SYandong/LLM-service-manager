# Generated-By: Codex / gpt-6-astra
"""Real catalog checkpoint failure must not become an unqualified unfenced label."""
import copy
import json

import pytest
from test_catalog_lifecycle import catalog, registry_catalog, registry_fixture, make_quiet
from test_llm_events import api


@pytest.fixture
def published_fence(registry_catalog, monkeypatch):
    c, registry, weights = registry_catalog
    save = c.store.save_catalog

    def fail_release(expected, record, **kwargs):
        if record['phase'] == 'released':
            raise OSError('fixture final checkpoint failure')
        return save(expected, record, **kwargs)

    monkeypatch.setattr(c.store, 'save_catalog', fail_release)
    job = registry.add({'name': 'fine', 'path': str(weights), 'base': 'base'})
    make_quiet(c.q.quiet, c.clock)
    with pytest.raises(OSError, match='final checkpoint'):
        c.runtime.process_once()
    assert c.q.get(job['id'])['status'] == 'applied'
    assert not c.q.fenced and c.s.catalog_fenced and c.store.catalog_pending()
    assert c.store.catalog_checkpoint()['phase'] == 'published'
    return c


def retained_state(c):
    return (c.path.read_bytes(), tuple(c.store._db.iterdump()), copy.deepcopy(c.q.queue_snapshot()),
            list(c.world['calls']), c.s.events_since(0))


def test_actual_http_keeps_global_catalog_blocker_despite_applied_queue(api, published_fence):
    c = published_fence
    before = retained_state(c)
    client = api['SchedulerClient']('http://%s:%s' % c.address)
    args = api['build_parser']().parse_args(['registry'])
    result = api['execute_command'](args, client)
    assert result['queue']['fenced'] is False
    assert result['queue']['jobs'][0]['status'] == 'applied'
    assert 'catalog_reconciliation_required' in [item['reason'] for item in result['blocked_by']]
    text = api['format_result'](args, result)
    assert '\nQueue/config fence: no' in text
    global_line = text.split('\nGlobal blockers: ', 1)[1].split('\n', 1)[0]
    assert 'catalog_reconciliation_required' in global_line
    assert '\nFenced: no' not in text
    assert 'not global action readiness' in text
    # The complete server projection remains available, including every null.
    assert json.loads(text[text.index('{'):]) == result
    args.json = True
    assert json.loads(api['format_result'](args, result)) == result
    assert retained_state(c) == before
    assert c.s.catalog_fenced and c.store.catalog_pending()


def test_absent_reported_blockers_do_not_claim_global_readiness(api, published_fence):
    c = published_fence
    args = api['build_parser']().parse_args(['registry'])
    result = api['execute_command'](args, api['SchedulerClient']('http://%s:%s' % c.address))
    # A separate formatter edge case, not a fabricated server proof of release.
    result['blocked_by'] = []
    text = api['format_result'](args, result)
    assert 'Global blockers: none reported (not a readiness guarantee)' in text
    assert 'not global action readiness' in text
