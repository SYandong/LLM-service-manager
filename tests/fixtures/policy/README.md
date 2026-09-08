# Policy replay fixtures

`scenarios.json` (M2 intentions), `placement.json` (M3 placement) and
`pressure_recovery.json` (per-GPU TTL and sleeping recovery) contain
synthetic regression examples constructed from
`docs/DESIGN.md`. These are not the 2026-09-06 19:00 or 18:42 historical traces.
The scenario adapter in the replay tests supplies explicit known defaults for
omitted fields; production snapshots must preserve unknown values.

Historical acceptance for #15 and #18 remains pending until telemetry supplies
sanitized source snapshots with capture time, source hash and provenance. Do not
change `provenance.kind` to historical without that evidence. The policy tests
are CPU-only and must complete within five seconds.

The #16 pressure thresholds remain configurable candidates. No one-week
observation or live latency validation is implied by these offline snapshots.

<!-- Generated-By: Codex / gpt-6-astra -->
