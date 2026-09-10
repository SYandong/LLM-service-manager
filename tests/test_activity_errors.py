# Generated-By: Codex / gpt-6-astra
"""Deterministic SQLite/budget error replay; no host-load or soak claim."""
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

import llmsvc.activity as activity
import llmsvc.collectors as collectors
from llmsvc.activity import ActivityReader, activity_error_reason
from llmsvc.collectors import Collector
from test_collectors import FakeProbes


SECRET = 'SECRET SELECT private_column FROM /private/198.51.100.8/activity.sqlite'


def database(path, count=1):
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE activity (id INTEGER PRIMARY KEY, ts_created INTEGER, '
                 'model_id TEXT, input_tokens INTEGER, output_tokens INTEGER)')
    conn.executemany('INSERT INTO activity VALUES (?,100,"m",2,3)',
                     [(i,) for i in range(1, count+1)])
    conn.commit()
    return conn


class BudgetClock:
    def __init__(self):
        self.calls = 0
        self.expire = True

    def monotonic(self):
        self.calls += 1
        return .081 if self.expire and self.calls > 1 else 0


@pytest.mark.parametrize('method', ['read', 'usage'])
def test_actual_sqlite_budget_interrupt_is_deadline_and_next_call_is_fresh(tmp_path, monkeypatch, method):
    path = tmp_path/'activity.sqlite'
    database(path, 2048).close()
    clock = BudgetClock()
    monkeypatch.setattr(activity, 'time', SimpleNamespace(monotonic=clock.monotonic))
    reader = ActivityReader(path)
    result = getattr(reader, method)(now=200)
    assert reader.deadline_ms == 80
    assert reader.last_error_code == 'deadline'
    assert reader.last_error == 'activity read deadline exceeded'
    if method == 'read':
        assert result == {}
    else:
        assert result['known'] is False
        assert all(value is None for value in result['totals'].values())
    clock.expire = False
    result = getattr(reader, method)(now=200)
    assert reader.last_error is reader.last_error_code is None
    if method == 'read':
        assert result['m']['requests_last_10m'] == 2048
    else:
        assert result['totals']['requests'] == 2048


def test_budget_callback_precedes_schema_looking_empty_pragma(tmp_path, monkeypatch):
    path = tmp_path/'activity.sqlite'
    database(path).close()
    clock = BudgetClock()
    monkeypatch.setattr(activity, 'time', SimpleNamespace(monotonic=clock.monotonic))
    connect = sqlite3.connect

    class Empty:
        def fetchall(self):
            return []

    class MaskedPragma(sqlite3.Connection):
        def set_progress_handler(self, callback, count):
            self.callback = callback
            return super().set_progress_handler(callback, count)

        def execute(self, sql, *args):
            if sql == 'PRAGMA table_info(activity)':
                assert self.callback() == 1  # Replay a budget cancellation with an empty PRAGMA surface.
                return Empty()
            return super().execute(sql, *args)

    monkeypatch.setattr(activity.sqlite3, 'connect', lambda *a, **kw: connect(*a, factory=MaskedPragma, **kw))
    reader = ActivityReader(path)
    assert reader.read(now=200) == {}
    assert reader.last_error_code == 'deadline'  # Never a missing-table/schema report.


def test_real_lock_schema_and_invalid_utf8_have_distinct_collector_codes(tmp_path):
    path = tmp_path/'activity.sqlite'
    writer = database(path)
    reader = ActivityReader(path, deadline_ms=5)  # Fixture-only short lock wait; production default stays80.
    collector = Collector({'m': {}}, swap_url='http://unused', probes=FakeProbes(), activity_reader=reader)
    try:
        writer.execute('BEGIN EXCLUSIVE')
        locked = collector()
        assert 'activity: locked' in locked.errors
        assert locked.activity[0].requests_last_10m is None
        writer.rollback()
        writer.execute("UPDATE activity SET model_id=CAST(X'80' AS TEXT)")
        writer.commit()
        parsed = collector()
        assert 'activity: parse' in parsed.errors
        writer.execute('DROP TABLE activity')
        writer.commit()
        schema = collector()
        assert 'activity: schema' in schema.errors
        assert schema.activity[0].requests_last_10m is None
    finally:
        writer.close()
        collector.close()


