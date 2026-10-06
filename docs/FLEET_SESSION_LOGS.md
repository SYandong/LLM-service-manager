# Fleet session logs

The archive worker keeps a gzip-compressed JSON summary for each observed LLM
process session and a chronological gzip JSONL usage stream from the existing
minute samples. It updates running sessions every minute and keeps completed
sessions. The default directory is `/var/lib/llmsvc/fleet-sessions`.

A session follows a server process from its recorded start time to the first
complete inventory that reports it absent. Its filename hashes the full process
identity. Restarting a server creates a new session; rediscovering the same
process updates its existing file.

## Statistics

Each file contains process/model ownership, GPU metadata, lifecycle observations,
hourly usage and observed request and token totals. Input, output, total and
cached tokens are separate fields. Total tokens are input plus output; cached
tokens are recorded separately. Latest reported counters are also retained with
their own timestamps and counter epochs, without adding them to observed totals.

The first counter reading establishes a baseline. Collection gaps, resets and
unsampled startup/exit traffic mean these totals describe observed usage.
Unavailable counters are `null`. Ollama currently provides loaded-model and
activity observations without request/token counters; its session follows the
server process. `loaded_models` keeps the last observed model set,
`loaded_models_at` its observation time, and `loaded_models_history` its recent
changes. A failed scrape preserves the prior set and timestamp; `null` means no
valid observation and `[]` means an observed empty server. Model histories keep
the latest 32 changes and record when older entries have been omitted.

The archive retains captured hours after fleet database cleanup. It records
history that may have expired before the first archive or during a prolonged
archive outage. Keep `hourly_retention_days` equal to the fleet database's
configured retention. Logs contain numeric aggregates and selected metadata;
prompts, responses, command lines, model paths and API addresses are omitted.

## Read a session

```sh
gzip -dc /var/lib/llmsvc/fleet-sessions/SESSION_ID.json.gz | python3 -m json.tool
```

`usage.input_tokens`, `usage.output_tokens`, `usage.total_tokens` and
`usage.cached_tokens` are the archived observed totals. `hourly` contains UTC
hour keys for later daily or monthly aggregation. `latest_reported_counters`
contains the server's latest captured baselines, with `value`, `ts` and `epoch`.
`coverage.missing_intervals` records source history that may have expired before
archive capture. Lifecycle timestamps are Unix seconds; `state` is `running`
or `ended`. The summary schema remains version 1, including files written before
sampled usage was introduced.

## Minute usage records

Daily UTC files live at `usage/YYYY-MM-DD/SESSION_ID.jsonl.gz`. `SESSION_ID` is
the same full process-identity SHA-256 used by the summary filename. Each line
is one JSON event, ordered by observation time within that file. A minute with
no new source observation produces no usage record. The stream follows a server
process, independently of its clients or conversations.

```sh
gzip -dc /var/lib/llmsvc/fleet-sessions/usage/YYYY-MM-DD/SESSION_ID.jsonl.gz
```

Every event has `schema_version: 1`, a stable `event_id`, `type`, an ISO 8601 UTC
`timestamp`, its exact Unix-seconds `ts`, `session_id`, `instance_id`, `engine`,
`model` and `model_basis`. Model labels use `session_metadata_at_export`: source
minute rows do not store historical model labels, so this label describes the
session metadata when exported. Previously published labels remain unchanged
on retries. Unsafe path/address labels are null, and Ollama has no single model
label for its server session.

| Event type | Additional fields and meaning |
|---|---|
| `session_started` | First observed time, `process_started_at` and `startup_unobserved_seconds`; no inferred startup usage. |
| `usage` | One retained `fleet_samples` row, with `usage`, `interval_seconds`, `observed_seconds`, `scrape_ok`, `gap`, `counter_reset`, `baseline` and a numeric-source `source_sample_sha256`. |
| `session_ended` | Complete inventory observed absence; `last_seen` and `final_counters_sampled: false`. This event contains no usage counts. |
| `session_reobserved` | The same process identity was seen after observed absence; `previous_ended_at` and `observation_basis` describe the retained evidence. |
| `source_retention_gap` | `start_at`, `end_at` and `reason` mark a range where raw samples may have expired before initial export or during an archive outage. They do not synthesize missing minutes. |

