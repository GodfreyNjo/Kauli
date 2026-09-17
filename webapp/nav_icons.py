"""Real inline SVG glyphs for the sidebar nav, replacing the old single-
letter icon boxes ("O", "Q", "R"...) that read as unfinished/placeholder
rather than a deliberate choice - see the Sept 2026 dashboard redesign
request. Deliberately hand-drawn with straight lines, circles and simple
arcs only (no freehand bezier path data) so every glyph is easy to verify
correct by eye instead of trusting an unrenderable curve.

Each icon shares one 20x20 viewBox and takes its color from the existing
.icon CSS box (stroke="currentColor"), so it drops into the current nav
markup/hover/active states with no other template or CSS changes."""

from markupsafe import Markup

_STROKE = 'fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"'

_ICONS = {
    "grid": """<rect x="2.5" y="2.5" width="6.5" height="6.5" rx="1.3"/><rect x="11" y="2.5" width="6.5" height="6.5" rx="1.3"/>
        <rect x="2.5" y="11" width="6.5" height="6.5" rx="1.3"/><rect x="11" y="11" width="6.5" height="6.5" rx="1.3"/>""",
    "checklist": """<circle cx="4" cy="5.5" r="1.3"/><line x1="8" y1="5.5" x2="17.5" y2="5.5"/>
        <circle cx="4" cy="10.5" r="1.3"/><line x1="8" y1="10.5" x2="17.5" y2="10.5"/>
        <circle cx="4" cy="15.5" r="1.3"/><line x1="8" y1="15.5" x2="17.5" y2="15.5"/>""",
    "bars": """<line x1="2.5" y1="17" x2="17.5" y2="17"/><rect x="4" y="10.5" width="3" height="6.5"/>
        <rect x="8.5" y="6" width="3" height="11"/><rect x="13" y="12.5" width="3" height="4.5"/>""",
    "target": """<circle cx="10" cy="10" r="7"/><circle cx="10" cy="10" r="3.8"/><circle cx="10" cy="10" r="0.8" fill="currentColor" stroke="none"/>""",
    "users": """<circle cx="10" cy="6.7" r="3.2"/><path d="M3.5 17c0-3.5 3-6 6.5-6s6.5 2.5 6.5 6"/>""",
    "alert": """<path d="M10 2.8l7.8 13.9a1 1 0 01-.9 1.5H3.1a1 1 0 01-.9-1.5z"/>
        <line x1="10" y1="8" x2="10" y2="12.2"/><circle cx="10" cy="14.6" r="0.85" fill="currentColor" stroke="none"/>""",
    "card": """<rect x="2.3" y="5" width="15.4" height="10.5" rx="1.6"/><line x1="2.3" y1="8.4" x2="17.7" y2="8.4"/>
        <line x1="4.7" y1="12.4" x2="8.2" y2="12.4"/>""",
    "paper": """<rect x="3.3" y="2.5" width="13.4" height="15" rx="1.3"/><line x1="6.2" y1="6.6" x2="13.8" y2="6.6"/>
        <line x1="6.2" y1="9.8" x2="13.8" y2="9.8"/><line x1="6.2" y1="13" x2="11" y2="13"/>""",
    "mail": """<rect x="2.3" y="4.7" width="15.4" height="10.6" rx="1.4"/><polyline points="2.9,5.6 10,10.8 17.1,5.6"/>""",
    "book_open": """<path d="M10 5.3c-1.6-1.1-4-1.6-6.7-1.1v10.9c2.7-.5 5.1 0 6.7 1.1c1.6-1.1 4-1.6 6.7-1.1V4.2c-2.7-.5-5.1 0-6.7 1.1z"/>
        <line x1="10" y1="5.3" x2="10" y2="16.2"/>""",
    "book": """<rect x="4.3" y="2.8" width="12.4" height="14.4" rx="1.3"/><line x1="7.3" y1="2.8" x2="7.3" y2="17.2"/>""",
    "folder": """<path d="M2.5 6.3a1 1 0 011-1h4.2l1.6 2h7.2a1 1 0 011 1v7.4a1 1 0 01-1 1h-13a1 1 0 01-1-1z"/>""",
    "pulse": """<polyline points="2,10.5 5.8,10.5 7.6,3.8 12,16.5 14,10.5 18,10.5"/>""",
    "mic": """<rect x="7.5" y="2.3" width="5" height="9.2" rx="2.5"/><path d="M4.3 9.8a5.7 5.7 0 0011.4 0"/>
        <line x1="10" y1="15.5" x2="10" y2="18"/><line x1="6.8" y1="18" x2="13.2" y2="18"/>""",
    "shield": """<path d="M10 2.4l6.7 2.4v5.1c0 4.6-2.9 7.4-6.7 9c-3.8-1.6-6.7-4.4-6.7-9V4.8z"/>
        <polyline points="7.1,9.9 9.2,12 13,7.8"/>""",
    "lock": """<rect x="4.3" y="9" width="11.4" height="8.2" rx="1.5"/><path d="M6.5 9V6.2a3.5 3.5 0 017 0V9"/>""",
    "briefcase": """<rect x="2.4" y="7" width="15.2" height="9.3" rx="1.5"/><path d="M7.1 7V5.1a1 1 0 011-1h3.8a1 1 0 011 1V7"/>
        <line x1="2.4" y1="11.3" x2="17.6" y2="11.3"/>""",
    "message": """<path d="M3 4.3h14v9.4H8.2l-3.6 2.7v-2.7H3z"/>""",
    "gear": """<circle cx="10" cy="10" r="3"/><line x1="10" y1="2.2" x2="10" y2="4.7"/><line x1="10" y1="15.3" x2="10" y2="17.8"/>
        <line x1="2.2" y1="10" x2="4.7" y2="10"/><line x1="15.3" y1="10" x2="17.8" y2="10"/>
        <line x1="4.9" y1="4.9" x2="6.6" y2="6.6"/><line x1="13.4" y1="13.4" x2="15.1" y2="15.1"/>
        <line x1="4.9" y1="15.1" x2="6.6" y2="13.4"/><line x1="13.4" y1="6.6" x2="15.1" y2="4.9"/>""",
}


