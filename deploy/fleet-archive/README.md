# Container-local fleet session archive

The standalone Python 3.10 archiver reads the existing fleet database and writes
compressed session records into the configured directory. The separate
`llmsvc-fleet-archive.timer` runs every 60 seconds. Installation enables it with
`enable --now`; it does not restart the scheduler or issue model commands.

Copy `fleet-archive.example.json`, adjust its absolute paths and keep
`hourly_retention_days` aligned with the fleet history setting (default 180).
The JSON `_comments` and `_generated_by` fields are metadata. Install from the
repository checkout so the admin tool can copy `llmsvc/fleet/archive.py`:

```bash
python3 -I -B deploy/fleet-archive/fleet-archive-admin.py install --config /path/to/archive.json --dry-run
sudo python3 -I -B deploy/fleet-archive/fleet-archive-admin.py install --config /path/to/archive.json
sudo python3 -I -B deploy/fleet-archive/fleet-archive-admin.py rollback
```

First installation requires absent archive files and units. An identical repeat
checks the receipt and installed hashes. A changed source/config requires
rollback before reinstallation. Unknown command results retain the receipt and
block resubmission; there is no force option. Rollback stops/disables only the
archive timer and oneshot, removes unchanged owned installed files, and retains
the archive directory and all compressed logs. The receipt records the timer's
initial enabled/active state, which must be absent/inactive for first install.

The installer provisions an owned directory mode 0700. Archive logs are 0600.
The service uses `ProtectSystem=strict`, `ProtectHome=true`, `PrivateTmp=true`,
the configured database parent in `ReadOnlyPaths` and only the archive directory
in `ReadWritePaths`. The archive directory must not contain the source database.
Paths containing unit syntax or whitespace are rejected. Existing WAL databases
need their readable WAL sidecars, as checked by the archiver.

For offline staging, append `--root /tmp/fleet-archive-stage`. Logical runtime
paths stay unchanged and systemctl is never called. `--dry-run` writes no files,
directories, locks, receipts or journal entries. Installation/rollback emit a
structured result; actual container operations also log that result to journal.

<!-- Generated-By: Codex / gpt-6.1-sol -->
