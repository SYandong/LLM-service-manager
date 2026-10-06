# Fleet observation and declarations (#307)

Fleet reads the schema-1 host export and records observations about independent
inference services. It never forwards requests, edits their configuration, or
stops their processes. `fleet_enabled: false` is the default: no history
database is opened or created and no fleet worker runs. The scheduler's
`--dry-run`, `--once`, `--check-config` and sampling-only startup also start no
fleet writer. Enabling fleet explicitly starts one ingestion worker; claims
and ingestion share a serialized SQLite connection independent of the managed
model intent store and its action lock.

The example settings are in
[scheduler.example.yaml](../deploy/scheduler.example.yaml). Snapshot reads have
a 2 MiB limit and reject unsupported schemas, duplicate keys, non-finite
numbers, unsafe file types, symlinks and group/world-writable exports. The
configured export directory is supplied by the administrator as a read-only
host mount. `fleet_ingest_interval_seconds` is the file read cadence; observed
activity uses actual host sample intervals, normally 60 seconds.

## HTTP contract

| Request | Result |
|---|---|
| `GET /v1/fleet` | Schema 1 overview, per-card occupants, container summaries, service states, 24h/7d windows and 24 hourly activity values |
| `GET /v1/fleet?mine=1` | The same shape, services/occupants filtered to the actual socket peer's mapped container; an unmapped peer returns 403 |
| `GET /v1/fleet/history?service=ID&hours=24` | `resolution: minute`, `samples` containing `ts`, gauges, `d_requests`, `d_gen_tokens`, `d_prompt_tokens`, `active`, `scrape_ok`, `observed_seconds`, `active_minutes`, `gap`, `counter_reset` |
| `GET /v1/fleet/history?service=ID&hours=168` | `resolution: hourly`, calendar-hour points covering the requested range, including partial boundaries, with `ts`/`hour_ts`, `requests`, `gen_tokens`, `prompt_tokens`, `active_minutes`, `observed_seconds`, `coverage_ratio`; unobserved buckets retain null activity/counters |
| `POST /v1/fleet/claims` | `{service_id,until,reason}` where `until` is finite epoch seconds in the future, at most the configured 1–7 days, and `reason` is 1–200 characters |
| `DELETE /v1/fleet/claims/ID` | Revoke a declaration owned by the mapped calling container; an already revoked declaration retains its original revocation time |

Overview responses expose `pid` and display metadata but exclude the full
command line and model path. History contains a `service` detail with only the
redacted `argv_redacted` summary. Host services have `host: true` and a null
container; unknown models and other-process ownership may also be null.
`mine` is false for host or unmapped services.

