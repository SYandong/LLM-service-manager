# Generated-By: OpenCode / deepseek-v4.1-flash
"""Per-user/model/day usage report attributed by the patched ``client_ip`` field."""

import json
import os
import time

from llmsvc.activity import USAGE_REPORT_COUNTS, ActivityReader

from test_usage import insert_activity, make_db


NOW = 2_000_000
COUNTS = set(USAGE_REPORT_COUNTS)

# (model, input, output, client_ip, status, error_msg, duration_ms)
ROWS = [
    ("alpha", 10, 20, "10.0.0.1", 200, None, 50),
    ("beta", 3, 4, "10.0.0.2", 500, None, 30),
    ("alpha", 5, 0, "127.0.0.1", 200, None, 20),
    ("gamma", 0, 0, "10.0.0.9", 200, None, 10),
    ("delta", 7, 8, None, 200, None, 40),
    ("alpha", 0, 0, "10.0.0.1", 200, None, 5),
    ("beta", 1, 1, "10.0.0.1", 200, "boom", 15),
]

TOTALS = {
    "requests": 7,
    "errors": 2,
    "input_tokens": 26,
    "output_tokens": 33,
    "total_tokens": 59,
    "untracked_requests": 2,
    "duration_ms": 170,
}


def write_report_db(path):
    with make_db(path) as conn:
        for index, (model, inp, out, ip, status, err, duration) in enumerate(ROWS, 1):
            metadata = {"fifo_priority": "0"}
            if ip is not None:
                metadata["client_ip"] = ip
            insert_activity(conn, index, NOW - 100 * index, model, inp, out,
                            json.dumps(metadata), status=status, error_msg=err,
                            duration_ms=duration)
        conn.commit()


def write_map(path, containers, generated_at):
    path.write_text(json.dumps({"generated_at": generated_at, "containers": containers}))


def assert_invariants(report):
    assert set(report["totals"]) == COUNTS
    for name in COUNTS:
        assert sum(row[name] for row in report["rows"]) == report["totals"][name]
    for row in report["rows"]:
        for name in COUNTS:
            assert sum(entry[name] for entry in row["breakdown"]) == row[name]


def test_by_user_attribution_counts_breakdown_and_order(tmp_path):
    db = tmp_path / "activity.sqlite"
    map_path = tmp_path / "ip-containers.json"
    write_report_db(db)
    write_map(map_path, {"10.0.0.2": "ctr-b"}, 1700000000)

    reader = ActivityReader(db, {"10.0.0.1": "ctr-a"},
                            ip_containers_path=map_path, timezone="UTC")
    report = reader.usage_report(days=7, by="user", now=NOW)

    assert report["known"] is True and report["error"] is None
    assert report["since"] == NOW - 7 * 86400 and report["until"] == NOW
    assert report["attribution"] == {
        "mode": "client_ip",
        "map_source": "config+file",
        "map_updated_at": 1700000000,
        "mapped_ips": 2,
    }
    assert report["totals"] == TOTALS
    assert [row["user"] for row in report["rows"]] == [
        "ctr-a", "ctr-b", "host", "ip:10.0.0.9", "unattributed",
    ]
    kinds = {row["user"]: row["kind"] for row in report["rows"]}
    assert kinds == {
        "ctr-a": "container", "ctr-b": "container", "host": "host",
        "ip:10.0.0.9": "ip", "unattributed": "unattributed",
    }
    rows = {row["user"]: row for row in report["rows"]}
    assert rows["ctr-a"] == {
        "requests": 3, "errors": 1, "input_tokens": 11, "output_tokens": 21,
        "total_tokens": 32, "untracked_requests": 1, "duration_ms": 70,
        "user": "ctr-a", "kind": "container",
        "first_seen": NOW - 700, "last_seen": NOW - 100,
        "breakdown": [
            {"requests": 2, "errors": 0, "input_tokens": 10, "output_tokens": 20,
             "total_tokens": 30, "untracked_requests": 1, "duration_ms": 55,
             "model": "alpha", "kind": "model"},
            {"requests": 1, "errors": 1, "input_tokens": 1, "output_tokens": 1,
             "total_tokens": 2, "untracked_requests": 0, "duration_ms": 15,
             "model": "beta", "kind": "model"},
        ],
    }
    assert rows["unattributed"]["requests"] == 1
    assert rows["unattributed"]["total_tokens"] == 15
    assert rows["host"]["kind"] == "host" and rows["host"]["errors"] == 0
    assert rows["ip:10.0.0.9"]["untracked_requests"] == 1
    assert_invariants(report)


