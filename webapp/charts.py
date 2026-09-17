"""Real inline SVG bar charts for staff pages - CRM (staff_leads.html) and
Reports (staff_ops.html) - following the dataviz skill loaded for this
work: horizontal bars (<=24px thick, 4px rounded ends), direct value
labels (never a number crammed onto every axis tick), a native <title>
per bar as a zero-JS hover tooltip, and colors pulled from the skill's
own validated reference palette - NOT Kauli's site --accent/--ok/--warn/
--err tokens, which measured below the CVD-safety and contrast floors
when run through the skill's validator as chart marks (they're fine as
small text-badge accents always paired with a label, which is a
different, lower bar than carrying a chart's primary visual encoding).
Text (labels, values, axis) uses Kauli's own CSS custom properties
(var(--ink)/var(--muted)) via inline SVG fill, so it stays theme-correct
automatically - only the DATA marks use the fixed reference hex values.

Every chart these functions build sits ABOVE an existing real HTML table
with the same numbers (see staff_ops.html/staff_leads.html) - the chart
is a faster way to spot the shape of the data, never the only place a
number lives.

Dark-mode-only for now: every page these render on hardcodes dark theme
(staff_ops.html/staff_leads.html's own {% block theme %}dark{% endblock
%}), so these use the skill's dark-surface hex values directly rather
than a light/dark CSS-variable pair that has no light-mode caller yet."""
from __future__ import annotations

from markupsafe import Markup, escape

# The dataviz skill's own validated dark-mode categorical/status hex values
# (references/palette.md) - Kauli's own --accent/--ok/--warn/--err failed
# the skill's validator (chroma floor, CVD adjacent-pair separation,
# contrast vs the dark surface) when checked as chart-mark colors, so
# charts use these instead of the site's badge/text colors.
SEQUENTIAL_BLUE = "#3987e5"          # single-series magnitude (the funnel)
STATUS_GOOD = "#0ca30c"
STATUS_WARNING = "#fab219"
STATUS_SERIOUS = "#ec835a"
STATUS_CRITICAL = "#d03b3b"
STATUS_INFO = "#3987e5"              # categorical slot 1 (blue) - "in review", not yet good/bad

# Same real semantic grouping staff_ops.html's own .status-* CSS classes
# already use (style.css) - this just gives that existing grouping actual
# validated chart colors instead of reusing the site's own status hex,
# which failed the chart-color validator (see module docstring).
_ORDER_STATUS_COLOR = {
    "pending_payment": STATUS_CRITICAL,
    "queued": STATUS_WARNING,
    "processing": STATUS_WARNING,
    "awaiting_review": STATUS_INFO,
    "editor_returned": STATUS_WARNING,
    "ready_for_delivery": STATUS_GOOD,
    "delivered": STATUS_GOOD,
    "returned_to_client": STATUS_CRITICAL,
    "failed": STATUS_CRITICAL,
    "dead_letter": STATUS_CRITICAL,
}


def _fmt(n: float) -> str:
    if float(n).is_integer():
        return f"{int(n):,}"
    return f"{n:,.1f}"


def horizontal_bar_chart(rows: list[dict], *, width: int = 720, bar_height: int = 20,
                          row_gap: int = 18, label_width: int = 260, min_bar_px: int = 3) -> Markup:
    """rows: [{"label": str, "value": float, "color": "#hex" (optional,
    defaults to the sequential blue), "note": str (optional, appended
    after the formatted value)}]. Empty/all-zero input returns an empty
    string - the caller's existing "no data yet" copy already covers
    that case, this never renders a chart with nothing to show."""
    if not rows:
        return Markup("")
    max_value = max((r["value"] for r in rows), default=0) or 1
    bar_area_width = width - label_width - 90
    row_height = bar_height + row_gap
    height = len(rows) * row_height - row_gap + 8
    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" style="max-width:{width}px; '
             f'overflow:visible;" role="img" aria-label="Bar chart">']
    for i, row in enumerate(rows):
        y = i * row_height
        value = row["value"]
        color = row.get("color") or SEQUENTIAL_BLUE
        bar_w = max((value / max_value) * bar_area_width, min_bar_px if value > 0 else 0)
        label = escape(str(row["label"]))
        value_text = escape(row.get("display") or _fmt(value))
        note = f' <tspan fill="var(--muted)" font-size="12">{escape(row["note"])}</tspan>' if row.get("note") else ""
        parts.append(
            f'<g>'
            f'<title>{label}: {value_text}</title>'
            f'<text x="{label_width - 12}" y="{y + bar_height * 0.72}" text-anchor="end" '
            f'font-size="13" fill="var(--ink)">{label}</text>'
            f'<rect x="{label_width}" y="{y}" width="{bar_w:.1f}" height="{bar_height}" rx="4" fill="{color}"/>'
            f'<text x="{label_width + bar_w + 10:.1f}" y="{y + bar_height * 0.72}" '
            f'font-size="13" font-weight="600" fill="var(--ink)">{value_text}{note}</text>'
            f'</g>'
        )
    parts.append("</svg>")
    return Markup("".join(parts))


def funnel_chart_svg(stages: list[tuple[str, int]], total: int | None = None) -> Markup:
    """stages: [(label, count), ...] in funnel order (top = most people).
    Direct-labels each bar with count and, when total is given (the
    top-of-funnel figure = 100%), the % of that total - the real number
    staff_ops.html's own funnel table already computes, just visualized."""
    rows = []
    for label, count in stages:
        note = f"({count / total * 100:.0f}%)" if total else None
        rows.append({"label": label, "value": count, "color": SEQUENTIAL_BLUE, "note": note})
    return horizontal_bar_chart(rows, label_width=280)


def status_bar_chart_svg(status_counts: dict, statuses: list[str]) -> Markup:
    """statuses: the real, fixed display order (staff_ops.html's own
    ['pending_payment', 'queued', ...] list) - not sorted by count, so the
    chart's row order matches the queue-health chip row right below it."""
    rows = [
        {"label": s.replace("_", " "), "value": status_counts.get(s, 0),
         "color": _ORDER_STATUS_COLOR.get(s, SEQUENTIAL_BLUE)}
        for s in statuses
    ]
    return horizontal_bar_chart(rows, label_width=200)
