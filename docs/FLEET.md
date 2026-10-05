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
also satisfy the unknown rule. The idle limit is a reminder, never permission
to act on a service.

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

## Storage and verification

The independent `fleet.sqlite` uses schema 1 and WAL with full synchronous
transactions. Raw service/GPU samples default to 14 days, hourly summaries to
180 days. Retention runs at most hourly after ingestion and cuts raw data at
whole-hour boundaries. Hourly summaries are incrementally maintained exactly
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

<!-- Generated-By: Codex / gpt-6.1-sol -->
