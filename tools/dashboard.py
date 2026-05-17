from __future__ import annotations

import json
import socket
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, Sparkline, Static
import yaml


ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / "var" / "log" / "vllm.log"
CONFIG_PATH = ROOT / "config" / "server.yaml"
PID_FILE = ROOT / "var" / "run" / "vllm.pid"
UTC_OFFSET = timedelta(hours=8)
REFRESH = 10


def _get_start_time(pid: int):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        idx = stat.rfind(")")
        fields = stat[idx + 2 :].split()
        return int(fields[19])
    except (FileNotFoundError, IndexError, ValueError):
        return None


def service_state():
    if not PID_FILE.exists():
        return False, None, None
    try:
        data = json.loads(PID_FILE.read_text())
        pid = data["pid"]
        actual_start = _get_start_time(pid)
        if actual_start != data.get("start_time"):
            return False, None, None
        return True, pid, data.get("model")
    except (OSError, ValueError, KeyError, TypeError):
        return False, None, None


def load_config():
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open() as handle:
        return yaml.safe_load(handle) or {}


def get_display_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def categorize(line):
    if not line.startswith("(APIServer pid="):
        return "other"
    rest = line.split(") ", 1)[1]
    if rest.startswith("INFO:     "):
        return "access" if rest[10:].split(" ", 2)[1] == "-" else "other"
    parts = rest.split(" ", 3)
    if len(parts) == 4 and parts[3].startswith("["):
        module = parts[3][1:].split(":")[0]
        if parts[0] == "INFO" and module == "loggers.py":
            return "stats"
        if parts[0] == "ERROR" and module == "serving.py":
            return "request_error"
    return "other"


def parse_stats_timestamp(date_part, time_part, reference):
    ts_utc = datetime.strptime(
        f"{reference.year}-{date_part} {time_part}",
        "%Y-%m-%d %H:%M:%S",
    )
    ts_local = ts_utc + UTC_OFFSET
    if ts_local - reference > timedelta(days=1):
        ts_utc = ts_utc.replace(year=ts_utc.year - 1)
        ts_local = ts_utc + UTC_OFFSET
    return ts_local


def parse_stats_line(line, reference):
    rest = line.split(") ", 1)[1]
    parts = rest.split(" ", 3)
    ts = parse_stats_timestamp(parts[1], parts[2], reference)
    msg = parts[3].split("] ", 1)[1]
    prompt_tps = float(msg.split("Avg prompt throughput: ")[1].split(" tokens/s")[0])
    gen_tps = float(msg.split("Avg generation throughput: ")[1].split(" tokens/s")[0])
    running = int(msg.split("Running: ")[1].split(" reqs")[0])
    waiting = int(msg.split("Waiting: ")[1].split(" reqs")[0])
    kv = float(msg.split("GPU KV cache usage: ")[1].split("%")[0])
    prefix = float(msg.split("Prefix cache hit rate: ")[1].split("%")[0])
    return ts, gen_tps, prompt_tps, running, waiting, kv, prefix


def interval_midpoint(start, end):
    return start + (end - start) / 2


def flush_pending(pending, bucket, request_counts, success_counts, failed_counts):
    for status in pending:
        request_counts[bucket] += 1
        if 200 <= status < 300:
            success_counts[bucket] += 1
        else:
            failed_counts[bucket] += 1
    pending.clear()


BACKEND = SimpleNamespace(
    Counter=Counter,
    LOG_PATH=LOG_PATH,
    REFRESH=REFRESH,
    UTC_OFFSET=UTC_OFFSET,
    categorize=categorize,
    datetime=datetime,
    defaultdict=defaultdict,
    flush_pending=flush_pending,
    get_display_ip=get_display_ip,
    interval_midpoint=interval_midpoint,
    load_config=load_config,
    parse_stats_line=parse_stats_line,
    parse_stats_timestamp=parse_stats_timestamp,
    service_state=service_state,
    timedelta=timedelta,
)

RANGE_OPTIONS = {
    "hour": {
        "label": "hour",
        "window": BACKEND.timedelta(hours=1),
        "bucket": BACKEND.timedelta(minutes=5),
    },
    "day": {
        "label": "day",
        "window": BACKEND.timedelta(hours=24),
        "bucket": BACKEND.timedelta(minutes=30),
    },
    "week": {
        "label": "week",
        "window": BACKEND.timedelta(days=7),
        "bucket": BACKEND.timedelta(hours=6),
    },
}


