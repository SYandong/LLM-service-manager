# Thin launcher integration (#14)

`deploy/vllm-launch` is a Python 3.10 standard-library client for the scheduler
place/confirm/release protocol. It preserves the positional launch interface:

```sh
python3 deploy/vllm-launch 0.5 vllm-example --config /path/to/launcher.json \
  --dry-run -- /path/to/vllm serve /path/to/cached/model --port 8101
```

Copy and edit `launcher.example.json` for the target. The unit must be in the
`vllm-<model>` namespace, and `<model>` must equal the scheduler/llama-swap model
identifier. A `.service` suffix is accepted. The weight directory is not the
scheduler identifier. Preserve or map existing aliases before adoption.

Dry-run only sends `{model, util}` to `POST /v1/place?dry_run=1`: no lock file,
unit, confirm or release is created. A read-only M1 server returns an error
if its runtime lacks placement previews. Integrated previews must still be
checked for blockers; HTTP200 alone does not authorize a launch.

A live invocation serializes same-model launchers with a bounded local flock,
reuses an existing active/activating unit, requests placement, starts only the assigned unit,
and confirms after readiness. The scheduler remains responsible for atomic
budget reservations, protected-model checks, expiry and restart reconciliation.
The default HTTP timeout of 130 seconds accommodates the scheduler's maximum
120-second placement wait; startup follows the 900-second lease/window setting.

`lease.util` records the fraction supplied by the placement request and used by
vllm-launch; it is not necessarily the full charged fraction. The scheduler's
`budget_gb` is the authoritative reserved amount: it is at least
`max(request.util, configured util) * selected GPU total_gb`, and a larger
configured explicit `budget_gb` also applies. For example, a request util of 0.2
with configured util 0.6 on a 100 GiB GPU reserves 60 GiB while `lease.util`
remains 0.2. The launcher must not derive available capacity from `lease.util`
or reduce the accounting reservation; only the scheduler manages that budget.

An existing inactive/failed unit returns an error requesting scheduler cleanup
instead of reporting a successful launch; no reset-failed or stop is issued by
that branch. Unknown systemd state or ambiguous startup failure retains the lease budget and
returns an error. A release requires named systemd properties showing the unit
absent, or inactive with no main PID/control group. Property output ordering is
not assumed. A late-confirm 409 stops only the unit whose exact lease token
matches the token recorded by this invocation. A still-loading unit at startup
timeout is not reported as ready and remains charged for scheduler recovery.
Structured JSON messages go to stderr, captured by the calling service journal.

If an allocated model is removed/renamed or its unit mapping changes, follow
[verified configuration recovery](../docs/OPERATIONS.md#allocated-model-configuration-recovery-93).
Restore the verified original metadata and reopen the same schema-v2 ledger in
an authorized scheduler maintenance window. Retain unknown/loading budgets and
pin; release requires proven exit. This condition grants no authority to delete
SQLite rows, force-revoke a lease or stop an unconfigured unit.

## Placement errors and victim blockers (#14 / #17)

The guarded victim path in merged #99 retains the default-off settings:
`read_only: true`, `placement_enabled: false`, and `model_actions_enabled: false`.
Victim execution requires both opt-ins, a confirmed durable daemon account and
an active configured unit with the matching current `LLMSVC_LEASE_ID`, followed
by fresh protection checks. These are implementation prerequisites, not
permission to enable the path in production.

| Response field/value | Meaning |
|---|---|
| HTTP503 `placement_action_blocked` | Current unit/lease identity did not authorize the proposed victim stop. |
| HTTP503 `placement_action_failed` | The action attempt reported a sanitized failure; inspect its blocker reason. |
| HTTP503 `placement_no_progress` | Victim exit and durable account release could not be confirmed. |
| `blockers[].reason: unit_identity_unconfirmed` | The victim's currently observed unit/lease identity was not verified. |
| `blockers[].reason: unleased_model` | The model has no eligible confirmed durable account; this path cannot adopt or stop it speculatively. |
| `blockers[].reason: operation_in_progress` | Another pending model operation or free operation protects the model from a duplicate stop. |

Blocker reasons can also appear in previews or the final HTTP409
`placement_timeout`; they are not separate stop commands. On a placement-stage
failure the launcher logs the response and exits unsuccessfully, without starting
or stopping a unit. None of these errors/reasons authorizes the launcher to stop
its own unit, a named victim or another workload, or to clear/revoke a lease.
They must not be handled as the later **confirm** HTTP409, whose existing cleanup
is restricted to this invocation's exact matching lease token.

An action error may follow a positively observed exit: the current placement
still fails without granting a new lease. Consult `placement_action_result` and
fresh observations; preserve confirmed partial progress and retain budgets whose
exit is still unproven. Keep the ledger intact. Further allocation/accounting is
the scheduler's responsibility; there is no speculative cleanup or retry stop in
the launcher.

Do not replace the installed launcher from this draft. Keep a complete original
script backup, finish the scheduler lease/reserve/recovery integration and the
full-day alternate-port dry-run gate, then obtain the production transition
handoff. Existing unit aliases and real cold-start latency need verification.
The tests use fake systemd responses, an actual concurrent flock test and a
local fake HTTP server; they do not constitute a live GPU/cold-start benchmark.

<!-- Generated-By: Codex / gpt-6-astra -->
