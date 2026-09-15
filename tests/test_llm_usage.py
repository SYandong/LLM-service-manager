# Generated-By: Codex / gpt-6-astra
# Generated-By: OpenCode / deepseek-v4.1-flash
"""CLI usage reconciliation against a disposable /v1/usage/report fake server.

The report endpoint is implemented in a parallel change, so these tests never
depend on it: a threaded stdlib HTTP server answers only the new path from a
synthetic, internally consistent dataset.
"""

import json
import runpy
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

COUNT_KEYS = ("requests", "errors", "input_tokens", "output_tokens",
              "total_tokens", "untracked_requests", "duration_ms")
USAGE_OTHER = {"user": "model", "model": "user", "day": "model"}
UNTIL = 1757971080


def counts(requests=0, errors=0, input_tokens=0, output_tokens=0,
           untracked_requests=0, duration_ms=0):
    return {"requests": requests, "errors": errors, "input_tokens": input_tokens,
            "output_tokens": output_tokens, "total_tokens": input_tokens + output_tokens,
            "untracked_requests": untracked_requests, "duration_ms": duration_ms}


def accumulate(target, source):
    for key in COUNT_KEYS:
        target[key] += source[key]


# One record per (user, model, day) contribution; the window picks ages < days.
RECORDS = (
    {"user": "host", "user_kind": "host", "model": "gemma-4-26b-a4b-nvfp4",
     "day": "2026-09-15", "age_days": 0, "last_offset": 120,
     "counts": counts(1000, 0, 353000, 17800, 0, 3200000)},
    {"user": "host", "user_kind": "host", "model": "qwen3.8-27b",
     "day": "2026-09-15", "age_days": 1, "last_offset": 180,
     "counts": counts(204, 0, 49110, 2501, 0, 532400)},
    {"user": "yandong", "user_kind": "container", "model": "gemma-4-31b-it-nvfp4",
     "day": "2026-09-14", "age_days": 1, "last_offset": 3600,
     "counts": counts(900, 3, 150000, 18000, 0, 1710000)},
    {"user": "ip:192.0.2.2", "user_kind": "ip", "model": "qwen3-coder-30b",
     "day": "2026-09-13", "age_days": 3, "last_offset": 7200,
     "counts": counts(25, 0, 2000, 500, 0, 20000)},
    {"user": "unattributed", "user_kind": "unattributed", "model": "qwen3-coder-30b",
     "day": "2026-09-10", "age_days": 5, "last_offset": 3 * 86400,
     "counts": counts(49, 0, 5109, 495, 12, 40000)},
    {"user": "unattributed", "user_kind": "unattributed", "model": "qwen2.5-7b-instruct",
     "day": "2026-09-10", "age_days": 5, "last_offset": 3 * 86400,
     "counts": counts(32, 0, 4000, 300, 0, 24800)},
    {"user": "old-user", "user_kind": "container", "model": "legacy-model",
     "day": "2026-08-20", "age_days": 20, "last_offset": 20 * 86400,
     "counts": counts(7, 1, 700, 70, 0, 7000)},
)


def build_report(days=7, by="user", *, map_source="config", until=UNTIL):
    """Aggregate the synthetic records exactly as the contract describes."""
    other_key = USAGE_OTHER[by]
    selected = [record for record in RECORDS if record["age_days"] < days]
    groups, order = {}, []
    for record in selected:
        key = record["day"] if by == "day" else record[by]
        group = groups.get(key)
        if group is None:
            group = {"counts": counts(), "kind": record["user_kind"] if by == "user" else by,
                     "breakdown": {}, "first": [], "last": []}
            groups[key] = group
            order.append(key)
        accumulate(group["counts"], record["counts"])
        label = record[other_key]
        item = group["breakdown"].setdefault(label, {"counts": counts(), "kind": None})
        item["kind"] = record["user_kind"] if other_key == "user" else "model"
        accumulate(item["counts"], record["counts"])
        group["first"].append(UNTIL - (record["age_days"] + 1) * 86400)
        group["last"].append(until - record["last_offset"])
    rows = []
    for key in order:
        group = groups[key]
        row = dict(group["counts"])
        row[by], row["kind"] = key, group["kind"]
        row["first_seen"], row["last_seen"] = min(group["first"]), max(group["last"])
        row["breakdown"] = []
        for label, item in group["breakdown"].items():
            item_row = dict(item["counts"])
            item_row[other_key], item_row["kind"] = label, item["kind"]
            row["breakdown"].append(item_row)
        rows.append(row)
    if by == "day":
        rows.sort(key=lambda row: row["day"])
    totals = counts()
    for row in rows:
        accumulate(totals, row)
    return {"days": days, "by": by, "since": until - days * 86400, "until": until,
            "known": True, "error": None, "timezone": "Asia/Shanghai",
            "attribution": {"mode": "client_ip", "map_source": map_source,
                            "map_updated_at": until - 100000, "mapped_ips": 1},
            "rows": rows, "totals": totals}


