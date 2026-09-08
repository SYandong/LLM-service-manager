# Sanitized replay export

Export a captured state and optional JSONL journal capture:

```sh
python -m llmsvc.collectors.export --state state.json --journal journal.jsonl \
  --output replay-input.json --dry-run
python -m llmsvc.collectors.export --state state.json --journal journal.jsonl \
  --output replay-input.json
```

`--state-url http://127.0.0.1:8001/v1/state` can replace `--state`. The HTTP
operation is GET only. Journal input is the JSONL format produced by
`journalctl -o json`; capture start/end filters belong to the operator/ops lane.
No journal process or lifecycle action is launched by the exporter itself.

The output retains numeric policy inputs, unknowns, model states and protection
flags. Model/source/GPU/PID labels are consistently pseudonymized. Arbitrary
fields, raw logs, command lines, paths, credentials and free-text reasons are
omitted. Supported journal records become only a timestamp, model alias, GPU
index and action marker. A text marker is evidence that a word appeared in a
message, not proof that the action completed. Unsupported lines are counted in
`journal_dropped`; they are never silently represented as successful actions.

`expected_actions` is deliberately null. The policy lane must select an actual
scenario and author/check expected actions before adding it to the six-scenario
replay suite. A snapshot captured today and historical journal messages are not
contemporaneous observations; the export does not reconstruct missing historical
GPU state. Synthetic test inputs remain synthetic.

`--dry-run` reads and validates input but creates no output file or temporary
file. Normal output uses atomic replacement; replacement failure retains an
existing output and removes the temporary file. Each input is limited to 4 MiB.

`tests/fixtures/telemetry/snapshot-real.json` is a sanitized live read-only
snapshot. Its capture metadata identifies the historical journal date range and
number of unsupported records; it is a replay input, not a completed policy test.

<!-- Generated-By: Codex / gpt-6-astra -->
