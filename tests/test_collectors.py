# Generated-By: Codex / gpt-6-astra
import json
import time
from dataclasses import replace
from pathlib import Path

import pytest

from llmsvc.collectors import Collector
from llmsvc.collectors.parsers import (
    parse_gpus, parse_meminfo, parse_processes, parse_running, parse_sleeping, parse_units,
)
from llmsvc.collectors.probes import Probes
from llmsvc.state import GPUProcess, GPUState

FIXTURES = Path(__file__).parent / "fixtures" / "telemetry"


def test_real_gpu_and_absent_unit_fixtures():
    gpus = parse_gpus((FIXTURES / "gpu-real.csv").read_text())
    assert len(gpus) == 4
    assert gpus[1].used_gb == 130437 / 1024
    assert gpus[1].free_gb == 12720 / 1024
    units = parse_units((FIXTURES / "unit-absent-real.txt").read_text())
    assert units['vllm-gemma-4-26b-a4b-nvfp4.service']['unit_active'] is False
    assert parse_running(json.loads((FIXTURES / "running-real.json").read_text())) == {}


def test_synthetic_active_unit_extracts_only_managed_single_gpu():
    text = '''Id=vllm-model.service
ActiveState=active
SubState=running
MainPID=42
ControlGroup=/system.slice/vllm-model.service
Environment=CUDA_VISIBLE_DEVICES=2 "TOKEN=not-relevant" OTHER=x
ExecStart={ path=/opt/vllm; argv[]=/opt/vllm serve /models/m --port 8102 --gpu-memory-utilization=0.5 ; }

Id=vllm-reaper.timer
ActiveState=active
'''
    unit = parse_units(text)['vllm-model.service']
    assert (unit['gpu'], unit['port'], unit['util']) == (2, 8102, .5)
    assert 'TOKEN' not in str(unit)
    assert len(parse_units(text)) == 1
    assert parse_units(text.replace('CUDA_VISIBLE_DEVICES=2', 'CUDA_VISIBLE_DEVICES=0,1'))['vllm-model.service']['gpu'] is None
    assert parse_units(text.replace('--port 8102', '--tensor-parallel-size 2 --port 8102'))['vllm-model.service']['gpu'] is None


def test_probe_unknowns_and_invalid_shapes():
    assert parse_gpus('0, GPU-a, N/A, 1, 2, [Not Supported]')[0].total_gb is None
    with pytest.raises(ValueError):
        parse_gpus('broken')
    with pytest.raises(ValueError):
        parse_processes('GPU-a, nope, 1, process')
    with pytest.raises(ValueError):
        parse_sleeping({'is_sleeping': 'false'})
    with pytest.raises(ValueError):
        parse_running({'running': [{'model': 'm', 'state': 'new-state'}]})
    assert parse_sleeping({'is_sleeping': False}) is False
    assert parse_meminfo('MemAvailable: 1048576 kB\n') == 1


def test_cgroup_identity_required_for_managed_process(tmp_path):
    proc = tmp_path / '42'
    proc.mkdir()
    (proc / 'cgroup').write_text('0::/system.slice/vllm-m.service/worker\n')
    probes = Probes('http://localhost', proc_root=tmp_path)
    units = {'vllm-m.service': {'model': 'm', 'main_pid': 42, 'cgroup': '/system.slice/vllm-m.service'}}
    assert probes.owner(42, units) == 'm'
    (proc / 'cgroup').write_text('0::/system.slice/vllm-m.service-other\n')
    assert probes.owner(42, units) is None
    assert probes.owner(999, units) is None


class FakeEvents:
    states = {'m': 'ready'}

    def count(self, model):
        return 2 if model == 'm' else 0


class FakeProbes:
    def gpus(self):
        return (GPUState(0, 'GPU-a', 100, 50, 50),)

    def processes(self):
        return {'GPU-a': (GPUProcess(10, 20), GPUProcess(20, 5))}

    def units(self):
        return {'vllm-m.service': dict(model='m', unit_active=True, gpu=0, util=.5, port=8101)}

    def memory(self):
        return 500

    def running(self):
        return {'m': 'ready'}

    def events(self):
        return FakeEvents()

    def health(self, url):
        return True

    def sleeping(self, url):
        return False

    def owner(self, pid, units):
        return 'm' if pid == 10 else None