def nav_icon(name: str) -> Markup:
    body = _ICONS.get(name)
    if not body:
        return Markup('<span class="icon">?</span>')
    return Markup(f'<span class="icon"><svg viewBox="0 0 20 20" {_STROKE} aria-hidden="true">{body}</svg></span>')


# Extra glyphs for the redesigned stat-tile icon circles (dashboard-stat-
# card / stat-tile) - kept separate from _ICONS above since these render
# bare (no .icon nav-box wrapper), sized by the .stat-icon CSS instead.
_STAT_ICONS = {
    **_ICONS,
    "spinner": """<circle cx="10" cy="10" r="6.5" stroke-dasharray="24 41"/>""",
    "eye": """<path d="M2.3 10S5.3 4.5 10 4.5 17.7 10 17.7 10 14.7 15.5 10 15.5 2.3 10 2.3 10z"/><circle cx="10" cy="10" r="2.4"/>""",
    "check": """<polyline points="4,10.5 8,14.5 16,5.5"/>""",
}


def icon_svg(name: str) -> Markup:
    body = _STAT_ICONS.get(name, "")
    return Markup(f'<svg viewBox="0 0 20 20" {_STROKE} aria-hidden="true">{body}</svg>')


def trend_arrow(pct) -> Markup:
    """Small up/down triangle for a trend-chip - pct >= 0 points up (real
    growth), negative points down. Kept as one shared glyph instead of two
    near-duplicate feather icons since it's just a triangle either way."""
    if pct is None:
        return Markup("")
    if pct >= 0:
        points = "5,9 10,3 15,9"
    else:
        points = "5,3 10,9 15,3"
    return Markup(f'<svg viewBox="0 0 20 12" fill="currentColor" aria-hidden="true"><polygon points="{points}"/></svg>')
