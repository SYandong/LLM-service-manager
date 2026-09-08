# Generated-By: Codex / gpt-6-astra
import sqlite3
import time

import llmsvc.activity as activity
from llmsvc.activity import ActivityReader


SCHEMA = """
CREATE TABLE activity (
    id INTEGER PRIMARY KEY,
    ts_created INTEGER NOT NULL,
    model_id TEXT NOT NULL,
    req_path TEXT,
    resp_content_type TEXT,
    resp_status_code INTEGER,
    cache_tokens INTEGER,
    draft_tokens INTEGER,
    draft_acc_tokens INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    prompt_per_second REAL,
    tokens_per_second REAL,
    duration_ms INTEGER,
    error_msg TEXT,
    metadata_json TEXT
);
CREATE INDEX idx_activity_created_id ON activity(ts_created DESC, id DESC);
CREATE INDEX idx_activity_model_created_id ON activity(model_id, ts_created DESC, id DESC);
"""


def make_db(path, schema=SCHEMA):
    conn = sqlite3.connect(path)
    conn.executescript(schema)
    return conn


def insert_activity(conn, row_id, ts, model, input_tokens=0, output_tokens=0, metadata=None):
    conn.execute(
        """
        INSERT INTO activity (
            id, ts_created, model_id, req_path, resp_content_type, resp_status_code,
            cache_tokens, draft_tokens, draft_acc_tokens, input_tokens, output_tokens,
            prompt_per_second, tokens_per_second, duration_ms, error_msg, metadata_json
        )
        VALUES (?, ?, ?, '/v1/chat/completions', 'application/json', 200, 0, 0, 0, ?, ?, 1.0, 2.0, 10, NULL, ?)
        """,
        (row_id, ts, model, input_tokens, output_tokens, metadata),
    )


def test_read_aggregates_recent_counts_and_latest_source(tmp_path):
    db = tmp_path / "activity.sqlite"
    now = 2_000_000
    with make_db(db) as conn:
        insert_activity(conn, 1, now - 3700, "qwen", metadata='{"src": "ip:10.0.0.1"}')
        insert_activity(conn, 2, now - 590, "qwen", metadata='{"src": "ip:10.0.0.2"}')
        insert_activity(conn, 3, now - 100, "qwen", metadata='{"fifo_priority": 1}')
        insert_activity(conn, 4, now - 60, "gemma", metadata='{"source_ip": "10.0.0.3"}')
        conn.commit()

    result = ActivityReader(db, {"10.0.0.3": "ctr-gemma"}).read(now=now)

    assert result["qwen"] == {
        "last_used": now - 100,
        "requests_last_hour": 2,
        "requests_last_10m": 2,
        "source_ip": None,
        "source_container": "unknown",
    }
    assert result["gemma"]["requests_last_hour"] == 1
    assert result["gemma"]["source_ip"] == "10.0.0.3"
    assert result["gemma"]["source_container"] == "ctr-gemma"


def test_read_excludes_future_requests_and_breaks_latest_tie_by_id(tmp_path):
    db = tmp_path / "activity.sqlite"
    now = 2_000_000
    with make_db(db) as conn:
        insert_activity(conn, 1, now - 60, "qwen", metadata='{"src": "ip:10.0.0.1"}')
        insert_activity(conn, 2, now - 60, "qwen", metadata='{"src": "ip:10.0.0.2"}')
        insert_activity(conn, 3, now + 60, "qwen", metadata='{"src": "ip:10.0.0.3"}')
        conn.commit()

    result = ActivityReader(db).read(now=now)

    assert result["qwen"]["last_used"] == now - 60
    assert result["qwen"]["requests_last_hour"] == 2
    assert result["qwen"]["source_ip"] == "10.0.0.2"


