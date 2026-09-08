# Per-model concurrency preparation and bounded proof (#13)

`concurrency_smoke.py` stages a reviewed configuration copy and measures a single
concurrent batch through a real, unchanged pinned llama-swap. It uses a controlled
fake backend and a dummy CPU process: no vLLM model, wrapper, GPU, systemd unit,
production endpoint, existing configuration, observer or reaper is operated.
Configuration preparation, rollback candidates, tests and measurements are one
#13 slice. The issue remains open for the explicitly separate live-serving and
activation acceptance below.

## Pin and meaning of the limit

The measured binary is v252/e31a1ad, upstream commit
`e31a1adee494bb7a578e2a97ec891b3e809899dc`, SHA256
`32aea60b5c1be987c27dde6ea4aaa84f9be7ad93eaede011295fad1e276e80ea`.
Every actual invocation verifies that executable hash. Use Python3.10 with Linux
pidfd support and the existing PyYAML dependency; no new package is required.

The pinned [FIFO admission code](https://github.com/mostlygeek/llama-swap/blob/e31a1adee494bb7a578e2a97ec891b3e809899dc/internal/router/scheduler_fifo.go)
uses an effective default of10 per ordinary model. Omitted/zero `concurrencyLimit`
uses that default; zero is not unlimited. A positive per-model value overrides
it. Admission counts queued/starting/serving requests until completion/cancellation;
excess requests receive429 rather than waiting for a free admission slot.
ComfyUI's separate minimum override is outside this ordinary-model fixture.

64 is a proposed admission floor for the requested32-client batch, not proof of
64 simultaneous generations or a GPU capacity recommendation. vLLM's own
`--max-num-seqs` and queueing, context/KV memory, timeouts and workload mix remain
independent. Keep any existing larger per-model cap unless separately reviewed.

## Prepare an exact candidate without touching the running configuration

`concurrency.example.yaml` shows the per-model keys only; it is **not** a complete
configuration and must not replace the existing file. Supply a verified full
config copy and a new private staging directory **outside all watched config
locations**. Paths are explicit operator inputs; this tool does not discover
production watch roots or activate a candidate.

```sh
python3 deploy/concurrency_smoke.py prepare \
  --source-config /path/to/private/config-copy.yaml \
  --output-dir /path/to/private/concurrency-stage \
  --llama-swap-binary /path/to/pinned/llama-swap --limit 64 --dry-run
# Same argv without --dry-run stages and validates; it does not reload anything.
python3 deploy/concurrency_smoke.py prepare \
  --source-config /path/to/private/config-copy.yaml \
  --output-dir /path/to/private/concurrency-stage \
  --llama-swap-binary /path/to/pinned/llama-swap --limit 64
```

Preparation updates each explicit model's `concurrencyLimit` to at least64,
retains larger values, and preserves other bytes: comments, commands/proxies,
macros, routing, global/model TTL and unrelated settings. It uses YAML source
positions and verifies the resulting semantic mapping. Unsupported aliases,
anchors, tags, duplicate keys or edited flow mappings fail closed rather than
being reformatted. Review such layouts separately; do not normalize the live file
just to make this command accept it.

The new0700 staging directory contains private0600 `original.yaml` (exact source
bytes), `candidate.yaml`, and `plan.json` with hashes/changed-model list. Both full
files pass the existing pinned `CommandValidator` (`-config PATH -validate`).
Failed staging removes only that new directory. Source bytes are never replaced.
`--dry-run` prints the plan without files, sockets, subprocesses or validation
processes. Review the full candidate and the exact model list; no current private
production configuration was inspected or published by this slice.

## Deterministic32-client measurement

```sh
python3 deploy/concurrency_smoke.py measure \
  --llama-swap-binary /path/to/pinned/llama-swap \
  --output-dir /path/to/private/concurrency-results \
  --requests 32 --limit default --deadline-seconds 45 --dry-run
# Actual isolated default-limit contrast:
python3 deploy/concurrency_smoke.py measure \
  --llama-swap-binary /path/to/pinned/llama-swap \
  --output-dir /path/to/private/concurrency-results \
  --requests 32 --limit default --deadline-seconds 45
# Separate owned process/static config, candidate limit:
python3 deploy/concurrency_smoke.py measure \
  --llama-swap-binary /path/to/pinned/llama-swap \
  --output-dir /path/to/private/concurrency-results \
  --requests 32 --limit 64 --deadline-seconds 45
```

One client program starts32 threads behind a barrier. A fresh UUID model and
OS-selected loopback listeners prevent reuse of an existing route. The mock health
endpoint waits for the owned dummy process's explicit startup marker. Admitted
requests receive streaming headers and are held at the backend release event.
Only after all32 distinct requests have either reached that backend or produced a
terminal client outcome does the controller release the streams. Distinct-ID union
accounting prevents an admitted client that later fails from being counted twice.
No successful proof depends on finishing within a sub-100ms scheduling interval.

The candidate must demonstrate32 simultaneously held backend requests before any
completion; the default case must hold10 while the other22 already have429.
Both then require exact completion/rejection totals, zero other error categories
and one dummy process start. This avoids the false-positive case where short
responses recycle admission slots before later clients arrive. Requests use the
actual `/v1/chat/completions` route and require `[DONE]` without an earlier stream
error; HTTP200 headers alone do not count as completed.

Each run has its own temporary config/store/logs and process session. The supervisor
reuses the existing pidfd/session cleanup, including child process groups, and
reserves cleanup time within the requested cap (at most300seconds). No watcher or
reload signal is configured. Private results include all client outcomes and a
bounded diagnostic log. Startup failures and hard deadlines return nonzero;
partial accounting is retained when available, otherwise explicitly unavailable.
Cleanup failure preserves the owned directory for investigation instead of claiming
success. Raw native logs may contain host discovery details and remain private.

## Measured controlled-backend results

The public allowlisted result is `concurrency-results-20260908.json`; full native
logs/private paths are not published. These are new #13 fixtures, not repeats of
observer, GPU or reload/witness experiments.

| Variant | Attempted | Simultaneously held before release | Completed | HTTP429 | Timeouts/other failures |
|---|---:|---:|---:|---:|---:|
| Pinned default (effective10) | 32 | 10 | 10 | 22 | 0 |
| Explicit per-model64 | 32 | 32 | 32 | 0 | 0 |

Both cases started one owned dummy process and cleaned their own sessions and temp
files. Durations including cleanup were about2seconds; duration is not the proof
of overlap. The actual rejection body was:

```json
{"src":"llama-swap","error":{"message":"Too many requests","type":"rate_limit_error","param":null,"code":"concurrency_limit"}}
```

A synthetic full-config copy also passed real pinned validation before/after
staging. Two model caps rose to64, an existing128 stayed128, unrelated bytes stayed
intact and the rollback review file equaled the retained original bytes. Final
post-measurement changes harden failed/partial-timeout diagnostics only, tested
with deterministic stubs; the successful native cases were not repeated for them.

## Rollback candidate and activation preconditions

These commands prepare/check rollback bytes only; they do not restore a watched
file, signal a service or claim production rollback readiness:

```sh
python3 deploy/concurrency_smoke.py rollback-candidate \
  --stage-dir /path/to/private/concurrency-stage \
  --current-config /path/to/private/current-config-copy.yaml \
  --output /path/to/private/rollback-review.yaml --dry-run
python3 deploy/concurrency_smoke.py rollback-candidate \
  --stage-dir /path/to/private/concurrency-stage \
  --current-config /path/to/private/current-config-copy.yaml \
  --output /path/to/private/rollback-review.yaml
/path/to/pinned/llama-swap -config /path/to/private/rollback-review.yaml -validate
```

The backup must match the original hash and the supplied current copy must match
the staged candidate hash. A changed file, wrong backup or existing output is an
error; do not force a restore or edit hashes to silence it. The new rollback file
must be outside watched locations. It restores the original cap declarations,
including omitted keys and larger values, and all other original settings.

Before any actual activation **or** rollback, separately verify:

1. Explicit authority for the exact target config, changed model set and rollback
   scope; fresh whole-config backup/hashes, correct binary, source/watch mode and
   no competing writer. This staging helper never applies to the target itself.
2. Trusted continuous quiet evidence and protection/aggregate RAM checks under
   the reviewed transaction boundary. #53 is not bypassed by polling a zero count;
   default/pin/in-flight/lease protections remain, as do unknown host-source gates.
3. One reviewed reload trigger, candidate-file/active-generation binding and
   independent old-server settlement. No unconditional SIGHUP, fallback signal or
   second write. New-model listing, HTTP200 or generic completion is insufficient.
4. A reviewed final forward/rollback transaction binding. If the guarded adopter
   adds a fresh generation marker or changes any candidate bytes, this offline
   hash check will correctly refuse that different file. Prepare/review the final
   transaction manifest and fresh rollback-generation semantics through the agreed
   adoption mechanism; the raw backup is not an automatic production transaction.
5. After an authorized transition, record real backend32-client outcomes and any
   resource/queueing failures before claiming full live acceptance. On adverse
   behavior, use the same guarded rollback/preconditions and original settings;
   never stop/sleep another workload merely to complete the test.

`production_rollback_ready` therefore stays false in the staging plan until the
separate operational transaction is established. The controlled fake-backend proof
establishes swap admission sensitivity and32-request completion only. It does not
measure real vLLM queue/KV behavior, latency, wrapper/systemd integration or live
serving under this change. #13 stays open for that acceptance and guarded actual
configuration activation. Core automatic-policy cycle119 is not a dependency.
Long-term stability/calibration remain NOT MEASURED; no calendar wait or release
change is introduced. Integration alone owns five-PR releases and merge approval.

<!-- Generated-By: Codex / gpt-6-astra -->