def format_rate(success, total):
    return f"{100 * success / total:.1f}%" if total else "—"


def highlight_text(text, style):
    rendered = Text(str(text))
    rendered.stylize(style)
    return rendered


def highlight_fraction(text, value, invert=False):
    value = max(0.0, min(1.0, value))
    if invert:
        if value >= 0.85:
            style = "bold green"
        elif value >= 0.6:
            style = "bold yellow"
        elif value >= 0.3:
            style = "yellow"
        else:
            style = "bold red"
    else:
        if value >= 0.85:
            style = "bold red"
        elif value >= 0.6:
            style = "bold yellow"
        elif value >= 0.3:
            style = "yellow"
        else:
            style = "dim"
    return highlight_text(text, style)


def highlight_positive_fraction(text, value):
    value = max(0.0, min(1.0, value))
    if value >= 0.85:
        style = "bold green"
    elif value >= 0.6:
        style = "green"
    else:
        style = "dim"
    return highlight_text(text, style)


def build_bucket_rows(buckets, req_counts, succ_counts, fail_counts, tok_counts):
    rows = []
    for bucket in buckets:
        total = req_counts.get(bucket, 0)
        if total == 0:
            continue
        success = succ_counts.get(bucket, 0)
        failed = fail_counts.get(bucket, 0)
        tokens = int(round(tok_counts.get(bucket, 0)))
        rows.append((bucket.strftime("%Y-%m-%d %H:%M"), total, format_rate(success, total), failed, tokens))
    return rows


def build_error_rows(error_counts):
    return sorted(error_counts.items(), key=lambda item: (-item[1], item[0]))


def build_summary_cards(
    *,
    model,
    pid,
    url,
    is_running,
    last_stats,
    total_req,
    total_success,
    total_failed,
    top_error,
    updated_at,
):
    display_url = url.removeprefix("http://").removeprefix("https://")
    status_rows = [
        ("Model", highlight_text(model or "—", "bold cyan")),
        ("PID", str(pid or "—")),
        ("URL", display_url),
        ("Status", highlight_text("running" if is_running else "stopped", "bold green" if is_running else "bold red")),
    ]
    if last_stats:
        gen_tps, prompt_tps, running, waiting, kv, prefix = last_stats
        engine_rows = [
            ("Gen throughput", highlight_positive_fraction(f"{gen_tps:.1f} tok/s", min(gen_tps / 50.0, 1.0))),
            ("Prompt throughput", highlight_positive_fraction(f"{prompt_tps:.1f} tok/s", min(prompt_tps / 1000.0, 1.0))),
            ("Running", f"{running} reqs"),
            ("Waiting", f"{waiting} reqs"),
            ("KV cache", highlight_fraction(f"{kv:.1f}%", kv / 100.0)),
            ("Prefix cache", highlight_positive_fraction(f"{prefix:.1f}%", prefix / 100.0)),
        ]
    else:
        engine_rows = [("Engine", "No data")]
    summary_rows = [
        ("Requests", str(total_req)),
        ("Success rate", highlight_positive_fraction(format_rate(total_success, total_req), (total_success / total_req) if total_req else 0.0)),
        ("Failed", str(total_failed)),
        ("Top error", top_error),
    ]
    return {"status": status_rows, "engine": engine_rows, "summary": summary_rows}


def render_key_values(rows):
    width = max(len(label) for label, _ in rows)
    rendered = Text()
    for index, (label, value) in enumerate(rows):
        rendered.append(f"{label:<{width}}  ")
        if isinstance(value, Text):
            rendered.append_text(value)
        else:
            rendered.append(str(value))
        if index != len(rows) - 1:
            rendered.append("\n")
    return rendered


def render_card(title, rows):
    body = render_key_values(rows)
    rendered = Text(title, style="bold")
    rendered.append("\n\n")
    rendered.append_text(body)
    return rendered


def format_compact_number(value):
    abs_value = abs(value)
    if abs_value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B"
    if abs_value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if abs_value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(int(value))