def unknown_report(days=7, by="user", error="usage_not_configured"):
    return {"days": days, "by": by, "since": None, "until": None, "known": False,
            "error": error, "timezone": "Asia/Shanghai", "attribution": None,
            "rows": [], "totals": {key: None for key in COUNT_KEYS}}


class UsageHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        service = self.server.service
        service.paths.append(self.path)
        parsed = urlsplit(self.path)
        if parsed.path != "/v1/usage/report":
            self._send(404, {"error": "not_found"})
            return
        query = parse_qs(parsed.query)
        days, by = query.get("days", []), query.get("by", [])
        valid = (len(days) == 1 and days[0].isdigit() and 1 <= int(days[0]) <= 365
                 and len(by) == 1 and by[0] in ("user", "model", "day")
                 and set(query) <= {"days", "by"})
        if not valid:
            self._send(400, {"error": "invalid_usage_query"})
            return
        days, by = int(days[0]), by[0]
        builder = service.scheduler._usage
        if builder is None:
            self._send(503, unknown_report(days=days, by=by))
            return
        try:
            payload = builder(days=days, by=by)
        except Exception:
            self._send(503, unknown_report(days=days, by=by, error="usage_unavailable"))
            return
        self._send(200, payload)


@pytest.fixture
def usage_api():
    return runpy.run_path(str(Path(__file__).resolve().parents[1] / "cli" / "llm"))


@pytest.fixture
def usage_service():
    service = SimpleNamespace(paths=[], scheduler=SimpleNamespace(_usage=build_report),
                              backend=build_report, url=None)
    server = ThreadingHTTPServer(("127.0.0.1", 0), UsageHandler)
    server.service = service
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    service.url = "http://127.0.0.1:%d" % server.server_address[1]
    try:
        yield service
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def execute(usage_api, usage_service, argv):
    args = usage_api["build_parser"]().parse_args(["usage", *argv])
    result = usage_api["execute_command"](args, usage_api["SchedulerClient"](usage_service.url))
    return args, result


@pytest.mark.parametrize("days", [1, 7, 30])
@pytest.mark.parametrize("by", ["user", "model", "day"])
def test_http_totals_match_backend_and_render_without_rounding(usage_api, usage_service, days, by):
    args, result = execute(usage_api, usage_service, ["--days", str(days), "--by", by])
    expected = build_report(days=days, by=by)
    assert result == json.loads(json.dumps(expected))
    assert result["totals"] == expected["totals"]
    assert "/v1/usage/report?days=%s&by=%s" % (days, by) in usage_service.paths
    text = usage_api["format_result"](args, result, width=200)
    for key in ("requests", "errors", "input_tokens", "output_tokens", "total_tokens"):
        assert usage_api["usage_number"](result["totals"][key]) in text
    assert all(usage_api["cell_width"](line) <= 40 for line in
               usage_api["format_usage"](result, width=40).splitlines())


def test_exact_large_counts_are_not_rounded(usage_api):
    large = 9007199254740993
    row = counts(1, 0, large, 0, 0, 0)
    row.update({"user": "host", "kind": "host", "first_seen": UNTIL - 10, "last_seen": UNTIL})
    breakdown = counts(1, 0, large, 0, 0, 0)
    breakdown.update({"model": "model-x", "kind": "model"})
    row["breakdown"] = [breakdown]
    result = {"days": 7, "by": "user", "since": UNTIL - 7 * 86400, "until": UNTIL,
              "known": True, "error": None, "timezone": "Asia/Shanghai",
              "attribution": {"mode": "client_ip", "map_source": "config",
                              "map_updated_at": UNTIL, "mapped_ips": 1},
              "rows": [row], "totals": counts(1, 0, large, 0, 0, 0)}
    text = usage_api["format_usage"](result, width=120)
    assert "9,007,199,254,740,993" in text
    assert "9,007,199,254,740,992" not in text


