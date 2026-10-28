"""Send a finished transcript to a customer's own Notion workspace.

The customer creates an internal integration in Notion, shares one page or
database with it, and gives Kauli the integration token plus that page's link.
Kauli has no Notion app of its own to authorise, so there is no OAuth here:
every call goes to api.notion.com with the customer's token, and only that.

Pure logic with the HTTP call injected (`http`), so it is tested without a network."""
from __future__ import annotations

import re
import time
from typing import Callable

import httpx

API = "https://api.notion.com"
NOTION_VERSION = "2022-06-28"
MAX_TEXT = 1900          # Notion rejects a single rich-text object over 2000 characters
MAX_BLOCKS_PER_REQUEST = 100
PAUSE_BETWEEN_REQUESTS = 0.35   # Notion allows about 3 requests a second

_ID_RE = re.compile(r"[0-9a-fA-F]{32}$")


class NotionError(Exception):
    """A problem the customer can act on; the message is safe to show them."""


def parse_notion_id(link: str) -> str | None:
    """The 32-character page or database id inside a Notion link (or a bare id)."""
    if not link:
        return None
    # The id is always the LAST 32 characters of the final path segment. Searching the
    # whole string would also swallow hex letters (a-f) from the end of the page name.
    segment = link.strip().split("?")[0].split("#")[0].rstrip("/").split("/")[-1].replace("-", "")
    found = _ID_RE.search(segment)
    return found.group(0).lower() if found else None


def _call(token: str, method: str, path: str, body: dict | None, http: Callable) -> dict:
    try:
        resp = http(method, f"{API}{path}", headers={
            "Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json"}, json=body, timeout=20.0, follow_redirects=False)
    except httpx.HTTPError as exc:
        raise NotionError(f"Could not reach Notion ({type(exc).__name__}). Try again in a moment.") from exc
    if resp.status_code == 200:
        return resp.json()
    if resp.status_code == 401:
        raise NotionError("Notion rejected that token. Copy the integration's secret again from notion.so/my-integrations.")
    if resp.status_code == 404:
        raise NotionError("Notion can't find that page. Open it in Notion, choose the ... menu, then Connections, "
                          "and add your integration.")
    if resp.status_code == 403:
        raise NotionError("Your Notion integration isn't allowed to do that. Give it the Insert content capability.")
    if resp.status_code == 429:
        raise NotionError("Notion is rate limiting us. Try again in a minute.")
    try:
        detail = resp.json().get("message", "")
    except Exception:
        detail = ""
    raise NotionError(f"Notion refused the request ({resp.status_code}). {detail}".strip())


def validate_connection(token: str, parent_id: str, http: Callable = httpx.request) -> dict:
    """Checks the token and that the page/database is shared with it.
    Returns {"kind": "database" | "page", "title_prop": str, "bot_name": str}."""
    me = _call(token, "GET", "/v1/users/me", None, http)
    bot_name = (me.get("name") or "your integration")[:80]
    try:
        db = _call(token, "GET", f"/v1/databases/{parent_id}", None, http)
        title_prop = next((name for name, prop in (db.get("properties") or {}).items()
                           if prop.get("type") == "title"), "Name")
        return {"kind": "database", "title_prop": title_prop, "bot_name": bot_name}
    except NotionError as database_problem:
        # An id that is a page (not a database) comes back as 400 or 404 here; try it as a page.
        try:
            _call(token, "GET", f"/v1/pages/{parent_id}", None, http)
            return {"kind": "page", "title_prop": "title", "bot_name": bot_name}
        except NotionError:
            raise database_problem


def _text(content: str, link: str | None = None, bold: bool = False) -> dict:
    node = {"type": "text", "text": {"content": content}}
    if link:
        node["text"]["link"] = {"url": link}
    if bold:
        node["annotations"] = {"bold": True}
    return node


def _paragraph(rich: list[dict]) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich}}


def _split_text(text: str, limit: int = MAX_TEXT) -> list[str]:
    """Pieces of at most `limit` characters, broken on spaces where possible."""
    text = text.strip()
    pieces = []
    while len(text) > limit:
        cut = text.rfind(" ", 0, limit)
        cut = cut if cut > limit // 2 else limit
        pieces.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        pieces.append(text)
    return pieces


def build_blocks(meta: str, order_link: str, lines: list[str], ai_note: bool) -> list[dict]:
    blocks = [_paragraph([_text("Made with Kauli. ", bold=True), _text("Open the order", link=order_link)]),
              _paragraph([_text(meta[:MAX_TEXT])])]
    if ai_note:
        blocks.append(_paragraph([_text("Instant AI transcript: generated by AI and not reviewed by a person, "
                                        "so check names, numbers and key terms before you publish it.")]))
    blocks.append({"object": "block", "type": "heading_2",
                   "heading_2": {"rich_text": [_text("Transcript")]}})
    for line in lines:
        for piece in _split_text(line):
            blocks.append(_paragraph([_text(piece)]))
    return blocks


def create_page(token: str, parent_id: str, kind: str, title_prop: str, title: str, blocks: list[dict],
                http: Callable = httpx.request, sleep: Callable = time.sleep) -> dict:
    """Creates the page with the first 100 blocks, then appends the rest in order."""
    title_rich = {"title": [_text(title[:200])]}
    body = {
        "parent": {"database_id": parent_id} if kind == "database" else {"page_id": parent_id},
        "properties": {title_prop if kind == "database" else "title": title_rich},
        "children": blocks[:MAX_BLOCKS_PER_REQUEST],
    }
    page = _call(token, "POST", "/v1/pages", body, http)
    rest = blocks[MAX_BLOCKS_PER_REQUEST:]
    for i in range(0, len(rest), MAX_BLOCKS_PER_REQUEST):
        sleep(PAUSE_BETWEEN_REQUESTS)
        _call(token, "PATCH", f"/v1/blocks/{page['id']}/children",
              {"children": rest[i:i + MAX_BLOCKS_PER_REQUEST]}, http)
    return {"id": page["id"], "url": page.get("url")}
