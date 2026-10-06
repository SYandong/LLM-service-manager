# Fleet session logs

The archive worker keeps a gzip-compressed JSON file for each observed LLM
process session. It updates running sessions every minute and keeps completed
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
server process, including changes to its loaded models.

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
or `ended`.

## Storage

Files use compact JSON and gzip. Existing hourly aggregates are replaced by hour
key, so repeated runs do not double-count usage. Completed files that have not
changed are not rewritten. Logs have mode `0600` and the archive directory has
mode `0700`. Archives are retained until the operator removes them.

A single writer lock and atomic publication protect retries and interrupted
writes. Corrupt archives and source regressions produce an error while retaining
the previous file. The source database is opened for SQL reads; the service
mounts the source read-only and permits writes only to the archive directory.

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