def test_kinds_legend_and_untracked_footnote(usage_api, usage_service):
    args, result = execute(usage_api, usage_service, ["--days", "7", "--by", "user"])
    text = usage_api["format_result"](args, result, width=200)
    assert "host = requests from the host machine" in text
    assert "unattributed = recorded before source tracking (no client IP)" in text
    assert "ip:<addr> = client address without a container mapping" in text
    assert "12 requests have no token counts (streaming without stream_options.include_usage)" in text
    assert "563,219 input" in text


def test_breakdown_subrows(usage_api, usage_service):
    args, result = execute(usage_api, usage_service, ["--days", "7", "--by", "user", "--breakdown"])
    text = usage_api["format_result"](args, result, width=200)
    assert "  └ " in text
    assert text.count("└") == 6
    assert "└ gemma-4" in text and "└ qwen3.8" in text
    assert usage_api["format_result"](args, result, width=200) != usage_api["format_result"](
        usage_api["build_parser"]().parse_args(["usage", "--days", "7", "--by", "user"]),
        result, width=200)


@pytest.mark.parametrize("width,model,avg,last,errors,stacked", [
    (120, True, True, True, True, False),
    (100, True, True, True, True, False),
    (80, True, False, False, True, False),
    (60, False, False, False, True, False),
    (50, False, False, False, False, True),
])
def test_responsive_widths(usage_api, usage_service, width, model, avg, last, errors, stacked):
    _, result = execute(usage_api, usage_service, ["--days", "7", "--by", "user"])
    text = usage_api["format_usage"](result, width=width)
    assert ("MODELS" in text) is model
    assert ("AVG TIME" in text) is avg
    assert ("LAST SEEN" in text) is last
    assert ("ERRORS" in text) is errors
    assert ("host (host)" in text) is stacked
    assert all(usage_api["cell_width"](line) <= width for line in text.splitlines())


def test_responsive_width_keeps_models_with_large_counts(usage_api):
    rows = []
    for user, requests, errors, in_tok, out_tok, last_offset in (
        ("container-team-alpha-0001", 12345678, 5, 87654321, 12345678, 120),
        ("container-team-beta-00022", 23456789, 0, 76543210, 23456789, 3600),
        ("container-team-gamma-333", 34567890, 1, 65432109, 34567890, 7200),
    ):
        row = counts(requests, errors, in_tok, out_tok, 0, requests * 80)
        row.update({"user": user, "kind": "container", "first_seen": UNTIL - 8 * 86400,
                    "last_seen": UNTIL - last_offset})
        item = counts(requests, errors, in_tok, out_tok, 0, requests * 80)
        item.update({"model": "model-" + user[-4:], "kind": "model"})
        row["breakdown"] = [item]
        rows.append(row)
    totals = counts()
    for row in rows:
        accumulate(totals, row)
    result = {"days": 7, "by": "user", "since": UNTIL - 7 * 86400, "until": UNTIL,
              "known": True, "error": None, "timezone": "UTC",
              "attribution": {"mode": "client_ip", "map_source": "config",
                              "map_updated_at": UNTIL, "mapped_ips": 1},
              "rows": rows, "totals": totals}
    text = usage_api["format_usage"](result, width=100)
    assert "MODELS" in text
    assert "AVG TIME" not in text
    assert "LAST SEEN" not in text
    assert all(usage_api["cell_width"](line) <= 100 for line in text.splitlines())


def test_breakdown_label_width_covers_subrows(usage_api, usage_service):
    args, result = execute(usage_api, usage_service, ["--days", "7", "--by", "user", "--breakdown"])
    text = usage_api["format_result"](args, result, width=200)
    assert "  └ qwen2.5-7b-instruct" in text
    without = usage_api["format_usage"](result, width=200)
    assert "└" not in without
    header = next(line for line in without.splitlines() if line.startswith("USER"))
    assert header.index("REQUESTS") == 14


def test_footnote_wraps_on_spaces(usage_api, usage_service):
    _, result = execute(usage_api, usage_service, ["--days", "7", "--by", "user"])
    text = usage_api["format_usage"](result, width=30)
    assert "counts (streaming without" in text
    assert all(usage_api["cell_width"](line) <= 30 for line in text.splitlines())


def test_total_row_has_blank_last_seen(usage_api, usage_service):
    _, result = execute(usage_api, usage_service, ["--days", "7", "--by", "user"])
    text = usage_api["format_usage"](result, width=120)
    total = next(line for line in text.splitlines() if line.startswith("TOTAL"))
    assert "LAST SEEN" in text
    assert "—" not in total
    assert "2.5s" in total


