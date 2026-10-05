# Generated-By: Codex / gpt-6.1-sol
"""Pure accounting and terminal rendering for the fleet GPU overview."""

import colorsys
from dataclasses import dataclass
import hashlib
import math

from rich.text import Text


FREE_COLOR = "#28313d"
NEUTRAL_COLOR = "#66717f"
KIND_LABEL = {"llm": "LLM", "other": "Other", "unknown": "Unknown type"}
RESIDUAL_KEY = ("system", "unattributed")
FREE_KEY = ("system", "free")
MEASURED_KEY = ("system", "measured")


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
    hue = int.from_bytes(hashlib.sha256(identity.encode("utf-8")).digest()[:4], "big") / 2**32
    rgb = colorsys.hls_to_rgb(hue, .67, .48)
    return "#%02x%02x%02x" % tuple(round(channel * 255) for channel in rgb)


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


def account_gpu(gpu, services=()):
    services = {service["id"]: service for service in services}
    groups = {}
    for occupant in gpu.get("occupants", []):
        container = occupant.get("container")
        service = services.get(occupant.get("service_id"), {})
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
        group["members"].append((occupant.get("service_id"), value if numeric(value) else None))
    allocations = []
    for (identity, kind), group in sorted(groups.items()):
        members = tuple(group["members"])
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
                        owner_color(allocation.identity), " " if allocation.kind == "llm" else "╱")
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
        return fit("Proportional VRAM unavailable", width), []
    cells = proportional_cells([segment.used_gb for segment in segments], account.total_gb, width)
    result, hits, x = Text(), [], 0
    for segment, length in zip(segments, cells):
        if not length:
            continue
        style = "#16212b on " + segment.color
        if segment.key == selected:
            style += " bold underline"
        chunk = Text(segment.pattern * length, style=style)
        label = clean(segment.label) + " " + compact_gib(segment.used_gb)
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
    if account.issues:
        result.append(" · " + "; ".join(account.issues), style="bold yellow")
    if account.total_gb and account.used_gb is not None:
        result.append(" · VRAM %.0f%%" % (account.used_gb / account.total_gb * 100))
    result.append(" · compute %s%%" % number(account.util_percent))
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
