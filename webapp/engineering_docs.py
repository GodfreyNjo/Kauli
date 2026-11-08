"""The engineering guide that staff read at /staff/engineering.

The guide is a folder of Markdown files, `webapp/staff_docs/engineering/`, kept in the same repository as the code.
That is deliberate: a change to how the system works and the change to its documentation travel in the same commit
and are reviewed together, so the guide cannot quietly fall behind. This module only lists and renders those files.
"""
from __future__ import annotations

import re
from pathlib import Path

import markdown

DOCS_DIR = Path(__file__).parent / "staff_docs" / "engineering"
MARKDOWN_EXTENSIONS = ["tables", "fenced_code", "toc", "sane_lists"]
TITLE_LINE = re.compile(r"^#\s+(.+)$", re.MULTILINE)


def _title_of(text: str, fallback: str) -> str:
    match = TITLE_LINE.search(text)
    return match.group(1).strip() if match else fallback


def available() -> list[dict]:
    """Every page of the guide, in reading order (the file names start with a number): [{"slug", "title"}]."""
    pages = []
    for path in sorted(DOCS_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        pages.append({"slug": path.stem, "title": _title_of(text, path.stem)})
    return pages


def render(slug: str) -> dict | None:
    """One page as {"slug", "title", "html"}, or None when there is no such page.

    Only names that exist in the folder are accepted, so a slug can never reach any other file.
    """
    if slug not in {page["slug"] for page in available()}:
        return None
    text = (DOCS_DIR / f"{slug}.md").read_text(encoding="utf-8")
    html = markdown.markdown(text, extensions=MARKDOWN_EXTENSIONS)
    return {"slug": slug, "title": _title_of(text, slug), "html": html}