def test_by_day_uses_day_label_and_drops_last_seen(usage_api, usage_service):
    args, result = execute(usage_api, usage_service, ["--days", "30", "--by", "day"])
    text = usage_api["format_result"](args, result, width=120)
    assert "DAY" in text
    assert "LAST SEEN" not in text
    assert "2026-09-10" in text and "2026-08-20" in text


def test_json_passthrough(usage_api, usage_service):
    args, result = execute(usage_api, usage_service, ["--days", "7", "--by", "model", "--json"])
    output = usage_api["format_result"](args, result, width=40)
    assert json.loads(output) == json.loads(json.dumps(build_report(days=7, by="model")))


@pytest.mark.parametrize("as_json", [False, True])
def test_copied_cli_success_and_unknown_exit_status(tmp_path, usage_service, as_json):
    script = tmp_path / "llm"
    shutil.copyfile(Path(__file__).resolve().parents[1] / "cli" / "llm", script)
    command = [sys.executable, "-I", "-S", str(script), "--url", usage_service.url,
               "--config", str(tmp_path / "missing"), "usage", "--days", "7"]
    if as_json:
        command.append("--json")
    result = subprocess.run(command, cwd=tmp_path, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    if as_json:
        assert json.loads(result.stdout)["totals"]["requests"] == 2210
    else:
        assert "TOTAL" in result.stdout and "2210" not in result.stdout
        assert "2,210" in result.stdout

    usage_service.scheduler._usage = None
    result = subprocess.run(command, cwd=tmp_path, text=True, capture_output=True, timeout=10)
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    if as_json:
        payload = json.loads(result.stdout)
        assert payload["known"] is False
        assert all(value is None for value in payload["totals"].values())
    else:
        assert "Unavailable: usage_not_configured" in result.stdout
        assert "Requests ?  Tokens ?" in result.stdout
        assert "TOTAL" not in result.stdout


def test_attribution_none_footnote_and_unknown_zone(usage_api):
    result = build_report(days=7, by="user", map_source="none")
    text = usage_api["format_usage"](result, width=200)
    assert "Attribution map: none (all sources shown as ip:…)" in text
    result["timezone"] = "No/Such_Zone"
    fallback = usage_api["format_usage"](result, width=200)
    assert "(UTC)" in fallback


def test_available_empty_window_is_known_zero(usage_api):
    result = build_report(days=1, by="user")
    result["rows"] = []
    result["totals"] = counts()
    text = usage_api["format_usage"](result, width=120)
    assert "No requests in this window (source available)" in text
    assert "TOTAL" in text
    assert "Unavailable" not in text


@pytest.mark.parametrize("args", [["--days", "0"], ["--days", "366"], ["--days", "7.5"],
                                  ["--by", "ip"], ["--by", "container"]])
def test_invalid_usage_parameters(usage_api, args):
    with pytest.raises(SystemExit) as error:
        usage_api["build_parser"]().parse_args(["usage", *args])
    assert error.value.code == 2


def test_unknown_window_is_rejected(usage_api):
    with pytest.raises(usage_api["ClientError"]):
        usage_api["validate_usage"](build_report(days=7, by="user"), days=30, by="user")
    with pytest.raises(usage_api["ClientError"]):
        usage_api["validate_usage"](build_report(days=7, by="user"), by="model")


def test_response_invariants_are_rejected(usage_api):
    result = build_report(days=7, by="user")
    result["totals"]["requests"] += 1
    with pytest.raises(usage_api["ClientError"]):
        usage_api["validate_usage"](result)

    result = build_report(days=7, by="user")
    result["rows"][0]["input_tokens"] = -1
    with pytest.raises(usage_api["ClientError"]):
        usage_api["validate_usage"](result)

    result = build_report(days=7, by="user")
    result["rows"][0]["breakdown"][0]["requests"] = 1
    with pytest.raises(usage_api["ClientError"]):
        usage_api["validate_usage"](result)

    result = build_report(days=7, by="user")
    del result["rows"][0]["breakdown"]
    with pytest.raises(usage_api["ClientError"]):
        usage_api["validate_usage"](result)

    result = build_report(days=7, by="user")
    result["rows"][0]["requests"] = 1.5
    with pytest.raises(usage_api["ClientError"]):
        usage_api["validate_usage"](result)

    result = build_report(days=7, by="user")
    result["known"] = False
    with pytest.raises(usage_api["ClientError"]):
        usage_api["validate_usage"](result)


def test_unknown_totals_must_be_null(usage_api):
    result = unknown_report()
    usage_api["validate_usage"](result, days=7, by="user")
    result["totals"]["requests"] = 0
    with pytest.raises(usage_api["ClientError"]):
        usage_api["validate_usage"](result)
