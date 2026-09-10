# Native maintenance adapter

`maintenance_executor.py` implements the command protocol used by the existing
catalog worker in explicit `catalog_mode: maintenance`. Ordinary hot reload keeps
its independent quiet/adoption checks. This adapter does not enable scheduler
flags or install/change any service on import, validation, inspection or dry-run.
See [the controller contract](../llmsvc/MAINTENANCE.md) for schema6, effect fences,
protected cleanup, and explicit recovery.

The scheduler still defaults to read-only and hot reload. Actual maintenance
requires all controller opt-ins, the reviewed native profile, fresh use/protection
and RAM checks, and the site's scoped operational authority. An automatic
read-only package upgrade does not provision this profile or activate maintenance.

## Site profile and service prerequisites

For an installation without confirmed preload accounts, use the separate
[first managed-start transaction](BOOTSTRAP.md). Maintenance does not adopt
unleased units or remove the default preload to make inspection pass.

Start from [maintenance.profile.example.json](maintenance.profile.example.json).
Replace every placeholder using actual site observations and the verified source
artifact. The profile and state directory must belong to the executing service
user and be private (0600/0700). Deployment-owned helper paths must be absolute,
canonical paths without whitespace or shell metacharacters; model command args
remain explicit arrays. Nothing creates missing profile/state directories as a
side effect of a read-only request.

The profile binds:

- The exact native service name and service-fragment SHA256. Use a persistent
  reviewed service definition with `Restart=no`, `KillMode=control-group`, and
  no additional ExecStartPre/Post, ExecStop/Post or ExecCondition commands.
  Untracked service hooks and automatic restart are not certified by this adapter.
- The native binary SHA256, actual configuration path/directory, literal HTTP IP
  origin, and matching explicit `-listen`/`-config` argv. The running image and
  listener ownership are checked; DNS, redirects and environment proxies are not
  used. The native config and binary must be regular, nonsymlink files.
- A dedicated `EnvironmentFile=<state_dir>/launch.env` in that service definition.
  Create it privately, initially empty. It holds only the adapter's three attempt
  identifiers; it is not a replacement for an operator environment file.
- Each managed model's exact configured wrapper argv, backend unit and direct
  backend origin. The backend must expose the matching current lease/GPU and
  nonzero systemd invocation, own its listener, and support the vLLM level-1 sleep
  plus `/is_sleeping` protocol. Missing/unmanaged profiles block rather than guess.
- Optional `source_auxiliaries`, each with exact argv and running-image SHA256,
  for native read-only helpers such as its configured performance monitor. They
  must be direct children of the native process. Other actors remain unknown.

`listen_host` binds the exact native `-listen` spelling: `""` means `:PORT`,
`"0.0.0.0"` means `0.0.0.0:PORT`, and `"::"` means `[::]:PORT`. These spellings
are not normalized into each other. An empty-host wildcard is never an HTTP
destination. Keep `native_origin` as the primary literal-IP HTTP origin.

An empty host requires an explicit `native_probe_origins` list containing the
primary origin. The example requires both IPv4 and IPv6 loopback. Each configured
origin must use the same port, belong to the pinned native cgroup, and return a
complete native event snapshot within the shared operation deadline; ownership
is rechecked afterward and the primary snapshot is sampled last. Literal listen
profiles may omit this list to retain their one-origin behavior. No DNS, proxy,
credential-bearing origin, duplicate endpoint or primary-only fallback is used.

Carry forward the address families verified before migration. An IPv6 socket
inode does not prove IPv4 reachability: a V6ONLY listener must not satisfy a
required IPv4 probe. An unavailable required family blocks the transition;
it does not silently narrow the listener. Explicit IPv4-only profiles provide
no IPv6 evidence. Local loopback checks also do not prove remote firewall or
client reachability. Address-availability checks temporarily bind both wildcard
families and close their own sockets; they change no native socket or host setting
and are not an ingress lock. Start/instance/listener proofs are still required.

Each native model's `cmdStop` must be the reviewed helper command, with the
literal model name and native `${PID}` substitution:

```text
<helper_python> -B <maintenance_native.py> helper --profile <private-profile.json> --model <model-name> --pid '${PID}'
```

The native model `cmd` must match that model profile's `process_argv`. Source
preloads require already managed, confirmed backend bindings; an implicit native
startup profile is rejected in favor of an explicit managed preload list. These
requirements must be reconciled with the actual site's defaults and aliases
before provisioning. Never overwrite live native configuration just to make a
profile pass, or certify an unwrapped old helper from generic completion logs.

## Actual operations and proof boundaries

`validate` executes exactly the pinned binary's `-config <candidate> -validate`.
It checks the owned candidate directory/digest and unchanged inputs. It starts
no listener or backend. `inspect`/`preflight` bind PID/start ticks, boot ID, unit
invocation/cgroup, actual image/argv/listeners, configuration, native snapshot and
confirmed backend bindings. A snapshot with zero requests is a current use check,
not continuous quiet evidence.

`stop_old` and `stop_candidate` require the controller's durable unacknowledged
submission, current identity and fresh zero-inflight observation. They persist
an immutable source-owned stop record and then use a pidfd to signal only that
exact native process. New helpers may appear during shutdown; their outcome is
not inferred from the old PID or listener disappearing.

During a maintenance stop, each helper is run as a uniquely named systemd
oneshot job outside the retiring proxy's cgroup. Its source-owned descriptor,
actual invocation/environment/argv, start receipt, exit code/timestamps, empty
cgroup and no pending job must all agree. `RemainAfterExit=yes` retains the result
until evidence collection/owned cleanup. The helper verifies the backend lease,
GPU, invocation and listener, requests level-1 sleep, confirms actual sleeping,
then signals its exact wrapper through a pidfd. HTTP200 alone is not completion.
Failed, missing, duplicated or still-running jobs keep settlement false. Outside
a maintenance claim, the same reviewed helper performs the native sleep/stop
behavior directly; it creates no catalog claim or maintenance job.

