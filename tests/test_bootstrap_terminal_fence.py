# Generated-By: Codex / gpt-6-astra
"""Deterministic final-commit races using the actual bootstrap/native fixture."""
from dataclasses import replace

import pytest

from llmsvc.bootstrap import BootstrapError
from test_bootstrap_native_integration import combined, bootstrap_service, site


def invalidate(c, kind):
    if kind == 'stopping':
        c.s.stopping.set()
    elif kind == 'deadline':
        c.c.clock = lambda: c.c.deadline + 1
    elif kind == 'config':
        c.s.config = replace(c.s.config, read_only=True)
    else:
        path = c.native_site.rows['native_config']['target']
        from pathlib import Path
        with Path(path).open('ab') as stream:
            stream.write(b'\n# fixture foreign source mutation\n')


@pytest.mark.parametrize('kind', ['stopping', 'deadline', 'config', 'source'])
def test_complete_retains_fence_after_last_confirmation_changes(combined, monkeypatch, kind):
    c = combined
    confirmed = c.c._confirmed
    injected = []
    def changed_after_confirmation(record):
        result = confirmed(record)
        if record['stage'] == 'activate_submitted' and not injected:
            injected.append(kind)
            invalidate(c, kind)
        return result
    monkeypatch.setattr(c.c, '_confirmed', changed_after_confirmation)
    with pytest.raises(BootstrapError):
        c.c.run()
    assert injected == [kind]
    record = c.store.bootstrap_checkpoint()
    assert record['stage'] == 'activate_submitted' and c.store.bootstrap_pending()
    lease = c.store.lease(record['lease_id'])[0]
    assert lease.status == 'confirmed' and lease.budget_gb == 40
    assert c.world['unit'].poll() is None and c.world['calls'].count('systemd-run') == 1


@pytest.mark.parametrize('kind', ['stopping', 'deadline', 'config', 'source'])
def test_abort_retains_fence_after_last_source_confirmation_changes(combined, monkeypatch, kind):
    c = combined
    save = c.c._save
    def before_start(record, **changes):
        if changes.get('stage') == 'start_submitted':
            raise OSError('fixture interruption before unit launch')
        return save(record, **changes)
    monkeypatch.setattr(c.c, '_save', before_start)
    with pytest.raises(BootstrapError):
        c.c.run()
    monkeypatch.setattr(c.c, '_save', save)
    source = c.c._source_state
    injected = []
    def changed_after_source_read():
        result = source()
        record = c.store.bootstrap_checkpoint()
        if 'rollback' in record['effects'] and not injected:
            injected.append(kind)
            invalidate(c, kind)
        return result
    monkeypatch.setattr(c.c, '_source_state', changed_after_source_read)
    with pytest.raises(BootstrapError):
        c.c.recover('rollback')
    assert injected == [kind]
    record = c.store.bootstrap_checkpoint()
    assert record['stage'] != 'aborted' and c.store.bootstrap_pending()
    # The absent pending unit was already positively reconciled. A failed final
    # fence check must neither restore this released budget nor invent completion.
    assert c.store.lease(record['lease_id'])[0].status == 'released'
    assert c.world['unit'] is None and 'systemd-run' not in c.world['calls']
