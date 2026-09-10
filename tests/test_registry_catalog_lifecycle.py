# Generated-By: Codex / gpt-6-astra
"""Actual ModelRegistry -> CatalogRuntime -> queue/store/place lifecycle fixtures."""
import hashlib

import pytest
import yaml

from llmsvc.registry import ModelRegistry
from llmsvc.reload import ReloadError
from llmsvc.leases import UnitObservation
from llmsvc.state import Pin
from test_catalog_lifecycle import catalog, profile
from test_registry_api import registry
from test_registry_http_preview import request
from test_reload import make_quiet


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def bridge(catalog, registry):
    c = catalog
    _, source, weights, *_ = registry
    document = yaml.safe_load(source.path.read_bytes())
    document['models']['base']['macros']['util'] = '.4'
    raw = yaml.safe_dump(document).encode().replace(b'8101', b'21000')
    c.path.write_bytes(raw)
    # Explicit trusted fixture profiles, not inferred measurements from macros.
    trusted = {'base': profile('base', 21000), 'fine': profile('fine', 21001)}
    submitted = []
    def profiles(candidate):
        names = yaml.safe_load(candidate)['models']
        submitted.append(set(names))
        return {name:trusted[name] for name in names}
    c.runtime.profile_provider = profiles
    c.runtime.instance_provider = lambda **kw: c.binding.instance
    api = ModelRegistry(c.q, shared_roots=(weights.parent,), daemon_port_range=(21000, 21010),
        stop_model=lambda *a, **kw: pytest.fail('stopped fixture must not invoke model stop'),
        unit_absent=lambda name, **kw:not c.world['units'].get(name, UnitObservation(False, True)).active,
        now=c.clock)
    c.runtime.connect_registry(api)
    c.s.registry = api
    return c, api, weights, submitted


def add_model(c, api, weights):
    status, job = request(c.address, 'POST', '/v1/models', {'name':'fine', 'path':str(weights), 'base':'base'})
    assert status == 200, job
    assert job['description'] == {'kind':'add_model', 'model':'fine', 'base':'base'}
    make_quiet(c.q.quiet, c.clock)
    result = c.runtime.process_once()
    assert result['status'] == 'applied'
    c.s.sample_once()
    return result


def test_real_registry_add_installs_catalog_before_existing_place_confirm(bridge):
    c, api, weights, submitted = bridge
    add_model(c, api, weights)
    assert submitted == [{'base', 'fine'}]
    assert api.records()['fine']['daemon_port'] == 21001
    assert c.s.placement.transport.models['fine']['is_default'] is False
    status, placed = request(c.address, 'POST', '/v1/place', {'model':'fine', 'util':.2})
    assert status == 200 and c.store.lease(placed['lease_id'])[0].budget_gb == 40
    c.world['units']['fine'] = UnitObservation(True, False, True, placed['lease_id'], '1'*32)
    c.s.sample_once()
    status, confirmed = request(c.address, 'POST', '/v1/place/'+placed['lease_id']+'/confirm')
    assert status == 200 and confirmed['status'] == 'confirmed'


def test_real_registry_remove_keeps_late_pin_and_then_retires_admission(bridge):
    c, api, weights, _ = bridge
    add_model(c, api, weights)
    assert not any(lease.model == 'fine' for lease, _ in c.store.leases())
    assert c.store.fault('fine') is None and c.store.recovery('fine') is None
    status, job = request(c.address, 'DELETE', '/v1/models/fine')
    assert status == 200, job
    assert job['description']['kind'] == 'remove_model'
    before = c.path.read_bytes(), c.store.catalog_checkpoint()
    c.store.put_pin(Pin('fine', c.clock()+100, 'fixture-owner'))
    make_quiet(c.q.quiet, c.clock)
    result = c.runtime.process_once()
    assert result['status'] == 'queued' and result['blocked_by']
    assert before == (c.path.read_bytes(), c.store.catalog_checkpoint())
    c.store.remove_pin('fine')
    make_quiet(c.q.quiet, c.clock)
    assert c.runtime.process_once()['status'] == 'applied'
    c.s.sample_once()
    assert 'fine' not in api.records()
    # This fixture has no retained lease/fault/recovery reference. Observation
    # retention is optional here; removed names must never regain admission.
    assert 'fine' not in c.s.placement.transport.active_models
    status, body = request(c.address, 'POST', '/v1/place', {'model':'fine', 'util':.2})
    assert status == 404 and body['error'] == 'unknown_model'


def test_real_registry_preview_never_enters_catalog_or_persists_claim(bridge):
    c, api, weights, submitted = bridge
    before = c.path.read_bytes(), c.store.catalog_checkpoint(), len(c.world['collectors']), c.store._db.execute('PRAGMA user_version').fetchone()[0]
    assert api.preview_add({'name':'fine', 'path':str(weights), 'base':'base'})['would']
    assert submitted == [] and c.world['calls'] == [] and not c.q._pending
    assert before == (c.path.read_bytes(), c.store.catalog_checkpoint(), len(c.world['collectors']), c.store._db.execute('PRAGMA user_version').fetchone()[0])


def test_source_change_during_registry_prepare_never_queues_stale_candidate(bridge, monkeypatch):
    c, api, weights, submitted = bridge
    prepare = c.runtime.prepare
    changed = c.path.read_bytes()+b'# external replacement\n'
    def external_change(*args, **kwargs):
        c.path.write_bytes(changed)
        return prepare(*args, **kwargs)
    monkeypatch.setattr(c.runtime, 'prepare', external_change)
    with pytest.raises(ReloadError, match='source changed'):
        api.add({'name':'fine', 'path':str(weights), 'base':'base'})
    assert c.path.read_bytes() == changed and not c.q._pending
    assert c.store.catalog_checkpoint() is None and c.world['calls'] == []
