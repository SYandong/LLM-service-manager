# Generated-By: Codex / gpt-6-astra
import sqlite3
import time

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


def test_usage_groups_by_container_with_token_totals_and_provenance(tmp_path):
    db = tmp_path / "activity.sqlite"
    now = 2_000_000
    with make_db(db) as conn:
        insert_activity(conn, 1, now - 60, "qwen", 10, 20, '{"src": "ip:10.0.0.1"}')
        insert_activity(conn, 2, now - 120, "gemma", 5, 7, '{"source": "ip:10.0.0.1"}')
        insert_activity(conn, 3, now - 180, "qwen", 3, 4, '{"src": "ip:10.0.0.2"}')
        insert_activity(conn, 4, now - 8 * 86400, "old", 99, 99, '{"src": "ip:10.0.0.1"}')
        conn.commit()

    result = ActivityReader(db, {"10.0.0.1": "ctr-a"}).usage(days=7, by="container", now=now)

    assert result["known"] is True
    assert result["error"] is None
    assert result["totals"] == {"requests": 3, "input_tokens": 18, "output_tokens": 31}
    rows = {row["container"]: row for row in result["rows"]}
    assert rows["ctr-a"]["requests"] == 2
    assert rows["ctr-a"]["input_tokens"] == 15
    assert rows["ctr-a"]["output_tokens"] == 27
    assert rows["ctr-a"]["source_ips"] == ("10.0.0.1",)
    assert rows["ctr-a"]["source_containers"] == ("ctr-a",)
    assert rows["ip:10.0.0.2"]["requests"] == 1
    assert rows["ip:10.0.0.2"]["source_known"] is True


def test_usage_groups_by_ip_and_model(tmp_path):
    db = tmp_path / "activity.sqlite"
    now = 2_000_000
    with make_db(db) as conn:
        insert_activity(conn, 1, now - 60, "qwen", 1, 2, '{"src": "ip:10.0.0.1"}')
        insert_activity(conn, 2, now - 120, "qwen", 3, 4, '{"fifo_priority": 1}')
        conn.commit()

    by_ip = ActivityReader(db).usage(days=1, by="ip", now=now)
    assert {row["ip"]: row["requests"] for row in by_ip["rows"]} == {
        "10.0.0.1": 1,
        "unknown": 1,
    }

    by_model = ActivityReader(db).usage(days=1, by="model", now=now)
    assert by_model["rows"][0]["model"] == "qwen"
    assert by_model["rows"][0]["requests"] == 2


def test_usage_invalid_query_and_bad_database_report_unknown_counts(tmp_path):
    db = tmp_path / "bad.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE activity (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    invalid = ActivityReader(db).usage(days=0, by="container")
    assert invalid["known"] is False
    assert invalid["totals"] == {"requests": None, "input_tokens": None, "output_tokens": None}

    bool_days = ActivityReader(db).usage(days=True, by="container")
    assert bool_days["known"] is False
    assert "positive integer" in bool_days["error"]

    bad_schema = ActivityReader(db).usage(days=1, by="container")
    assert bad_schema["known"] is False
    assert "missing required columns" in bad_schema["error"]


def test_usage_invalid_now_and_negative_tokens_report_unknown(tmp_path):
    db = tmp_path / "activity.sqlite"
    with make_db(db) as conn:
        insert_activity(conn, 1, 100, "qwen", -1, 2, '{"src": "ip:10.0.0.1"}')
        conn.commit()

    bad_now = ActivityReader(db).usage(days=1, by="container", now=["not-a-time"])
    assert bad_now["known"] is False
    assert bad_now["totals"] == {"requests": None, "input_tokens": None, "output_tokens": None}

    infinite_now = ActivityReader(db).usage(days=1, by="container", now=float("inf"))
    assert infinite_now["known"] is False
    assert "finite timestamp" in infinite_now["error"]

    negative_tokens = ActivityReader(db).usage(days=1, by="container", now=200)
    assert negative_tokens["known"] is False
    assert "negative" in negative_tokens["error"]


def test_usage_nonintegral_real_tokens_report_unknown(tmp_path):
    db = tmp_path / "activity.sqlite"
    with make_db(db) as conn:
        insert_activity(conn, 1, 100, "qwen", 1.5, 2, '{"src": "ip:10.0.0.1"}')
        conn.commit()

    result = ActivityReader(db).usage(days=1, by="container", now=200)

    assert result["known"] is False
    assert result["totals"] == {"requests": None, "input_tokens": None, "output_tokens": None}


def test_usage_large_fixture_stays_under_100ms(tmp_path):
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
                    '{{"src": "ip:10.0.0.{}"}}'.format(index % 5),
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
    result = reader.usage(days=7, by="container", now=now)
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert result["known"] is True
    assert result["totals"]["requests"] == row_count
    assert elapsed_ms < 100
