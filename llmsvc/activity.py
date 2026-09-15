# Generated-By: Codex / gpt-6-astra
# Generated-By: OpenCode / deepseek-v4.1-flash
"""Read-only llama-swap activity and usage aggregation."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .config import canonical_ip


UNKNOWN_SOURCE = "unknown"

# Fixed, ordered counter set shared by report rows, breakdown rows and totals.
USAGE_REPORT_COUNTS = (
    "requests",
    "errors",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "untracked_requests",
    "duration_ms",
)

DEFAULT_HOST_IPS = ("127.0.0.1", "::1")

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
        *,
        ip_containers_path: Optional[os.PathLike[str] | str] = None,
        host_ips: Optional[Any] = None,
        timezone: str = "UTC",
    ) -> None:
        self.path = Path(path)
        self.ip_containers: dict[str, str] = {}
        for source, container in (ip_containers or {}).items():
            source = canonical_ip(source)
            if source in self.ip_containers and self.ip_containers[source] != container:
                raise ValueError("conflicting container mappings for the same IP")
            self.ip_containers[source] = container
        self.ip_containers_path = None if ip_containers_path is None else Path(ip_containers_path)
        if host_ips is None:
            host_ips = DEFAULT_HOST_IPS
        if isinstance(host_ips, str) or not isinstance(host_ips, (list, tuple, set, frozenset)):
            raise ValueError("host_ips must be a sequence of IP address strings")
        self.host_ips = frozenset(canonical_ip(ip) for ip in host_ips)
        if not isinstance(timezone, str) or not timezone:
            raise ValueError("timezone must be a nonempty string")
        try:
            self._zone = ZoneInfo(timezone)
        except (KeyError, ValueError, OSError) as exc:
            raise ValueError("unknown timezone") from exc
        self.timezone = timezone
        self.deadline_ms = deadline_ms
        self.last_error: Optional[str] = None
        self.last_error_code: Optional[str] = None
        self._deadline_expired = False
        # The host export is re-read only when the file identity changes.
        self._ip_map_key: Any = object()
        self._ip_map_containers: dict[str, str] = {}
        self._ip_map_generated_at: Optional[int] = None

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

    def usage_report(
        self,
        days: int = 7,
        by: str = "user",
        now: Optional[float] = None,
    ) -> dict[str, Any]:
        """Per-user / per-model / per-day report attributed by ``client_ip``.

        Rows are grouped in one SQL pass and folded in Python. Every group
        carries the same fixed counter set, a breakdown, and min/max first/last
        timestamps. Failure keeps the report unknown rather than zero.
        """

        self._reset_error()
        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 365:
            self._fail(ActivityReadError("parse"))
            return _unknown_usage_report(days, by, self.timezone, "days must be an integer in 1..365")
        if not isinstance(by, str) or by not in {"user", "model", "day"}:
            self._fail(ActivityReadError("parse"))
            return _unknown_usage_report(days, by, self.timezone, "by must be one of: user, model, day")

        try:
            now_ts = _coerce_now(now)
            with closing(self._connect()) as conn:
                columns = _activity_columns(conn)
                _require_columns(columns, {"id", "ts_created", "model_id"})
                if "input_tokens" not in columns or "output_tokens" not in columns:
                    raise ActivityReadError("schema")
                metadata_expr = "metadata_json" if "metadata_json" in columns else "NULL"
                status_expr = "resp_status_code" if "resp_status_code" in columns else "NULL"
                error_expr = "error_msg" if "error_msg" in columns else "NULL"
                duration_expr = "duration_ms" if "duration_ms" in columns else "NULL"
                since = int(now_ts - days * 86400)
                until = int(now_ts)
                file_containers, generated_at = self._file_containers()
                rows = conn.execute(
                    f"""
                    SELECT
                        model_id AS model,
                        {metadata_expr} AS metadata_json,
                        ts_created AS ts,
                        COUNT(*) AS requests,
                        SUM(input_tokens) AS input_tokens,
                        SUM(output_tokens) AS output_tokens,
                        SUM(CASE
                            WHEN {status_expr} >= 400 THEN 1
                            WHEN {error_expr} IS NOT NULL AND {error_expr} != '' THEN 1
                            ELSE 0
                        END) AS errors,
                        SUM(CASE
                            WHEN ({status_expr} IS NULL OR {status_expr} < 400)
                                 AND input_tokens = 0 AND output_tokens = 0 THEN 1
                            ELSE 0
                        END) AS untracked_requests,
                        SUM({duration_expr}) AS duration_ms,
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
                    GROUP BY model_id, metadata_json, ts_created
                    """,
                    (since, until),
                ).fetchall()
                report = self._build_usage_report(
                    days, by, since, until, file_containers, generated_at, rows
                )
                if self._deadline_expired:
                    raise ActivityReadError("deadline")
                return report
        except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError) as exc:
            self._fail(exc)
            return _unknown_usage_report(days, by, self.timezone, self.last_error)

    def _build_usage_report(
        self,
        days: int,
        by: str,
        since: int,
        until: int,
        file_containers: Mapping[str, str],
        generated_at: Optional[int],
        rows: Any,
    ) -> dict[str, Any]:
        if self.ip_containers and file_containers:
            map_source = "config+file"
        elif self.ip_containers:
            map_source = "config"
        elif file_containers:
            map_source = "file"
        else:
            map_source = "none"

        primary: dict[str, dict[str, Any]] = {}
        totals = _empty_counts()
        for row in rows:
            if int(row["invalid_tokens"]):
                raise ActivityReadError("parse")
            metadata = _metadata_object(row["metadata_json"])
            client_ip = metadata.get("client_ip") if metadata else None
            user, user_kind = self._attribution(client_ip, file_containers)
            model = row["model"]
            input_tokens = int(row["input_tokens"] or 0)
            output_tokens = int(row["output_tokens"] or 0)
            counts = {
                "requests": int(row["requests"]),
                "errors": int(row["errors"] or 0),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "untracked_requests": int(row["untracked_requests"] or 0),
                "duration_ms": int(row["duration_ms"] or 0),
            }
            seen = int(row["ts"])
            _accumulate(totals, counts)

            if by == "user":
                key, kind = user, user_kind
                other, other_kind = model, "model"
            elif by == "model":
                key, kind = model, "model"
                other, other_kind = user, user_kind
            else:
                key, kind = _local_day(seen, self._zone), "day"
                other, other_kind = model, "model"

            bucket = primary.get(key)
            if bucket is None:
                bucket = primary[key] = {
                    "kind": kind,
                    "counts": _empty_counts(),
                    "first_seen": seen,
                    "last_seen": seen,
                    "breakdown": {},
                }
            _accumulate(bucket["counts"], counts)
            bucket["first_seen"] = min(bucket["first_seen"], seen)
            bucket["last_seen"] = max(bucket["last_seen"], seen)
            entry = bucket["breakdown"].get(other)
            if entry is None:
                entry = bucket["breakdown"][other] = {"kind": other_kind, "counts": _empty_counts()}
            _accumulate(entry["counts"], counts)

        report_rows = []
        for key, bucket in primary.items():
            item = dict(bucket["counts"])
            item[by] = key
            item["kind"] = bucket["kind"]
            item["first_seen"] = bucket["first_seen"]
            item["last_seen"] = bucket["last_seen"]
            other = "model" if by in ("user", "day") else "user"
            breakdown = []
            for other_key, entry in bucket["breakdown"].items():
                sub = dict(entry["counts"])
                sub[other] = other_key
                sub["kind"] = entry["kind"]
                breakdown.append(sub)
            breakdown.sort(key=lambda item: (-item["requests"], item[other]))
            item["breakdown"] = breakdown
            report_rows.append(item)
        if by == "day":
            report_rows.sort(key=lambda item: item["day"])
        else:
            report_rows.sort(key=lambda item: (-item["requests"], item[by]))

        return {
            "days": days,
            "by": by,
            "since": since,
            "until": until,
            "known": True,
            "error": None,
            "timezone": self.timezone,
            "attribution": {
                "mode": "client_ip",
                "map_source": map_source,
                "map_updated_at": generated_at,
                "mapped_ips": len(set(self.ip_containers) | set(file_containers)),
            },
            "rows": report_rows,
            "totals": totals,
        }

    def _attribution(self, client_ip: Any, file_containers: Mapping[str, str]) -> tuple[str, str]:
        if not isinstance(client_ip, str) or not client_ip.strip():
            return "unattributed", "unattributed"
        try:
            ip = canonical_ip(client_ip.strip())
        except ValueError:
            return "unattributed", "unattributed"
        if ip in self.host_ips:
            return "host", "host"
        container = self.ip_containers.get(ip)
        if container is None:
            container = file_containers.get(ip)
        if container is not None:
            return container, "container"
        return "ip:" + ip, "ip"

    def _file_containers(self) -> tuple[dict[str, str], Optional[int]]:
        """Lazily parse the host IP->container export, cached by file identity."""

        if self.ip_containers_path is None:
            return {}, None
        try:
            stat = os.stat(self.ip_containers_path)
            key: Any = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            key = None
        if key is not None and key == self._ip_map_key:
            return self._ip_map_containers, self._ip_map_generated_at

        containers: dict[str, str] = {}
        generated_at: Optional[int] = None
        if key is not None:
            try:
                with self.ip_containers_path.open(encoding="utf-8") as stream:
                    payload = json.load(stream)
                if isinstance(payload, dict):
                    generated = payload.get("generated_at")
                    if isinstance(generated, int) and not isinstance(generated, bool):
                        generated_at = generated
                    raw = payload.get("containers")
                    if isinstance(raw, dict):
                        for source, name in raw.items():
                            if not isinstance(name, str) or not name.strip():
                                continue
                            try:
                                source = canonical_ip(source)
                            except ValueError:
                                continue
                            containers[source] = name
            except (OSError, ValueError, TypeError):
                containers, generated_at = {}, None
        self._ip_map_key = key
        self._ip_map_containers = containers
        self._ip_map_generated_at = generated_at
        return containers, generated_at

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
        raw = _first_text(src, _metadata_source(metadata_json), _metadata_client_ip(metadata_json))
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


def _metadata_object(metadata_json: Optional[str]) -> dict[str, Any]:
    if not metadata_json:
        return {}
    try:
        metadata = json.loads(metadata_json)
    except (TypeError, ValueError):
        return {}
    return metadata if isinstance(metadata, dict) else {}


def _metadata_client_ip(metadata_json: Optional[str]) -> Optional[str]:
    value = _metadata_object(metadata_json).get("client_ip")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _empty_counts() -> dict[str, int]:
    return {name: 0 for name in USAGE_REPORT_COUNTS}


def _accumulate(target: dict[str, int], counts: Mapping[str, int]) -> None:
    for name in USAGE_REPORT_COUNTS:
        target[name] += counts[name]


def _local_day(ts: int, zone: ZoneInfo) -> str:
    return datetime.fromtimestamp(ts, zone).strftime("%Y-%m-%d")


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


def _unknown_usage_report(days: Any, by: Any, timezone: str, error: str) -> dict[str, Any]:
    return {
        "days": days,
        "by": by,
        "since": None,
        "until": None,
        "known": False,
        "error": error,
        "timezone": timezone,
        "attribution": None,
        "rows": [],
        "totals": {name: None for name in USAGE_REPORT_COUNTS},
    }
