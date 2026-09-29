# Generated-By: Codex / gpt-6-astra
"""Deterministic SQL-work and snapshot regressions, not wall-clock guarantees."""
import sqlite3
from types import SimpleNamespace

import pytest

import llmsvc.activity as activity
from llmsvc.activity import ActivityReader
from test_activity import large_read_fixture, make_db, insert_activity


# Previous query retained as an independent work baseline, not a live source.
LEGACY_LATEST = """
SELECT model_id, src, metadata_json FROM (
 SELECT model_id, NULL AS src, metadata_json,
 ROW_NUMBER() OVER (PARTITION BY model_id ORDER BY ts_created DESC, id DESC) AS n
 FROM activity WHERE ts_created <= ?
) WHERE n = 1
"""


def test_indexed_read_does_less_work_than_old_latest_query_alone(tmp_path, monkeypatch):
    path, now = large_read_fixture(tmp_path)
    steps = {"legacy": 0, "reader": 0}
    with sqlite3.connect(path) as conn:
        def count_legacy():
            steps["legacy"] += 100
            return 0
        conn.set_progress_handler(count_legacy, 100)
        assert len(conn.execute(LEGACY_LATEST, (now,)).fetchall()) == 9

    # Count the real reader's SQLite VM instructions while retaining its own
    # cancellation callback. Scheduling is controlled independently of SQL work.
    original_connect = sqlite3.connect

    class CountingConnection(sqlite3.Connection):
        def set_progress_handler(self, callback, count):
            def counted():
                steps["reader"] += count
                return callback()
            return super().set_progress_handler(counted, count)

    monkeypatch.setattr(activity.sqlite3, "connect", lambda *a, **kw: original_connect(
        *a, factory=CountingConnection, **kw))
    monkeypatch.setattr(activity, "time", SimpleNamespace(monotonic=lambda: 0.0))
    reader = ActivityReader(path)
    result = reader.read(now=now)
    assert reader.deadline_ms == 80
    assert reader.last_error_code is None
    assert len(result) == 9
    assert sum(row["requests_last_hour"] for row in result.values()) == 3601
    assert sum(row["requests_last_10m"] for row in result.values()) == 601
    assert steps["reader"] < steps["legacy"] * 0.75


@pytest.mark.parametrize("indexed", [True, False])
def test_latest_ties_old_only_and_unknown_origin_preserved(tmp_path, indexed):
    path = tmp_path / "activity.sqlite"
    with make_db(path) as conn:
        if not indexed:
            conn.execute("DROP INDEX idx_activity_model_created_id")
            conn.execute("DROP INDEX idx_activity_created_id")
        conn.execute("ALTER TABLE activity ADD COLUMN src TEXT")
        insert_activity(conn, 10, 100, "old", metadata='{"src":"ip:192.0.2.1"}')
        insert_activity(conn, 20, 100, "old")
        insert_activity(conn, 1, 9900, "recent")  # ID does not order timestamps.
        insert_activity(conn, 30, 9800, "recent")
        insert_activity(conn, 40, 11000, "recent")
        conn.execute("UPDATE activity SET src='ip:192.0.2.2' WHERE id=1")
        conn.execute("UPDATE activity SET src='ip:192.0.2.3' WHERE id=40")
    reader = ActivityReader(path)
    result = reader.read(now=10000)
    assert reader.last_error_code is None
    assert result["old"]["last_used"] == 100
    assert result["old"]["requests_last_hour"] == 0
    assert result["old"]["source_ip"] is None  # Never seek an older known origin.
    assert result["recent"]["last_used"] == 9900
    assert result["recent"]["requests_last_10m"] == 2
    assert result["recent"]["source_ip"] == "192.0.2.2"


