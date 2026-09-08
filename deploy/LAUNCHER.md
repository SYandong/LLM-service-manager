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
because the placement policy is not integrated yet; this is not a successful
placement preview.

A live invocation serializes same-model launchers with a bounded local flock,
checks for an existing unit, requests placement, starts only the assigned unit,
and confirms after readiness. The scheduler remains responsible for atomic
budget reservations, protected-model checks, expiry and restart reconciliation.
The default HTTP timeout of 130 seconds accommodates the scheduler's maximum
120-second placement wait; startup follows the 900-second lease/window setting.

Unknown systemd state or ambiguous startup failure retains the lease budget and
returns an error. A release requires named systemd properties showing the unit
absent, or inactive with no main PID/control group. Property output ordering is
not assumed. A late-confirm 409 stops only the unit whose exact lease token
matches the token recorded by this invocation. A still-loading unit at startup
timeout is not reported as ready and remains charged for scheduler recovery.
Structured JSON messages go to stderr, captured by the calling service journal.

Do not replace the installed launcher from this draft. Keep a complete original
script backup, finish the scheduler lease/reserve/recovery integration and the
full-day alternate-port dry-run gate, then obtain the production transition
handoff. Existing unit aliases and real cold-start latency need verification.
The tests use fake systemd responses, an actual concurrent flock test and a
local fake HTTP server; they do not constitute a live GPU/cold-start benchmark.

<!-- Generated-By: Codex / gpt-6-astra -->
