# Patched llama-swap build

<!-- Generated-By: OpenCode / deepseek-v4.1-flash -->

The scheduler's usage attribution needs llama-swap to record the request peer
address as `client_ip` in `activity.metadata_json`, and its per-model
`concurrencyQueue` capacity needs the matching queue behaviour. Upstream v252 has
neither, so this directory carries the patch set and a reproducible build
script.

## Layout

- `patches/*.patch` — every patch is applied in name order with `patch -p1`.
  The set is maintained separately; the build fails if it is empty. It carries
  `0001-record-client-ip.patch` (usage attribution) and
  `0002-concurrency-queue.patch` (per-model `concurrencyQueue` wait list).
- `build.sh` — downloads the pinned tarball, verifies its sha256, applies the
  patches, builds the UI bundle and the Go binary in Docker, then prints the
  output path and its sha256.

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
(`-X main.version=v252-llmsvc.2 -X main.commit=e31a1ad -X main.date=...`).
The binary is written to
`<work-dir>/llama-swap-252/build/llama-swap-linux-amd64`.

Promote a built binary to production only after verifying its printed sha256
and validating it on an alternate port before switching the live service, as
required by `AGENTS.md`.
