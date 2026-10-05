# Patched llama-swap build

<!-- Generated-By: OpenCode / deepseek-v4.1-flash -->

The scheduler's usage attribution needs llama-swap to record the request peer
address as `client_ip` in `activity.metadata_json`, and its per-model
`concurrencyQueue` capacity needs the matching queue behaviour. Upstream v252 has
neither. The launcher also needs its temporary GPU-capacity refusal to reach
inference clients as HTTP 503. This directory carries the patch set and a
reproducible build script.

## Layout

- `patches/*.patch` — every patch is applied in name order with `patch -p1`.
  The set is maintained separately; the build fails if it is empty. It carries
  `0001-record-client-ip.patch` (usage attribution) and
  `0002-concurrency-queue.patch` (per-model `concurrencyQueue` wait list), plus
  `0003-surface-start-unavailable.patch` (startup GPU-capacity errors).
- `build.sh` — downloads the pinned tarball, verifies its sha256, applies the
  patches, builds the UI bundle, CPU test responder and unmodified upstream
wrapper for local smoke tests, runs `go test ./...`,
  builds the Go binary in Docker, then prints the output path and its sha256.

## Build

```bash
deploy/llama-swap/build.sh /path/to/work-dir
```

The work directory defaults to `./build/llama-swap`. The script requires
`curl`, `patch`, `sha256sum` and Docker; it downloads
`llama-swap-v252.tar.gz` and verifies
`8681d563ea2766a9348aee6ae1b20f6c792a3945fb91f72e7ce712ab335d078f`.

The UI is built with `node:22-slim` (`npm ci && npm run build`) and the binary
with `golang:1.26` using `-tags embed_ui` and the pinned ldflags
(`-X main.version=v252-llmsvc.3 -X main.commit=e31a1ad -X main.date=...`).
The binary is written to
`<work-dir>/llama-swap-252/build/llama-swap-linux-amd64`.

Promote a built binary to production only after verifying its printed sha256
and validating it on an alternate port before switching the live service, as
required by `AGENTS.md`.

## Startup capacity errors (#304)

The third patch captures stdout/stderr synchronously at the existing process
log monitor writer. Each startup and each output stream has a separate bounded
line buffer; the last valid `llmsvc_unavailable` JSON line from that startup
is retained. `cmd.Wait` drains both writers before startup failure is reported,
so a final flushed marker cannot race the response. Log history and asynchronous
monitor subscribers are not used to classify errors.

Only whole JSON lines of at most 4096 bytes including the newline, with the
fixed fields and types documented in [LAUNCHER.md](../LAUNCHER.md), are accepted.
Each field must occur once with its exact name. Invalid, oversized and unfinished
lines keep the ordinary startup error path;
messages are capped at 512 Unicode characters. A new startup starts with no
marker, including retries after either a failed or successful process.

A matching startup failure returns an OpenAI error envelope with HTTP 503,
`Retry-After` and `error.code: no_gpu_available`. If the loading SSE stream
has already sent HTTP 200, it terminates with the same JSON error envelope in
a `data:` frame followed by `[DONE]`; HTTP status and headers are already
committed. HTTP 409 placement errors and unrelated upstream failures keep their
existing behavior. The upstream vllm-wrapper remains unchanged.

For the wrapper's optional `--journal-unit` forwarder, the launcher bounds
cleanup of the verified, pidfd-bound journal child before terminating its parent.
See [LAUNCHER.md](../LAUNCHER.md) for the identity checks and capability fallback.
This prevents a surviving helper from adding llama-swap's 10-second output-drain
timeout to the scheduler's refusal grace.

The opt-in Python smoke exercises the built proxy, unmodified wrapper and real
launcher against local fake scheduler/backend HTTP servers and fake systemd
commands. It covers normal, HTTP409 and HTTP503 placement for both ordinary
HTTP and loading SSE, including a 10-second simulated refusal grace and a
20-second request limit. Two additional HTTP503 cases enable `--journal-unit`
with a simulated journal helper that ignores SIGTERM and holds both output pipes;
they verify the response deadline and the helper's exit. It allocates no GPU and
does not contact production. Run it with the deployment-compatible Python 3.10
on Linux with pidfd support; the held-pipe cases check that capability before
creating their helper:

```bash
LLMSVC_TEST_LLAMA_SWAP=/path/to/work-dir/llama-swap-252/build/llama-swap-linux-amd64 \
LLMSVC_TEST_VLLM_WRAPPER=/path/to/work-dir/llama-swap-252/build/vllm-wrapper \
python3 -m pytest -q -s tests/test_deploy_launch_llamaswap.py
```

After integrating A1, add `LLMSVC_TEST_REAL_PLACEMENT=1` to run the HTTP503
cases through the real scheduler HTTP handler, sampler and placement controller.
Observations are synthetic, the default 10-second grace is retained, and any
actuator/unit probe fails the test. Other cases keep the simulator controls.
If A1 is absent, those requested real-controller cases explicitly skip with
the missing-capability reason; they never fall back to simulated 503 evidence.

<!-- Generated-By: Codex / gpt-6.1-sol -->
