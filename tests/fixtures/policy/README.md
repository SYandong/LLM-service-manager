# Policy replay fixtures

`scenarios.json` (M2 intentions), `placement.json` (M3 placement) and
`pressure_recovery.json` (per-GPU TTL and sleeping recovery) contain
synthetic regression examples constructed from
`docs/DESIGN.md`. These are not the 2026-09-06 19:00 or 18:42 historical traces.
The scenario adapter in the replay tests supplies explicit known defaults for
omitted fields; production snapshots must preserve unknown values.

Historical acceptance for #15 and #18 remains pending until telemetry supplies
sanitized historical-time source snapshots with capture time, source hash and
provenance. Do not
change `provenance.kind` to historical without that evidence. The policy tests
are CPU-only and must complete within five seconds.

The #16 pressure thresholds remain configurable candidates. No one-week
observation or live latency validation is implied by these offline snapshots.

`current_snapshot_expectations.json` separately attaches independently authored
policy outcomes to #49's unmodified `../telemetry/snapshot-real.json`, checked by
SHA-256. Its collection error, null host RAM and stopped models justify empty
action lists with an explicit unknown-snapshot blocker. Missing observations are
not filled, and the telemetry export's `expected_actions: null` is untouched.
These are seven hypothetical policy evaluations of **one** captured current
snapshot, not seven captures or observed commands. The 30 older journal markers
and 70 dropped records are noncontemporaneous; they neither prove completed
actions nor reconstruct the 2026-09-06 19:00/18:42 scenarios. Historical #15/#18
acceptance remains open.

<!-- Generated-By: Codex / gpt-6-astra -->
