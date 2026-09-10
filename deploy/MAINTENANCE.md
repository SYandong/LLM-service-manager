# Maintenance executor observations

`maintenance_executor.py` is the ops contribution to the combined #60 instance
transition implementation. Its current executable surface is read-only: `validate`, `inspect`,
`preflight`, `observe_old`, `observe_candidate`, `observe_base`, and
`observe_candidate_absent`. **It cannot perform a maintenance
transition yet.** Other operations return an error; `--dry-run` reports an
unaccepted planned operation without invoking a command or writing files. Do not
install this intermediate contribution as a production maintenance adapter.

A private operator profile selects the systemd unit, absolute `systemctl`
executable, and SHA256 of its service fragment. Requests cannot select another
unit or executable. Inspection binds kernel PID/start ticks to the boot ID,
systemd invocation, control-group path, and service-fragment hash. It returns the
observed cgroup actor identities for the core durable checkpoint. The core must
preserve the returned scope and actor set when requesting a later observation.

The JSON command envelope uses `operation`, `context`, `timeout_seconds`, and
`request_id` (SHA256 of canonical sorted compact JSON for the other three fields).
Responses echo the request and transaction IDs. Observation context includes the
expected instance identity, `observed_scope`, and `observed_actors`. Observations
include `observed_at` from the host monotonic clock and `ingress_state=unknown`.
Preflight is explicitly not ready: unknown inflight and external helpers cannot
be replaced by empty/zero/true values. `old_settled` describes only old proxy
identity absence plus the stable empty scope; `helpers_settled=false` continues
to block complete transition proof. Limits are
4 MiB of input, 64 KiB of output, and a finite remaining timeout up to 900 seconds.
The caller's absolute operation deadline remains authoritative.

Process absence is distinct from successful teardown. A reused PID proves only
that the old identity is absent; a zombie remains present. An unreadable actor,
changed systemd invocation, missing cgroup, or changed service fragment blocks
observation. The inspector reads all descendant cgroups and refuses partial
inventories. It never signals a process while inspecting.

Even an empty old cgroup plus absence of its captured actors leaves
`settlement_confirmed`, `cleanup_confirmed`, `backends_confirmed`, and
`exclusion_confirmed` false. An asynchronous helper may have escaped the old
scope; a helper that exited unsuccessfully may have left external resources.
The selected `stop_instance` mode establishes exclusion by positively settling
the old instance, helpers and address ownership; it does not require a fictional
pause/socket gate. The full adapter still needs attributable-helper exit outcomes
and backend observations before it can report those facts.
No proxy exit, command exit code, or caller-supplied boolean substitutes for them.

CPU regression tests use real temporary child processes and pipe barriers with
temporary systemd/cgroup descriptions. They exercise delayed/failed helper exit,
PID reuse, restart races, unknown scope, bounded output, and absolute command
expiry. These tests establish process-handling behavior; their temporary cgroup
files do not establish a production systemd or network-exclusion result.

```sh
python3.10 -m pytest -q tests/test_deploy_maintenance_executor.py
```

The internal `stop_bound_process` primitive implements the selected exact-instance
SIGTERM action using a Linux pidfd, with full identity/scope checks before and
after binding the descriptor. Its ACK reports signal submission only. Dry-run
does not open a pidfd or signal anything; late/unknown results require observation,
not a resend. Core must durably record submission before calling it. The CLI
does not expose this primitive until the complete preflight/helper/backend
protocol is wired. Its CPU tests signal only their own temporary child and
verify that a separate child remains alive.

The bounded command runner kills and reaps only its own command if it expires.
It does not guess at descendants or stop other helpers. A timed-out external
effect must therefore keep the core's durable submitted fence. There is no
resend, forced-clear, rollback, ledger mutation, or old-backup restoration in
this observation contribution.

## Native configuration validation

The `validate` operation uses the pinned native binary with exactly
`-config <candidate_path> -validate`. Configure `native_binary`, its
`native_binary_sha256`, and `native_config_dir` in the operator profile. The
candidate must be a regular, nonsymlink file directly within that directory,
with the exact request `candidate_sha256`. The binary is hash checked before
execution; input bytes and binary file identity are rechecked afterward.
Output is bounded and native errors stay out of public diagnostics. Dry-run
prints the planned argv and does not invoke the binary.

This adds actual native parsing to the combined controller's validate callback.
It does not complete helper/backend settlement or arm stop/start dispatch.
A bounded validation-only check against the retained pinned v252 binary used an
empty-model temporary configuration, created no additional files, and started
no listener, backend or model. Whole-transition native rehearsal remains a
separate required part of this same feature.

<!-- Generated-By: Codex / gpt-6-astra -->