`usage` contains `requests`, `input_tokens`, `output_tokens`, `total_tokens` and
`cached_tokens`. Total tokens are input plus output, excluding the separate
cached field. Unknown counters remain null; total is null if either input or
output is null. A true initial sample has `interval_seconds: 0` and
`baseline: true`; its deltas are null because it only establishes counters.
The earliest retained sample after cleanup can already contain valid deltas,
which are preserved.

Other deltas are copied from the source endpoint. The source assigns zero when
establishing or resetting an individual counter, and exposes only an aggregate
`counter_reset` flag. A reset zero does not prove absence of traffic. Per-counter
baseline/reset details cannot be recovered from old minute rows. Failed scrapes
keep null deltas. A later successful delta can span earlier failed scrapes;
`interval_seconds` is the interval between source observations, rather than
proof that every token belongs to that minute. `gap` marks a source interval
over five minutes. `observed_seconds` describes activity coverage and does not
certify token completeness.

Initial export backfills only raw samples still present in the fleet database
(normally 14 days). Hourly totals may reach further back and remain in the
summary; they are never expanded into fabricated minute records. Expired raw
samples cannot be reconstructed from latest counters. Already exported daily
files survive source cleanup and are not automatically deleted.

## Storage

Files use compact JSON and gzip. Existing hourly aggregates are replaced by hour
key, so repeated runs do not double-count usage. Completed files that have not
changed are not rewritten. Logs have mode `0600` and the archive directory has
mode `0700`. Archives are retained until the operator removes them.

A single writer lock and atomic publication protect retries and interrupted
writes. Resume metadata lives separately at `usage-state/SESSION_ID.json.gz`;
it is a checkpoint, rather than a usage total. Source reads include only the
last sampled endpoint and newer rows for that session, within shared row, byte
and time bounds. The consistent database read ends before gzip publication.

Daily files are merged by event ID, then published using a temporary gzip file,
file fsync, atomic replacement and directory fsync. The checkpoint advances
only after its records are durable. An interrupted run can merge already
published records without duplicating them. Referenced start, last-sample, end
and retention-gap files are checked even after their raw source rows expire.
Corrupt summaries, checkpoints or daily files encountered during export, missing
checkpoint-referenced records and source regressions produce errors while
retaining existing files. Older daily files outside those checks remain retained;
export does not perform a complete historical integrity scan each minute.

All daily files and checkpoints are `0600`; their directories are `0700`.
The source database is opened for SQL reads; the service mounts it read-only
and permits writes only to the archive directory. Logs contain usage metadata
without conversation history.

## Installation

Copy `deploy/fleet-archive/fleet-archive.example.json` to a private configuration
file and set `database`, `directory` and `hourly_retention_days`. From the checkout,
preview and install as an operator:

```sh
sudo python3 -I -B deploy/fleet-archive/fleet-archive-admin.py install --config /absolute/archive.json --dry-run
sudo python3 -I -B deploy/fleet-archive/fleet-archive-admin.py install --config /absolute/archive.json
systemctl is-enabled llmsvc-fleet-archive.timer
systemctl status llmsvc-fleet-archive.timer
journalctl -u llmsvc-fleet-archive.service
```

The worker uses only Python 3.10's standard library and runs independently of
the scheduler. Installation enables the timer, so logging resumes after reboot.
To update an existing installation after worker source changes, use the existing
rollback command and then install from the new checkout. This preserves all
summary, JSONL and checkpoint data. No timer or service configuration change is
needed for sampled records. Rolling back the worker binary lets the previous
summary-only worker continue reading the unchanged summary format, while
retaining sampled records for a later upgrade.
To stop logging and remove the owned installation while keeping all archives:

```sh
sudo python3 -I -B deploy/fleet-archive/fleet-archive-admin.py rollback --dry-run
sudo python3 -I -B deploy/fleet-archive/fleet-archive-admin.py rollback
```

The worker supports `--dry-run`: it validates and reports planned archive
updates without creating archive directories, locks or files. WAL-mode source
reads require the existing readable WAL companion files; do not bypass SQLite
WAL consistency to preview a running database.

<!-- Generated-By: Codex / unknown model -->
<!-- Generated-By: Codex / gpt-6.1-sol -->
