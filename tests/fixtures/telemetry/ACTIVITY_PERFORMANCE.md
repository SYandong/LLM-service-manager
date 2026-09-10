# Activity functional, budget and performance evidence (#191)

The original large fixtures contain **25,500 rows**. The expected 3601 is the
inclusive recent-hour aggregate, not the database size. A single failure followed
by isolated successes does not establish its cause; retain that historical
failure instead of treating repeat-to-green as diagnosis.

Ordinary pytest now checks the same large SQLite datasets with only the activity
module's monotonic clock controlled. It verifies all model groups, recent-window
counts and usage/token totals while still constructing the default80ms reader.
This is functional SQL/data coverage, **not** real elapsed-time evidence. The
production module/clock/budget are unchanged and no tests skip under load.

`tests/test_activity_errors.py` separately exercises the actual SQLite progress
handler at the original80ms budget, plus cancellation surfaced as empty schema
or rows. Failures remain unknown and cannot turn into fresh zero/cached counts.
Those tests are reused; large-fixture success does not replace them.

The real `<100ms` checks are retained in the explicit performance tool:

```sh
python tests/fixtures/telemetry/activity_performance.py --dry-run
# Only for a separately chosen measurement window, with an explicit load limit:
python tests/fixtures/telemetry/activity_performance.py --measure --max-load-per-cpu 0.5
```

The example load ratio is a benchmark-window choice, not a production threshold
or proof of an idle machine. The tool records source/fixture hashes, Python and
SQLite versions, affinity-visible CPU count, before/after1min load, actual window
and each real elapsed result. Its temporary databases are synthetic; it does not
read production activity or run models. It uses the unchanged80ms reader, tests
correct results AND `<100ms`, and never retries. A cancelled empty result reports
unknown counts/failure even if it returned quickly. Load-gated/inconclusive output
has exit2, actual measured failure exit1, and a passing admitted observation
exit0; none promises future latency, an hour of stability or a causal diagnosis.

This #191 repair does not run fresh live/performance measurements. Existing
published measurements retain their original source/window limits. A future
performance result must be recorded as its own bounded artifact, not folded into
functional-suite success or public private-derived claims without clearance.

<!-- Generated-By: Codex / gpt-6-astra -->

## Query-work regression (#167)

`tests/test_activity_query_cost.py` compares SQLite VM instruction counts on the
existing 25,500-row synthetic fixture with a controlled clock. The reader now
uses the summary's latest timestamp and the producer's existing model/time/ID
index to seek the highest-ID row, instead of ranking every historical row for
source lookup. Counts and source selection stay in one read transaction; old-only
models, future rows, timestamp ties and unknown latest origins retain their
meaning. Missing indexes do not change correctness; no index or database write
is performed by the reader.

The regression requires the entire read to use less work than the old latest-row
query alone. It is an algorithmic-work check, not a new wall-clock benchmark,
lock-availability guarantee or proof that arbitrary host scheduling fits 80ms.
Existing real cancellation/lock replay remains authoritative for unknown/error
handling. Runtime 80ms and collector 1.8s limits are unchanged; source attribution
and live error-frequency acceptance remain separate.

<!-- Generated-By: Codex / gpt-6-astra -->
