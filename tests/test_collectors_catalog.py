# Generated-By: Codex / gpt-6-astra
"""Owner adapter evidence for core's guarded runtime-catalog lifecycle (#157)."""
from concurrent.futures import ThreadPoolExecutor
import threading

from llmsvc.collectors import Collector
from llmsvc.collectors.relay import DataPlaneEventBuffer, DataPlaneEventRelay
from test_collectors import FakeProbes


def make_collector(probes, url='http://old.invalid', weights=10):
    return Collector({'m': {'daemon_url': url, 'weights_gb': weights}},
                     swap_url='http://swap.invalid', probes=probes)


def assert_retired(snapshot):
    assert snapshot.sampled_at is None
    assert snapshot.models == snapshot.activity == snapshot.gpus == ()
    assert snapshot.errors == ('collector: closed',)


def test_close_before_first_round_is_idempotent_and_never_probes():
    class NeverProbe:
        def __getattr__(self, name):
            raise AssertionError('retired collector must not even resolve probes')

    collector = make_collector(NeverProbe())
    collector.close()
    collector.close()
    assert_retired(collector())


def test_retire_first_phase_never_submits_daemon_probes():
    entered, release = threading.Event(), threading.Event()
    calls = []

    class HeldUnits(FakeProbes):
        def units(self):
            entered.set()
            assert release.wait(2)
            return super().units()

        def health(self, url):
            calls.append(('health', url))
            return True

        def sleeping(self, url):
            calls.append(('sleeping', url))
            return False

    collector = make_collector(HeldUnits())
    with ThreadPoolExecutor(max_workers=1) as runner:
        future = runner.submit(collector)
        try:
            assert entered.wait(2)
            collector.close()  # Must return while a probe is held.
            release.set()
            assert_retired(future.result(timeout=2))
            assert calls == []
        finally:
            release.set()
            collector.close()


def test_executor_shutdown_between_retirement_check_and_submit_is_unknown(monkeypatch):
    entered, resume = threading.Event(), threading.Event()
    collector = make_collector(FakeProbes())
    original = collector.pool.submit

    def racing_submit(*args, **kwargs):
        entered.set()
        assert resume.wait(2)
        return original(*args, **kwargs)  # Executor has now been shut down.

    monkeypatch.setattr(collector.pool, 'submit', racing_submit)
    with ThreadPoolExecutor(max_workers=1) as runner:
        future = runner.submit(collector)
        try:
            assert entered.wait(2)
            collector.close()
            resume.set()
            assert_retired(future.result(timeout=2))
        finally:
            resume.set()
            collector.close()


def test_new_generation_collects_while_old_endpoint_finishes_late():
    entered, release = threading.Event(), threading.Event()
    old_urls, new_urls = [], []

    class HeldHealth(FakeProbes):
        def health(self, url):
            old_urls.append(url)
            entered.set()
            assert release.wait(2)
            return True

    class NewProbes(FakeProbes):
        def health(self, url):
            new_urls.append(url)
            return True

    old = make_collector(HeldHealth())
    new = make_collector(NewProbes(), 'http://new.invalid', 25)
    with ThreadPoolExecutor(max_workers=1) as runner:
        previous = runner.submit(old)
        try:
            assert entered.wait(2)
            old.close()
            current = new()
            model, = current.models
            assert model.name == 'm' and model.state == 'awake'
            assert model.weights_gb == 25 and model.is_default is False
            assert new_urls == ['http://new.invalid']
            release.set()
            assert_retired(previous.result(timeout=2))
            assert old_urls == ['http://old.invalid']
            assert_retired(old())
        finally:
            release.set()
            old.close()
            new.close()


def test_constructor_detaches_profile_and_discovery_does_not_create_profile():
    profiles = {'configured': {'daemon_url': 'http://configured.invalid', 'weights_gb': 7}}
    probes = FakeProbes()  # Discovers active m, which is not configured.
    urls = []
    probes.health = lambda url: urls.append(url) or True
    collector = Collector(profiles, swap_url='http://swap.invalid', probes=probes)
    profiles['configured']['weights_gb'] = 999
    profiles['m'] = {'daemon_url': 'http://untrusted.invalid', 'weights_gb': 999, 'is_default': True}
    try:
        models = {m.name: m for m in collector().models}
        assert models['configured'].weights_gb == 7
        assert models['m'].weights_gb is None and models['m'].health_ok is None
        assert models['m'].is_default is False and models['m'].state == 'unknown'
        assert urls == []
    finally:
        collector.close()


def test_replacement_relay_keeps_filter_queue_dedup_and_quiet_evidence_separate():
    names = {'old'}
    old = DataPlaneEventBuffer(names)
    old.observe({'type': 'modelStatus', 'data': [{'id': 'old', 'state': 'ready'}]})
    names.add('new')
    new = DataPlaneEventBuffer({'new'})
    envelope = {'type': 'modelStatus', 'data': [
        {'id': 'old', 'state': 'ready'}, {'id': 'new', 'state': 'ready'},
    ]}
    new.observe(envelope)
    result = new.drain()
    assert [e['model'] for e in result['events']] == ['new']
    assert result['dropped_by_reason'] == {'unlisted_model': 1}
    assert all(e['detail']['trusted_for_quiet'] is False for e in result['events'])
    assert old.model_ids == frozenset({'old'})
    assert [e['model'] for e in old.drain()['events']] == ['old']


def test_prepared_relay_has_no_stream_and_can_be_discarded_without_start():
    def forbidden_stream(*args):
        raise AssertionError('preparing a generation must not start a stream')

    relay = DataPlaneEventRelay('http://swap.invalid', ['m'], stream_factory=forbidden_stream)
    assert relay.subscription._thread is None
    assert relay.drain()['events'] == []
    relay.close()
    relay.close()
    assert relay.subscription._thread is None
    assert relay.subscription.ordered_source is False