def build_chart_meta(values, buckets, compact=False):
    formatter = format_compact_number if compact else lambda value: f"{int(value):,}"
    if not values:
        return "Total 0 | Peak 0 @ — | Latest 0 @ —"
    peak_index = max(range(len(values)), key=values.__getitem__)
    return (
        f"Total {formatter(sum(values))} | "
        f"Peak {formatter(values[peak_index])} @ {buckets[peak_index].strftime('%H:%M')} | "
        f"Latest {formatter(values[-1])} @ {buckets[-1].strftime('%H:%M')}"
    )


def build_bucket_detail(bucket, req_counts, succ_counts, fail_counts, tok_counts):
    total = req_counts.get(bucket, 0)
    success = succ_counts.get(bucket, 0)
    failed = fail_counts.get(bucket, 0)
    tokens = int(round(tok_counts.get(bucket, 0)))
    return "\n".join(
        [
            bucket.strftime("%Y-%m-%d %H:%M"),
            "",
            f"Requests:     {total}",
            f"Success:      {success}",
            f"Failed:       {failed}",
            f"Success rate: {format_rate(success, total)}",
            f"Tokens:       {tokens:,}",
        ]
    )


def build_error_detail(message, count):
    return "\n".join(
        [
            "Error Detail",
            "",
            f"Count:   {count}",
            "",
            "Message:",
            message,
        ]
    )


def parse_log_timestamp(line, reference):
    rest = line.split(") ", 1)[1]
    parts = rest.split(" ", 3)
    return BACKEND.parse_stats_timestamp(parts[1], parts[2], reference)


def to_bucket_for(ts, bucket_size):
    epoch = BACKEND.datetime(ts.year, ts.month, ts.day)
    return epoch + (ts - epoch) // bucket_size * bucket_size


def window_buckets_for(reference, bucket_size, window):
    current = to_bucket_for(reference, bucket_size)
    first = to_bucket_for(reference - window, bucket_size)
    bucket = first
    buckets = []
    while bucket <= current:
        buckets.append(bucket)
        bucket += bucket_size
    return buckets


def bucket_display_time(bucket, bucket_size, reference):
    return min(bucket + bucket_size, reference)


def parse_log_for(lines, bucket_size, reference, window):
    request_counts = BACKEND.defaultdict(int)
    success_counts = BACKEND.defaultdict(int)
    failed_counts = BACKEND.defaultdict(int)
    token_counts = BACKEND.defaultdict(float)
    error_counts = BACKEND.Counter()
    last_stats = None
    pending = []
    cutoff = reference - window
    last_stats_ts = None

    for line in lines:
        cat = BACKEND.categorize(line)
        if cat == "access":
            pending.append(int(line.split('" ')[1].split()[0]))
        elif cat == "stats":
            ts, gen_tps, prompt_tps, running, waiting, kv, prefix = BACKEND.parse_stats_line(line, reference)
            if last_stats_ts is None:
                request_ts = ts
            else:
                request_ts = BACKEND.interval_midpoint(last_stats_ts, ts)
                token_counts[to_bucket_for(request_ts, bucket_size)] += gen_tps * (ts - last_stats_ts).total_seconds()
            BACKEND.flush_pending(
                pending,
                to_bucket_for(request_ts, bucket_size),
                request_counts,
                success_counts,
                failed_counts,
            )
            last_stats_ts = ts
            last_stats = (gen_tps, prompt_tps, running, waiting, kv, prefix)
        elif cat == "request_error":
            ts = parse_log_timestamp(line, reference)
            if cutoff <= ts <= reference:
                error_counts[line.split("message='")[1].split("'")[0]] += 1

    if pending:
        request_ts = reference if last_stats_ts is None else BACKEND.interval_midpoint(last_stats_ts, reference)
        BACKEND.flush_pending(
            pending,
            to_bucket_for(request_ts, bucket_size),
            request_counts,
            success_counts,
            failed_counts,
        )

    return request_counts, success_counts, failed_counts, token_counts, error_counts, last_stats


