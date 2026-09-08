# Watcher and active-generation evidence

Refs #60 / #20. `watcher_witness.py` measures the proposed watcher-only native
configuration witness against the **unchanged pinned v252 binary**, using owned
CPU dummy processes and a mock HTTP backend. It does not implement a production
reload notifier, registry editor, transaction barrier or recovery engine. Its
`status: ok` means the measurement fixture completed; examine `outcome` and
`classification` for failed adoption or required reconciliation.

Use Linux Python 3.10 with PyYAML and pidfd support. The retained measured binary
is v252/e31a1ad, full upstream commit
`e31a1adee494bb7a578e2a97ec891b3e809899dc`, SHA256
`32aea60b5c1be987c27dde6ea4aaa84f9be7ad93eaede011295fad1e276e80ea`.
Another binary is rejected; do not silently substitute an updated build. No
wrapper, model weights, GPU, systemd unit or container installation is required.

```sh
# Read/print settings only: no socket, process, output directory or file mutation.
/usr/bin/python3 deploy/watcher_witness.py \
  --llama-swap-binary /path/to/verified/llama-swap \
  --scenario delayed-stop --deadline-seconds 18 \
  --output-dir /tmp/watcher-evidence --dry-run

# Remove --dry-run only for the authorized owned CPU fixture.
/usr/bin/python3 deploy/watcher_witness.py \
  --llama-swap-binary /path/to/verified/llama-swap \
  --scenario stranded-stop --deadline-seconds 45 \
  --output-dir /tmp/watcher-evidence
```

For reproduction, run each scenario independently. `delayed-stop`,
`invalid-candidate`, `same-stat`, `missing-witness`, `restart`, and `deadline`
use an 18-second cap. `stranded-stop` and `failed-stop` need 45 seconds to observe
the unchanged upstream's 30-second main-process stop timeout. Caps include a
cleanup reserve and cannot exceed 300 seconds. Ports are OS-selected loopback
ports; all config/store/helper records are in a fresh private temporary directory.
The supervisor reuses the #79 pidfd/session cleanup, including cmdStop descendants
that use other process groups. Only its owned session and temporary directory
are removed. Private output filenames are unique and existing output files are
preserved. Expected negative outcomes return zero when the fixture completes;
startup/worker failure or its hard deadline returns nonzero.

## What is read and bound

The actual native read is **POST `/api/mcp`**, a read-only tool call, with:

```http
Content-Type: application/json
Mcp-Protocol-Version: 2026-07-28
Mcp-Method: tools/call
Mcp-Name: config__get_config
```

```json
{"jsonrpc":"2.0","id":"fresh-request-id","method":"tools/call","params":{"name":"config__get_config","arguments":{"path":"macros.llmsvc_reload_generation"}}}
```

A successful response has matching JSON-RPC id, no protocol or tool error, and
one text content item containing this complete prefix and fenced YAML scalar:

````text
Current llama-swap configuration at "macros.llmsvc_reload_generation" (credentials redacted, values resolved):

```yaml
gen_<32 hexadecimal digits>
```
````

The fixture uses a fresh, unreferenced global macro. It records old G, candidate
G, exact original/candidate file SHA256 and owned swap PID/start ticks before one
atomic replacement. The proposed macro is injected only into temporary fixture
config; the registry's production format-preserving marker edit still requires
its own reviewed implementation. JSON is valid YAML in this controlled fixture;
this is not evidence of production comment-preserving edits.

The swap starts with one `-config PATH -watch-config` source. There is **one
candidate replacement and no SIGHUP/fallback write**. A missing baseline witness
blocks before replacement. The initial delay allows the watcher to initialize
but does not establish a general readiness guarantee. Every native response is
bound to the same process identity and current file digest. The first old-G
response after candidate bytes are on disk establishes that this is an active
Server snapshot, not a fresh disk read. New G supplies candidate-generation
visibility under the fixture's exclusive source/writer control; v252 does not
supply a native file-digest or transaction-generation endpoint.

Request-start and response-received timestamps are separate. Reported visibility
is the **response completion observation**, not request start or the exact
internal pointer-swap instant. The verification phase caps socket timeout and
poll delay by its remaining budget, and rejects late results. Subsequent
read-only diagnostics use the remaining overall worker budget without upgrading
an expired classification. The parent process enforces the overall hard cap;
individual urllib timeouts are per socket operation, not a general HTTP total
wall-clock guarantee.

## Settlement is a separate unresolved requirement