Schema 1 adds host ownership metadata for GPU allocations (#339). Every GPU
occupant carries `host`, `host_uid` and `host_user`; services and historical
service details also expose nullable `host_uid` / `host_user`. For other jobs,
`host: true` requires a verified host namespace identity, null container and a
real UID (integer 0–4294967295). `host: false` describes a resolved container,
with null host UID/user. Unresolved ownership has all three fields and container
null. A null container alone does not establish host ownership. Older schema-1
exports remain accepted; their null-container other jobs stay unknown and their
resolved-container jobs use `host: false`.

Host usernames come from the configured bounded local passwd file and are
nullable labels of at most 128 characters, without control characters. A failed
or ambiguous name lookup retains the verified UID with a null username. Host
LLMs and other jobs group by UID. Fleet text and the TUI default to stable
`User 123456789` labels. `--show-names` in the CLI or N in the TUI reveals the
username or `UID <uid>`, without a Host prefix. Everyone can reveal names;
this setting requires no authentication. Missing owner identity displays
`Unknown`.
Container summaries separate host LLM users by UID and include `host`,
`host_uid` and `host_user`, retaining a null container. These labels are
observations, not caller authentication. Socket-peer ownership and claims keep
their existing rules. GPU occupants contain no process command line or comm.

Anonymous labels use SHA-256 of `container:<name>` or `host:uid:<uid>`, modulo
1,000,000,000 with nine decimal digits. They stay stable across CLI/TUI views,
refreshes and history, and keep host and container identities separate. Known
owner names in visible IDs, parameters, copied text and diagnostics use the
same setting. Absolute model paths display only the basename; repository IDs
such as `google/gemma` remain intact. Other GPU jobs display `Work`. API data,
raw service IDs, selection/color keys, requests and `--json` are unchanged.
The display setting does not conceal identities in the API.

Up/Down selects every observed GPU, including GPU 4 and 5 in tall expanded
views. Any arrow key returns the viewport to the selected GPU heading;
Left/Right selects its allocations. In iTerm2 with successful input-protocol
negotiation, wheel/trackpad scrolls freely and keeps selection. Unsupported
protocols retain arrow selection and Page Up/Page Down scrolling. Compact bars
have three rows, with labels on the
middle row, and scroll when needed. `Memory readings differ` reports an excess
of attributed memory over measured use without changing either amount; the
sequential observations can differ in time.

Services also expose `api_address` (HTTP base URI or null), `api_access`
(`shared`, `local_only`, `direct`, `unknown`) and `idle_time_sensitive`.
Loopback bindings are Local only. Fresh verified wildcard bindings are Shared
and use a trusted advertised container/host IP with the observed port. Container
IPs come from the fresh owner export; host IPs use configured `collectors.host_ips`.
A verified dual-stack IPv6 listener may use an IPv4 owner address. Specific
nonloopback bindings expose their direct address. Missing or stale listener
observations have unknown access. Address metadata adds no routing or process actions.

History includes `start_at` / `end_at` epoch bounds. Hourly output pads gaps
and includes partial calendar-hour boundaries, so a 168-hour interval has up
to 169 points; `partial` and observed coverage describe each boundary bucket.

The writes return `{ok:true,dry_run:false,claim:{id,instance_id,service_id,container,model,
until,reason,created_by_container,created_at,revoked_at}}`. Both accept
`?dry_run=1`; POST previews have no allocated ID. DELETE previews include the
existing ID and proposed revocation time. Previews do not open a new database,
allocate an ID, change history, or emit scheduler events. They do write the
structured `fleet_claim` / `fleet_unclaim` preview log. A new declaration
replaces the previous effective declaration for that instance while preserving
both records in history. A lost write response has unknown outcome; clients
should refresh rather than automatically repeat it.
`service_id` and `instance_id` name the same instance in every public claim.

`fleet_claims_enabled` controls declarations independently of scheduler
`read_only`; declarations can be used with a read-only managed-model
scheduler and never enable model actions. Disabled fleet returns 503
`fleet_disabled`; disabled claims return 405 `fleet_claims_disabled`. Invalid
input is 400, an unknown service/claim is 404, foreign ownership is 403, an
unobserved/ended/stale target is 409, and an unavailable history store is 503.

Ownership comes exclusively from the HTTP socket peer and a freshly read
`collectors.ip_containers_path` host export. Its `generated_at` must be within
`fleet_stale_after_seconds`, with no future timestamp, malformed or conflicting
canonical IP entries. Missing, stale or unmapped exports return 403 for claims
and `mine=1`. Static collector labels, request body ownership fields and
forwarded headers cannot supply or override this identity. V1 provides no
administrator override.

`/v1/events` emits `fleet_status_changed` when a previously observed service
changes state. Its `detail` is `{service_id,from,to}`; events use the existing
scheduler SSE history and cursor. Timer-driven freshness/expiry changes are
checked on each ingestion tick, even when the host export is unchanged.

## Observations, counters and coverage

The presentation policy is pure. Priority is `unknown`, `claimed`, `active`,
`over_limit`, `idle`. A stale/unreadable export, unsupported activity engine,
failed latest scrape, missing discovery, or an interval whose activity cannot
be localized yields `unknown` even with a declaration. Three failed scrapes
also satisfy the unknown rule. The idle limit is an informational reminder,
shown as `Running · inactive` in the TUI. Fresh verified wildcard services default
to Shared and are exempt from this reminder.

Counter baselines and the `generated_at` watermark commit atomically with
instance metadata, raw samples and hourly rollups. Duplicate and older exports
are ignored after restart. Instance IDs bind PID, start time, engine and
container; conflicting identities roll back the entire ingestion transaction.
First observations and negative counter resets have zero delta. Available
`*_created` epochs and the counter-series identity fingerprint also break the
delta chain, including resets whose new total exceeds the old total. Resets
without a changed advertised epoch/signature or a negative total cannot be
detected and remain a measurement limitation.

Failed scrapes preserve the last successful per-counter baseline. A later
success keeps the accumulated difference; it does not attribute that unknown
interval to active minutes or invent a precise last-request time.
Missing services in an incomplete discovery also record an unknown sample and
break the observed interval while preserving their successful baselines.
Intervals over five minutes keep counter differences and record `gap: 1`, with zero
observed duration. Consecutive valid intervals are capped at twice the host
nominal interval, and coverage is split across hour boundaries. Ollama expiry
postponement is an activity proxy; request/token counters remain null.
Unsupported engines and missing signals also retain null activity.

`last_active_at` is a genuinely observed active sample, or null. No observed
active sample since `first_seen` gives `never_active: true`; this does not prove
the service was never used before observation. Its start time never substitutes for an
observed request. `idle_seconds` is the accumulated observed idle lower bound,
also exposed as `idle_observed_seconds`. Gaps and failures reset this bound;
unknown current observations yield null `idle_seconds`. A service first
discovered after weeks of uptime starts with zero observed idle time.

Window `active_ratio` is active seconds divided by `min(window,uptime)` as in
the public plan. `observed_active_ratio` divides by observed seconds instead;
`observed_seconds` and `coverage_ratio` disclose the observation coverage.
Activity/ratios are null without observed coverage. Hourly bars are null when
unobserved and otherwise contain measured active minutes (0–60), including
partial coverage. Historical gaps are not filled as idle zeros.

Only snapshots with `inventory_complete: true` may end a missing instance.
`gpu_inventory_complete`, `gpu_attribution_complete` and per-service
`gpu_observation_complete` preserve partial/failed GPU observations separately;
an empty failed GPU query does not assert zero memory use.
Missing discovery or a stale snapshot makes per-service `gpu_gb` null and
`gpu_observation_complete` false, including retained instance metadata.

The host scanner uses a separate `gpu_query_timeout_seconds` for each GPU and
compute-process query (default 5 seconds); HTTP targets retain their 2-second
limit. Every operation remains bounded by the remaining 20-second scan budget.
Failed GPU queries retain completeness errors and unknown observations; measured
card usage with missing process attribution remains unattributed rather than
free memory. Host jobs are observed and never become fleet process actions.

## Storage and verification

The independent `fleet.sqlite` uses schema 1 and WAL with full synchronous
transactions. Raw service/GPU samples default to 14 days, hourly summaries to
180 days. The explicitly enabled fleet worker runs retention at most hourly in
a separate transaction, including during stale, missing, invalid or repeated
exports. Retention opens only an existing database; it creates no database or
directory before the first successful ingestion. Disabled fleet and modes that
start no fleet worker perform no retention writes. Both retention tiers keep
partial boundary hours. Hourly summaries are incrementally maintained exactly
once; overview windows combine complete hourly buckets with raw boundary
intervals, avoiding overlap and loading only aggregate rows. Claims and the
successful counter baselines survive raw retention. History is bounded to
24/168 hours; raw reads have a row limit.
Boundary queries include the maximum accepted five-minute observation
interval. Coverage is clipped to exact interval bounds; an hour-boundary
counter belongs to the next bucket, while the requested final counter endpoint
is included once.

`tests/test_fleet.py` uses synthetic inputs and the host scanner's actual
synthetic schema fixture. It covers state replays, counter and identity epochs,
restart dedupe, out-of-order exports, gaps, failed/partial observations, safe
reads, retention, claims, live loopback HTTP, no-database preview/default
paths, worker shutdown and a 21k-row overview query budget of 100 ms. These
tests establish code behavior, not a production shadow-run or live accuracy
receipt.

Process-session usage can be retained beyond database cleanup with
[compressed session logs](FLEET_SESSION_LOGS.md).

<!-- Generated-By: Codex / gpt-6.1-sol -->
<!-- Generated-By: Codex / unknown model -->
