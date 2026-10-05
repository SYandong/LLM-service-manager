# Generated-By: Codex / gpt-6.1-sol
# Generated-By: Codex / unknown model
"""Pure accounting and terminal rendering for the fleet GPU overview."""

from dataclasses import dataclass
import hashlib
import math

from rich.text import Text
from rich.console import Console


FREE_COLOR = "#28313d"
NEUTRAL_COLOR = "#66717f"
OWNER_COLORS = ("#db9c9c", "#97b4dd", "#c6c786", "#d8a2c8",
                "#94c5b7", "#e4b778", "#8fc397", "#dba5a1",
                "#82b9d8", "#c6ad86", "#a9c4a7", "#b996d9")
KIND_LABEL = {"llm": "LLM", "other": "Other", "unknown": "Unknown type"}
RESIDUAL_KEY = ("system", "unattributed")
FREE_KEY = ("system", "free")
MEASURED_KEY = ("system", "measured")
SERVICE_STYLES = {"active": "#71c695", "idle": "#c5ced8", "over_limit": "#e0b568",
                  "claimed": "#80c1d7", "unknown": "#98a4b4"}


def numeric(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def gib(value):
    """Retain source precision, including allocations smaller than one cell."""
    if not numeric(value):
        return "?"
    return str(int(value)) if value == int(value) else str(value)


def compact_gib(value):
    if not numeric(value):
        return "?"
    rounded = ("%.2f" % value).rstrip("0").rstrip(".")
    return "%.2g" % value if value > 0 and rounded == "0" else rounded


def owner_color(identity):
    if identity == "unknown":
        return NEUTRAL_COLOR
    slot = int.from_bytes(hashlib.sha256(identity.encode("utf-8")).digest(), "big") % len(OWNER_COLORS)
    return OWNER_COLORS[slot]


@dataclass(frozen=True)
class Allocation:
    identity: str
    owner: str
    kind: str
    used_gb: float | None
    members: tuple

    @property
    def key(self):
        return self.identity, self.kind


@dataclass(frozen=True)
class Segment:
    key: tuple
    label: str
    used_gb: float
    color: str
    pattern: str


@dataclass(frozen=True)
class GpuAccount:
    index: int
    total_gb: float | None
    used_gb: float | None
    util_percent: float | None
    allocations: tuple
    attributed_gb: float | None
    residual_gb: float | None
    free_gb: float | None
    issues: tuple

    @property
    def reconciled(self):
        return not self.issues

    def selection_keys(self):
        keys = [allocation.key for allocation in self.allocations]
        if self.residual_gb is not None and self.residual_gb > 0:
            keys.append(RESIDUAL_KEY)
        if self.free_gb is not None and self.free_gb > 0:
            keys.append(FREE_KEY)
        return keys


def service_amounts(members):
    """Compute the per-card service totals used in expanded model rows."""
    groups = {}
    for ident, value in members:
        if ident is not None:
            groups.setdefault(ident, []).append(value)
    return {ident: math.fsum(values) if all(numeric(value) for value in values) else None
            for ident, values in groups.items()}


def account_gpu(gpu, services=()):
    services = {service["id"]: service for service in services}
    groups = {}
    for occupant in gpu.get("occupants", []):
        service_id = occupant.get("service_id")
        if service_id is not None and (not isinstance(service_id, str) or not service_id):
            raise ValueError("Invalid GPU service ID.")
        container = occupant.get("container")
        service = services.get(service_id, {})
        if isinstance(container, str) and container:
            identity, owner = "container:" + container, container
        elif service.get("host") is True:
            identity, owner = "host", "host"
        else:
            identity, owner = "unknown", "unknown"
        kind = occupant.get("kind")
        kind = "llm" if kind in ("llm", "inference") else "other" if kind == "other" else "unknown"
        group = groups.setdefault((identity, kind), {"owner": owner, "members": []})
        value = occupant.get("used_gb")
        group["members"].append((service_id, value if numeric(value) else None))
    allocations = []
    for (identity, kind), group in sorted(groups.items()):
        members = tuple(group["members"])
        service_amounts(members)
        values = [value for _, value in members]
        used = math.fsum(values) if all(numeric(value) for value in values) else None
        allocations.append(Allocation(identity, group["owner"], kind, used, members))
    values = [allocation.used_gb for allocation in allocations]
    attributed = math.fsum(values) if all(numeric(value) for value in values) else None
    used = gpu.get("used_gb") if numeric(gpu.get("used_gb")) else None
    total = gpu.get("total_gb") if numeric(gpu.get("total_gb")) else None
    util = gpu.get("util_percent") if numeric(gpu.get("util_percent")) else None
    issues = []
    if total is None or total == 0:
        issues.append("capacity unknown")
    if used is None:
        issues.append("usage unknown")
    if attributed is None:
        issues.append("allocation amount unknown")
    if used is not None and attributed is not None and attributed > used:
        issues.append("attribution conflict +%s GiB" % gib(attributed - used))
    if used is not None and total is not None and used > total:
        issues.append("over capacity +%s GiB" % gib(used - total))
    residual = used - attributed if used is not None and attributed is not None and attributed <= used else None
    free = total - used if total is not None and used is not None and used <= total else None
    return GpuAccount(gpu["index"], total, used, util, tuple(allocations), attributed,
                      residual, free, tuple(issues))


def proportional_cells(values, total, width):
    """Largest-remainder rounding; a tiny allocation may receive no cell."""
    if (not numeric(total) or total <= 0 or type(width) is not int or width < 0
            or not all(numeric(value) for value in values)
            or not math.isclose(math.fsum(values), total, rel_tol=1e-12, abs_tol=1e-12)):
        raise ValueError("Bar values must account for the supplied capacity.")
    exact = [value / total * width for value in values]
    cells = [math.floor(value) for value in exact]
    remainder = width - sum(cells)
    order = sorted(range(len(values)), key=lambda index: (-(exact[index] - cells[index]), index))
    for index in order[:remainder]:
        cells[index] += 1
    return cells


def drawing_segments(account):
    if account.total_gb is None or account.total_gb == 0 or account.used_gb is None:
        return []
    if account.issues:
        # Conflicting allocations remain intact; only measured usage is drawn.
        used = min(account.used_gb, account.total_gb)
        segments = [Segment(MEASURED_KEY, "Measured used", used, NEUTRAL_COLOR, "╳")]
        if account.used_gb <= account.total_gb:
            segments.append(Segment(FREE_KEY, "Free", account.free_gb, FREE_COLOR, " "))
        return segments
    segments = [Segment(allocation.key, allocation.owner, allocation.used_gb,
                        owner_color(allocation.identity), " " if allocation.kind == "llm" else "·")
                for allocation in account.allocations]
    segments += [Segment(RESIDUAL_KEY, "Unattributed", account.residual_gb, NEUTRAL_COLOR, "░"),
                 Segment(FREE_KEY, "Free", account.free_gb, FREE_COLOR, " ")]
    return segments


def fit(text, width):
    result = text.copy() if isinstance(text, Text) else Text(str(text))
    result.truncate(max(0, width), overflow="ellipsis")
    return result


def render_bar(account, width, selected=None, labels=True, clean=str):
    segments = drawing_segments(account)
    if not segments:
        return fit("Proportional VRAM unavailable" if labels else "", width), []
    cells = proportional_cells([segment.used_gb for segment in segments], account.total_gb, width)
    result, hits, x = Text(), [], 0
    for segment, length in zip(segments, cells):
        if not length:
            continue
        foreground = "#a2abba" if segment.key == FREE_KEY else "#16212b"
        style = foreground + " on " + segment.color
        if segment.key == selected:
            style += " bold underline"
        chunk = Text(segment.pattern * length, style=style)
        kind = KIND_LABEL.get(segment.key[1], "")
        label = " ".join(value for value in (clean(segment.label), kind, compact_gib(segment.used_gb)) if value)
        if labels and Text(label).cell_len + 2 <= length:
            offset = (length - Text(label).cell_len) // 2
            chunk = Text(segment.pattern * offset, style=style)
            chunk.append(label, style=style)
            chunk.append(segment.pattern * (length - offset - Text(label).cell_len), style=style)
        result.append_text(chunk)
        hits.append((x, x + length, segment.key))
        x += length
    return result, hits


def allocation_legend(account, width, selected=None, clean=str):
    entries = []
    for allocation in account.allocations:
        label = "%s %s %s" % (clean(allocation.owner), KIND_LABEL[allocation.kind], compact_gib(allocation.used_gb))
        entries.append((allocation.key, label, owner_color(allocation.identity),
                        "■" if allocation.kind == "llm" else "▨"))
    if account.residual_gb is not None and account.residual_gb > 0:
        entries.append((RESIDUAL_KEY, "Unattributed " + compact_gib(account.residual_gb), NEUTRAL_COLOR, "░"))
    if account.free_gb is not None:
        entries.append((FREE_KEY, "Free " + compact_gib(account.free_gb), "#a2abba", "·"))
    if selected is not None:
        entries.sort(key=lambda entry: entry[0] != selected)
    result, hits = Text(), []
    for number, (key, label, color, marker) in enumerate(entries):
        chunk = Text(("  " if number else " ") + marker + " " + label,
                     style=color + (" bold underline" if key == selected else ""))
        remaining = len(entries) - number - 1
        tail = "  +%d · Enter" % remaining if remaining else ""
        if result.cell_len + chunk.cell_len + len(tail) > width:
            result.append("  +%d · Enter" % (len(entries) - number), style="dim")
            return fit(result, width), hits
        start = result.cell_len
        result.append_text(chunk)
        hits.append((start, result.cell_len, key))
    return fit(result, width), hits


def card_header(account, width, selected=False, stale=False, exact=False):
    number = gib if exact else compact_gib
    title = "%sGPU %s  %s/%s GiB" % ("› " if selected else "  ", account.index,
                                        number(account.used_gb), number(account.total_gb))
    if stale:
        title += " · STALE"
    result = Text(title, style="bold #e0e7ef" if selected else "#c1cad5")
    warning = "; ".join(account.issues)
    if not exact:
        warning = warning.replace("attribution conflict", "conflict")
    compute = " · compute %s%%" % number(account.util_percent)
    memory_percent = ""
    if account.total_gb and account.used_gb is not None:
        memory_percent = " · VRAM %.0f%%" % (account.used_gb / account.total_gb * 100)
    suffix = " · " + warning if warning else ""
    if exact or Text(title + memory_percent + compute + suffix).cell_len <= width:
        result.append(memory_percent)
    result.append(compute)
    if warning:
        result.append(suffix, style="bold yellow")
    return fit(result, width)


def render_overview(accounts, width, bar_rows, selected_gpu=None, selected_segment=None,
                    stale=False, clean=str, show_legends=True):
    """Return the six-card view and cell ranges used for mouse selection."""
    lines, hits = [], []
    for account in accounts:
        header_y = len(lines)
        lines.append(card_header(account, width, account.index == selected_gpu, stale))
        hits.append((header_y, 0, width, account.index, None))
        selected = selected_segment if account.index == selected_gpu else None
        for row in range(bar_rows):
            bar, ranges = render_bar(account, width, selected, labels=row == 0, clean=clean)
            hits.extend((len(lines), left, right, account.index, key) for left, right, key in ranges)
            lines.append(bar)
        if show_legends or account.index == selected_gpu:
            legend, ranges = allocation_legend(account, width, selected, clean)
            hits.extend((len(lines), left, right, account.index, key) for left, right, key in ranges)
            lines.append(legend)
    result = Text("\n").join(lines) if lines else Text("GPU observations unavailable")
    return result, hits


def expanded_header(account, selected=False, stale=False):
    text = Text()
    text.append("%sGPU %s" % ("› " if selected else "  ", account.index), style="bold #eaf1f7")
    text.append(" · VRAM ", style="#98a4b4")
    text.append("%s used of %s GiB" % (compact_gib(account.used_gb), compact_gib(account.total_gb)), style="bold #eaf1f7")
    if account.total_gb and account.used_gb is not None:
        text.append(" (%.0f%%)" % (account.used_gb / account.total_gb * 100), style="#98a4b4")
    text.append(" · Compute ", style="#98a4b4")
    text.append(compact_gib(account.util_percent) + "%", style="bold #80c1d7")
    if stale:
        text.append("   STALE", style="bold #e0b568")
    return text


def service_lines(service, used_gb, index, clean=str, status=None):
    """Four logical rows; activity and counters describe the whole service."""
    status = status or service.get("status", "unknown")
    if status not in SERVICE_STYLES:
        status = "unknown"
    first = Text()
    first.append("    Model ", style="#98a4b4")
    first.append(clean(service.get("model") or "unknown"), style="bold #eaf1f7")
    first.append(" · Engine " + clean(service.get("engine") or "unknown"), style="#98a4b4")
    first.append(" · State ", style="#98a4b4")
    first.append(status.replace("_", " "), style="bold " + SERVICE_STYLES[status])
    first.append(" · GPU %s VRAM: " % index, style="#98a4b4")
    first.append(gib(used_gb) + " GiB", style="bold #eaf1f7")
    ident = Text("    Service ID: " + clean(service["id"]), style="#98a4b4")
    values = service.get("hourly_active_24h") or [None] * 24
    activity = "".join("·" if not numeric(value) else "▁▂▃▄▅▆▇█"[
        min(7, int(min(value, 60) * 7 / 60))] for value in values)
    window = service.get("window_24h") or {}
    coverage = window.get("coverage_ratio")
    observed = compact_gib(coverage * 100) + "%" if numeric(coverage) else "?"
    third = Text("    24h service activity ", style="#98a4b4")
    third.append(activity, style="#80c1d7")
    third.append(" active ")
    third.append(gib(window.get("active_minutes")) + " min", style="bold #eaf1f7")
    third.append(" · coverage ")
    third.append(observed, style="bold #eaf1f7")
    fourth = Text("    24h service requests ", style="#98a4b4")
    fourth.append(gib(window.get("requests")), style="bold #eaf1f7")
    fourth.append(" · input ")
    fourth.append(gib(window.get("prompt_tokens")), style="bold #eaf1f7")
    fourth.append(" · output ")
    fourth.append(gib(window.get("gen_tokens")), style="bold #eaf1f7")
    return first, ident, third, fourth


def render_expanded(accounts, services, width, bar_rows=4, selected_gpu=None,
                    selected_segment=None, stale=False, clean=str, statuses=None):
    """Wrap complete cards and return anchors and mouse targets for scrolling."""
    width = max(1, width)
    console = Console(width=width)
    ranks = {service["id"]: index for index, service in enumerate(services)}
    services = {service["id"]: service for service in services}
    statuses = statuses or {}
    lines, hits, anchors, service_hits = [], [], {}, {}

    def append(text, index, key=None, ident=None, indent=0):
        indent = min(indent, width - 1)
        content = text[indent:] if indent else text
        for number, line in enumerate(content.wrap(console, width - indent, overflow="fold", no_wrap=False)):
            if indent:
                prefix = text[:indent] if number == 0 else Text(" " * indent)
                prefix.append_text(line)
                line = prefix
            y = len(lines)
            lines.append(line)
            hits.append((y, 0, width, index, key))
            if ident is not None:
                service_hits[y] = ident

    for account in accounts:
        anchors[account.index] = len(lines)
        append(expanded_header(account, account.index == selected_gpu, stale), account.index)
        if account.issues:
            append(Text("  " + " · ".join(account.issues), style="bold #e0b568"), account.index)
        selected = selected_segment if account.index == selected_gpu else None
        for row in range(bar_rows):
            bar, ranges = render_bar(account, width, selected, labels=row == 0, clean=clean)
            hits.extend((len(lines), left, right, account.index, key) for left, right, key in ranges)
            lines.append(bar)
        for allocation in account.allocations:
            anchors[(account.index, allocation.key)] = len(lines)
            label = "%s%s · %s · %s GiB" % ("› " if allocation.key == selected else "  ",
                clean(allocation.owner), KIND_LABEL[allocation.kind], gib(allocation.used_gb))
            owner_style = owner_color(allocation.identity) + (" bold underline" if allocation.key == selected else "")
            append(Text(label, style=owner_style), account.index, allocation.key, indent=2)
            if allocation.kind != "llm":
                continue
            amounts = service_amounts(allocation.members)
            for ident in sorted((ident for ident in amounts if ident in services), key=lambda ident: ranks[ident]):
                memory = amounts[ident]
                for line in service_lines(services[ident], memory, account.index, clean, statuses.get(ident)):
                    append(line, account.index, allocation.key, ident, indent=4)
        if (account.issues or gib(account.used_gb) != compact_gib(account.used_gb)
                or gib(account.total_gb) != compact_gib(account.total_gb)):
            append(Text("  Measured VRAM · Used %s GiB · Total %s GiB" % (gib(account.used_gb), gib(account.total_gb)),
                        style="#98a4b4"), account.index, MEASURED_KEY, indent=2)
        for key, label, memory in ((RESIDUAL_KEY, "Unattributed used", account.residual_gb),
                                   (FREE_KEY, "Free VRAM", account.free_gb)):
            anchors[(account.index, key)] = len(lines)
            value = Text("%s%s · %s GiB" % ("› " if key == selected else "  ", label, gib(memory)),
                         style="#c5ced8" + (" bold underline" if key == selected else ""))
            append(value, account.index, key, indent=2)
        lines.append(Text("─" * width, style="#364354"))
        lines.append(Text(""))
    result = Text("\n").join(lines) if lines else Text("GPU observations unavailable")
    return result, hits, anchors, service_hits


def detail_lines(account, selected=None, clean=str):
    result = [card_header(account, 10000, exact=True).plain]
    for allocation in account.allocations:
        if selected is None or selected in (MEASURED_KEY, allocation.key):
            result.append("%s · %s · %s GiB" % (clean(allocation.owner),
                          KIND_LABEL[allocation.kind], gib(allocation.used_gb)))
    if selected in (None, MEASURED_KEY, RESIDUAL_KEY):
        result.append("Unattributed: %s GiB" % gib(account.residual_gb))
    if selected in (None, MEASURED_KEY, FREE_KEY):
        result.append("Free: %s GiB" % gib(account.free_gb))
    return "\n".join(result)
