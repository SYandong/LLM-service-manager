# Standalone host observer deployment

Keep the existing host scanner and IP exporter. The observer reads their atomic
exports, serves fleet/history/events and writes its own SQLite history. The
archive timer preserves private compressed session summaries and minute usage
records.

## Prepare

Stage an immutable root-owned source tree and a root-owned Python 3.10 executable.
Materialize package symlinks as regular files in the staged artifact. Create a
dedicated system account with a nonlogin shell and set its actual UID in
[fleet-observer-install.example.json](fleet-observer-install.example.json).
Choose separate private state, archive and receipt directories. Configure the
export paths and specific private bind address in
[fleet-observer.example.json](fleet-observer.example.json).

```sh
python3 -m llmsvc.fleet.observer --config /path/to/observer.json --check-config
python3 deploy/fleet-observer/fleet-observer-admin.py install \
  --settings /path/to/install.json --config /path/to/observer.json --dry-run
```

The installer checks source, interpreter, account, paths and configuration.
Dry-run creates no files and runs no service actions. `--root` supports an
isolated filesystem rehearsal without controlling host services.

## Preview and transfer

Preview on a spare port with a consistent database backup and separate archive
storage. Preserve all service identities, counter baselines, watermark, minute
samples and hourly history. Copy the complete archive tree, including `usage/`
and `usage-state/`; serialize copying with the existing archive lock.

Before the final transfer, pause the previous ingestion and archive writers and
confirm their processes have stopped. Take a fresh SQLite backup, verify its
integrity and table contents, and replace the divergent preview copy with this
latest authoritative data. Verify every copied gzip/checkpoint and private file
permission before activation.

Run the reviewed installer as root without `--dry-run`. It enables and starts
both `llmsvc-fleet-observer.service` and
`llmsvc-fleet-observer-archive.timer`. The archive worker is
`llmsvc-fleet-observer-archive.service`. The installer records exact source,
configuration and unit bindings; uncertain effects stay recorded for reconciliation.

Check the fleet/history GETs, event reconnect, archive updates and the command
inside actual mounted consumer containers. Promote the shared `llm` launcher
through its directory-mounted helper; retain the fixed file-mount trampoline.
Update the server's model-library README while preserving human edits.

Retire only inventory-bound central services and their upgrade/start triggers.
Check fresh process identities, stop hooks, in-flight requests and backend absence
before stopping them. Keep scanner/IP-export timers enabled and user jobs
running. Confirm the replacement still serves reads and archives after the
central container is stopped and its autostart is disabled.

## Rollback

The installer removes only its owned units/configuration and retains data:

```sh
python3 deploy/fleet-observer/fleet-observer-admin.py rollback \
  --settings /path/to/install.json --dry-run
```

For a data-preserving rollback, stop the new observation/archive writers first.
Transfer their **latest** database and full archive tree to fresh staging, verify
contents and permissions, and then resume the previous observation runtime.
Never restore a pre-cutover backup over observations collected since cutover.
Restore client connection settings alongside the observation endpoint. Central
shared serving is a separate operation; legacy upgraders also start scheduler
and reaper services, so they are outside this rollback path.

Receipt checks reject changed source, fragments, drop-ins, ownership and unknown
action outcomes. Keep deployment receipts and previous storage until acceptance
is recorded. Record public acceptance without private user or network metadata.

<!-- Generated-By: Codex / unknown model -->
