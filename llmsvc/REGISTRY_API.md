# Configured temporary-model list and previews

The scheduler mounts the existing ModelRegistry when the optional `registry`
configuration is supplied. An omitted/empty mapping leaves it unconfigured.

```yaml
registry:
  config_path: /srv/llmsvc/llama-swap.yaml
  shared_roots: [/srv/models]
  daemon_port_range: [8101, 8199]
  reserved_ports: []
  config_max_bytes: 1048576
  model_config_max_bytes: 1048576
  weight_index_max_bytes: 8388608
```

Paths must be absolute, roots nonempty, and port values valid integers. Core
also reserves its configured listen port and collector model `port` values.
This does not provision any path or enable configuration writes. No reload
worker, validator process, unit action or adoption notifier is started.

The three optional byte limits bound configuration YAML, model `config.json`
and weight-index JSON respectively. Each is an integer in 1..16777216; omitting
them retains the finite registry defaults shown above. There is no unlimited
mode. Configured YAML paths must be canonical, without symlink components;
model links may resolve within the configured shared roots. No-follow,
nonblocking regular-file reads reject special files, excess bytes and detected
identity changes. Actual weights are only sampled for readability, not read in
full. These are allocation and special-file guards, not wall-clock guarantees
for an unresponsive regular filesystem.

`GET /v1/models` (no query) returns:

```json
{"records": {}, "discovered": [], "writes_enabled": false,
 "blocked_by": [{"reason": "registry_writes_disabled"},
                {"reason": "inflight_stream_unknown"}]}
```

`records` maps temporary model names to existing `llmsvc_registry` metadata in
the configured on-disk YAML. It does not list all permanent observations, prove
proxy adoption or assert that a pending configuration is active. Use `/v1/state`
for model observations. List remains readable during an interrupted transaction
and adds `registry_reconciliation_required` to its blockers.

The additive `inventory` field contains the existing registry's configured model
rows, configuration digest, pending changes and recovery fence. Unlike `records`,
these rows can also include permanent configured names (`source: config`,
`temporary: false`). Runtime state is separately observed and stays unknown for
missing/stale observations. `last_used_at`/`expires_at` remain null when unknown;
a derived seven-day expiry is not a scheduled or completed deletion. `removable`
is model-level eligibility, not global reload readiness. The action lock
serializes daemon operations; separate registry reads do not promise an atomic
view of external filesystem writers. `inventory.config_sha256` describes that
inventory read, not proof of data-plane adoption.

The additive `discovered` field lists one row per direct subdirectory of a
configured shared root that contains a readable `llmsvc.json`:

```json
{"name": "foo-7b", "path": "/srv/models/Foo-7B", "base": "qwen3-32b",
 "util": 0.45, "weights_gb": 14.5, "status": "importable", "reason": null}
```

`status` is `importable`, `imported` (the name is already a configured model)
or `invalid` (with a `reason`). Discovery is not admission: a row only means a
descriptor parsed, never that the model is registered, admitted or adopted.
Directories without `llmsvc.json` are not listed; symlinked entries, unreadable
roots and rejected descriptors are listed as `invalid` without being followed.
The scan reads one level per root, at most 200 rows sorted by name, bounds each
descriptor by `model_config_max_bytes` through the same no-follow, nonblocking
regular-file reader, and is cached until a directory or descriptor mtime
changes. It allocates no port, creates no job and writes no file. An
unconfigured discovery source returns `[]`, not an error.

