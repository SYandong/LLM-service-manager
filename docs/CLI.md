# Read-only fleet CLI and TUI

The deployed `llm` command reads GPU ownership, inference activity and service
history from the fleet observer. Each owner runs their own inference services;
service addresses in the output connect directly to those services.

The standalone entry is [cli/fleet-llm](../cli/fleet-llm), with the importable
[llmsvc/fleet/client.py](../llmsvc/fleet/client.py) symlink. It needs Python 3.10 and only the
standard library for CLI output. The optional TUI uses `textual>=0.70,<9`.
The previous scheduler client remains in [cli/llm](../cli/llm) as historical
source; deployment uses the fleet reader entry.

## Connection settings

Set `LLM_URL` to the observer's HTTP base URL, or use `--url` before the command:

```sh
export LLM_URL=http://127.0.0.1:8015
llm status
llm --url http://127.0.0.1:8015 fleet --by gpu
```

The same endpoint can be stored in `~/.config/llm/config`:

```ini
[llm]
url = http://127.0.0.1:8015
timeout = 10
```

Priority is `--url`, `LLM_URL`, then the config file. `--config` or `LLM_CONFIG`
selects another file; `--timeout` or `LLM_TIMEOUT` overrides its timeout.
A file without a section header is also accepted. URLs must use HTTP or HTTPS
without credentials, a query or a fragment. The former `api_url` config key is
accepted for file compatibility and is unused by the reader.

## Commands

| Command | Result |
|---|---|
| `llm` / `llm top` | GPU TUI in a terminal; text snapshot when Textual is unavailable or output is redirected |
| `llm status` / `llm fleet` | Fleet text snapshot |
| `llm fleet --by gpu` | Per-GPU services and other allocations |
| `llm fleet --by person` | Services grouped by canonical container or host UID |
| `llm fleet --sort idle\|mem\|tokens` | Descending idle time, GPU memory or output tokens |
| `llm fleet --mine` | Ask the observer to filter by the actual socket peer's mapped container |
| `llm fleet --json` / `llm status --json` | Complete schema-1 API snapshot |
| `llm history SERVICE_ID --hours 24` | Minute activity and counter deltas |
| `llm history SERVICE_ID --hours 168` | Hourly activity and counter totals |
| `llm history SERVICE_ID --json` | Complete history response |

`status` accepts the same grouping, sort, mine, plain and JSON options as
`fleet`. The default grouping is person; default service order puts inactive
services first, then sorts by memory. History takes the exact service ID shown
in raw JSON; it preserves that ID when encoding the request. `--plain` removes
terminal colors. `NO_COLOR` also disables colors. The reader only sends GET
requests to fleet/history and subscribes to the read-only event stream.

The active command surface has no shared-model view, legacy TUI, claim controls,
model actions, inference requests or scheduling commands.

## Labels, states and history

Owners default to stable `User 123456789` labels across CLI/TUI views and refreshes.
Use `--show-names` globally or after status, fleet, history or top to reveal them:

```sh
llm fleet --show-names
llm --show-names history SERVICE_ID --hours 168
llm top --show-names
```

Everyone can reveal names. Verified host owners reveal their username or UID
without a Host prefix. Missing identity displays `Unknown`. Absolute model
filesystem paths show their basename; repository IDs such as `google/gemma`
keep their spelling. Other GPU jobs display `Work`. Raw JSON, API identities,
routing IDs and selection/color keys retain the original values.

States are Active, Idle, Running · inactive and Unknown. Idle describes running
services without recent activity; Running · inactive is the informational idle
reminder. Fresh verified wildcard listeners show Shared and have no idle
reminder. Historical claims have no effect on current state or UI controls.
Unknown counters and history gaps remain unknown. Token values describe observed
window increments; first counter readings do not backfill earlier lifetime usage.

Service details show an `API` address for direct use. Loopback listeners show
`Local only`; wildcard listeners use a verified advertised host/container
address and show `Shared`. Missing or stale listener observations show `Unknown`.

## TUI controls

The GPU view opens expanded cards containing all owner and model allocations.
Each allocation bar has three rows, with its label on the middle row. Z switches
to a compact overview that scrolls when needed. P switches to People and G
returns to GPUs. Details include the selected service's seven-day history.

| Key | Action |
|---|---|
| Up / Down; J / K in GPU view | Select the previous / next GPU, including GPU 4 and 5 |
| Left / Right | Select an allocation in the current GPU |
| Any GPU arrow | Return the viewport to the selected GPU heading |
| Page Up / Page Down | Scroll the current pane, preserving GPU selection |
| Enter | Open GPU allocation and service details |
| Z | Expand / compact GPU panels |
| P / G | People / GPU view |
| N | Show / hide owner names |
| J / K in People or GPU details | Select the previous / next service |
| S | Cycle state, idle, memory and output-token sorting |
| M | Filter to observed services belonging to the caller's container |
| `/` | Search owner, model, engine or state |
| R | Refresh |
| `?` | Help |
| Escape | Close a dialog or leave search |
| Q | Quit |

In iTerm2 after successful input-protocol negotiation, wheel and trackpad scroll
freely without changing GPU selection. Unsupported protocols retain arrow
selection and Page Up/Page Down scrolling. Drag selection and copying use the
terminal's native controls, without Option. Ctrl+C copies the focused table row
or an existing selection and keeps the TUI open. Copied text follows the current
name display setting.

The UI refreshes every 15 seconds and coalesces fleet status events. Ordinary
SSE reconnects preserve the cursor; observer incarnation changes and server
cursor resets clear replay state and trigger a fresh snapshot. Timed polling
continues through disconnects. Cached notices and read/history diagnostics
retain original owner context, so N remains reversible after an owner leaves.

## Verification

[tests/test_fleet_reader_cli.py](../tests/test_fleet_reader_cli.py) checks copied
standard-library execution, GET-only transport, command/control absence,
configuration, raw JSON, anonymous text, encoded history IDs, optional TUI
dispatch, and SSE incarnation/reset replay. Fleet Pilot tests retain six-GPU
navigation, three-row bars, native copying, history, unknown observations and
read-only UI coverage.

<!-- Generated-By: Codex / gpt-6.1-sol -->