def tail_log_for(reference, window):
    if not BACKEND.LOG_PATH.exists():
        return []

    cutoff = reference - window
    chunk_size = 1024 * 1024
    data = b""

    with BACKEND.LOG_PATH.open("rb") as handle:
        handle.seek(0, 2)
        end = handle.tell()
        while True:
            start = max(0, end - chunk_size)
            handle.seek(start)
            data = handle.read(end - start) + data
            lines = data.decode("utf-8", errors="replace").splitlines()

            for line in lines:
                if BACKEND.categorize(line) != "stats":
                    continue
                ts, *_ = BACKEND.parse_stats_line(line, reference)
                if ts <= cutoff:
                    return lines

            if start == 0:
                return lines
            end = start


def build_snapshot(range_key="day"):
    range_config = RANGE_OPTIONS[range_key]
    config = BACKEND.load_config()
    display_ip = BACKEND.get_display_ip()
    port = config.get("port", 8000)
    is_running, pid, running_model = BACKEND.service_state()
    reference = BACKEND.datetime.now() + BACKEND.UTC_OFFSET
    lines = tail_log_for(reference, range_config["window"])
    req_counts, succ_counts, fail_counts, tok_counts, error_counts, last_stats = parse_log_for(
        lines,
        range_config["bucket"],
        reference,
        range_config["window"],
    )
    buckets = window_buckets_for(reference, range_config["bucket"], range_config["window"])
    display_times = [bucket_display_time(bucket, range_config["bucket"], reference) for bucket in buckets]
    req_values = [req_counts.get(bucket, 0) for bucket in buckets]
    tok_values = [int(round(tok_counts.get(bucket, 0))) for bucket in buckets]
    total_req = sum(req_values)
    total_success = sum(succ_counts.get(bucket, 0) for bucket in buckets)
    total_failed = sum(fail_counts.get(bucket, 0) for bucket in buckets)
    top_error = error_counts.most_common(1)[0][0] if error_counts else "none"
    updated_at = reference

    return {
        "range_label": range_config["label"],
        "cards": build_summary_cards(
            model=running_model,
            pid=pid,
            url=f"http://{display_ip}:{port}/v1",
            is_running=is_running,
            last_stats=last_stats,
            total_req=total_req,
            total_success=total_success,
            total_failed=total_failed,
            top_error=top_error,
            updated_at=updated_at,
        ),
        "request_values": req_values,
        "token_values": tok_values,
        "request_meta": build_chart_meta(req_values, display_times),
        "token_meta": build_chart_meta(tok_values, display_times, compact=True),
        "bucket_rows": build_bucket_rows(buckets, req_counts, succ_counts, fail_counts, tok_counts),
        "bucket_details": [
            build_bucket_detail(bucket, req_counts, succ_counts, fail_counts, tok_counts)
            for bucket in buckets
            if req_counts.get(bucket, 0) > 0
        ],
        "error_rows": build_error_rows(error_counts),
        "error_details": [build_error_detail(message, count) for message, count in build_error_rows(error_counts)],
    }