def test_read_supports_older_src_column_and_retains_unmapped_ip(tmp_path):
    db = tmp_path / "activity.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE activity (
            id INTEGER PRIMARY KEY,
            ts_created INTEGER NOT NULL,
            model_id TEXT NOT NULL,
            src TEXT
        );
        CREATE INDEX idx_activity_created_id ON activity(ts_created DESC, id DESC);
        CREATE INDEX idx_activity_model_created_id ON activity(model_id, ts_created DESC, id DESC);
        """
    )
    conn.execute(
        "INSERT INTO activity (id, ts_created, model_id, src) VALUES (1, 100, 'mistral', 'ip:192.0.2.44')"
    )
    conn.commit()
    conn.close()

    result = ActivityReader(db).read(now=200)

    assert result["mistral"]["source_ip"] == "192.0.2.44"
    assert result["mistral"]["source_container"] == "ip:192.0.2.44"


def test_recent_boundaries_are_inclusive(tmp_path):
    db = tmp_path / "activity.sqlite"
    now = 10_000
    with make_db(db) as conn:
        insert_activity(conn, 1, now - 3600, "qwen")
        insert_activity(conn, 2, now - 600, "qwen")
        insert_activity(conn, 3, now - 3601, "qwen")
        conn.commit()

    result = ActivityReader(db).read(now=now)

    assert result["qwen"]["requests_last_hour"] == 2
    assert result["qwen"]["requests_last_10m"] == 1


def test_missing_and_incompatible_database_return_unknown_not_zero(tmp_path):
    missing = tmp_path / "missing.sqlite"
    reader = ActivityReader(missing)
    assert reader.read() == {}
    assert reader.last_error is not None

    db = tmp_path / "bad.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE activity (id INTEGER PRIMARY KEY, model_id TEXT)")
    conn.commit()
    conn.close()

    reader = ActivityReader(db)
    assert reader.read() == {}
    assert "missing required columns" in reader.last_error

    corrupt = tmp_path / "corrupt.sqlite"
    corrupt.write_bytes(b"not a sqlite database")
    reader = ActivityReader(corrupt)
    assert reader.read() == {}
    assert reader.last_error is not None


def test_read_handles_wal_database_without_writing(tmp_path):
    db = tmp_path / "activity.sqlite"
    writer = make_db(db)
    writer.execute("PRAGMA journal_mode=WAL")
    insert_activity(writer, 1, 100, "qwen", metadata='{"src": "ip:10.0.0.8"}')
    writer.commit()
    writer.execute("BEGIN IMMEDIATE")
    insert_activity(writer, 2, 110, "qwen", metadata='{"src": "ip:10.0.0.9"}')

    result = ActivityReader(db, {"10.0.0.8": "ctr-a"}).read(now=120)

    assert result["qwen"]["source_container"] == "ctr-a"
    writer.rollback()
    writer.close()


def test_read_closes_sqlite_connection(tmp_path, monkeypatch):
    db = tmp_path / "activity.sqlite"
    with make_db(db) as conn:
        insert_activity(conn, 1, 100, "qwen")
        conn.commit()

    original_connect = sqlite3.connect
    seen = []

    class TrackingConnection(sqlite3.Connection):
        def close(self):
            seen.append("closed")
            super().close()

    def connect(*args, **kwargs):
        kwargs["factory"] = TrackingConnection
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(activity.sqlite3, "connect", connect)

    assert ActivityReader(db).read(now=200)["qwen"]["last_used"] == 100
    assert seen == ["closed"]


def test_read_invalid_now_returns_unknown_without_crashing(tmp_path):
    db = tmp_path / "activity.sqlite"
    with make_db(db) as conn:
        insert_activity(conn, 1, 100, "qwen")
        conn.commit()

    reader = ActivityReader(db)
    assert reader.read(now=["bad"]) == {}
    assert "finite timestamp" in reader.last_error

    reader = ActivityReader(db)
    assert reader.read(now=float("inf")) == {}
    assert "finite timestamp" in reader.last_error


def test_read_rollback_journal_lock_returns_unknown_bounded(tmp_path):
    db = tmp_path / "activity.sqlite"
    writer = make_db(db)
    insert_activity(writer, 1, 100, "qwen")
    writer.commit()
    writer.execute("BEGIN EXCLUSIVE")
    insert_activity(writer, 2, 110, "qwen")

    reader = ActivityReader(db, deadline_ms=20)
    started = time.perf_counter()
    try:
        result = reader.read(now=120)
    finally:
        writer.rollback()
        writer.close()
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert result == {}
    assert reader.last_error is not None
    assert elapsed_ms < 100


def test_read_large_fixture_stays_under_100ms(tmp_path):
    db = tmp_path / "activity.sqlite"
    now = 2_000_000
    row_count = 25_500
    with make_db(db) as conn:
        rows = []
        for index in range(row_count):
            rows.append(
                (
                    index + 1,
                    now - index % (7 * 86400 - 1),
                    "model-{}".format(index % 9),
                    index % 17,
                    index % 31,
                    '{{"fifo_priority": {}}}'.format(index % 3),
                )
            )
        conn.executemany(
            """
            INSERT INTO activity (
                id, ts_created, model_id, req_path, resp_content_type, resp_status_code,
                cache_tokens, draft_tokens, draft_acc_tokens, input_tokens, output_tokens,
                prompt_per_second, tokens_per_second, duration_ms, error_msg, metadata_json
            )
            VALUES (?, ?, ?, '/v1/chat/completions', 'application/json', 200, 0, 0, 0, ?, ?, 1.0, 2.0, 10, NULL, ?)
            """,
            rows,
        )
        conn.commit()

    reader = ActivityReader(db)
    started = time.perf_counter()
    result = reader.read(now=now)
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert len(result) == 9
    assert sum(row["requests_last_hour"] for row in result.values()) == 3601
    assert elapsed_ms < 100