The pinned [reload path](https://github.com/mostlygeek/llama-swap/blob/e31a1adee494bb7a578e2a97ec891b3e809899dc/llama-swap.go#L288)
selects the new active Server **before** shutting down the old one. The native
[config provider](https://github.com/mostlygeek/llama-swap/blob/e31a1adee494bb7a578e2a97ec891b3e809899dc/internal/config/mcpprovider.go#L21)
reflects that Server's snapshot. Neither it, `/v1/models`, an untagged SSE event,
nor generic `configuration reloaded` identifies successful old-generation
resource settlement.

The long fixture makes this gap reproducible without modifying upstream code:
old `cmdStop` writes its PID/start marker and waits 40 seconds; the main dummy
process stays alive until upstream's 30-second stop timeout kills its group.
The stop helper is in a different process group and can still be running when
`configuration reloaded` is logged. The
[pinned command stop implementation](https://github.com/mostlygeek/llama-swap/blob/e31a1adee494bb7a578e2a97ec891b3e809899dc/internal/process/process_command.go#L610)
launches the stop command asynchronously relative to the main-process wait.
The fixture records both identities independently, then the outer supervisor
cleans them. That final cleanup is not proof they had settled at adoption time.

`failed-stop` instead exits the stop command with status 7. It is a **stop-command
failure**, not an asserted error return from `old.Shutdown`: the router can log
stop failures and still return nil, and the entrypoint emits generic completion
regardless of a shutdown error. Record the actual failure and resource timeline
without inventing an unavailable generation-tagged successful-teardown ACK.

No independent native old-server settlement witness was established in this
installed build. All visible-G cases therefore retain
`barrier_must_remain: true` and `reconciliation_required: true` in the evidence
classifier. The fixture's retained binding is copied into private results before
its temporary files are removed; this illustrates what production recovery must
preserve, and does not implement or certify the registry's durable barrier.

## Measured on 2026-09-08

These are bounded observations, not service guarantees. New-G timing is response
completion after replacement; in `restart` the changed identity rejects that
otherwise matching generation. All eight final runs cleaned their owned session
and temporary directory. Seven scenarios made one replacement, and
`missing-witness` made none; no scenario sent a reload signal or fallback write.

| Case | First new-G response | Generic completion | Stop helper alive at completion | Streams completed/truncated | Reload log entries |
|---|---|---|---|---|---|
| deadline | — | 1.694 s | false | 2/0 | 1 |
| delayed-stop | 1.692 s | 3.650 s | false | 2/0 | 1 |
| failed-stop | 1.672 s | 31.622 s | false | 2/0 | 1 |
| invalid-candidate | — | — | — | 2/0 | 1 |
| missing-witness | — | — | — | —/— | — |
| restart | 0.854 s | — | — | 0/2 | 0 |
| same-stat | — | — | — | 2/0 | 0 |
| stranded-stop | 1.673 s | 31.625 s | true | 2/0 | 1 |

The decisive stranded-stop observation is a live old stop-helper identity after
the generic completion message; its main model process had already exited.
`failed-stop` recorded exit status 7. Each pending/failed adoption classification
retained its fixture recovery binding. `same-stat` produced no reload log entry;
invalid YAML produced one rejected reload and no automatic retry in the bounded
window. Restart truncated both finite requests. All other executed finite pairs
completed; none of these mock-backend outcomes certifies real model behavior.

The public JSON includes exact selected native request/response envelopes and
old/new G/file bindings, plus hashes of the private full records. Repeated
unchanged samples are omitted from the public extract. The measured harness hash
is recorded; final code only hardens failed/truncated HTTP-body diagnostics after
those successful native-response measurements, with dedicated offline tests.
No unchanged binary scenario was rerun for that error-only adjustment.

## Negative cases and limits

| Scenario | Controlled fault / required interpretation |
|---|---|
| delayed-stop | New G can be visible while the old model and stop helper remain alive. |
| stranded-stop | Generic completion can precede stop-helper exit; no settlement success. |
| failed-stop | cmdStop exit 7, eventual main-process kill, generic completion; no inferred shutdown success. |
| invalid-candidate | Deliberately inject malformed temporary YAML; watcher rejects it and retains old G. The unchanged failed file is not a second trigger. |
| same-stat | Deliberately preserve mtime and size during replacement; watcher misses different bytes. A real notifier must reject this before commit. |
| missing-witness | Missing macro returns a native tool error despite HTTP200; no candidate write. This is separate from docs-disabled HTTP503. |
| restart | Deliberately kill/restart only the owned swap process; even new G and matching bytes cannot preserve the original process binding. This is fault injection, not a retry strategy. |
| deadline | Short verification deadline expires; later completion diagnostics cannot clear it or the settlement gate. |

Actual wrong-version and unknown-tool calls also returned HTTP200 JSON-RPC
errors. Offline regressions cover id replay, missing/redacted/nonscalar YAML,
truncated/wrong envelopes, docs-disabled HTTP503 diagnostics, changed file hash,
PID reuse/restart, hard worker deadline, pending client accounting and zero
mutation dry-run. Docs-disabled/auth/redirect and malformed remote-envelope
variants are not all installed-build measurements; retain that distinction.

Per-request results separately count completion, HTTP5xx, truncation, timeout,
other HTTP errors and any pending-at-capture clients. The fake backend remains
independent of the dummy model process; its finite completed streams do not prove
real vLLM/wrapper sleep behavior or zero-interruption production reload. The
fixture is intentionally busy at replacement and never claims #53 continuous
quiet, pin safety, RAM admission or a production-safe write. LoRA, actual host
memory, M1/week observation, future M2/day, owner/admin/threshold/legacy and final
activation gates remain unchanged. This evidence neither updates authoritative
DESIGN nor approves registry proposal A; the actual notifier still needs its own
agreed design/current-head Fable review/CI.

Measured results and exact native examples are in
`watcher-witness-results-20260908.json`; private full logs retain source file
hashes for the compact public extracts. No release/version/tag or September 8
post-alpha.1 publication is part of this slice.

<!-- Generated-By: Codex / gpt-6-astra -->