def test_latest_lookup_retains_summary_snapshot_across_concurrent_commit(tmp_path, monkeypatch):
    path = tmp_path / "activity.sqlite"
    with make_db(path) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        insert_activity(writer, 1, 100, "m", metadata='{"src":"ip:192.0.2.1"}')
    original_connect = sqlite3.connect
    committed = []

    class SnapshotConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            cursor = super().execute(sql, parameters)
            if "MAX(ts_created)" in sql and not committed:
                # Real WAL commit after the reader's summary establishes its
                # snapshot, before latest-source lookup. Even a same-time tie
                # must not splice the new row into the old counts.
                with original_connect(path) as writer:
                    insert_activity(writer, 2, 100, "m", metadata='{"src":"ip:192.0.2.2"}')
                committed.append(True)
            return cursor

    monkeypatch.setattr(activity.sqlite3, "connect", lambda *a, **kw: original_connect(
        *a, factory=SnapshotConnection, **kw))
    reader = ActivityReader(path)
    first = reader.read(now=200)
    assert committed and reader.last_error_code is None
    assert first["m"]["requests_last_10m"] == 1
    assert first["m"]["source_ip"] == "192.0.2.1"
    second = reader.read(now=200)
    assert second["m"]["requests_last_10m"] == 2
    assert second["m"]["source_ip"] == "192.0.2.2"


# The summary aggregate that scanned every row ever logged (#289).
LEGACY_SUMMARY = """
SELECT model_id, MAX(ts_created),
       SUM(CASE WHEN ts_created >= ? THEN 1 ELSE 0 END),
       SUM(CASE WHEN ts_created >= ? THEN 1 ELSE 0 END)
FROM activity WHERE ts_created <= ? GROUP BY model_id
"""


@pytest.mark.parametrize("indexed", [True, False])
def test_summary_work_does_not_grow_with_old_history(tmp_path, monkeypatch, indexed):
    # Generated-By: Claude Code / claude-opus-5-5
    path = tmp_path / "activity.sqlite"
    now = 10_000_000
    with make_db(path) as conn:
        if not indexed:
            conn.execute("DROP INDEX idx_activity_model_created_id")
            conn.execute("DROP INDEX idx_activity_created_id")
        conn.executemany(
            """
            INSERT INTO activity (
                id, ts_created, model_id, req_path, resp_content_type, resp_status_code,
                cache_tokens, draft_tokens, draft_acc_tokens, input_tokens, output_tokens,
                prompt_per_second, tokens_per_second, duration_ms, error_msg, metadata_json
            )
            VALUES (?, ?, ?, '/v1/chat/completions', 'application/json', 200,
                    0, 0, 0, 0, 0, 1.0, 2.0, 10, NULL, NULL)
            """,
            [(i + 1, now - 86400 - i, "model-{}".format(i % 6)) for i in range(60_000)],
        )
        insert_activity(conn, 60_001, now - 30, "model-0")
        insert_activity(conn, 60_002, now - 1200, "model-1")
        insert_activity(conn, 60_003, now + 50, "future-only")

    legacy = {"n": 0}
    with sqlite3.connect(path) as conn:
        def tick():
            legacy["n"] += 100
            return 0
        conn.set_progress_handler(tick, 100)
        expected = {row[0]: tuple(row[1:]) for row in conn.execute(
            LEGACY_SUMMARY, (now - 3600, now - 600, now)).fetchall()}

    steps = {"n": 0}
    original_connect = sqlite3.connect

    class CountingConnection(sqlite3.Connection):
        def set_progress_handler(self, callback, count):
            def counted():
                steps["n"] += count
                return callback()
            return super().set_progress_handler(counted, count)

    monkeypatch.setattr(activity.sqlite3, "connect", lambda *a, **kw: original_connect(
        *a, factory=CountingConnection, **kw))
    monkeypatch.setattr(activity, "time", SimpleNamespace(monotonic=lambda: 0.0))
    reader = ActivityReader(path)
    result = reader.read(now=now)
    assert reader.last_error_code is None
    assert {model: (row["last_used"], row["requests_last_hour"], row["requests_last_10m"])
            for model, row in result.items()} == expected
    assert "future-only" not in result
    assert result["model-0"]["requests_last_10m"] == 1
    assert result["model-1"]["requests_last_hour"] == 1
    if indexed:
        assert steps["n"] < legacy["n"] * 0.05