`POST /v1/models?dry_run=1` also accepts `{"import": "<discovered name>"}`. The
scheduler re-reads that directory's own descriptor and derives name, path, base
and the whitelisted overrides itself; a client cannot supply them. Mixing
`import` with any other key is rejected. Supported descriptor keys are `base`
(required), `name`, `util`, `max_model_len`, `aliases` and `weights_gb`;
`is_default`, any command/argv/shell fragment and every unknown key return
HTTP400 with the offending field. Overrides rewrite only the cloned block: the
`util` macro plus the launcher share and `--gpu-memory-utilization`, the
`--max-model-len` value, and the block's aliases. A literal base command
without `--gpu-memory-utilization` and without a `util` macro is rejected
rather than given a launcher share that vLLM would not honour. The remaining
path, weight, name, alias, base and port checks are unchanged, and a missing,
already configured or invalid candidate returns HTTP400 with its reason.
`weights_gb` defaults to the deduplicated size of the files named by
`*.safetensors.index.json`, or of the directory's `*.safetensors` when there is
no index; those are file sizes, not a measured GPU allocation.

A base model may stop through either supported `cmdStop` shape, and the clone
keeps that shape. The wrapper form carrying `--vllm-url` has its upstream port
rewritten and must agree with `cmd`. The native maintenance helper form
(`... helper --profile <profile> --model <base> --pid '${PID}'`) carries no
port: only `--model <base>` becomes `--model <name>`, every other token is
preserved verbatim, the daemon port comes from `cmd` alone, and a `--model`
naming anything but the base model is rejected. The two shapes are mutually
exclusive; a `cmdStop` matching neither is rejected. On such a site the import
also synchronizes the maintenance profile: before the candidate reaches the
adapter's `validate`, the new model's `{unit, backend_origin, process_argv}`
row is written atomically (temporary file plus rename, mode 0600) into the JSON
named by the configured `maintenance_command`'s `--profile`, preserving every
other field and key order. A submission that is refused withdraws the row it
added; a removed model's row is dropped only after its transaction is released,
because the adapter hashes that file into every scope observation. A dry run
writes nothing, and a missing or misshapen profile is reported without any
write.

A model imported this way needs no hand-written `catalog_profiles` entry. Its
collector/catalog profile is derived from the saved record: `unit` and
`daemon_url` from the allocated daemon port, `util`/`weights_gb` from the
descriptor, `is_default: false`, and `budget_gb` = `util` times the smallest
known GPU `total_gb` in the latest fresh snapshot. A stale snapshot or unknown
card size blocks the import with that reason instead of guessing a capacity. A
configured profile still wins, but must agree with the record on unit,
daemon_url, port, is_default, util and weights_gb, or the request is rejected.

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

Successful previews also include `plan`, selecting the owner's existing
`model` (for add), `projected_base_sha256`, `candidate_sha256`,
`port_reserved: false` where supplied, and `config_written: false`. Add's model
fields are `name`, `base`, `daemon_port` and `util_macro`; util is configured
metadata, not measured bytes or an allocation guarantee. No candidate bytes or
full command blocks are returned. Pending FIFO edits affect the projection;
repeated previews do not reserve a port or enqueue a request. A future actual
submission must compute its own candidate again. Protected/invalid remove still
returns HTTP400, including when the internal preview helper reports blockers
with an empty `would`; it is not converted into a successful response.

Without the complete trusted catalog capability, actual POST/DELETE returns
HTTP405 `read_only`, or `operation_not_enabled` even when ordinary intents are
writable. The default remains unchanged. With `catalog_enabled`, a writable
store and explicitly connected profile/instance/verification adapters, the same
paths return the existing registry job envelope (`id`, `description`, `status`,
`blocked_by`, `config_committed`, `error`, `apply_seconds`). `queued` is incomplete;
the existing CLI keeps its nonzero incomplete exit convention. One bounded
scheduler worker drives the queue. `writes_enabled` then means submission
capability, not commit readiness. A durable catalog fence independently blocks
conflicting actions after partial publication, even if the raw configuration
queue phase says `applied`. See [CATALOG.md](CATALOG.md). Normal entrypoint config
does not invent the missing proof adapters or expose a client proof endpoint.
Other responses are:

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
<!-- Generated-By: Claude Code / claude-fable-5-1 -->