@pytest.mark.parametrize(('exception','reason'), [
    (sqlite3.OperationalError('database is locked'), 'locked'),
    (sqlite3.OperationalError('database table is locked: '+SECRET), 'locked'),
    (sqlite3.OperationalError('no such table: '+SECRET), 'schema'),
    (sqlite3.OperationalError('no such column: '+SECRET), 'schema'),
    (sqlite3.OperationalError('interrupted'), 'interrupted'),
    (sqlite3.DatabaseError('file is not a database'), 'corrupt'),
    (sqlite3.OperationalError('disk I/O error'), 'io'),
    (sqlite3.OperationalError(SECRET), 'read_failed'),
    (OSError(SECRET), 'unavailable'),
    (ValueError(SECRET), 'parse'),
])
def test_python310_fallbacks_never_export_raw_errors(tmp_path, monkeypatch, exception, reason):
    assert activity_error_reason(exception) == reason
    reader = ActivityReader(tmp_path/'missing')

    def fail():
        raise exception

    monkeypatch.setattr(reader, '_connect', fail)
    assert reader.read(now=200) == {}
    assert reader.last_error_code == reason
    usage = reader.usage(now=200)
    assert usage['known'] is False
    assert SECRET not in json.dumps(usage)
    assert '/private/' not in reader.last_error and 'SELECT' not in reader.last_error
    collector = Collector({'m': {}}, swap_url='http://unused', probes=FakeProbes(), activity_reader=reader)
    try:
        snapshot = collector()
        assert 'activity: '+reason in snapshot.errors
        assert SECRET not in json.dumps(snapshot.to_dict())
    finally:
        collector.close()


def test_structured_sqlite_error_name_is_supported_without_echoing_message():
    error = sqlite3.OperationalError(SECRET)
    error.sqlite_errorname = 'SQLITE_LOCKED_SHAREDCACHE'
    assert activity_error_reason(error) == 'locked'
    assert activity_error_reason(error, deadline_expired=True) == 'deadline'


def test_parent_deadline_and_previous_probe_never_publish_late_counts(tmp_path, monkeypatch):
    path = tmp_path/'activity.sqlite'
    database(path).close()
    entered, release = threading.Event(), threading.Event()

    class DelayedReader(ActivityReader):
        delayed = True
        def read(self, *args, **kwargs):
            result = super().read(*args, **kwargs)
            if self.delayed:
                self.delayed = False
                entered.set()
                assert release.wait(2)
            return result

    # Fixed observation time; real monotonic deadlines, no artificial server load.
    monkeypatch.setattr(collectors, 'time', SimpleNamespace(time=lambda:200, monotonic=__import__('time').monotonic))
    reader = DelayedReader(path)
    collector = Collector({'m': {}}, swap_url='http://unused', probes=FakeProbes(), activity_reader=reader, deadline=.04)
    try:
        initial = collector()
        assert entered.is_set() and 'activity: round_deadline' in initial.errors
        assert initial.activity[0].requests_last_10m is None
        pending = collector.pending['activity']
        following = collector()
        assert 'activity: previous_probe_running' in following.errors
        assert following.activity[0].requests_last_10m is None
        with sqlite3.connect(path) as writer:
            writer.execute('INSERT INTO activity VALUES (2,150,"m",5,7)')
        release.set()
        pending.result(timeout=2)  # Old result contains one request, never copied into the new round.
        fresh = collector()
        assert fresh.activity[0].requests_last_10m == 2
        assert not any(e.startswith('activity:') for e in fresh.errors)
    finally:
        release.set()
        collector.close()


def test_parse_token_failure_clears_error_on_success_without_cached_totals(tmp_path):
    path = tmp_path/'activity.sqlite'
    writer = database(path)
    reader = ActivityReader(path)
    assert reader.usage(now=200)['totals']['requests'] == 1
    with writer:
        writer.execute('UPDATE activity SET input_tokens=?', (SECRET,))
    failed = reader.usage(now=200)
    assert reader.last_error_code == 'parse' and failed['known'] is False
    assert all(value is None for value in failed['totals'].values())
    assert SECRET not in failed['error']
    with writer:
        writer.execute('UPDATE activity SET input_tokens=5')
    assert reader.usage(now=200)['totals']['input_tokens'] == 5
    assert reader.last_error_code is reader.last_error is None
    writer.close()


@pytest.mark.parametrize('method', ['read', 'usage'])
def test_cancelled_sqlite_empty_rows_cannot_become_success_or_fresh_zero(tmp_path, monkeypatch, method):
    path = tmp_path/'activity.sqlite'
    database(path).close()
    clock = BudgetClock()
    monkeypatch.setattr(activity, 'time', SimpleNamespace(monotonic=clock.monotonic))
    connect = sqlite3.connect

    class Empty:
        def fetchall(self):
            return []

    class MaskedRows(sqlite3.Connection):
        def set_progress_handler(self, callback, count):
            self.callback = callback

        def execute(self, sql, *args):
            if 'FROM activity' in sql:
                assert self.callback() == 1
                return Empty()  # Driver-surface replay: cancellation with no raised exception.
            return super().execute(sql, *args)

    monkeypatch.setattr(activity.sqlite3, 'connect', lambda *a, **kw: connect(*a, factory=MaskedRows, **kw))
    reader = ActivityReader(path)
    result = getattr(reader, method)(now=200)
    assert reader.last_error_code == 'deadline'
    if method == 'read':
        assert result == {}
    else:
        assert result['known'] is False
        assert all(value is None for value in result['totals'].values())
