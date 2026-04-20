from pathlib import Path
from collections import Counter, defaultdict
from datetime import datetime, timedelta
import matplotlib.pyplot as plt

ROOT = Path(__file__).parent.parent
LOG_PATH = ROOT / "var/log/vllm.log"
REPORT_PATH = ROOT / "var/log_report.md"
WORKLOAD_PATH = ROOT / "var/workload.png"

YEAR = datetime.now().year
UTC_OFFSET = timedelta(hours=8)

lines = LOG_PATH.read_text().splitlines()

access, stats, request_error, server_log, exception = [], [], [], [], []

def categorize(line):
    if line.startswith("(EngineCore pid="):
        return server_log
    if line.startswith("(APIServer pid="):
        rest = line.split(") ", 1)[1]
        if rest.startswith("INFO:     "):
            return access if rest[10:].split(" ")[1] == "-" else server_log
        parts = rest.split(" ", 3)
        if len(parts) == 4 and parts[3].startswith("["):
            module = parts[3][1:].split(":")[0]
            if parts[0] == "INFO" and module == "loggers.py": return stats
            if parts[0] == "ERROR" and module == "serving.py": return request_error
        return server_log
    parts = line.split(" ", 3)
    if line.startswith("INFO ") and len(parts) >= 4 and len(parts[1]) == 5 and parts[1][2] == "-":
        return server_log
    return exception

for line in lines:
    categorize(line).append(line)

total = len(access) + len(stats) + len(request_error) + len(server_log) + len(exception)
if total != len(lines):
    raise ValueError(f"Categorization incomplete: {total} categorized, {len(lines)} total")

# --- report ---

statuses = [int(line.split('" ')[1].split()[0]) for line in access]
success = sum(1 for s in statuses if 200 <= s < 300)
failed  = sum(1 for s in statuses if s >= 400)
total_requests = len(statuses)

msg_counts = Counter(
    line.split("message='")[1].split("'")[0]
    for line in request_error
)
unmatched = failed - len(request_error)
if unmatched > 0:
    msg_counts["Malformed request"] = unmatched

report = f"""# vLLM Log Report

## Request Outcomes

| Outcome | Count | Percentage |
|---------|-------|------------|
| Success | {success} | {100*success/total_requests:.1f}% |
| Failed  | {failed}  | {100*failed/total_requests:.1f}% |
| **Total** | **{total_requests}** | 100% |

## Failed Request Breakdown

| Reason | Count | Percentage |
|--------|-------|------------|
"""

for msg, count in msg_counts.most_common():
    report += f"| {msg} | {count} | {100*count/failed:.1f}% |\n"

REPORT_PATH.write_text(report)
print(f"Report written to {REPORT_PATH}")

# --- workload chart ---

times, gen_tps, running = [], [], []
for line in stats:
    parts = line.split(") ", 1)[1].split(" ", 3)
    ts = datetime.strptime(f"{YEAR}-{parts[1]} {parts[2]}", "%Y-%m-%d %H:%M:%S") + UTC_OFFSET
    msg = parts[3].split("] ", 1)[1]
    times.append(ts)
    gen_tps.append(float(msg.split("Avg generation throughput: ")[1].split(" tokens/s")[0]))
    running.append(int(msg.split("Running: ")[1].split(" reqs")[0]))

BUCKET = timedelta(hours=3)

def time_bucket(ts):
    epoch = datetime(ts.year, ts.month, ts.day)
    return epoch + ((ts - epoch) // BUCKET) * BUCKET

token_buckets   = defaultdict(float)
running_buckets = defaultdict(int)

for ts, tps, req in zip(times, gen_tps, running):
    b = time_bucket(ts)
    token_buckets[b]   += tps * 10
    running_buckets[b] += req

all_buckets = sorted(set(token_buckets) | set(running_buckets))
labels      = [t.strftime("%m-%d %H:%M") for t in all_buckets]
x           = range(len(labels))

fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7), sharex=True)

ax1.bar(x, [token_buckets[b] for b in all_buckets], color="steelblue")
ax1.set_ylabel("Tokens generated")
ax1.set_title("Workload per 6-Hour Period (UTC+8)")

ax2.bar(x, [running_buckets[b] for b in all_buckets], color="darkorange")
ax2.set_ylabel("Running request samples")
ax2.set_xticks(x)
ax2.set_xticklabels(labels, rotation=45, ha="right")

plt.tight_layout()
plt.savefig(WORKLOAD_PATH, dpi=120)
print(f"Workload chart saved to {WORKLOAD_PATH}")
