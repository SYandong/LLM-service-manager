# Activity source compatibility (#5 / #145)

`source-ip-formats.json` contains synthetic documentation addresses and source-derived formats. It is not a live capture or an executed upstream producer test. Existing v252 history lacks origins; supporting a future producer must not fabricate those origins.

The candidate source pin is `8fa85899e424d47b81fa60aff08b238a793e2e2b`, already inspected during #53. It is an inspection/test target, not an approved production version. The deployed v252 pin remains `e31a1adee494bb7a578e2a97ec891b3e809899dc`.

| Boundary | Exact behavior and evidence | Acceptance limit |
|---|---|---|
| Producer source selection | [`activitySource`](https://github.com/mostlygeek/llama-swap/blob/8fa85899e424d47b81fa60aff08b238a793e2e2b/internal/server/metrics.go#L118-L129) first uses the tailcat connection context. Otherwise it writes `ip:` plus the host from `net.SplitHostPort(RemoteAddr)`, with raw RemoteAddr fallback when splitting fails. It ignores forwarding headers. | This is source inspection. No spoofed-header HTTP test against the upstream binary was executed here. A proxy/NAT peer is not necessarily the original container. Non-IP context identities remain unknown to this IP-only reader. |
| Record coverage | [`CreateMetricsMiddleware`](https://github.com/mostlygeek/llama-swap/blob/8fa85899e424d47b81fa60aff08b238a793e2e2b/internal/server/metrics_middleware.go) records only eligible POST model endpoints with a resolved model, after the downstream handler returns. [`record`](https://github.com/mostlygeek/llama-swap/blob/8fa85899e424d47b81fa60aff08b238a793e2e2b/internal/server/metrics.go) assigns Src before handling client-closed, non-200, empty and invalid responses. | Those branches may persist a row with zero/partial tokens. A persisted request count is not a count of every request attempt, nor proof of complete token extraction. |
| Persistence | [`InsertActivity`](https://github.com/mostlygeek/llama-swap/blob/8fa85899e424d47b81fa60aff08b238a793e2e2b/internal/store/store.go#L194-L235) writes Src alongside timestamp/model/token fields. Despite its name, `queueMetrics` calls the store synchronously using a separate background context with a five-second timeout, allowing it to record after the request context was cancelled. It checks insertion success before event emission and prunes the in-memory store after insertion. | Insert failure can omit the row; events are not an authoritative repair log. A cancelled request still needs to reach the recording path. No production database upgrade or migration was run. |
| Consumer | `ActivityReader` uses the existing core `canonical_ip` for source literals and configured mapping keys. IPv6 compresses consistently and IPv4-mapped IPv6 resolves to the IPv4 identity. Equivalent keys with conflicting owners fail construction. Unmapped valid addresses retain `ip:<canonical address>`. | Mapping keys remain bare IP literals. A supplied map attributes the peer only; it is not authentication. Malformed/non-IP sources stay unknown without losing valid counts or tokens. |
| History | Mixed old-null/empty and new-source rows are aggregated separately in a temporary SQLite fixture. The reader preserves database bytes. | This fixture models compatibility; it does not execute the producer migration or reconstruct historical origins. Source-column precedence and existing legacy metadata fallback are retained. |

Run the bounded consumer regression with Python 3.10:

```sh
python -m pytest -q tests/test_activity_sources.py
```

This fixture adds IPv6/malformed-source coverage missing from the existing aggregation suite. It does not repeat the >21k timing benchmark as producer evidence. #25's already-accepted real retained-window reconciliation is separate and unchanged.

The remaining producer acceptance has a concrete shape: under a separately approved nonproduction producer test, send successful, failed and cancelled requests to a CPU fake backend; inspect only the resulting disposable activity rows. Verify direct peer identity, ignored spoofed forwarding headers, source-context precedence, failure/cancellation row and token coverage, and old-empty rows after the actual candidate migration. Compare the store before/after without assigning new sources to old rows. A target binary and its approved staging configuration are required before that test; this PR neither obtains nor launches a data plane.

Current source-production attribution, producer migration and long-term calibration are **NOT MEASURED** by these tests. `ordered_source=False` remains required for actual v252; source attribution does not provide continuous quiet, reload certification or loss detection.

<!-- Generated-By: Codex / gpt-6-astra -->