class DashboardTextualApp(App):
    TITLE = "JIT LLM Service Dashboard"
    SUB_TITLE = ""
    CSS = """
    Screen {
        layout: vertical;
        background: #11151c;
        color: #f3efe0;
    }

    #body {
        layout: vertical;
        height: 1fr;
        padding: 1;
    }

    #top_row {
        height: 10;
    }

    #charts_row {
        height: 12;
        margin-top: 1;
    }

    #bottom_row {
        height: 1fr;
        margin-top: 1;
    }

    .card, .panel {
        border: round #3b82f6;
        background: #1b2430;
        color: #f3efe0;
        padding: 1 2;
        width: 1fr;
        height: 1fr;
        margin-right: 1;
    }

    .panel:last-child, .card:last-child {
        margin-right: 0;
    }

    .panel_title {
        text-style: bold;
        color: #9fd3c7;
        margin-bottom: 1;
    }

    .card_title {
        text-style: bold;
        color: #9fd3c7;
    }

    .card_body {
        margin-top: 1;
    }

    Sparkline {
        height: 1fr;
        color: #f59e0b;
    }

    #token_chart {
        color: #34d399;
    }

    .chart_meta {
        margin-top: 1;
        color: #cbd5e1;
    }

    DataTable {
        height: 1fr;
        margin-top: 1;
    }

    #detail_body {
        height: 1fr;
        margin-top: 1;
    }
    """
    BINDINGS = [
        Binding("h", "set_range('hour')", "Hour"),
        Binding("d", "set_range('day')", "Day"),
        Binding("w", "set_range('week')", "Week"),
        Binding("b", "toggle_table", "Toggle Table"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self.range_key = "day"
        self.table_mode = "buckets"
        self.row_details = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="body"):
            with Horizontal(id="top_row"):
                yield Static("", id="status_card", classes="card")
                yield Static("", id="engine_card", classes="card")
                yield Static("", id="summary_card", classes="card")
            with Horizontal(id="charts_row"):
                with Vertical(classes="panel"):
                    yield Static("", id="request_title", classes="panel_title")
                    yield Sparkline([], id="request_chart")
                    yield Static("", id="request_meta", classes="chart_meta")
                with Vertical(classes="panel"):
                    yield Static("", id="token_title", classes="panel_title")
                    yield Sparkline([], id="token_chart")
                    yield Static("", id="token_meta", classes="chart_meta")
            with Horizontal(id="bottom_row"):
                with Vertical(classes="panel"):
                    yield Static("", id="table_title", classes="panel_title")
                    yield DataTable(zebra_stripes=True, cursor_type="row", id="summary_table")
                with Vertical(classes="panel"):
                    yield Static("Detail", classes="panel_title")
                    yield Static("", id="detail_body")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_snapshot()
        self.set_interval(BACKEND.REFRESH, self.refresh_snapshot)

    def action_toggle_table(self) -> None:
        self.table_mode = "errors" if self.table_mode == "buckets" else "buckets"
        self.populate_table()

    def action_set_range(self, range_key: str) -> None:
        if range_key == self.range_key:
            return
        self.range_key = range_key
        self.refresh_snapshot()

    def refresh_snapshot(self) -> None:
        snapshot = build_snapshot(self.range_key)
        self.update_cards(snapshot)
        self.update_charts(snapshot)
        self.snapshot = snapshot
        self.populate_table()

    def update_cards(self, snapshot) -> None:
        cards = snapshot["cards"]
        self.query_one("#status_card", Static).update(render_card("Service Status", cards["status"]))
        self.query_one("#engine_card", Static).update(render_card("Engine Stats", cards["engine"]))
        self.query_one("#summary_card", Static).update(
            render_card(f"Session Summary · last {snapshot['range_label']}", cards["summary"])
        )

    def update_charts(self, snapshot) -> None:
        self.query_one("#request_title", Static).update(f"Requests · last {snapshot['range_label']}")
        self.query_one("#token_title", Static).update(f"Tokens · last {snapshot['range_label']}")
        self.query_one("#request_chart", Sparkline).data = snapshot["request_values"] or [0]
        self.query_one("#token_chart", Sparkline).data = snapshot["token_values"] or [0]
        self.query_one("#request_meta", Static).update(snapshot["request_meta"])
        self.query_one("#token_meta", Static).update(snapshot["token_meta"])

    def populate_table(self) -> None:
        table = self.query_one("#summary_table", DataTable)
        selected_row = min(table.cursor_row, len(self.row_details) - 1) if self.row_details else 0
        table.clear(columns=True)

        if self.table_mode == "buckets":
            self.query_one("#table_title", Static).update(f"Bucket Breakdown · last {self.snapshot['range_label']}")
            table.add_columns("Bucket", "Requests", "Success", "Failed", "Tokens")
            rows = self.snapshot["bucket_rows"]
            self.row_details = self.snapshot["bucket_details"]
        else:
            self.query_one("#table_title", Static).update(f"Error Breakdown · last {self.snapshot['range_label']}")
            table.add_columns("Error", "Count")
            rows = self.snapshot["error_rows"]
            self.row_details = self.snapshot["error_details"]

        if rows:
            for row in rows:
                table.add_row(*row)
            selected_row = min(selected_row, len(rows) - 1)
            table.move_cursor(row=selected_row, column=0, animate=False, scroll=False)
            self.update_detail(selected_row)
        else:
            self.update_detail(None)

    def update_detail(self, row_index) -> None:
        detail = self.query_one("#detail_body", Static)
        if row_index is None or not self.row_details:
            detail.update("No data")
            return
        detail.update(self.row_details[row_index])

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self.update_detail(event.cursor_row)


def main():
    DashboardTextualApp().run()


if __name__ == "__main__":
    main()
