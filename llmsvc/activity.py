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

from .config import canonical_ip


UNKNOWN_SOURCE = "unknown"

_ERROR_MESSAGES = {
    "deadline": "activity read deadline exceeded",
    "locked": "activity database locked",
    "schema": "activity schema missing required columns or incompatible",
    "parse": "activity parse failed: finite timestamp and non-negative integer tokens required",
    "unavailable": "activity database unavailable",
    "corrupt": "activity database corrupt or not SQLite",
    "interrupted": "activity read interrupted",
    "io": "activity database I/O failed",
    "read_failed": "activity read failed",
}


class ActivityReadError(ValueError):
    """Only fixed, redacted reasons cross the collector boundary."""

    def __init__(self, reason):
        self.reason = reason if isinstance(reason, str) and reason in _ERROR_MESSAGES else "read_failed"
        super().__init__(_ERROR_MESSAGES[self.reason])


def activity_error_reason(exc, *, deadline_expired=False):
    if deadline_expired:
        return "deadline"
    if isinstance(exc, ActivityReadError):
        return exc.reason if isinstance(exc.reason, str) and exc.reason in _ERROR_MESSAGES else "read_failed"
    if isinstance(exc, sqlite3.Error):
        # Structured exception metadata is not available on Python 3.10.
        # Match known SQLite diagnostics locally; never return their text.
        name = getattr(exc, "sqlite_errorname", "")
        name = name if isinstance(name, str) else ""
        message = str(exc)[:256].lower()
        if name.startswith(("SQLITE_BUSY", "SQLITE_LOCKED")) or message in (
            "database is locked", "database table is locked", "database schema is locked"
        ) or message.startswith(("database table is locked:", "database schema is locked:")):
            return "locked"
        if name == "SQLITE_INTERRUPT" or message == "interrupted":
            return "interrupted"
        if name.startswith(("SQLITE_CORRUPT", "SQLITE_NOTADB")) or message in (
            "database disk image is malformed", "file is not a database"
        ):
            return "corrupt"
        if name == "SQLITE_SCHEMA" or message == "database schema has changed" or message.startswith((
            "no such table:", "no such column:"
        )):
            return "schema"
        if name.startswith("SQLITE_CANTOPEN") or message == "unable to open database file":
            return "unavailable"
        if name.startswith("SQLITE_IOERR") or message == "disk i/o error":
            return "io"
        if message.startswith("could not decode to utf-8"):
            return "parse"
        return "read_failed"
    if isinstance(exc, (ValueError, TypeError, OverflowError)):
        return "parse"
    if isinstance(exc, OSError):
        return "unavailable"
    return "read_failed"


class ActivityReader:
    """Bounded read-only access to llama-swap ``activity.sqlite``."""

    def __init__(
        self,
        path: os.PathLike[str] | str,
        ip_containers: Optional[Mapping[str, str]] = None,
        deadline_ms: int = 80,
    ) -> None:
        self.path = Path(path)
        self.ip_containers: dict[str, str] = {}
        for source, container in (ip_containers or {}).items():
            source = canonical_ip(source)
            if source in self.ip_containers and self.ip_containers[source] != container:
                raise ValueError("conflicting container mappings for the same IP")
            self.ip_containers[source] = container
        self.deadline_ms = deadline_ms
        self.last_error: Optional[str] = None
        self.last_error_code: Optional[str] = None
        self._deadline_expired = False

    def _reset_error(self):
        self.last_error = self.last_error_code = None
        self._deadline_expired = False

    def _fail(self, exc):
        self.last_error_code = activity_error_reason(exc, deadline_expired=self._deadline_expired)
        self.last_error = _ERROR_MESSAGES[self.last_error_code]

    def read(self, now: Optional[float] = None) -> dict[str, dict[str, Any]]:
        """Return recent activity keyed by model id.

        Counts cover completed activity rows in the source table. When the
        database cannot be read safely, an empty mapping is returned and
        ``last_error`` describes why; callers should treat missing counts as
        unknown rather than zero.
        """

        self._reset_error()
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
                latest_by_model: dict[str, tuple[Optional[str], Optional[str]]] = {}
                for row in summary_rows:
                    # The aggregate already found the last eligible timestamp.
                    # Seek only its highest-ID row using the producer's existing
                    # (model_id, ts_created DESC, id DESC) index instead of ranking
                    # every historical row. Keep this inside the same read snapshot.
                    latest = conn.execute(
                        f"""
                        SELECT {src_expr} AS src, {metadata_expr} AS metadata_json
                        FROM activity
                        WHERE model_id IS ? AND ts_created = ?
                        ORDER BY id DESC
                        LIMIT 1
                        """,
                        (row["model_id"], row["last_used"]),
                    ).fetchone()
                    if latest is None:
                        raise ActivityReadError("read_failed")
                    latest_by_model[row["model_id"]] = (latest["src"], latest["metadata_json"])

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
            if self._deadline_expired:
                raise ActivityReadError("deadline")
            return result
        except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError) as exc:
            self._fail(exc)
            return {}

    def usage(
        self,
        days: int = 7,
        by: str = "container",
        now: Optional[float] = None,
    ) -> dict[str, Any]:
        """Return request and token usage totals grouped by source."""

        self._reset_error()
        if not isinstance(days, int) or isinstance(days, bool) or days < 1:
            self._fail(ActivityReadError("parse"))
            return _unknown_usage(days, by, "days must be a positive integer")
        if not isinstance(by, str) or by not in {"container", "ip", "model"}:
            self._fail(ActivityReadError("parse"))
            return _unknown_usage(days, by, "by must be one of: container, ip, model")

        try:
            now_ts = _coerce_now(now)
            with closing(self._connect()) as conn:
                columns = _activity_columns(conn)
                _require_columns(columns, {"id", "ts_created", "model_id"})
                if "input_tokens" not in columns or "output_tokens" not in columns:
                    raise ActivityReadError("schema")
                src_expr = "src" if "src" in columns else "NULL"
                metadata_expr = "metadata_json" if "metadata_json" in columns else "NULL"
                since = int(now_ts - days * 86400)
                if by == "model":
                    result = self._usage_by_model(conn, days, by, since, int(now_ts))
                else:
                    result = self._usage_by_source(
                        conn,
                        days,
                        by,
                        since,
                        int(now_ts),
                        src_expr,
                        metadata_expr,
                    )
                if self._deadline_expired:
                    raise ActivityReadError("deadline")
                return result
        except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError) as exc:
            self._fail(exc)
            return _unknown_usage(days, by, self.last_error)

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
                raise ActivityReadError("parse")
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
                raise ActivityReadError("parse")
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
            if time.monotonic() > deadline:
                self._deadline_expired = True
                return 1
            return 0

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
        raise ActivityReadError("schema")
    return columns


def _require_columns(columns: set[str], required: set[str]) -> None:
    missing = sorted(required - columns)
    if missing:
        raise ActivityReadError("schema")


def _coerce_now(now: Optional[float]) -> float:
    try:
        value = float(time.time() if now is None else now)
    except (TypeError, OverflowError) as exc:
        raise ActivityReadError("parse") from exc
    if not math.isfinite(value):
        raise ActivityReadError("parse")
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
    try:
        return canonical_ip(text)
    except ValueError:
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