def collector(probes=None, **kwargs):
    return Collector({'m': {'daemon_url': 'http://daemon', 'weights_gb': 10}, 'absent': {}},
                     swap_url='http://swap', probes=probes or FakeProbes(), **kwargs)


def test_snapshot_preserves_independent_signals_and_invisible_gpu_memory():
    c = collector()
    try:
        s = c.collect()
        m = next(m for m in s.models if m.name == 'm')
        assert (m.state, m.unit_active, m.health_ok, m.is_sleeping, m.swap_state) == ('awake', True, True, False, 'ready')
        assert m.budget_gb == 50 and m.resident_gb == 20
        assert s.gpus[0].managed_gb == 20
        assert s.gpus[0].external_gb == 30  # Includes invisible allocations.
        assert s.activity[1].in_flight == 2
        assert s.models[0].state == 'stopped'
        assert s.memory.sleeping_weights_gb == 0
        assert s.read_only is True
    finally:
        c.close()


def test_unit_exit_ready_sleeping_and_health_failure_remain_distinct():
    p = FakeProbes()
    p.sleeping = lambda url: True
    c = collector(p)
    try:
        m = c.collect().models[1]
        assert m.state == 'sleeping' and m.swap_state == 'ready' and m.is_sleeping is True
        p.health = lambda url: False
        m = c.collect().models[1]
        assert m.state == 'unknown' and m.health_ok is False and m.unit_active is True
        p.units = lambda: {}
        m = c.collect().models[1]
        assert m.unit_active is False and m.state == 'stopped' and m.health_ok is None
    finally:
        c.close()


def test_failed_sources_never_become_stopped_zero_or_stale():
    p = FakeProbes()
    c = collector(p)
    try:
        assert c.collect().models[1].state == 'awake'
        def failure(*args):
            raise OSError('secret path or payload must not leak')
        p.units = p.events = p.gpus = p.health = p.sleeping = failure
        s = c.collect()
        assert s.models[1].state == 'unknown'
        assert s.models[1].unit_active is None and s.models[1].health_ok is None
        assert s.activity[1].in_flight is None and s.gpus == ()
        assert 'secret' not in str(s.errors)
    finally:
        c.close()


def test_slow_probes_share_round_deadline_and_do_not_accumulate():
    p = FakeProbes()
    calls = []
    def slow():
        calls.append(1)
        time.sleep(.35)
        return {}
    p.units = slow
    c = collector(p, deadline=.1)
    try:
        start = time.monotonic()
        s = c.collect()
        assert time.monotonic() - start < .2
        assert s.models[1].unit_active is None
        s = c.collect()
        assert len(calls) == 1
        assert any('previous probe' in e for e in s.errors)
    finally:
        c.close()


def test_deadline_must_be_below_two_seconds():
    with pytest.raises(ValueError):
        collector(deadline=2)


def test_build_collector_config_adapter_and_no_upstream_probes():
    from llmsvc.collectors import build_collector
    c = build_collector({'swap_url': 'http://localhost:8000', 'models': {}})
    assert isinstance(c, Collector)
    c.close()
    with pytest.raises(ValueError):
        build_collector({'swap_url': 'http://localhost', 'models': {'m': {'daemon_url': 'http://localhost/upstream/m'}}})
    with pytest.raises(ValueError):
        build_collector({'swap_url': 'http://localhost', 'unexpected': 1})


def test_container_memory_is_never_labelled_host_memory_without_explicit_source(tmp_path):
    (tmp_path / 'meminfo').write_text('MemAvailable: 1048576 kB\n')
    probes = Probes('http://localhost', proc_root=tmp_path)
    with pytest.raises(ValueError):
        probes.memory()
    probes = Probes('http://localhost', host_meminfo_path=tmp_path / 'meminfo')
    assert probes.memory() == 1


def test_successfully_observed_failed_unit_retains_exit_signal_and_blocks_policy():
    p = FakeProbes()
    p.units = lambda: parse_units('''Id=vllm-m.service
ActiveState=failed
SubState=failed
Result=exit-code
ExecMainStatus=1
MainPID=0
''')
    c = collector(p)
    try:
        s = c.collect()
        m = s.models[1]
        assert m.unit_active is False
        assert m.state == 'unknown' and m.swap_state == 'ready'
        assert any('fault cleanup required' in e for e in s.errors)
    finally:
        c.close()
