# Configured temporary-model list and previews

The scheduler mounts the existing ModelRegistry when the optional `registry`
configuration is supplied. An omitted/empty mapping leaves it unconfigured.

```yaml
registry:
  config_path: /srv/llmsvc/llama-swap.yaml
  shared_roots: [/srv/models]
  daemon_port_range: [8101, 8199]
  reserved_ports: []
```

Paths must be absolute, roots nonempty, and port values valid integers. Core
also reserves its configured listen port and collector model `port` values.
This does not provision any path or enable configuration writes. No reload
worker, validator process, unit action or adoption notifier is started.

`GET /v1/models` (no query) returns:

```json
{"records": {}, "writes_enabled": false,
 "blocked_by": [{"reason": "registry_writes_disabled"},
                {"reason": "inflight_stream_unknown"}]}
```

`records` maps temporary model names to existing `llmsvc_registry` metadata in
the configured on-disk YAML. It does not list all permanent observations, prove
proxy adoption or assert that a pending configuration is active. Use `/v1/state`
for model observations. List remains readable during an interrupted transaction
and adds `registry_reconciliation_required` to its blockers.

`POST /v1/models?dry_run=1` accepts `{name,path,base}`. The existing registry
checks full-weight paths within shared roots, name/alias collisions, permanent
base configuration and daemon port availability. LoRA remains disabled.
`DELETE /v1/models/{name}?dry_run=1` accepts no body and uses the existing
registered-temporary/default/pin/activity/lease checks. Names are decoded once.

Successful previews return the owner's unchanged `would` descriptions, plus
`dry_run: true`, `config_committed: false` and `blocked_by`. Valid editing does
not mean commit readiness: blockers include disabled writes, actual unknown
quiet state, current reload admission and fault-fence blockers. Polling zero
inflight never establishes quiet. Preview creates no job ID, staging file,
queued job, event, configuration write or unit/network action.

Actual POST/DELETE remains rejected in every mode: HTTP405 `read_only`, or
`operation_not_enabled` when ordinary intent writes are enabled. There is no
registry write-enabling setting in this slice. Other responses are:

- HTTP503 `registry_not_configured` when no registry is configured.
- HTTP400 `registry_invalid_request` plus the registry's validation message.
- HTTP503 `registry_unavailable` for unsafe/unreadable configuration files.
- HTTP409 `registry_reconciliation_required` for preview blocked by a pending
  transaction marker; the marker is not altered or interpreted as completion.
- Existing malformed-body/query HTTP400 and body-size HTTP413 limits apply.

This is partial #19/#20 integration. Job/recovery inspection follows through
the registry owner's interface; this surface creates no queue jobs and starts
no expiry/removal worker. Real reload still needs the separately verified #53
quiet/source and #60 adoption/settlement contracts and operational authority.
CPU loopback tests do not establish live latency or long-term stability, which
remain NOT MEASURED. No production routing/TTL/reaper/reload authority follows.

<!-- Generated-By: Codex / gpt-6-astra -->
