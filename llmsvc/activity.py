# Generated-By: Codex / gpt-6-astra
"""Read-only llama-swap activity and usage aggregation."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import quote


UNKNOWN_SOURCE = "unknown"


class ActivityReader:
    """Bounded read-only access to llama-swap ``activity.sqlite``."""

    def __init__(
        self,
        path: os.PathLike[str] | str,
        ip_containers: Optional[Mapping[str, str]] = None,
        deadline_ms: int = 80,
    ) -> None:
        self.path = Path(path)
        self.ip_containers = dict(ip_containers or {})
        self.deadline_ms = deadline_ms
        self.last_error: Optional[str] = None

    def read(self, now: Optional[float] = None) -> dict[str, dict[str, Any]]:
        """Return recent activity keyed by model id.

        Counts cover completed activity rows in the source table. When the
        database cannot be read safely, an empty mapping is returned and
        ``last_error`` describes why; callers should treat missing counts as
        unknown rather than zero.
        """

        self.last_error = None
        try:
            now_ts = _coerce_now(now)
            with closing(self._connect()) as conn:
                columns = _activity_columns(conn)
                _require_columns(columns, {"id", "ts_created", "model_id"})
                src_expr = "src" if "src" in columns else "NULL"
                metadata_expr = "metadata_json" if "metadata_json" in columns else "NULL"
                summary_rows = conn.execute(
                    """
                    SELECT
                        model_id,
                        MAX(ts_created) AS last_used,
                        SUM(CASE WHEN ts_created >= ? THEN 1 ELSE 0 END) AS requests_last_hour,
                        SUM(CASE WHEN ts_created >= ? THEN 1 ELSE 0 END) AS requests_last_10m
                    FROM activity
                    WHERE ts_created <= ?
                    GROUP BY model_id
                    """,
                    (int(now_ts - 3600), int(now_ts - 600), int(now_ts)),
                ).fetchall()
                latest_rows = conn.execute(
                    f"""
                    SELECT model_id, src, metadata_json
                    FROM (
                        SELECT
                            model_id,
                            {src_expr} AS src,
                            {metadata_expr} AS metadata_json,
                            ROW_NUMBER() OVER (
                                PARTITION BY model_id
                                ORDER BY ts_created DESC, id DESC
                            ) AS row_number
                        FROM activity
                        WHERE ts_created <= ?
                    )
                    WHERE row_number = 1
                    """,
                    (int(now_ts),),
                ).fetchall()
        except (OSError, sqlite3.Error, ValueError) as exc:
            self.last_error = str(exc)
            return {}

        latest_by_model: dict[str, tuple[Optional[str], Optional[str]]] = {}
        for row in latest_rows:
            model = row["model_id"]
            if model not in latest_by_model:
                latest_by_model[model] = (row["src"], row["metadata_json"])

        result: dict[str, dict[str, Any]] = {}
        for row in summary_rows:
            source = self._source_from_row(*latest_by_model.get(row["model_id"], (None, None)))
            result[row["model_id"]] = {
                "last_used": row["last_used"],
                "requests_last_hour": int(row["requests_last_hour"]),
                "requests_last_10m": int(row["requests_last_10m"]),
                "source_ip": source["source_ip"],
                "source_container": source["source_container"],
            }
        return result

    def usage(
        self,
        days: int = 7,
        by: str = "container",
        now: Optional[float] = None,
    ) -> dict[str, Any]:
        """Return request and token usage totals grouped by source."""

        if not isinstance(days, int) or isinstance(days, bool) or days < 1:
            return _unknown_usage(days, by, "days must be a positive integer")
        if by not in {"container", "ip", "model"}:
            return _unknown_usage(days, by, "by must be one of: container, ip, model")

        self.last_error = None
        try:
            now_ts = _coerce_now(now)
            with closing(self._connect()) as conn:
                columns = _activity_columns(conn)
                _require_columns(columns, {"id", "ts_created", "model_id"})
                if "input_tokens" not in columns or "output_tokens" not in columns:
                    raise ValueError("activity schema missing token columns: input_tokens, output_tokens")
                src_expr = "src" if "src" in columns else "NULL"
                metadata_expr = "metadata_json" if "metadata_json" in columns else "NULL"
                since = int(now_ts - days * 86400)
                if by == "model":
                    return self._usage_by_model(conn, days, by, since, int(now_ts))
                return self._usage_by_source(
                    conn,
                    days,
                    by,
                    since,
                    int(now_ts),
                    src_expr,
                    metadata_expr,
                )
        except (OSError, sqlite3.Error, ValueError) as exc:
            self.last_error = str(exc)
            return _unknown_usage(days, by, str(exc))

    def _usage_by_source(
        self,
        conn: sqlite3.Connection,
        days: int,
        by: str,
        since: int,
        now_ts: int,
        src_expr: str,
        metadata_expr: str,
    ) -> dict[str, Any]:
        rows = conn.execute(
            f"""
            SELECT
                {src_expr} AS src,
                {metadata_expr} AS metadata_json,
                COUNT(*) AS requests,
                SUM(input_tokens) AS input_tokens,
                SUM(output_tokens) AS output_tokens,
                SUM(CASE
                    WHEN input_tokens IS NULL OR output_tokens IS NULL THEN 1
                    WHEN typeof(input_tokens) NOT IN ('integer', 'real') THEN 1
                    WHEN typeof(output_tokens) NOT IN ('integer', 'real') THEN 1
                    WHEN input_tokens < 0 OR output_tokens < 0 THEN 1
                    WHEN input_tokens != CAST(input_tokens AS INTEGER) THEN 1
                    WHEN output_tokens != CAST(output_tokens AS INTEGER) THEN 1
                    ELSE 0
                END) AS invalid_tokens
            FROM activity
            WHERE ts_created >= ? AND ts_created <= ?
            GROUP BY src, metadata_json
            """,
            (since, now_ts),
        ).fetchall()
        aggregates: dict[str, dict[str, Any]] = {}
        totals = {"requests": 0, "input_tokens": 0, "output_tokens": 0}
        for row in rows:
            if int(row["invalid_tokens"]):
                raise ValueError(
                    "activity token columns contain null, non-numeric, or negative values"
                )
            source = self._source_from_row(row["src"], row["metadata_json"])
            group = _group_value(by, "", source)
            bucket = aggregates.setdefault(
                group,
                {
                    by: group,
                    "requests": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "source_ips": set(),
                    "source_containers": set(),
                    "source_known": False,
                },
            )
            requests = int(row["requests"])
            input_tokens = int(row["input_tokens"] or 0)
            output_tokens = int(row["output_tokens"] or 0)
            bucket["requests"] += requests
            bucket["input_tokens"] += input_tokens
            bucket["output_tokens"] += output_tokens
            totals["requests"] += requests
            totals["input_tokens"] += input_tokens
            totals["output_tokens"] += output_tokens
            if source["source_ip"] is not None:
                bucket["source_ips"].add(source["source_ip"])
            if source["source_container"] != UNKNOWN_SOURCE:
                bucket["source_containers"].add(source["source_container"])
                bucket["source_known"] = True
        usage_rows = []
        for bucket in aggregates.values():
            item = dict(bucket)
            item["source_ips"] = tuple(sorted(item["source_ips"]))
            item["source_containers"] = tuple(sorted(item["source_containers"]))
            usage_rows.append(item)
        usage_rows.sort(key=lambda item: item[by])
        return {
            "days": days,
            "by": by,
            "rows": usage_rows,
            "totals": totals,
            "known": True,
            "error": None,
        }

    def _usage_by_model(
        self,
        conn: sqlite3.Connection,
        days: int,
        by: str,
        since: int,
        now_ts: int,
    ) -> dict[str, Any]:
        rows = conn.execute(
            """
            SELECT
                model_id AS model,
                COUNT(*) AS requests,
                SUM(input_tokens) AS input_tokens,
                SUM(output_tokens) AS output_tokens,
                SUM(CASE
                    WHEN input_tokens IS NULL OR output_tokens IS NULL THEN 1
                    WHEN typeof(input_tokens) NOT IN ('integer', 'real') THEN 1
                    WHEN typeof(output_tokens) NOT IN ('integer', 'real') THEN 1
                    WHEN input_tokens < 0 OR output_tokens < 0 THEN 1
                    WHEN input_tokens != CAST(input_tokens AS INTEGER) THEN 1
                    WHEN output_tokens != CAST(output_tokens AS INTEGER) THEN 1
                    ELSE 0
                END) AS invalid_tokens
            FROM activity
            WHERE ts_created >= ? AND ts_created <= ?
            GROUP BY model_id
            ORDER BY model_id ASC
            """,
            (since, now_ts),
        ).fetchall()
        totals = {"requests": 0, "input_tokens": 0, "output_tokens": 0}
        usage_rows = []
        for row in rows:
            if int(row["invalid_tokens"]):
                raise ValueError(
                    "activity token columns contain null, non-numeric, or negative values"
                )
            item = {
                "model": row["model"],
                "requests": int(row["requests"]),
                "input_tokens": int(row["input_tokens"] or 0),
                "output_tokens": int(row["output_tokens"] or 0),
                "source_ips": (),
                "source_containers": (),
                "source_known": False,
            }
            usage_rows.append(item)
            totals["requests"] += item["requests"]
            totals["input_tokens"] += item["input_tokens"]
            totals["output_tokens"] += item["output_tokens"]
        return {
            "days": days,
            "by": by,
            "rows": usage_rows,
            "totals": totals,
            "known": True,
            "error": None,
        }

    def _connect(self) -> sqlite3.Connection:
        uri = "file:{}?mode=ro".format(quote(str(self.path.resolve()), safe="/"))
        conn = sqlite3.connect(uri, uri=True, timeout=self.deadline_ms / 1000)
        conn.row_factory = sqlite3.Row
        deadline = time.monotonic() + self.deadline_ms / 1000

        def stop_when_late() -> int:
            return 1 if time.monotonic() > deadline else 0

        conn.set_progress_handler(stop_when_late, 100)
        try:
            conn.execute("PRAGMA query_only = ON")
            # Hold one read snapshot across schema, counts, and latest-source queries.
            conn.execute("BEGIN")
        except sqlite3.Error:
            conn.close()
            raise
        return conn

    def _source_from_row(self, src: Optional[str], metadata_json: Optional[str]) -> dict[str, Any]:
        raw = _first_text(src, _metadata_source(metadata_json))
        ip = _source_ip(raw)
        if ip is None:
            return {"source_ip": None, "source_container": UNKNOWN_SOURCE}
        return {
            "source_ip": ip,
            "source_container": self.ip_containers.get(ip, "ip:{}".format(ip)),
        }


def _activity_columns(conn: sqlite3.Connection) -> set[str]:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(activity)").fetchall()}
    if not columns:
        raise ValueError("activity table not found")
    return columns


def _require_columns(columns: set[str], required: set[str]) -> None:
    missing = sorted(required - columns)
    if missing:
        raise ValueError("activity schema missing required columns: {}".format(", ".join(missing)))


def _coerce_now(now: Optional[float]) -> float:
    try:
        value = float(time.time() if now is None else now)
    except (TypeError, OverflowError) as exc:
        raise ValueError("now must be a finite timestamp") from exc
    if not math.isfinite(value):
        raise ValueError("now must be a finite timestamp")
    return value


def _metadata_source(metadata_json: Optional[str]) -> Optional[str]:
    if not metadata_json:
        return None
    try:
        metadata = json.loads(metadata_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(metadata, dict):
        return None
    for key in ("src", "source", "source_ip", "ip"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _first_text(*values: Optional[str]) -> Optional[str]:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _source_ip(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = value.strip()
    if text.startswith("ip:"):
        text = text[3:].strip()
    if not text:
        return None
    parts = text.split(".")
    if len(parts) == 4 and all(part.isdigit() and 0 <= int(part) <= 255 for part in parts):
        return text
    return None


def _group_value(by: str, model: str, source: Mapping[str, Any]) -> str:
    if by == "model":
        return model
    if by == "ip":
        return source["source_ip"] or UNKNOWN_SOURCE
    return source["source_container"]


def _unknown_usage(days: Any, by: str, error: str) -> dict[str, Any]:
    return {
        "days": days,
        "by": by,
        "rows": (),
        "totals": {"requests": None, "input_tokens": None, "output_tokens": None},
        "known": False,
        "error": error,
    }
