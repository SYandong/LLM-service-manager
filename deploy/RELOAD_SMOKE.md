# Isolated reload transport harness

Refs #20. Run `reload_smoke.py` with a **Linux Python build exposing
`os.pidfd_open` and `signal.pidfd_send_signal`** (the validated target is Python
3.10). Unsupported builds refuse before creating workers. The local Conda 3.13
build lacks these APIs; changing only the language version number is not a
capability check. No dependency or production environment is installed by this
harness.

The default mode uses an owned fake streaming HTTP upstream. A supplied
llama-swap binary enables an actual isolated swap process. Supplying the existing
wrapper binary additionally exercises its real transport/termination chain
against the fake backend. These are test processes, not a production reload.
All configs/stores/logs are in a UUID temporary directory. Loopback ports are
OS-selected; process identity, temporary ownership and per-request results are
recorded. The supervisor reserves cleanup time within the requested deadline
(maximum 300 seconds) and terminates only its own process session with stable
pidfd handles, including descendants moved into other process groups.

From the reviewed checkout, substitute private evidence and existing binary paths:

```sh
/usr/bin/python3 deploy/reload_smoke.py --dry-run \
  --output-dir /tmp/ops-reload-results --deadline-seconds 20
/usr/bin/python3 deploy/reload_smoke.py \
  --output-dir /tmp/ops-reload-results --mode abort
/usr/bin/python3 deploy/reload_smoke.py \
  --output-dir /tmp/ops-reload-results --mode wait
/usr/bin/python3 deploy/reload_smoke.py \
  --llama-swap-binary /path/to/llama-swap \
  --wrapper-binary /path/to/vllm-wrapper \
  --output-dir /tmp/ops-reload-results --deadline-seconds 20 \
  --request-count 4 --chunks 100 --chunk-delay-seconds 0.02 \
  --reload-after-seconds 0.2 --client-timeout-seconds 7 --mode abort
# Same isolated transport fixture, candidate first sleep query mode=wait:
/usr/bin/python3 deploy/reload_smoke.py \
  --llama-swap-binary /path/to/llama-swap \
  --wrapper-binary /path/to/vllm-wrapper \
  --output-dir /tmp/ops-reload-results --deadline-seconds 20 \
  --request-count 4 --chunks 100 --chunk-delay-seconds 0.02 \
  --reload-after-seconds 0.2 --client-timeout-seconds 7 --mode wait
```

Dry-run starts no process/socket and creates no files. The executable paths are
trusted operator input; use reviewed copies of the existing binaries. The real
mode passes only its temporary configuration and working/store paths. It sends
SIGHUP only to the process it started; it never signals an existing service or
uses a production config/endpoint. Temporary dummy model commands expire on the
same monotonic work deadline. No model weights or GPU are used.

The real-stream case waits until fixture requests actually reach the upstream;
requests interrupted during initial loading do not masquerade as streaming
interruptions. The abort variant uses the real wrapper `sleep --stop-pid` path.
The candidate wait variant first calls the fake backend's query `mode=wait`,
then signals the owned wrapper, whose own SIGTERM path may issue another sleep.
Both calls are recorded, including their query modes. The fake backend models
abort/wait behavior; this does **not** measure real vLLM generation/sleep.

Evidence distinguishes completed `[DONE]` streams, HTTP 5xx, truncated streams,
timeouts and other HTTP errors. It records the fixture's zero counter before
requests, active count at reload, actual rename, single trigger and adoption
(visibility of a new model identifier). Mock generation adoption is labeled as
mock. A real swap without a wrapper uses a persistent fake upstream; completion
in that control is not evidence about wrapper shutdown. Failed startup and hard
deadlines return nonzero. Result files are private; unrelated output files are
preserved.

The fixture's counter is not actual v252 continuous quiet certification. #53
still blocks that claim. Neither local apply/adoption timing nor a passing finite
batch establishes a production interruption window or zero-interruption
promise. Real GPU sleep/wake, LoRA retention/cost/routing, sustained arrival
races, protected-model/RAM admission and final activation authority remain
separate prerequisites. LoRA adapter absence is never replaced by base inference.

<!-- Generated-By: Codex / gpt-6-astra -->
