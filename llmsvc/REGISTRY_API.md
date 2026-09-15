# Configured model list and directory-driven registration

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

The scheduler also has directory-driven registration toggles:

```yaml
model_reconcile_enabled: true
model_reconcile_interval_seconds: 30.0
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
{"records": {}, "discovered": [], "writes_enabled": false, "reconcile": {"enabled": true, "last": null},
 "blocked_by": [{"reason": "registry_writes_disabled"},
                {"reason": "inflight_stream_unknown"}]}
```

`records` maps temporary (directory-registered) model names to their
`llmsvc_registry` metadata in the configured on-disk YAML. It does not list all
permanent observations, prove proxy adoption or assert that a pending
configuration is active. Use `/v1/state` for model observations. A model whose
descriptor was removed but whose record is not yet unregistered appears as an
`orphaned` discovery row; only models carrying a `metadata.llmsvc_registry`
record are ever unregistered, hand-written models are never touched. List
remains readable during an interrupted transaction and adds
`registry_reconciliation_required` to its blockers.

The additive `inventory` field contains the existing registry's configured model
rows, configuration digest, pending changes and recovery fence. Unlike `records`,
these rows can also include permanent configured names (`source: config`,
`temporary: false`). Runtime state is separately observed and stays unknown for
missing/stale observations. `last_used_at` remains null when unknown. `removable`
is model-level eligibility, not global reload readiness. The action lock
serializes daemon operations; separate registry reads do not promise an atomic
view of external filesystem writers. `inventory.config_sha256` describes that
inventory read, not proof of data-plane adoption.

The additive `reconcile` field reports the directory reconciler:
`enabled` is true when the scheduler mounted one, and `last` is its most recent
`run_once` result (or null before the first run):

```json
{"action": "add", "model": "foo-7b", "reason": null}
```

`action` is one of `add`, `stop`, `remove` or null; `model` names the subject;
`reason` carries a caught failure (`"<type>: <message>"`) or a skip reason.

The additive `discovered` field lists one row per direct subdirectory of a
configured shared root that contains a readable `llmsvc.json`, plus one
`orphaned` row per registered record whose descriptor disappeared or now names
a different model:

```json
{"name": "foo-7b", "path": "/srv/models/Foo-7B", "base": "qwen3-32b",
 "util": 0.45, "weights_gb": 14.5, "status": "pending", "reason": null}
```

`status` is `pending` (a new descriptor waits to be registered), `configured`
(the name is already a configured model), `invalid` (with a `reason`) or
`orphaned` (with a `reason` that includes what will happen). Discovery is not
admission: a row only means a descriptor parsed, never that the model is
registered, admitted or adopted. Directories without `llmsvc.json` are not
listed; symlinked entries, unreadable roots and rejected descriptors are listed
as `invalid` without being followed. The scan reads one level per root, at most
200 rows sorted by name, bounds each descriptor by `model_config_max_bytes`
through the same no-follow, nonblocking regular-file reader, and is cached until
a directory or descriptor mtime changes. It allocates no port, creates no job
and writes no file. An unconfigured discovery source returns `[]`, not an error.

## Registration is directory-driven

There is no HTTP write surface:

- `POST /v1/models` and `DELETE /v1/models/{name}` return HTTP405
  `{"error": "registry_writes_removed"}` before any other check, with or without
  `dry_run=1`, in read-only mode and in writable mode alike. The routes are
  retained only so the removed surface reports consistently; they never enqueue
  a job, stage a file or invoke the queue.

A model is served iff a one-level subdirectory of a configured shared root
contains a valid `llmsvc.json`. The scheduler's `DirectoryReconciler` submits at
most one action per idle tick:

- **add**: the first `pending` candidate not in backoff is submitted through
  `registry.add({"import": name})`, which re-reads that directory's own
  descriptor and derives name, path, base and the whitelisted overrides itself;
  a client cannot supply them. Supported descriptor keys are `base` (required),
  `name`, `util`, `max_model_len`, `aliases`, `weights_gb`, `tool_call_parser`,
  `reasoning_parser`, `speculative` and `max_num_seqs`; `is_default`, any
  command/argv/shell fragment and every unknown key are rejected. Overrides
  rewrite only the cloned block. A failed or queued submission starts a
  per-name backoff (60 s doubling to a 3600 s cap).
- **remove**: a record whose descriptor was deleted (or is no longer a regular
  file, or now declares a different name) is unregistered once its state is
  `stopped`. If it is `awake` or `sleeping`, it is stopped first
  (`by="reconcile"`) and unregistered on a later tick.

Every non-null action and caught failure is logged (`kind: model_reconcile`)
and emitted as a `model_reconcile` event. Expected failures (`RegistryError`,
`ReloadError`, `IntentWriteError`, `OSError`, `ValueError`) never escape
`run_once`; they are recorded in the returned result and the backoff entry.

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
because the adapter hashes that file into every scope observation.

A model registered this way needs no hand-written `catalog_profiles` entry. Its
collector/catalog profile is derived from the saved record: `unit` and
`daemon_url` from the allocated daemon port, `util`/`weights_gb` from the
descriptor, `is_default: false`, and `budget_gb` = `util` times the smallest
known GPU `total_gb` in the latest fresh snapshot. A stale snapshot or unknown
card size blocks the registration with that reason instead of guessing a
capacity. A configured profile still wins, but must agree with the record on
unit, daemon_url, port, is_default, util and weights_gb, or the submission is
rejected.

Other responses are:

- HTTP503 `registry_not_configured` when no registry is configured.
- HTTP503 `registry_unavailable` for unsafe/unreadable configuration files.
- HTTP400 malformed-body/query and HTTP413 body-size limits still apply.

This is partial #19/#20 integration. Job/recovery inspection follows through
the registry owner's interface. Real reload still needs the separately verified
#53 quiet/source and #60 adoption/settlement contracts and operational
authority. CPU loopback tests do not establish live latency or long-term
stability, which remain NOT MEASURED. No production routing/TTL/reaper/reload
authority follows.

<!-- Generated-By: Codex / gpt-6-astra -->
<!-- Generated-By: Claude Code / claude-fable-5-1 -->
<!-- Generated-By: OpenCode / deepseek-v4.1-flash -->
