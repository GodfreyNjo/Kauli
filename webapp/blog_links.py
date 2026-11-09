"""Never link a visitor, or Google, to an article that is not published.

An article that is still a draft answers 404. A link to it from a published article, or from the home page FAQ, is
therefore a dead end (and a "Not found" in Search Console). Rather than stop writing those links, which are right the
moment the draft is published, they are dropped at the moment a page is shown, and come back by themselves.

  keep_published_links(html)  the same html, with a link to an unpublished article turned into its plain text
  link_ok(href)               False for an address of an unpublished article, True for everything else

`register(app_module)` makes both available to the templates (`|keep_published_links` and `blog_link_ok(...)`).
"""
from __future__ import annotations

import re

from . import db

OUR_SITE = r"(?:https?://(?:www\.)?kauli-forgemedia\.com)?"          # a link to any other site is never touched
_BLOG_ADDRESS = re.compile(rf"^{OUR_SITE}/blog/([a-z0-9-]+)/?(?:[?#].*)?$", re.I)
_LINK = re.compile(
    rf"""<a\b[^>]*?\bhref=["']({OUR_SITE}/blog/[a-z0-9-]+/?(?:[?#][^"']*)?)["'][^>]*>(.*?)</a>""", re.S | re.I)


def published_slugs() -> set[str]:
    conn = db.get_conn()
    try:
        return {row["slug"] for row in conn.execute("SELECT slug FROM blog_posts WHERE status = 'published'")}
    finally:
        conn.close()


def link_ok(href: str, slugs: set[str] | None = None) -> bool:
    """False when `href` is the address of an article that is not published."""
    found = _BLOG_ADDRESS.match(href or "")
    if not found:
        return True
    return found.group(1).lower() in (published_slugs() if slugs is None else slugs)


def keep_published_links(html: str | None) -> str:
    """The html with every link to an unpublished article replaced by the words of the link."""
    text = html or ""
    if "/blog/" not in text:
        return text
    slugs = published_slugs()
    return _LINK.sub(lambda m: m.group(0) if link_ok(m.group(1), slugs) else m.group(2), text)


def register(app_module) -> None:
    app_module.templates.env.filters["keep_published_links"] = keep_published_links
    app_module.templates.env.globals["blog_link_ok"] = link_ok
