# Generated-By: Codex / gpt-6-astra
"""Test the evidence tool with controlled inputs; never run a benchmark in CI."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def tool():
    path = Path(__file__).parent/'fixtures/telemetry/activity_performance.py'
    spec = importlib.util.spec_from_file_location('activity_performance_tool', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_dry_run_does_not_create_data_or_sample_load(tool, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail('dry-run must not measure or create fixture data')
    monkeypatch.setattr(tool, 'load_context', forbidden)
    monkeypatch.setattr(tool, 'TemporaryDirectory', forbidden)
    assert tool.main([]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'not_run' and result['reader_deadline_ms'] == 80
    assert 'measurements' not in result


def test_load_gate_returns_no_measurement_without_skipping_functional_tests(tool, monkeypatch, capsys):
    monkeypatch.setattr(tool, 'load_context', lambda: {'load_1m': 2, 'available_cpus': 1, 'load_per_cpu': 2})
    monkeypatch.setattr(tool, 'TemporaryDirectory', lambda **kw: pytest.fail('load gate must precede data creation'))
    assert tool.main(['--measure', '--max-load-per-cpu', '1']) == 2
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'not_measured_load_gate' and 'measurements' not in result


@pytest.mark.parametrize('method', ['read', 'usage'])
def test_cancelled_empty_result_is_unknown_not_zero_or_performance_success(tool, monkeypatch, method):
    class Reader:
        deadline_ms = 80
        last_error = 'activity read deadline exceeded'
        last_error_code = 'deadline'
        def __init__(self, path):
            pass
        def read(self, **kwargs):
            return {}
        def usage(self, **kwargs):
            return {'known': False, 'totals': {'requests': None}}
    monkeypatch.setattr(tool, 'ActivityReader', Reader)
    clock = iter([0, .001])
    monkeypatch.setattr(tool, 'time', SimpleNamespace(perf_counter=lambda: next(clock)))
    result = tool.measure_case('unused', 100, method)
    assert result['elapsed_ms'] < 100
    assert result['passed'] is False and result['counts_verified'] is False
    assert result['counts'] is None and result['error_code'] == 'deadline'


@pytest.mark.parametrize('elapsed, passed', [(.099, True), (.100, False)])
def test_real_time_threshold_is_retained_separately_from_verified_counts(tool, monkeypatch, elapsed, passed):
    class Reader:
        deadline_ms = 80
        last_error = last_error_code = None
        def __init__(self, path):
            pass
        def read(self, **kwargs):
            return {str(i): {'requests_last_hour': 3601 if i == 0 else 0,
                             'requests_last_10m': 601 if i == 0 else 0} for i in range(9)}
    monkeypatch.setattr(tool, 'ActivityReader', Reader)
    clock = iter([0, elapsed])
    monkeypatch.setattr(tool, 'time', SimpleNamespace(perf_counter=lambda: next(clock)))
    result = tool.measure_case('unused', 100, 'read')
    assert result['counts_verified'] is True and result['counts']['models'] == 9
    assert result['passed'] is passed