def test_static_map_overrides_the_file_map(tmp_path):
    db = tmp_path / "activity.sqlite"
    map_path = tmp_path / "ip-containers.json"
    write_report_db(db)
    write_map(map_path, {"10.0.0.1": "file-a", "10.0.0.2": "ctr-b"}, 1700000000)

    reader = ActivityReader(db, {"10.0.0.1": "static-a"}, ip_containers_path=map_path)
    report = reader.usage_report(days=7, by="user", now=NOW)

    rows = {row["user"]: row for row in report["rows"]}
    assert "static-a" in rows and "file-a" not in rows
    assert rows["static-a"]["requests"] == 3
    assert report["attribution"]["map_source"] == "config+file"
    assert report["attribution"]["mapped_ips"] == 2


def test_missing_or_invalid_map_file_keeps_the_report_known(tmp_path):
    db = tmp_path / "activity.sqlite"
    write_report_db(db)
    missing = tmp_path / "missing.json"

    report = ActivityReader(db, ip_containers_path=missing).usage_report(days=7, by="user", now=NOW)
    assert report["known"] is True
    assert report["attribution"] == {
        "mode": "client_ip", "map_source": "none", "map_updated_at": None, "mapped_ips": 0,
    }
    assert {row["user"] for row in report["rows"]} == {
        "host", "ip:10.0.0.1", "ip:10.0.0.2", "ip:10.0.0.9", "unattributed",
    }

    invalid = tmp_path / "invalid.json"
    invalid.write_text("not json at all")
    report = ActivityReader(db, ip_containers_path=invalid).usage_report(days=7, by="user", now=NOW)
    assert report["known"] is True
    assert report["attribution"]["map_source"] == "none"


def test_by_model_carries_the_user_breakdown(tmp_path):
    db = tmp_path / "activity.sqlite"
    map_path = tmp_path / "ip-containers.json"
    write_report_db(db)
    write_map(map_path, {"10.0.0.2": "ctr-b"}, 1700000000)

    reader = ActivityReader(db, {"10.0.0.1": "ctr-a"}, ip_containers_path=map_path)
    report = reader.usage_report(days=7, by="model", now=NOW)

    assert [row["model"] for row in report["rows"]] == ["alpha", "beta", "delta", "gamma"]
    assert all(row["kind"] == "model" for row in report["rows"])
    rows = {row["model"]: row for row in report["rows"]}
    assert rows["alpha"]["requests"] == 3
    assert rows["alpha"]["first_seen"] == NOW - 600
    assert rows["alpha"]["last_seen"] == NOW - 100
    assert rows["alpha"]["breakdown"] == [
        {"requests": 2, "errors": 0, "input_tokens": 10, "output_tokens": 20,
         "total_tokens": 30, "untracked_requests": 1, "duration_ms": 55,
         "user": "ctr-a", "kind": "container"},
        {"requests": 1, "errors": 0, "input_tokens": 5, "output_tokens": 0,
         "total_tokens": 5, "untracked_requests": 0, "duration_ms": 20,
         "user": "host", "kind": "host"},
    ]
    assert [entry["user"] for entry in rows["beta"]["breakdown"]] == ["ctr-a", "ctr-b"]
    assert rows["beta"]["breakdown"][0]["kind"] == "container"
    assert_invariants(report)


