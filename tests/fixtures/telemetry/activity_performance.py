# Generated-By: Codex / gpt-6-astra
"""Explicit synthetic performance evidence, separate from functional pytest checks."""
import sys
if __name__ == '__main__':
    sys.dont_write_bytecode = True

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
import time
from tempfile import TemporaryDirectory

TESTS = Path(__file__).resolve().parents[2]
for folder in (TESTS.parent, TESTS):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))
from llmsvc.activity import ActivityReader
import llmsvc.activity as activity
from test_activity import large_read_fixture
from test_usage import large_usage_fixture


def load_context():
    try:
        load = os.getloadavg()[0]
    except (AttributeError, OSError):
        load = None
    try:
        cpus = len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else os.cpu_count()
    except OSError:
        cpus = None
    return {'load_1m': load, 'available_cpus': cpus,
            'load_per_cpu': load / cpus if load is not None and cpus else None}


def measure_case(path, now, method):
    reader = ActivityReader(path)  # Keep the real, unmodified production default.
    assert reader.deadline_ms == 80
    started = time.perf_counter()
    result = reader.read(now=now) if method == 'read' else reader.usage(days=7, now=now)
    elapsed = (time.perf_counter() - started) * 1000
    if method == 'read':
        correct = (reader.last_error is None and len(result) == 9
                   and sum(row['requests_last_hour'] for row in result.values()) == 3601
                   and sum(row['requests_last_10m'] for row in result.values()) == 601)
        counts = {'models': len(result), 'requests_last_hour': 3601} if correct else None
    else:
        expected = {'requests': 25_500,
                    'input_tokens': sum(i % 17 for i in range(25_500)),
                    'output_tokens': sum(i % 31 for i in range(25_500))}
        correct = reader.last_error is None and result['known'] and result['totals'] == expected
        counts = result['totals'] if correct else None
    return {'method': method, 'reader_deadline_ms': reader.deadline_ms,
            'threshold_ms': 100, 'elapsed_ms': elapsed, 'counts_verified': bool(correct),
            'counts': counts, 'error_code': reader.last_error_code,
            'passed': bool(correct and elapsed < 100)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--measure', action='store_true', help='run one real read and usage measurement on synthetic databases')
    mode.add_argument('--dry-run', action='store_true', help='show plan only (the default)')
    parser.add_argument('--max-load-per-cpu', type=float, help='explicit benchmark-window admission threshold, not a runtime policy setting')
    args = parser.parse_args(argv)
    base = {'generated_by': 'Codex / gpt-6-astra', 'fixture_rows': 25_500,
            'reader_deadline_ms': 80, 'threshold_ms': 100, 'retries': 0,
            'production_data_or_actions': False, 'causal_or_long_term_proof': False}
    if not args.measure:
        print(json.dumps({**base, 'status': 'not_run', 'would_measure': ['read', 'usage']}))
        return 0
    if args.max_load_per_cpu is None or not math.isfinite(args.max_load_per_cpu) or args.max_load_per_cpu <= 0:
        parser.error('--measure requires a finite positive --max-load-per-cpu')
    before = load_context()
    base.update(load_before=before, max_load_per_cpu=args.max_load_per_cpu,
                python=platform.python_version(), sqlite=sqlite3.sqlite_version,
                reader_source_sha256=hashlib.sha256(Path(activity.__file__).read_bytes()).hexdigest(),
                fixture_source_sha256={name: hashlib.sha256((TESTS/name).read_bytes()).hexdigest()
                                       for name in ('test_activity.py', 'test_usage.py')})
    if before['load_per_cpu'] is None or before['load_per_cpu'] > args.max_load_per_cpu:
        print(json.dumps({**base, 'status': 'not_measured_load_gate'}))
        return 2
    base['window_started_at'] = time.time()
    with TemporaryDirectory(prefix='llmsvc-activity-performance-') as directory:
        root = Path(directory)
        results = []
        for method, fixture in (('read', large_read_fixture), ('usage', large_usage_fixture)):
            folder = root/method
            folder.mkdir()
            path, now = fixture(folder)
            results.append(measure_case(path, now, method))
    after = load_context()
    base.update(window_finished_at=time.time(), load_after=after, measurements=results,
                temporary_databases_removed=True)
    admitted = after['load_per_cpu'] is not None and after['load_per_cpu'] <= args.max_load_per_cpu
    status = ('inconclusive_load_changed' if not admitted else
              'passed_observed_window_only' if all(row['passed'] for row in results) else 'failed')
    print(json.dumps({**base, 'status': status}))
    return 2 if not admitted else 0 if all(row['passed'] for row in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
