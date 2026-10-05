# Generated-By: Codex / gpt-6.1-sol
"""Independent SQLite WAL history, with one serialized connection and writer."""

import json
import math
import sqlite3
import threading
from pathlib import Path

from llmsvc.fleet.ingest import MAX_SAMPLE_GAP_SECONDS, sample, validate_snapshot

SCHEMA = """
CREATE TABLE fleet_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE fleet_instances(
 id TEXT PRIMARY KEY, engine TEXT, engine_version TEXT, container TEXT, host INTEGER,
 managed_by TEXT, pid INTEGER, started_at REAL, model TEXT, model_path TEXT, bind TEXT,
 port INTEGER, gpus_json TEXT, argv_redacted TEXT, first_seen REAL, last_seen REAL,
 ended_at REAL, metadata_json TEXT NOT NULL, state_json TEXT NOT NULL);
CREATE TABLE fleet_samples(
 instance_id TEXT NOT NULL, ts REAL NOT NULL, running REAL, waiting REAL, kv_perc REAL,
 d_requests REAL, d_gen_tokens REAL, d_prompt_tokens REAL, d_cached_tokens REAL,
 active INTEGER, scrape_ok INTEGER NOT NULL, interval_seconds REAL NOT NULL,
 observed_seconds REAL NOT NULL, active_seconds REAL NOT NULL, gap INTEGER NOT NULL,
 counter_reset INTEGER NOT NULL, PRIMARY KEY(instance_id, ts));
CREATE INDEX fleet_samples_ts ON fleet_samples(ts);
CREATE TABLE fleet_hourly(
 instance_id TEXT NOT NULL, hour_ts REAL NOT NULL, active_minutes REAL NOT NULL,
 requests REAL, gen_tokens REAL, prompt_tokens REAL, cached_tokens REAL,
 samples INTEGER NOT NULL, observed_seconds REAL NOT NULL,
 PRIMARY KEY(instance_id, hour_ts));
CREATE INDEX fleet_hourly_ts ON fleet_hourly(hour_ts);
CREATE TABLE fleet_claims(
 id TEXT PRIMARY KEY, instance_id TEXT NOT NULL, container TEXT NOT NULL, model TEXT,
 until REAL NOT NULL, reason TEXT NOT NULL, created_by_container TEXT NOT NULL,
 created_at REAL NOT NULL, revoked_at REAL);
CREATE INDEX fleet_claims_instance ON fleet_claims(instance_id, until);
CREATE UNIQUE INDEX fleet_claims_live ON fleet_claims(instance_id) WHERE revoked_at IS NULL;
CREATE TABLE fleet_gpu_samples(
 ts REAL NOT NULL, gpu INTEGER NOT NULL, used_mib REAL, util_percent REAL,
 llm_mib REAL, other_mib REAL, PRIMARY KEY(ts, gpu));
CREATE INDEX fleet_gpu_samples_ts ON fleet_gpu_samples(ts);
PRAGMA user_version=1;
"""

SAMPLE_COLUMNS = ("instance_id", "ts", "running", "waiting", "kv_perc", "d_requests",
                  "d_gen_tokens", "d_prompt_tokens", "d_cached_tokens", "active", "scrape_ok",
                  "interval_seconds", "observed_seconds", "active_seconds", "gap", "counter_reset")


def dumps(value):
    return json.dumps(value, allow_nan=False, separators=(",", ":"))


class FleetStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self._db = None
        self._writable = False
        self._closed = False

    def _connection(self, *, create=False):
        if self._closed:
            raise sqlite3.ProgrammingError("fleet_store_closed")
        if self._db is not None and (not create or self._writable):
            return self._db
        if not create and not self.path.exists():
            return None
        if self._db is not None:
            self._db.close()
            self._db = None
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        uri = self.path.resolve().as_uri() + ("?mode=rwc" if create else "?mode=ro")
        db = sqlite3.connect(uri, uri=True, timeout=2, check_same_thread=False)
        db.row_factory = sqlite3.Row
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0 and create and not db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                db.executescript(SCHEMA)
            elif version != 1:
                raise sqlite3.DatabaseError("unsupported_fleet_database_schema")
            if create:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=FULL")
            else:
                db.execute("PRAGMA query_only=ON")
        except Exception:
            db.close()
            raise
        self._db = db
        self._writable = create
        return db

    def close(self):
        with self.lock:
            if self._db is not None:
                self._db.close()
                self._db = None
            self._closed = True

    def metadata(self):
        with self.lock:
            db = self._connection()
            if db is None:
                return {}
            return {row["key"]: json.loads(row["value"]) for row in db.execute("SELECT * FROM fleet_meta")}

    @staticmethod
    def _meta(db, key, value):
        db.execute("INSERT INTO fleet_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, dumps(value)))

    def ingest(self, snapshot, config, now):
        validate_snapshot(snapshot)
        ts = snapshot["generated_at"]
        with self.lock:
            db = self._connection(create=True)
            # The source watermark, baselines, raw rows and rollup commit together.
            db.execute("BEGIN IMMEDIATE")
            try:
                old = db.execute("SELECT value FROM fleet_meta WHERE key='generated_at'").fetchone()
                if old is not None and ts <= json.loads(old[0]):
                    db.rollback()
                    return False
                services = snapshot["services"]
                ids = {service["id"] for service in services}
                for service in services:
                    previous = db.execute("SELECT * FROM fleet_instances WHERE id=?", (service["id"],)).fetchone()
                    if previous is not None and (previous["pid"] != service["pid"] or previous["started_at"] != service["started_at"]
                                                  or previous["container"] != service["container"] or previous["engine"] != service["engine"]):
                        raise ValueError("fleet_instance_identity_changed")
                    old_state = None if previous is None else json.loads(previous["state_json"])
                    if previous is not None and previous["ended_at"] is not None:
                        old_state.update(observed=False, idle_since=None, idle_observed_seconds=0)
                    row, state = sample(service, old_state, ts,
                                        snapshot.get("sample_interval_seconds", 60))
                    columns = ("id", "engine", "engine_version", "container", "host", "managed_by", "pid", "started_at",
                               "model", "model_path", "bind", "port", "gpus_json", "argv_redacted", "first_seen", "last_seen",
                               "ended_at", "metadata_json", "state_json")
                    values = [service.get(key) for key in columns[:12]] + [dumps(service["gpus"]), service.get("argv_redacted"),
                        ts if previous is None else previous["first_seen"], ts, None, dumps(service), dumps(state)]
                    db.execute("INSERT INTO fleet_instances(" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns)
                               + ") ON CONFLICT(id) DO UPDATE SET " + ",".join(key + "=excluded." + key for key in columns[1:]), values)
                    db.execute("INSERT INTO fleet_samples(" + ",".join(SAMPLE_COLUMNS) + ") VALUES(" + ",".join("?" for _ in SAMPLE_COLUMNS) + ")",
                               [row[key] for key in SAMPLE_COLUMNS])
                    self._hourly(db, row)
                # Missing discovery is not positive process-exit evidence.
                for instance in db.execute("SELECT * FROM fleet_instances WHERE ended_at IS NULL").fetchall():
                    if instance["id"] not in ids:
                        if snapshot.get("inventory_complete") is True:
                            db.execute("UPDATE fleet_instances SET ended_at=? WHERE id=?", (ts, instance["id"]))
                        else:
                            missing = json.loads(instance["metadata_json"])
                            missing["scrape"] = {"ok": False, "error": "discovery_unknown"}
                            row, state = sample(missing, json.loads(instance["state_json"]), ts, snapshot.get("sample_interval_seconds", 60))
                            db.execute("UPDATE fleet_instances SET state_json=? WHERE id=?", (dumps(state), instance["id"]))
                            db.execute("INSERT INTO fleet_samples(" + ",".join(SAMPLE_COLUMNS) + ") VALUES(" + ",".join("?" for _ in SAMPLE_COLUMNS) + ")",
                                       [row[key] for key in SAMPLE_COLUMNS])
                            self._hourly(db, row)
                llm = {}
                other = {}
                for service in services:
                    for gpu in service["gpus"]:
                        if gpu.get("used_mib") is not None:
                            llm[gpu["index"]] = llm.get(gpu["index"], 0) + gpu["used_mib"]
                for gpu in snapshot["other_gpu_processes"]:
                    if gpu.get("used_mib") is not None:
                        other[gpu["gpu"]] = other.get(gpu["gpu"], 0) + gpu["used_mib"]
                for gpu in snapshot["gpus"]:
                    known = snapshot.get("gpu_attribution_complete") is True
                    db.execute("INSERT INTO fleet_gpu_samples VALUES(?,?,?,?,?,?)", (ts, gpu["index"], gpu.get("used_mib"),
                        gpu.get("util_percent"), llm.get(gpu["index"], 0) if known else None,
                        other.get(gpu["index"], 0) if known else None))
                self._meta(db, "generated_at", ts)
                self._meta(db, "snapshot", snapshot)
                retained = db.execute("SELECT value FROM fleet_meta WHERE key='last_retention'").fetchone()
                if retained is None or now - json.loads(retained[0]) >= 3600:
                    # Whole-hour cuts keep the remaining raw and hourly split exact.
                    cutoff = math.floor((now - config.fleet_raw_retention_days * 86400) / 3600) * 3600
                    db.execute("DELETE FROM fleet_samples WHERE ts<?", (cutoff,))
                    db.execute("DELETE FROM fleet_gpu_samples WHERE ts<?", (cutoff,))
                    db.execute("DELETE FROM fleet_hourly WHERE hour_ts<?", (now - config.fleet_hourly_retention_days * 86400,))
                    self._meta(db, "raw_cutoff", cutoff)
                    self._meta(db, "last_retention", now)
                db.commit()
                return True
            except Exception:
                db.rollback()
                raise

    @staticmethod
    def _hourly(db, row):
        ts = row["ts"]
        # Interval coverage is split at hour boundaries; counters belong to their
        # observed endpoint. Re-reading an export never adds another contribution.
        hours = {math.floor(ts / 3600) * 3600: [0, 0, 1]}
        start = ts - row["observed_seconds"]
        while start < ts:
            hour = math.floor(start / 3600) * 3600
            seconds = min(ts, hour + 3600) - start
            contribution = hours.setdefault(hour, [0, 0, 0])
            contribution[0] += seconds / 60 if row["active"] else 0
            contribution[1] += seconds
            start += seconds
        endpoint = math.floor(ts / 3600) * 3600
        for hour, (active_minutes, observed_seconds, samples) in hours.items():
            counters = [row["d_" + key] if hour == endpoint else None for key in ("requests", "gen_tokens", "prompt_tokens", "cached_tokens")]
            db.execute("""INSERT INTO fleet_hourly VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(instance_id,hour_ts) DO UPDATE SET
                active_minutes=active_minutes+excluded.active_minutes,
                observed_seconds=observed_seconds+excluded.observed_seconds,samples=samples+excluded.samples,
                requests=CASE WHEN requests IS NULL AND excluded.requests IS NULL THEN NULL ELSE COALESCE(requests,0)+COALESCE(excluded.requests,0) END,
                gen_tokens=CASE WHEN gen_tokens IS NULL AND excluded.gen_tokens IS NULL THEN NULL ELSE COALESCE(gen_tokens,0)+COALESCE(excluded.gen_tokens,0) END,
                prompt_tokens=CASE WHEN prompt_tokens IS NULL AND excluded.prompt_tokens IS NULL THEN NULL ELSE COALESCE(prompt_tokens,0)+COALESCE(excluded.prompt_tokens,0) END,
                cached_tokens=CASE WHEN cached_tokens IS NULL AND excluded.cached_tokens IS NULL THEN NULL ELSE COALESCE(cached_tokens,0)+COALESCE(excluded.cached_tokens,0) END
                """, (row["instance_id"], hour, active_minutes, *counters, samples, observed_seconds))

    def instances(self, *, live=True):
        with self.lock:
            db = self._connection()
            if db is None:
                return []
            rows = db.execute("SELECT * FROM fleet_instances" + (" WHERE ended_at IS NULL" if live else "") + " ORDER BY id").fetchall()
            return [dict(row, metadata=json.loads(row["metadata_json"]), state=json.loads(row["state_json"])) for row in rows]

    def instance(self, instance_id):
        with self.lock:
            db = self._connection()
            row = None if db is None else db.execute("SELECT * FROM fleet_instances WHERE id=?", (instance_id,)).fetchone()
            return None if row is None else dict(row, metadata=json.loads(row["metadata_json"]), state=json.loads(row["state_json"]))

    def claims(self, now):
        with self.lock:
            db = self._connection()
            if db is None:
                return {}
            return {row["instance_id"]: dict(row, service_id=row["instance_id"]) for row in db.execute("SELECT * FROM fleet_claims WHERE revoked_at IS NULL AND until>?", (now,))}

    def claim(self, claim_id):
        with self.lock:
            db = self._connection()
            row = None if db is None else db.execute("SELECT * FROM fleet_claims WHERE id=?", (claim_id,)).fetchone()
            return None if row is None else dict(row, service_id=row["instance_id"])

    def put_claim(self, claim):
        with self.lock:
            db = self._connection(create=True)
            with db:
                # Replacement preserves historical declarations and one effective
                # declaration per instance, including expired non-revoked rows.
                db.execute("UPDATE fleet_claims SET revoked_at=? WHERE instance_id=? AND revoked_at IS NULL", (claim["created_at"], claim["instance_id"]))
                db.execute("INSERT INTO fleet_claims VALUES(?,?,?,?,?,?,?,?,?)", tuple(claim[key] for key in
                    ("id", "instance_id", "container", "model", "until", "reason", "created_by_container", "created_at", "revoked_at")))

    def revoke_claim(self, claim_id, now):
        with self.lock:
            db = self._connection(create=True)
            with db:
                db.execute("UPDATE fleet_claims SET revoked_at=COALESCE(revoked_at,?) WHERE id=?", (now, claim_id))

    def window(self, start, end, *, include_end=False):
        """Aggregate in SQLite, never load the raw time series to serve a fleet GET."""
        with self.lock:
            db = self._connection()
            if db is None:
                return {}
            first = math.ceil(start / 3600) * 3600
            last = math.floor(end / 3600) * 3600
            result = {}
            if first < last:
                for row in db.execute("""SELECT instance_id,SUM(active_minutes) active_minutes,
                    SUM(observed_seconds) observed_seconds,SUM(requests) requests,SUM(gen_tokens) gen_tokens,
                    SUM(prompt_tokens) prompt_tokens,SUM(cached_tokens) cached_tokens
                    FROM fleet_hourly WHERE hour_ts>=? AND hour_ts<? GROUP BY instance_id""", (first, last)):
                    result[row["instance_id"]] = dict(row)
            boundaries = [(start, min(first, end)), (max(last, first, start), end)]
            if first >= end:
                boundaries = [(start, end)]
            for lower, upper in boundaries:
                inclusive = include_end and upper == end
                if lower > upper or (lower == upper and not inclusive):
                    continue
                # Keep coverage exactly clipped. Only the final counter endpoint
                # may be inclusive; an hour boundary belongs to the next bucket.
                counter_upper = math.nextafter(upper, math.inf) if inclusive else upper
                for row in db.execute("""SELECT instance_id,
                    SUM(CASE WHEN active=1 THEN MAX(0,MIN(ts,?)-MAX(ts-observed_seconds,?))/60 ELSE 0 END) active_minutes,
                    SUM(MAX(0,MIN(ts,?)-MAX(ts-observed_seconds,?))) observed_seconds,
                    SUM(CASE WHEN ts>=? AND ts<? THEN d_requests END) requests,
                    SUM(CASE WHEN ts>=? AND ts<? THEN d_gen_tokens END) gen_tokens,
                    SUM(CASE WHEN ts>=? AND ts<? THEN d_prompt_tokens END) prompt_tokens,
                    SUM(CASE WHEN ts>=? AND ts<? THEN d_cached_tokens END) cached_tokens
                    FROM fleet_samples WHERE ts>=? AND ts<? GROUP BY instance_id""",
                    (upper, lower, upper, lower, lower, counter_upper, lower, counter_upper, lower, counter_upper,
                     lower, counter_upper, lower, upper + MAX_SAMPLE_GAP_SECONDS)):
                    target = result.setdefault(row["instance_id"], {"instance_id": row["instance_id"]})
                    for key in ("active_minutes", "observed_seconds", "requests", "gen_tokens", "prompt_tokens", "cached_tokens"):
                        value = row[key]
                        if value is not None:
                            target[key] = (target.get(key) or 0) + value
            return result

    def hourly(self, start, end, *, instance_id=None):
        with self.lock:
            db = self._connection()
            if db is None:
                return []
            sql = "SELECT * FROM fleet_hourly WHERE hour_ts>=? AND hour_ts<?"
            params = [start, end]
            if instance_id is not None:
                sql += " AND instance_id=?"
                params.append(instance_id)
            return [dict(row) for row in db.execute(sql + " ORDER BY hour_ts", params)]

    def raw(self, instance_id, start, end):
        with self.lock:
            db = self._connection()
            if db is None:
                return []
            # The host cadence is >=30 seconds; cap a malicious/foreign database.
            return [dict(row) for row in db.execute("SELECT * FROM fleet_samples WHERE instance_id=? AND ts>=? AND ts<=? ORDER BY ts LIMIT 10081",
                                                   (instance_id, start, end))]