def test_by_day_uses_the_configured_timezone(tmp_path):
    db = tmp_path / "activity.sqlite"
    first = 1704117600   # 2024-01-01 14:00 UTC -> 2024-01-01 22:00 Asia/Shanghai
    second = 1704148800  # 2024-01-01 20:00 UTC -> 2024-01-02 04:00 Asia/Shanghai
    now = 1704160000
    with make_db(db) as conn:
        insert_activity(conn, 1, first, "m", 2, 2, '{"client_ip": "10.0.0.1"}')
        insert_activity(conn, 2, second, "m", 1, 1, '{"client_ip": "10.0.0.1"}')
        conn.commit()

    shanghai = ActivityReader(db, timezone="Asia/Shanghai").usage_report(days=2, by="day", now=now)
    assert shanghai["timezone"] == "Asia/Shanghai"
    assert [row["day"] for row in shanghai["rows"]] == ["2024-01-01", "2024-01-02"]
    rows = {row["day"]: row for row in shanghai["rows"]}
    assert rows["2024-01-01"]["total_tokens"] == 4
    assert rows["2024-01-02"]["total_tokens"] == 2
    assert all(row["kind"] == "day" for row in shanghai["rows"])
    assert shanghai["rows"][0]["breakdown"] == [
        {"requests": 1, "errors": 0, "input_tokens": 2, "output_tokens": 2,
         "total_tokens": 4, "untracked_requests": 0, "duration_ms": 10,
         "model": "m", "kind": "model"},
    ]
    assert_invariants(shanghai)

    utc = ActivityReader(db, timezone="UTC").usage_report(days=2, by="day", now=now)
    assert [row["day"] for row in utc["rows"]] == ["2024-01-01"]
    assert utc["rows"][0]["requests"] == 2


def test_invalid_query_and_null_tokens_stay_unknown(tmp_path):
    db = tmp_path / "activity.sqlite"
    write_report_db(db)

    for kwargs in ({"days": 0}, {"days": 366}, {"days": True}):
        report = ActivityReader(db).usage_report(by="user", now=NOW, **kwargs)
        assert report["known"] is False
        assert set(report["totals"]) == COUNTS
        assert all(value is None for value in report["totals"].values())
        assert "1..365" in report["error"]

    report = ActivityReader(db).usage_report(days=7, by="ip", now=NOW)
    assert report["known"] is False
    assert report["error"] == "by must be one of: user, model, day"
    assert report["attribution"] is None and report["rows"] == []

    null_db = tmp_path / "null.sqlite"
    with make_db(null_db) as conn:
        insert_activity(conn, 1, NOW - 60, "qwen", None, 5, '{"client_ip": "10.0.0.1"}')
        conn.commit()
    report = ActivityReader(null_db).usage_report(days=7, by="user", now=NOW)
    assert report["known"] is False
    assert "parse failed" in report["error"]


def test_map_file_is_reread_only_when_it_changes(tmp_path):
    db = tmp_path / "activity.sqlite"
    with make_db(db) as conn:
        insert_activity(conn, 1, NOW - 60, "qwen", 1, 2, '{"client_ip": "10.0.0.1"}')
        conn.commit()
    map_path = tmp_path / "ip-containers.json"
    write_map(map_path, {"10.0.0.1": "one"}, 1)

    reader = ActivityReader(db, ip_containers_path=map_path)
    first = reader.usage_report(days=1, by="user", now=NOW)
    assert first["rows"][0]["user"] == "one"
    assert first["attribution"]["map_updated_at"] == 1

    again = reader.usage_report(days=1, by="user", now=NOW)
    assert again["rows"][0]["user"] == "one"

    future = time.time() + 1000
    write_map(map_path, {"10.0.0.1": "two"}, 2)
    os.utime(map_path, (future, future))
    third = reader.usage_report(days=1, by="user", now=NOW)
    assert third["rows"][0]["user"] == "two"
    assert third["attribution"]["map_updated_at"] == 2


def test_legacy_container_grouping_uses_client_ip_fallback(tmp_path):
    db = tmp_path / "activity.sqlite"
    with make_db(db) as conn:
        insert_activity(conn, 1, NOW - 60, "qwen", 1, 2, '{"client_ip": "10.0.0.1"}')
        conn.commit()

    usage = ActivityReader(db, {"10.0.0.1": "ctr-a"}).usage(days=1, by="container", now=NOW)
    assert usage["known"] is True
    assert usage["rows"][0]["container"] == "ctr-a"
