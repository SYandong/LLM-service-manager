# Generated-By: Codex / gpt-6-astra
"""Actual core catalog install with owner collectors/relays; no live proof."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
import hashlib
import threading
import sqlite3
import time

import pytest
import yaml

import llmsvc.collectors as collectors
from llmsvc.collectors import Collector
from llmsvc.activity import ActivityReader
from llmsvc.collectors.relay import DataPlaneEventRelay
from llmsvc.scheduler import DataPlaneBridge
from llmsvc.state import GPUState, Lease
from llmsvc.leases import UnitObservation
from test_catalog_lifecycle import catalog, install, prepare
from test_registry_http_preview import request


class FixtureProbes:
    def __init__(self, world, profiles):
        self.world, self.profiles = world, profiles
        self.hold_units = None
        self.bad_units = False
        self.urls = []

    def gpus(self):
        return (GPUState(0, 'fixture-gpu', 100, 0, 100),)

    def processes(self):
        return {}

    def units(self):
        if self.hold_units:
            self.hold_units()
        if self.bad_units:
            return []  # Unexpected adapter error after its old generation retired.
        return {self.profiles[name]['unit']: {
            'model': name, 'unit_active': unit.active, 'gpu': 0,
            'util': self.profiles[name]['util'], 'port': self.profiles[name]['port'],
        } for name, unit in self.world['units'].items()
            if name in self.profiles and unit.exists}

    def memory(self):
        return 500

    def running(self):
        return {name: 'ready' for name, unit in self.world['units'].items() if unit.active}

    def events(self):
        return SimpleNamespace(states=self.running(), count=lambda name: 0)

    def health(self, url):
        self.urls.append(url)
        return True

    def sleeping(self, url):
        return False

    def owner(self, *args):
        return None


@pytest.fixture
def real_catalog(catalog, monkeypatch):
    c = catalog
    # Inject only the collector's wall clock; use real monotonic round bounds.
    monkeypatch.setattr(collectors, 'time', SimpleNamespace(time=c.clock, monotonic=time.monotonic))
    c.real_collectors = []
    activity_path = c.path.parent / 'telemetry-activity.sqlite'
    with sqlite3.connect(activity_path) as db:
        db.execute('CREATE TABLE activity (id INTEGER PRIMARY KEY, ts_created INTEGER, '
                   'model_id TEXT, input_tokens INTEGER, output_tokens INTEGER)')

    def factory(config):
        profiles = config.collectors['models']
        instance = Collector(profiles, swap_url=config.collectors['swap_url'],
                             probes=FixtureProbes(c.world, profiles),
                             activity_reader=ActivityReader(activity_path))
        c.real_collectors.append(instance)
        return instance

    c.collector.close()
    c.collector = factory(c.cfg)
    c.s.collect = c.collector
    c.runtime.collector_factory = factory
    c.s.sample_once()
    try:
        yield c
    finally:
        for instance in c.real_collectors:
            instance.close()


@pytest.mark.parametrize('late_error', [False, True], ids=['retired-unknown', 'old-adapter-error'])
def test_real_installed_epoch_rejects_late_old_owner_snapshot_and_error(real_catalog, late_error):
    c = real_catalog
    entered, release = threading.Event(), threading.Event()

    def hold():
        entered.set()
        assert release.wait(3)

    c.collector.probes.hold_units = hold
    c.collector.probes.bad_units = late_error
    old_epoch = c.s.catalog_epoch
    with ThreadPoolExecutor(max_workers=1) as runner:
        old = runner.submit(c.s.sample_once)
        try:
            assert entered.wait(3)
            result, _ = install(c)  # The real queue, proof checks, checkpoint and pointer install.
            assert result['status'] == 'applied', result
            assert c.s.catalog_epoch != old_epoch and c.collector._closed.is_set()
            current = c.s.sample_once()
            publication = c.s._sample_published
            event_count = len(c.s.events_since(0))
            assert {m.name for m in current.models} == {'base', 'new'}
            release.set()
            assert old.result(timeout=3).models == current.models
            assert c.s.snapshot().models == current.models
            assert c.s._sample_published == publication
            assert len(c.s.events_since(0)) == event_count
            assert 'collector: closed' not in c.s.snapshot().errors
            assert not c.s.catalog_fenced and c.store.catalog_checkpoint()['phase'] == 'released'
        finally:
            release.set()


def test_real_relay_pending_and_overflow_retire_outside_lock_under_durable_gate(real_catalog):
    c = real_catalog
    opened = []

    def no_stream(*args):
        opened.append(args)
        pytest.fail('inactive scheduler fixture must not start a subscription')

    old = DataPlaneEventRelay('http://unused.invalid', ['base'], capacity=1, stream_factory=no_stream)
    bridge = DataPlaneBridge(c.s, old)
    c.s.event_bridge = bridge
    old_epoch = c.s.catalog_epoch
    envelope = lambda state: {'type': 'modelStatus', 'data': [{'id': 'base', 'state': state}]}
    old.buffer.observe(envelope('ready'))
    with ThreadPoolExecutor(max_workers=1) as runner:
        with c.s.action_lock:
            assert runner.submit(bridge.drain_once).result(timeout=2) is False
    assert bridge.pending and len(bridge.pending['events']) == 1
    old.buffer.observe(envelope('stopping'))
    old.buffer.observe(envelope('stopped'))  # Bounded local buffer overflow.
    observed_close = []
    close = old.close

    def checked_close():
        observed_close.append((c.s.action_lock._is_owned(), c.s.catalog_fenced,
                               (c.store.catalog_checkpoint() or {}).get('phase')))
        close()

    old.close = checked_close
    made = []

    def relay_factory(config):
        instance = DataPlaneEventRelay(config.collectors['swap_url'],
                                      tuple(config.collectors['models']), stream_factory=no_stream)
        made.append(instance)
        return instance

    c.runtime.relay_factory = relay_factory
    prepared = prepare(c)
    before = len(c.real_collectors)
    assert c.runtime.enqueue(prepared, dry_run=True)['would']
    assert len(c.real_collectors) == before and made == [] and not observed_close
    result, _ = install(c)
    assert result['status'] == 'applied', result
    assert observed_close == [(False, True, 'published')]
    assert bridge.closed and old.subscription._stop.is_set() and not opened
    discarded = [e for e in c.s.events_since(0) if e.kind == 'catalog_events_discarded']
    assert sum(e.detail['events'] for e in discarded) == 2
    assert sum(e.detail['dropped'] for e in discarded) == 1
    assert all(e.detail['catalog_epoch'] == old_epoch for e in discarded)
    assert not any(e.kind == 'data_plane_state' for e in c.s.events_since(0))
    new, = made
    assert new.buffer.model_ids == frozenset({'base', 'new'})
    new.buffer.observe({'type': 'modelStatus', 'data': [{'id': 'new', 'state': 'starting'}]})
    assert c.s.event_bridge.catalog_epoch == c.s.catalog_epoch
    assert c.s.event_bridge.drain_once()
    event = c.s.events_since(0)[-1]
    assert event.kind == 'data_plane_state' and event.model == 'new'
    assert event.detail['trusted_for_quiet'] is False


def test_real_retained_collector_keeps_old_endpoint_and_budget_without_readmission(real_catalog):
    c = real_catalog
    c.store.create_lease(Lease('old', 'base', 0, .4, 20000, 40), 'vllm-base.service')
    c.store.transition_lease('old', 'confirmed')
    c.world['units']['base'] = UnitObservation(True, False, True, 'old', '2'*32)
    c.s.sample_once()
    c.models = {'new': c.models['new']}
    c.candidate = yaml.safe_dump({'macros': {'llmsvc_reload_generation': 'gen_'+'1'*32},
                                 'models': {'new': {}}}).encode()
    c.binding = replace(c.binding, candidate_sha256=hashlib.sha256(c.candidate).hexdigest())
    result, _ = install(c)
    assert result['status'] == 'applied', result
    observed = {m.name: m for m in c.s.sample_once().models}
    assert observed['base'].weights_gb == 10 and observed['base'].budget_gb == 40
    assert c.s.collect.probes.urls == ['http://127.0.0.1:21000']
    assert c.s.placement.transport.active_models == frozenset({'new'})
    code, body = request(c.address, 'POST', '/v1/place', {'model': 'base', 'util': .4})
    assert code == 404 and body['error'] == 'unknown_model'
    assert c.store.lease('old')[0].status == 'confirmed'
    assert c.store.lease('old')[0].budget_gb == 40


def test_failed_real_relay_retirement_cannot_release_claim_on_reconcile(real_catalog):
    c = real_catalog
    entered, allow_close, released = threading.Event(), threading.Event(), threading.Event()

    class HeldStream:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

        def readline(self, *args):
            entered.set()
            assert released.wait(3)
            return b''

        def close(self):
            if not allow_close.is_set():
                raise RuntimeError('fixture upstream close not yet possible')
            released.set()

    old = DataPlaneEventRelay('http://unused.invalid', ['base'], timeout=.5,
                              stream_factory=lambda *args: HeldStream())
    c.s.event_bridge = DataPlaneBridge(c.s, old)
    old.start()
    try:
        assert entered.wait(2)
        with pytest.raises(RuntimeError, match='close not yet possible'):
            install(c)
        checkpoint = c.store.catalog_checkpoint()
        assert checkpoint['phase'] == 'published' and c.s.catalog_fenced
        assert old.subscription._thread.is_alive()
        # Recover the exact original receipt from the durable checkpoint in this
        # disposable fixture. Existing real queue/reconcile proof checks still run.
        c.q.marker.write_bytes(checkpoint['marker_json'].encode())
        try:
            c.runtime.reconcile()
        except (RuntimeError, OSError):
            pass
        assert old.subscription._thread.is_alive()
        assert c.s.catalog_fenced, 'live old relay lost from retirement tracking; global gate released'
        assert c.store.catalog_checkpoint()['phase'] != 'released'
    finally:
        allow_close.set()
        old.close()
        assert not old.subscription._thread.is_alive()