`start_candidate` and `start_base` require the previous instance/helper settlement,
address availability and validated exact configuration. A private start record
precedes the native command. The new process must carry the actual attempt IDs
loaded by systemd from the dedicated environment file. File/generation and
PID/scope checks then confirm the new instance. Startup commands are never
resent merely because an ACK was lost.

A recorded failure **before** native command submission can positively establish
that no candidate was started. After submission, a missing process alone does
not establish ownership or settlement: without a matching running attempt or
independent evidence, the fence remains. There is no force-clear or speculative
failed-start recovery. A successful rollback starts a distinct base instance,
uses the exact base bytes and preserves all current ledger rows.

Candidate observation reports backend/account integrity separately from removed-
model cleanup. A confirmed captured backend may remain intact while cleanup is
pending; candidate visibility alone cannot certify final completion. Base
rollback checks current accounts without reissuing the cancelled removal intent.
Already released accounts still require a recorded stop submission and positive
unit absence; no old account is restored. Attempt and helper settlement remain
separate required proofs even when backend integrity is confirmed.

`stop_model` is restricted to the controller-approved removed model and its exact
captured lease/unit/invocation. Its signal ACK releases no account. Actual unit
exit is observed before the core releases the lease. A later configuration
rollback does not restore that old lease or budget.

Every command has a remaining deadline, bounded input/output and correlated
request/transaction IDs. HTTP uses an absolute socket watchdog; late results are
rejected. OS/storage scheduling is not claimed hard real-time. Dry-run emits an
unaccepted plan without invoking mutation commands or writing proof files.

## Bound phase witness (#170)

With the scheduler's explicit `native_witness` opt-in, core records immutable
`native_provenance` (canonical MCP endpoint, source/image/dialect settings and
base generation) before effects. The adapter validates that context against its
pinned `native_origin` and `native_binary_sha256`. This profile has one executable
and no source-image installer: `old`, `candidate` and `restored` must all select
that same image. Additional allowlisted images do not enable an upgrade.

For example, the corresponding **scheduler** setting is the following mapping;
replace both placeholders with the verified source commit and executable hash.
This is separate from the native adapter profile, not an extra profile key:

```yaml
native_witness:
  images:
    - source_commit: <verified-v252-source-commit>
      executable_sha256: <same-native_binary_sha256-as-profile>
      dialect: v252-path
  phase_images:
    old: <same-native_binary_sha256-as-profile>
    candidate: <same-native_binary_sha256-as-profile>
    restored: <same-native_binary_sha256-as-profile>
```

The separately pinned query dialect may be selected for its supported source
image; it is never tried as a fallback. In bound mode the actual native
inspection still verifies the running image, owned listeners, configuration and
accounts. Candidate/base observations return only `configuration_file_confirmed`
for the independently checked bytes. They make no additional unbound generation
RPC and do not claim `configuration_confirmed` or a generation value. Core's
instance/image/listener-bound reader supplies generation visibility. All old
source, helper, backend, attempt and cleanup proofs remain separately required.
Missing provenance retains legacy behavior; malformed or conflicting provenance
fails closed, including before an effect or dry-run dispatch.

An untagged base cannot enter this mode. Prepare a generation-tagged **target**
configuration through the existing generation planner as an explicitly reviewed
[bootstrap](BOOTSTRAP.md) input, preserving default preload, aliases and resolved
model commands. Bind its exact bytes/hash into the bootstrap manifest before
activation. This is a candidate preparation route, not permission to stamp the
live base or to infer a null generation. Bootstrap still requires its reviewed
runtime and actual resource/account proofs; a release without bootstrap cannot
perform it. Never change provenance on a pending claim, discard a fence, or use
visibility as successful old-server settlement.

## Evidence retention and operator recovery

The state directory contains source-owned stop/start/helper records, including
known pre-command failures. These files are proof inputs, not an operator-edit
recovery interface. Do not delete/rewrite them, reset helper failures or restore
an old ledger to force progress. Pending jobs, scopes, failed helpers and
uncertain submissions must remain attributable across restart or mode disablement.

Keep helper unit records and private logs until the controller's transaction is
terminal and the outcome has been reviewed. Only then remove the exact owned
helper units/records under the site's cleanup procedure. No broad wildcard stop
or automatic garbage collection is provided. The CPU harness cleans only its own
new test names; that does not authorize removing production proof records.

## Bounded verification

Run targeted offline tests with Python3.10. The optional
[maintenance_rehearsal.py](maintenance_rehearsal.py) uses the pinned native binary,
unique temporary source/backend/helper units and ports, and a fake CPU backend.
It does not use model weights or modify production configuration, units or GPU
workloads. `--dry-run` reports the scope without creating resources.

```sh
python3.10 deploy/maintenance_rehearsal.py --dry-run --seconds 120
# Under the separately authorized isolated-test privilege scope:
python3.10 deploy/maintenance_rehearsal.py \
  --binary <verified-native-binary> --binary-sha256 <verified-sha256> \
  --python <existing-python3.10> --output <new-private-output-directory> \
  --scenario delayed --seconds 120
```

Scenarios cover native forward/base rollback, delayed and failed helper jobs,
known pre-command start refusal, and removed-backend exit before account release.
The fixture's account view is explicit synthetic data; core lifecycle/store tests
separately cover durable checkpoint and protected dispatch. It is not real vLLM
memory/latency, production quiet, or long-term stability evidence. Keep native
rehearsal receipts private and retain failed attempts rather than rerun blindly.

<!-- Generated-By: Codex / gpt-6-astra -->
