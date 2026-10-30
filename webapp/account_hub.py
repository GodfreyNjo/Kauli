"""The customer-account tools beyond orders: AI Notes, Analytics, the team Workspace, Custom vocabulary, Plans, Rewards
(referrals), Integrations, the help Assistant with hand-off to the team, and the staff side of that hand-off.

Routes live here (not in app.py) so that file does not grow further; app.py calls register(sys.modules[__name__]) once and
every handler reads current_user, templates, ... from that module at request time (tests replace current_user).
"""
from __future__ import annotations

import html
import json
import re
import secrets
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import quote

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import site_analytics as analytics_engine
from . import assistant_core, billing, blog_audio, cloud_import, connections, db, ga_events, ga_reports, mailer, order_ai_assist, rate_limit, transcript_export

router = APIRouter()
A = None          # the app module, set by register()


def register(app_module) -> None:
    global A
    A = app_module
    app_module.app.include_router(router)


# ------------------------------------------------------------------------------------------------ small helpers
def _client(request: Request):
    user = A.current_user(request)
    return user if user and user["role"] == "client" else None


def _to_login(request: Request):
    return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=303)


def _render(request: Request, name: str, user, **ctx):
    return A.templates.TemplateResponse(request, name, {"user": user, "role": "client", **ctx})


def _epoch(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value)).replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _owner(user) -> bool:
    return user["client_scope_id"] == user["id"]


# ================================================================================================ AI NOTES
_NOTE_TAGS = {"p", "br", "b", "strong", "i", "em", "u", "h1", "h2", "h3", "ul", "ol", "li", "blockquote", "a", "div", "span"}
MAX_NOTE_BYTES = 200_000


class _Sanitizer(HTMLParser):
    """Keeps formatting a note editor produces and nothing else: no scripts, styles, event handlers or odd links."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
            return
        if self.skip or tag not in _NOTE_TAGS:
            return
        if tag == "br":
            self.out.append("<br>")
            return
        if tag == "a":
            href = (dict(attrs).get("href") or "").strip()
            if not re.match(r"^(https?://|mailto:)", href, re.I):
                return
            self.out.append(f'<a href="{html.escape(href, quote=True)}" target="_blank" rel="noopener noreferrer">')
        else:
            self.out.append(f"<{tag}>")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag not in self.stack:
            return
        while self.stack:
            top = self.stack.pop()
            self.out.append(f"</{top}>")
            if top == tag:
                break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(html.escape(data))

    def result(self) -> str:
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return "".join(self.out)


def sanitize_note_html(raw: str) -> str:
    p = _Sanitizer()
    p.feed((raw or "")[:MAX_NOTE_BYTES])
    p.close()
    return p.result()


class _Text(HTMLParser):
    BLOCK = {"p", "div", "h1", "h2", "h3", "li", "blockquote", "br", "ul", "ol"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = [""]

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK:
            self.lines.append("• " if tag == "li" else "")

    def handle_endtag(self, tag):
        if tag in self.BLOCK and tag != "br":
            self.lines.append("")

    def handle_data(self, data):
        self.lines[-1] += data


def note_lines(body_html: str) -> list[str]:
    p = _Text()
    p.feed(body_html or "")
    return [ln.strip() for ln in p.lines if ln.strip() and ln.strip() != "•"]


_STOP = set("""a about above after again all also am an and any are as at be because been before being below between both but by
can could did do does doing down during each few for from further had has have having he her here hers him his how i if in into is it
its just me more most my no nor not now of off on once only or other our out over own same she should so some such than that the
their them then there these they this those through to too under until up very was we were what when where which while who whom why
will with would you your yeah okay um uh like really going get got one two""".split())
_ACTION = re.compile(r"\b(will|need to|needs to|should|must|have to|let's|let us|going to|action item|deadline|follow up|follow-up|by (monday|tuesday|wednesday|thursday|friday|tomorrow|next week|end of))\b", re.I)


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [p.strip() for p in parts if len(p.strip().split()) >= 4]


def extractive_notes(text: str) -> str:
    """Notes built only from the transcript's own sentences (no AI needed): overview, key points, action items, questions."""
    sents = _sentences(text)
    if not sents:
        return "<p>There was not enough spoken text to make notes from.</p>"
    freq = Counter(w for s in sents for w in re.findall(r"[a-zA-Z']{3,}", s.lower()) if w not in _STOP)
    scored = sorted(range(len(sents)), key=lambda i: -sum(freq[w] for w in re.findall(r"[a-zA-Z']{3,}", sents[i].lower())) / (len(sents[i].split()) ** 0.6))
    key_idx = sorted(scored[:6])
    actions = [s for s in sents if _ACTION.search(s)][:6]
    questions = [s for s in sents if s.endswith("?")][:5]

    def ul(items):
        return "<ul>" + "".join(f"<li>{html.escape(i)}</li>" for i in items) + "</ul>"

    out = ["<h2>Overview</h2><p>" + html.escape(" ".join(sents[:2])) + "</p>", "<h2>Key points</h2>" + ul([sents[i] for i in key_idx])]
    if actions:
        out.append("<h2>Action items</h2>" + ul(actions))
    if questions:
        out.append("<h2>Open questions</h2>" + ul(questions))
    return "".join(out)


NOTES_PROMPT = """You turn a transcript into clear meeting-style notes for the person who recorded it. Use ONLY what the transcript \
says: never invent names, numbers, dates or decisions. Output simple HTML using only <h2>, <p>, <ul>, <li>, <strong>. Sections, \
skipping any that would be empty: Overview (2-3 sentences), Key points, Decisions, Action items (who, what, when if stated), \
Open questions. No preamble, no markdown fences."""


def ai_notes(text: str) -> str:
    """Claude-written notes when an API key is set, otherwise (or on any failure) the extractive version."""
    if order_ai_assist.ai_assist_configured() and text.strip():
        try:
            resp = order_ai_assist._client().messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=1200, system=NOTES_PROMPT,
                messages=[{"role": "user", "content": f"Transcript:\n\n{text[:14000]}"}])
            out = sanitize_note_html("".join(b.text for b in resp.content if b.type == "text"))
            if out.strip():
                return out
        except Exception:  # noqa: BLE001 - fall back rather than fail the customer
            pass
    return extractive_notes(text)


def _transcript_text(order) -> str:
    job = A._load_job(order)
    if job is None:
        return ""
    return "\n".join((f"{r['speaker']}: " if r["speaker"] else "") + r["text"] for r in transcript_export.rows(job, "source"))


def _note_owner(user, note_id: str):
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM notes WHERE id = ? AND client_id = ?", (note_id, user["client_scope_id"])).fetchone()
    conn.close()
    return row


def _plain_preview(body_html: str, n: int = 110) -> str:
    text = " ".join(note_lines(body_html))
    return text[:n] + ("..." if len(text) > n else "")


@router.get("/client/notes", response_class=HTMLResponse)
def notes_page(request: Request, id: str | None = None, q: str = ""):
    user = _client(request)
    if not user:
        return _to_login(request)
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM notes WHERE client_id = ? ORDER BY updated_at DESC", (user["client_scope_id"],)).fetchall()
    conn.close()
    needle = q.strip().lower()
    shown = [r for r in rows if not needle or needle in r["title"].lower() or needle in " ".join(note_lines(r["body_html"])).lower()]
    current = next((r for r in rows if r["id"] == id), None) if id else None
    return _render(request, "account_notes.html", user, notes=[{"row": r, "preview": _plain_preview(r["body_html"]),
                                                                "when": datetime.fromtimestamp(r["updated_at"]).strftime("%b %d, %H:%M")} for r in shown],
                   current=current, q=q, total=len(rows))


@router.post("/client/notes/new")
def notes_new(request: Request):
    user = _client(request)
    if not user:
        return _to_login(request)
    note_id = uuid.uuid4().hex[:12]
    now = time.time()
    conn = db.get_conn()
    conn.execute("INSERT INTO notes (id, client_id, author_id, title, body_html, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (note_id, user["client_scope_id"], user["id"], "Untitled note", "", now, now))
    conn.commit()
    conn.close()
    track_feature(request, "note_created", source="blank")
    return RedirectResponse(f"/client/notes?id={note_id}", status_code=303)


@router.post("/client/notes/{note_id}/save")
async def notes_save(request: Request, note_id: str):
    user = _client(request)
    if not user:
        return JSONResponse({"error": "Sign in to continue."}, status_code=401)
    if not _note_owner(user, note_id):
        return JSONResponse({"error": "Note not found."}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Bad request."}, status_code=400)
    title = str(body.get("title") or "").strip()[:200] or "Untitled note"
    clean = sanitize_note_html(str(body.get("body_html") or ""))
    now = time.time()
    conn = db.get_conn()
    conn.execute("UPDATE notes SET title = ?, body_html = ?, updated_at = ? WHERE id = ?", (title, clean, now, note_id))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "updated_at": now})


@router.post("/client/notes/{note_id}/delete")
def notes_delete(request: Request, note_id: str):
    user = _client(request)
    if not user:
        return _to_login(request)
    conn = db.get_conn()
    conn.execute("DELETE FROM notes WHERE id = ? AND client_id = ?", (note_id, user["client_scope_id"]))
    conn.commit()
    conn.close()
    return RedirectResponse("/client/notes", status_code=303)


@router.get("/client/notes/{note_id}/download")
def notes_download(request: Request, note_id: str, format: str = "docx"):
    user = _client(request)
    if not user:
        return _to_login(request)
    note = _note_owner(user, note_id)
    if not note:
        return HTMLResponse("Note not found.", status_code=404)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", note["title"]).strip("_") or "note"
    lines = note_lines(note["body_html"])
    if format == "txt":
        data, media, ext = (note["title"] + "\n\n" + "\n\n".join(lines) + "\n").encode("utf-8"), "text/plain; charset=utf-8", "txt"
    elif format == "html":
        page = f"<!doctype html><meta charset=utf-8><title>{html.escape(note['title'])}</title><h1>{html.escape(note['title'])}</h1>{note['body_html']}"
        data, media, ext = page.encode("utf-8"), "text/html; charset=utf-8", "html"
    else:
        data = transcript_export.docx_from_paragraphs(note["title"], lines)
        media, ext = "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"
    return Response(content=data, media_type=media, headers={"Content-Disposition": f'attachment; filename="{stem}.{ext}"'})


@router.post("/client/orders/{order_id}/notes")
def notes_from_order(request: Request, order_id: str, kind: str = Query("ai", alias="as")):
    """Make a note from one of the customer's finished transcripts: AI-written (extractive when no AI key is set), or with
    ?as=transcript the transcript itself, to edit freely as a note."""
    user = _client(request)
    if not user:
        return _to_login(request)
    order = db.get_order(order_id)
    if not order or order["client_id"] != user["client_scope_id"]:
        return HTMLResponse("Order not found.", status_code=404)
    if A._deliverable_denied_reason(order, "txt"):
        return RedirectResponse(f"/client/orders/{order_id}", status_code=303)
    allowed, retry_after = rate_limit.check(f"notes-ai:{user['id']}", limit=10, window_s=600)
    if not allowed:
        return HTMLResponse(f"Too many requests. Try again in {retry_after}s.", status_code=429)
    text = _transcript_text(order)
    if kind == "transcript":
        body = "".join(f"<p>{html.escape(line)}</p>" for line in text.splitlines() if line.strip()) or "<p></p>"
        title = f"Transcript: {order['original_filename'] or order_id}"[:200]
    else:
        body = ai_notes(text)
        title = f"Notes: {order['original_filename'] or order_id}"[:200]
    note_id = uuid.uuid4().hex[:12]
    now = time.time()
    conn = db.get_conn()
    conn.execute("INSERT INTO notes (id, client_id, author_id, order_id, title, body_html, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (note_id, user["client_scope_id"], user["id"], order_id, title, body, now, now))
    conn.commit()
    conn.close()
    track_feature(request, "note_created", source="transcript" if kind == "transcript" else "ai")
    return RedirectResponse(f"/client/notes?id={note_id}", status_code=303)


# ================================================================================================ ANALYTICS
_POS = set("""good great excellent amazing awesome love loved happy pleased glad thanks thank wonderful perfect fantastic helpful
brilliant nice best better success successful agree agreed congratulations enjoy enjoyed excited exciting positive easy smooth
appreciate impressive improved improvement fine well win won solved resolved""".split())
_NEG = set("""bad terrible awful hate hated angry upset sad sorry problem problems issue issues wrong worse worst fail failed failure
broken complaint complain disappointed disappointing frustrated frustrating difficult hard delay delayed late cancel cancelled
refund unhappy confusing confused error mistake risk concern concerned unfortunately loss lost stuck""".split())


def sentiment_counts(text_lines: list[str]) -> tuple[int, int, int]:
    pos = neg = neu = 0
    for line in text_lines:
        words = re.findall(r"[a-z']+", line.lower())
        score = sum(w in _POS for w in words) - sum(w in _NEG for w in words)
        if score > 0:
            pos += 1
        elif score < 0:
            neg += 1
        else:
            neu += 1
    return pos, neg, neu


def _ensure_sentiment(orders, limit: int = 25) -> dict:
    """order_id -> (positive, negative, neutral) for finished English orders, computed once and kept."""
    ready = [o for o in orders if o["status"] in db.TERMINAL_OK_STATUSES and (o["source_lang"] or "") == "en"]
    conn = db.get_conn()
    known = {r["order_id"]: (r["positive"], r["negative"], r["neutral"]) for r in
             conn.execute("SELECT * FROM order_sentiment WHERE order_id IN (%s)" % ",".join("?" * len(ready)), [o["id"] for o in ready]).fetchall()} if ready else {}
    todo = [o for o in ready if o["id"] not in known][:limit]
    for o in todo:
        job = A._load_job(o)
        lines = [r["text"] for r in transcript_export.rows(job, "source")] if job else []
        counts = sentiment_counts(lines)
        conn.execute("INSERT OR REPLACE INTO order_sentiment (order_id, positive, negative, neutral, computed_at) VALUES (?, ?, ?, ?, ?)",
                     (o["id"], *counts, time.time()))
        known[o["id"]] = counts
    conn.commit()
    conn.close()
    return known


def _line_chart(points: list[tuple[str, float]], width: int = 640, height: int = 220) -> str:
    """A small inline-SVG line chart: label, value pairs. Axis text uses the page's own colours."""
    if not points:
        return ""
    pad_l, pad_r, pad_t, pad_b = 44, 14, 16, 30
    top = max(v for _, v in points) or 1.0
    top = float(f"{top:.2g}") if top < 10 else (int(top / 10 ** (len(str(int(top))) - 1)) + 1) * 10 ** (len(str(int(top))) - 1)
    w, h = width - pad_l - pad_r, height - pad_t - pad_b
    xs = [pad_l + (w * i / max(1, len(points) - 1)) for i in range(len(points))]
    ys = [pad_t + h - (h * v / top) for _, v in points]
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    grid = "".join(
        f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{pad_t + h * (1 - f):.1f}" y2="{pad_t + h * (1 - f):.1f}" class="an-grid"/>'
        f'<text x="{pad_l - 6}" y="{pad_t + h * (1 - f) + 4:.1f}" text-anchor="end" class="an-axis">{f"{top * f:.1f}".rstrip("0").rstrip(".")}</text>' for f in (0, 0.5, 1))
    dots = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" class="an-dot"><title>{html.escape(l)}: {v:.0f} min</title></circle>'
                   for x, y, (l, v) in zip(xs, ys, points))
    labels = "".join(f'<text x="{xs[i]:.1f}" y="{height - 8}" text-anchor="middle" class="an-axis">{html.escape(points[i][0])}</text>'
                     for i in range(len(points)) if i % max(1, len(points) // 6) == 0 or i == len(points) - 1)
    return (f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Transcription minutes per month" class="an-svg">{grid}'
            f'<polyline points="{path}" class="an-line" fill="none"/>{dots}{labels}</svg>')


def _donut(parts: list[tuple[str, float, str]]) -> str:
    total = sum(v for _, v, _ in parts) or 1.0
    r, c = 52, 2 * 3.14159265 * 52
    offset, rings = 0.0, []
    for label, v, cls in parts:
        length = c * v / total
        rings.append(f'<circle cx="70" cy="70" r="{r}" fill="none" class="{cls}" stroke-width="22" stroke-dasharray="{length:.2f} {c - length:.2f}" '
                     f'stroke-dashoffset="{-offset:.2f}" transform="rotate(-90 70 70)"><title>{html.escape(label)}: {v:.0f}</title></circle>')
        offset += length
    return f'<svg viewBox="0 0 140 140" role="img" aria-label="Conversation sentiment" class="an-donut">{"".join(rings)}</svg>'


@router.get("/client/analytics", response_class=HTMLResponse)
def analytics_page(request: Request, months_back: str = Query("12", alias="range")):
    user = _client(request)
    if not user:
        return _to_login(request)
    months = 6 if months_back == "6" else 3 if months_back == "3" else 12
    orders = list(db.list_orders_for_client(user["client_scope_id"]))
    done = [o for o in orders if o["status"] in db.TERMINAL_OK_STATUSES]
    now = datetime.now()
    keys = []
    y, m = now.year, now.month
    for _ in range(months):
        keys.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    keys.reverse()
    by_month = {k: 0.0 for k in keys}
    langs: Counter = Counter()
    services: Counter = Counter()
    total = 0.0
    for o in done:
        mins = float(o["duration_minutes"] or 0)
        d = datetime.fromtimestamp(_epoch(o["created_at"]))
        if (d.year, d.month) in by_month:
            by_month[(d.year, d.month)] += mins
            langs[o["source_lang"] or "?"] += mins
            services[o["service_level"] or "dub"] += mins
        total += mins
    month_names = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    points = [(f"{month_names[m - 1]}", by_month[(y, m)]) for y, m in keys]
    in_range = sum(by_month.values())
    names = A.LANGUAGE_NAMES
    lang_rows = [{"name": names.get(k, k.upper()), "minutes": v, "pct": 100 * v / (sum(langs.values()) or 1)} for k, v in langs.most_common(8)]
    svc_names = {"transcribe": "Transcription", "translate": "Translation", "dub": "Dubbing"}
    svc_rows = [{"name": svc_names.get(k, k.replace("_", " ").title()), "minutes": v, "pct": 100 * v / (sum(services.values()) or 1)} for k, v in services.most_common(6)]
    sent = _ensure_sentiment(done)
    pos = sum(v[0] for v in sent.values())
    neg = sum(v[1] for v in sent.values())
    neu = sum(v[2] for v in sent.values())
    return _render(request, "account_analytics.html", user, range=str(months), line=_line_chart(points) if in_range else "",
                   donut=_donut([("Positive", pos, "an-pos"), ("Neutral", neu, "an-neu"), ("Negative", neg, "an-neg")]) if (pos + neg + neu) else "",
                   sentiment={"pos": pos, "neg": neg, "neu": neu, "n": pos + neg + neu, "orders": len(sent)},
                   lang_rows=lang_rows, svc_rows=svc_rows, total_minutes=total, range_minutes=in_range, files=len(done),
                   all_files=len(orders))


# ================================================================================================ VOCABULARY
MAX_VOCAB_TERMS = 200


def list_vocab(client_id: str):
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM vocab_terms WHERE client_id = ? ORDER BY term COLLATE NOCASE", (client_id,)).fetchall()
    conn.close()
    return rows


def vocabulary_fixer(client_id: str):
    """A function that rewrites ASR text with this account's vocabulary, or None when they have none (the pipeline skips it)."""
    try:
        rows = list_vocab(client_id)
    except Exception:  # noqa: BLE001 - a vocabulary problem must never stop a transcript
        return None
    rules = []
    for r in rows:
        forms = {r["term"]} | {v.strip() for v in (r["variants"] or "").split(",") if v.strip()}
        for form in sorted(forms, key=len, reverse=True):
            rules.append((re.compile(r"(?<![\w])" + re.escape(form) + r"(?![\w])", re.I), r["term"]))
    if not rules:
        return None

    def fix(text: str) -> str:
        for pattern, term in rules:
            text = pattern.sub(term, text)
        return text
    fix.terms = [r["term"] for r in rows]            # also handed to the recognizer, so it hears the name right in the first place
    return fix


def _parse_vocab_lines(raw: str) -> list[tuple[str, str]]:
    """'Term' or 'Term | wrong1, wrong2' per line."""
    out = []
    for line in raw.splitlines():
        term, _, variants = line.partition("|")
        term = term.strip()[:80]
        if term:
            out.append((term, ", ".join(v.strip()[:80] for v in variants.split(",") if v.strip())[:300]))
    return out


@router.get("/client/vocabulary", response_class=HTMLResponse)
def vocabulary_page(request: Request, error: str | None = None, notice: str | None = None):
    user = _client(request)
    if not user:
        return _to_login(request)
    return _render(request, "account_vocabulary.html", user, terms=list_vocab(user["client_scope_id"]), error=error, notice=notice, limit=MAX_VOCAB_TERMS)


@router.post("/client/vocabulary/add")
def vocabulary_add(request: Request, entries: str = Form("")):
    user = _client(request)
    if not user:
        return _to_login(request)
    items = _parse_vocab_lines(entries)
    if not items:
        return RedirectResponse("/client/vocabulary?error=" + quote("Type a word or name to add."), status_code=303)
    cid = user["client_scope_id"]
    have = len(list_vocab(cid))
    conn = db.get_conn()
    added = 0
    for term, variants in items:
        if have + added >= MAX_VOCAB_TERMS:
            break
        cur = conn.execute("INSERT OR IGNORE INTO vocab_terms (id, client_id, term, variants, created_at) VALUES (?, ?, ?, ?, ?)",
                           (uuid.uuid4().hex[:12], cid, term, variants, time.time()))
        added += cur.rowcount
        if not cur.rowcount and variants:
            conn.execute("UPDATE vocab_terms SET variants = ? WHERE client_id = ? AND term = ?", (variants, cid, term))
    conn.commit()
    conn.close()
    track_feature(request, "vocabulary_added", count=added)
    note = f"Added {added}." if added else "Those are already in your list."
    if have + added >= MAX_VOCAB_TERMS:
        note += f" Your list is full ({MAX_VOCAB_TERMS} terms)."
    return RedirectResponse("/client/vocabulary?notice=" + quote(note), status_code=303)


@router.post("/client/vocabulary/delete")
def vocabulary_delete(request: Request, term_id: str = Form(...)):
    user = _client(request)
    if not user:
        return _to_login(request)
    conn = db.get_conn()
    conn.execute("DELETE FROM vocab_terms WHERE id = ? AND client_id = ?", (term_id, user["client_scope_id"]))
    conn.commit()
    conn.close()
    return RedirectResponse("/client/vocabulary?notice=Removed.", status_code=303)


# ================================================================================================ REWARDS
SIGNUP_REWARD_MINUTES = 100
UPGRADE_REWARD_MINUTES = 1000
MAX_REWARDED_SIGNUPS = 20          # per inviter: keeps a referral ring from turning into a free-transcription mill
MAX_REWARDED_UPGRADES = 5
_CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def referral_code_for(user) -> str:
    conn = db.get_conn()
    row = conn.execute("SELECT referral_code FROM users WHERE id = ?", (user["id"],)).fetchone()
    code = row["referral_code"] if row else None
    while not code:
        candidate = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
        try:
            conn.execute("UPDATE users SET referral_code = ? WHERE id = ? AND referral_code IS NULL", (candidate, user["id"]))
            conn.commit()
        except Exception:  # noqa: BLE001 - a code collision: try another
            continue
        code = conn.execute("SELECT referral_code FROM users WHERE id = ?", (user["id"],)).fetchone()["referral_code"]
    conn.close()
    return code


def _credit(conn, referrer_id: str, minutes: float) -> None:
    conn.execute("UPDATE subscriptions SET reward_minutes = reward_minutes + ? WHERE user_id = ?", (minutes, referrer_id))


def record_referral(new_user, code: str | None) -> bool:
    """Called once when a new client account is created. Links it to whoever invited them and credits the inviter."""
    if not code or not re.fullmatch(r"[a-z0-9]{4,16}", code.lower()):
        return False
    conn = db.get_conn()
    try:
        ref = conn.execute("SELECT id, role, email FROM users WHERE referral_code = ?", (code.lower(),)).fetchone()
        if not ref or ref["role"] != "client" or ref["id"] == new_user["id"] or (ref["email"] or "").lower() == (new_user["email"] or "").lower():
            return False
        if conn.execute("SELECT 1 FROM referrals WHERE referred_id = ?", (new_user["id"],)).fetchone():
            return False
        rewarded = conn.execute("SELECT COUNT(*) AS n FROM referrals WHERE referrer_id = ? AND signup_minutes > 0", (ref["id"],)).fetchone()["n"]
        minutes = SIGNUP_REWARD_MINUTES if rewarded < MAX_REWARDED_SIGNUPS else 0
        conn.execute("INSERT INTO referrals (id, referrer_id, referred_id, signed_up_at, signup_minutes) VALUES (?, ?, ?, ?, ?)",
                     (uuid.uuid4().hex[:12], ref["id"], new_user["id"], time.time(), minutes))
        conn.execute("UPDATE users SET referred_by = ? WHERE id = ?", (ref["id"], new_user["id"]))
        if minutes:
            _credit(conn, ref["id"], minutes)
        conn.commit()
    finally:
        conn.close()
    if minutes:
        db.create_notification(ref["id"], "referral", f"A friend joined with your link: +{minutes:.0f} minutes added", "/client/rewards")
    return True


def on_plan_paid(user_id: str, plan: str) -> None:
    """A referred account paying for a plan earns its inviter the bigger reward, once."""
    if plan not in ("pro", "premium", "enterprise"):
        return
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT * FROM referrals WHERE referred_id = ? AND upgraded_at IS NULL", (user_id,)).fetchone()
        if not row:
            return
        done = conn.execute("SELECT COUNT(*) AS n FROM referrals WHERE referrer_id = ? AND upgrade_minutes > 0", (row["referrer_id"],)).fetchone()["n"]
        minutes = UPGRADE_REWARD_MINUTES if done < MAX_REWARDED_UPGRADES else 0
        conn.execute("UPDATE referrals SET upgraded_at = ?, upgrade_minutes = ? WHERE id = ?", (time.time(), minutes, row["id"]))
        if minutes:
            _credit(conn, row["referrer_id"], minutes)
        conn.commit()
    finally:
        conn.close()
    if minutes:
        db.create_notification(row["referrer_id"], "referral", f"A friend you invited upgraded: +{minutes:.0f} minutes added", "/client/rewards")


@router.get("/r/{code}")
def referral_landing(request: Request, code: str):
    track_feature(request, "referral_visit")
    resp = RedirectResponse("/signup", status_code=303)
    if re.fullmatch(r"[a-z0-9]{4,16}", code.lower()):
        resp.set_cookie("kauli_ref", code.lower(), max_age=30 * 86400, httponly=True, samesite="lax", secure=request.url.scheme == "https")
    return resp


@router.get("/client/rewards", response_class=HTMLResponse)
def rewards_page(request: Request):
    user = _client(request)
    if not user:
        return _to_login(request)
    code = referral_code_for(user)
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM referrals WHERE referrer_id = ? ORDER BY signed_up_at DESC", (user["id"],)).fetchall()
    conn.close()
    sub = db.get_subscription(user["client_scope_id"])
    link = f"{A._site_base(request)}/r/{code}"
    text = f"Kauli turns audio and video into accurate transcripts and translations. Join with my link: {link}"
    return _render(request, "account_rewards.html", user, link=link, invited=len(rows), upgraded=sum(1 for r in rows if r["upgraded_at"]),
                   earned=sum((r["signup_minutes"] or 0) + (r["upgrade_minutes"] or 0) for r in rows),
                   balance=billing._reward_minutes(sub) if sub else 0.0, signup_minutes=SIGNUP_REWARD_MINUTES, upgrade_minutes=UPGRADE_REWARD_MINUTES,
                   whatsapp="https://wa.me/?text=" + quote(text), mailto="mailto:?subject=" + quote("Try Kauli") + "&body=" + quote(text),
                   x_link="https://twitter.com/intent/tweet?text=" + quote(text), max_signups=MAX_REWARDED_SIGNUPS, max_upgrades=MAX_REWARDED_UPGRADES)


# ================================================================================================ PLANS
@router.get("/client/plans", response_class=HTMLResponse)
def plans_page(request: Request, notice: str | None = None, error: str | None = None):
    user = _client(request)
    if not user:
        return _to_login(request)
    sub = db.get_subscription_current(user["client_scope_id"])
    plan = billing.effective_plan(user, sub)
    allowance = billing.plan_included_minutes(plan) + ((sub["bonus_minutes"] or 0.0) if sub else 0.0) + (billing._reward_minutes(sub) if sub else 0.0)
    used = (sub["minutes_used_this_period"] or 0.0) if sub else 0.0
    ends = sub["current_period_end"] if sub and plan != "free" else None
    resets = (sub["period_started_at"] + db.PERIOD_SECONDS) if sub and sub["period_started_at"] else None
    return _render(request, "account_plans.html", user, plans=billing.PLANS, current_plan=plan, subscription=sub,
                   recurring=bool(sub and billing._is_recurring(sub)), allowance=allowance, used=used, remaining=max(0.0, allowance - used),
                   ends=datetime.fromtimestamp(ends).strftime("%b %d, %Y") if ends else None,
                   resets=datetime.fromtimestamp(resets).strftime("%b %d") if resets else None,
                   is_owner=_owner(user), paystack_configured=billing.paystack_configured(), mpesa_configured=billing.mpesa_configured(),
                   notice=notice, error=error)


# ================================================================================================ WORKSPACE
@router.get("/client/workspace", response_class=HTMLResponse)
def workspace_page(request: Request):
    user = _client(request)
    if not user:
        return _to_login(request)
    owner_id = user["client_scope_id"]
    owner = db.get_user(owner_id) or user
    members = list(db.list_team_members(owner_id))
    orders = list(db.list_orders_for_client(owner_id))[:8]
    conn = db.get_conn()
    notes = conn.execute("SELECT id, title, updated_at, author_id FROM notes WHERE client_id = ? ORDER BY updated_at DESC LIMIT 5", (owner_id,)).fetchall()
    conn.close()
    sources = [
        {"name": "Zoom", "state": "live", "note": "Send Kauli to a meeting", "href": "/client/meetings"},
        {"name": "Microsoft Teams", "state": "live", "note": "Meetings and channel alerts", "href": "/client/meetings"},
        {"name": "Google Meet", "state": "live", "note": "Send Kauli to a meeting", "href": "/client/meetings"},
        {"name": "Google Drive", "state": "soon", "note": "Import from Drive", "href": "/client/integrations"},
        {"name": "OneDrive", "state": "soon", "note": "Import from OneDrive", "href": "/client/integrations"},
        {"name": "Dropbox", "state": "soon", "note": "Import from Dropbox", "href": "/client/integrations"},
    ]
    return _render(request, "account_workspace.html", user, owner=owner, members=members, orders=orders, notes=notes, is_owner=_owner(user),
                   sources=sources, when=lambda ts: datetime.fromtimestamp(_epoch(ts)).strftime("%b %d, %H:%M"))


# ================================================================================================ INTEGRATIONS
CATEGORIES = ["CRM", "Project Management", "Notes & Documentation", "Communication", "Meetings", "Developer"]
# kind "built" = connected from Kauli itself; "zapier" = connected through Zapier/Make with the API key and webhooks;
# state "soon" = not available yet (shown honestly, with a way to ask for it).
INTEGRATIONS = [
    {"id": "slack", "name": "Slack", "cat": "Communication", "kind": "built", "state": "live", "href": "/settings?tab=integrations#slack",
     "desc": "Get a message in your channel the moment a transcript is ready."},
    {"id": "teams", "name": "Microsoft Teams", "cat": "Communication", "kind": "built", "state": "live", "href": "/settings?tab=integrations#teams",
     "desc": "Post finished transcripts to a Teams channel."},
    {"id": "notion", "name": "Notion", "cat": "Notes & Documentation", "kind": "built", "state": "live", "href": "/settings?tab=integrations#notion",
     "desc": "Send transcripts and notes to a Notion database."},
    {"id": "zoom", "name": "Zoom", "cat": "Meetings", "kind": "built", "state": "live", "href": "/client/meetings",
     "desc": "Send Kauli to a Zoom meeting; it records and transcribes."},
    {"id": "meet", "name": "Google Meet", "cat": "Meetings", "kind": "built", "state": "live", "href": "/client/meetings",
     "desc": "Send Kauli to a Meet call and get the transcript afterwards."},
    {"id": "youtube", "name": "YouTube", "cat": "Meetings", "kind": "built", "state": "live", "href": "/settings?tab=integrations",
     "desc": "Transcribe a video from its link and send captions back to your channel."},
    {"id": "api", "name": "REST API", "cat": "Developer", "kind": "built", "state": "live", "href": "/settings?tab=integrations",
     "desc": "Create orders and fetch transcripts from your own software."},
    {"id": "webhooks", "name": "Webhooks", "cat": "Developer", "kind": "built", "state": "live", "href": "/settings?tab=integrations",
     "desc": "Get a signed call to your server when an order finishes, to post transcripts to your website."},
    {"id": "zapier", "name": "Zapier", "cat": "Developer", "kind": "zapier", "state": "live", "href": "/docs/api",
     "desc": "Connect Kauli to thousands of apps with Webhooks by Zapier and your API key."},
    {"id": "make", "name": "Make", "cat": "Developer", "kind": "zapier", "state": "live", "href": "/docs/api",
     "desc": "Build automations with Make using Kauli's webhooks and API."},
    {"id": "hubspot", "name": "HubSpot", "cat": "CRM", "kind": "zapier", "state": "live", "href": "/docs/api",
     "desc": "Log call and meeting transcripts to HubSpot contacts through Zapier."},
    {"id": "salesforce", "name": "Salesforce", "cat": "CRM", "kind": "zapier", "state": "live", "href": "/docs/api",
     "desc": "Attach transcripts to Salesforce records through Zapier."},
    {"id": "pipedrive", "name": "Pipedrive", "cat": "CRM", "kind": "zapier", "state": "live", "href": "/docs/api",
     "desc": "Add transcripts to Pipedrive deals through Zapier."},
    {"id": "trello", "name": "Trello", "cat": "Project Management", "kind": "zapier", "state": "live", "href": "/docs/api",
     "desc": "Create Trello cards from action items through Zapier."},
    {"id": "asana", "name": "Asana", "cat": "Project Management", "kind": "zapier", "state": "live", "href": "/docs/api",
     "desc": "Turn finished transcripts into Asana tasks through Zapier."},
    {"id": "monday", "name": "monday.com", "cat": "Project Management", "kind": "zapier", "state": "live", "href": "/docs/api",
     "desc": "Send transcripts to monday.com boards through Zapier."},
    {"id": "gdocs", "name": "Google Docs", "cat": "Notes & Documentation", "kind": "built", "state": "live", "href": "/client/integrations",
     "desc": "Save transcripts directly to Google Docs for easy editing, sharing, and collaboration."},
    {"id": "gsheets", "name": "Google Sheets", "cat": "Notes & Documentation", "kind": "zapier", "state": "live", "href": "/docs/api",
     "desc": "Add a row per transcript to a Google Sheet through Zapier."},
    {"id": "gdrive", "name": "Google Drive", "cat": "Notes & Documentation", "kind": "built", "state": "soon", "href": "",
     "desc": "Pick a recording from Google Drive when you upload a file."},
    {"id": "onedrive", "name": "OneDrive", "cat": "Notes & Documentation", "kind": "built", "state": "soon", "href": "",
     "desc": "Pick a recording from OneDrive when you upload a file."},
    {"id": "dropbox", "name": "Dropbox", "cat": "Notes & Documentation", "kind": "built", "state": "soon", "href": "",
     "desc": "Pick a recording from Dropbox when you upload a file."},
]
CALENDARS = [{"id": "google_calendar", "name": "Google Calendar"}, {"id": "outlook_calendar", "name": "Outlook Calendar"}]


def calendar_interest(client_id: str) -> set[str]:
    conn = db.get_conn()
    rows = conn.execute("SELECT provider FROM calendar_interest WHERE client_id = ?", (client_id,)).fetchall()
    conn.close()
    return {r["provider"] for r in rows}


@router.get("/client/integrations", response_class=HTMLResponse)
def integrations_page(request: Request, notice: str | None = None):
    user = _client(request)
    if not user:
        return _to_login(request)
    on = cloud_import.config()
    cloud = {"gdrive": "google", "onedrive": "onedrive", "dropbox": "dropbox"}
    items = [({**i, "state": "live", "href": "/client#order-form", "import": True} if cloud.get(i["id"]) in on else i) for i in INTEGRATIONS]
    # one-click connections: Google Docs always shows (as "Coming soon" until its keys are set); Slack switches from the old
    # webhook set-up to Connect only once its app is set up
    mine = connections.connections_for(user["client_scope_id"])
    for i in items:
        if i["id"] in connections.PROVIDERS and (i["id"] == "gdocs" or connections.configured(i["id"])):
            i["connect"], i["connectable"] = i["id"], connections.configured(i["id"])
            i["connected"] = i["id"] in mine
            i["account"] = mine.get(i["id"], {}).get("label", "")
    return _render(request, "account_integrations.html", user, items=items, categories=CATEGORIES, calendars=CALENDARS,
                   interest=calendar_interest(user["client_scope_id"]), notice=notice)


@router.post("/client/integrations/request")
def integrations_request(request: Request, body: str = Form(...)):
    user = _client(request)
    if not user:
        return _to_login(request)
    text = body.strip()[:1000]
    allowed, _ = rate_limit.check(f"intreq:{user['id']}", limit=10, window_s=3600)
    if text and allowed:
        conn = db.get_conn()
        conn.execute("INSERT INTO integration_requests (id, client_id, user_id, body, created_at) VALUES (?, ?, ?, ?, ?)",
                     (uuid.uuid4().hex[:12], user["client_scope_id"], user["id"], text, time.time()))
        conn.commit()
        conn.close()
        track_feature(request, "integration_request")
        db.notify_all_staff("integration_request", "Integration request: " + text[:60], "/staff/support")
    return RedirectResponse("/client/integrations?notice=" + quote("Thanks. We read every request." if text else "Write a few words first."), status_code=303)


@router.post("/client/calendar/interest")
def calendar_notify(request: Request, provider: str = Form(...)):
    user = _client(request)
    if not user:
        return _to_login(request)
    if provider in {c["id"] for c in CALENDARS}:
        conn = db.get_conn()
        conn.execute("INSERT OR IGNORE INTO calendar_interest (client_id, provider, created_at) VALUES (?, ?, ?)",
                     (user["client_scope_id"], provider, time.time()))
        conn.commit()
        conn.close()
    track_feature(request, "calendar_interest", provider=provider)
    return RedirectResponse("/client/integrations?notice=" + quote("Noted. We will email you the day calendar connections open."), status_code=303)


# ================================================================================================ ASSISTANT
def _kb() -> list[dict]:
    p = billing.PLANS
    return [
        {"k": "what can kauli do features offer services product about", "topic": "product",
         "a": "Kauli turns audio and video into accurate transcripts, translations, subtitles and dubbed audio or video. You can edit and share transcripts, make AI notes, record live meetings, and connect Kauli to your other tools."},
        {"k": "upload start new order transcribe file audio video how", "topic": "product",
         "a": "To start, choose New order on your dashboard, drop in an audio or video file (or paste a YouTube link), pick what you want (transcript, translation, subtitles or dubbing) and the language. Short Instant AI jobs finish in minutes."},
        {"k": "youtube link url video fetch bot sign in", "topic": "support",
         "a": "You can paste a YouTube link instead of uploading. If YouTube refuses the download, upload the video file itself (a screen recording or the original works) and it will be transcribed the same way. We are working on making links more reliable."},
        {"k": "language languages support which supported translate translation", "topic": "product",
         "a": "Kauli transcribes and translates a wide range of languages, including English, Swahili, Spanish, French, German, Portuguese, Arabic, Hindi, Chinese (Mandarin and Cantonese), Japanese and Korean. The language list appears when you start an order."},
        {"k": "accuracy accurate how good quality percent human review", "topic": "product",
         "a": "Instant AI is fast and very good on clear audio. For critical work choose Human review: a person checks and corrects the transcript, and we can reach up to 99% accuracy."},
        {"k": "time long turnaround fast how long take finish minutes ready", "topic": "support",
         "a": "Instant AI usually finishes in a few minutes for short files and scales with length. Human-reviewed orders take longer; the estimated time is shown before you pay. Open the order to see live progress."},
        {"k": "price pricing cost plans plan pay how much subscription upgrade", "topic": "billing",
         "a": f"Pay as you go starts with {billing.FREE_MINUTES_PER_MONTH:.0f} free Instant AI minutes every month. Pro is ${p['pro']['price_usd']}/month with {p['pro']['included_ai_minutes']:,} Instant AI minutes; Premium is ${p['premium']['price_usd']}/month with {p['premium']['included_ai_minutes']:,}{billing.hours_suffix(p['premium']['included_ai_minutes'])}. Both also include human-reviewed transcription minutes every month ({p['pro']['included_human_minutes']:.0f} on Pro, {p['premium']['included_human_minutes']:.0f} on Premium) at no per-minute charge; beyond them the normal human-review price applies. See Plans in the menu to compare or switch."},
        {"k": "free minutes trial free free-tier signup", "topic": "billing",
         "a": f"Every account gets {billing.FREE_MINUTES_PER_MONTH:.0f} free Instant AI minutes a month, covering transcription and translation. Unused free minutes do not carry over, but minutes you earn from referrals do."},
        {"k": "pay payment card mpesa m-pesa paystack bank transfer methods secure", "topic": "billing",
         "a": "Payments are processed securely by Paystack: cards, M-Pesa and Airtel Money where available. Bank transfer is also possible and is confirmed by our team."},
        {"k": "download export pdf word docx txt srt csv format transcript", "topic": "support",
         "a": "Open the order and use Download above the transcript: Word, PDF, text, SRT subtitles or CSV, with or without timestamps and speaker names, or copy it to the clipboard."},
        {"k": "edit correct fix change transcript mistakes find replace", "topic": "support",
         "a": "On an Instant AI transcript, choose Edit transcript, click a line and type. Use Find & replace for names that repeat, then Save. Downloads and captions update with your changes."},
        {"k": "share link email whatsapp send transcript someone", "topic": "support",
         "a": "Use Share above the transcript to make a read-only link you can copy, email or send on WhatsApp. Stop sharing at any time and the link stops working."},
        {"k": "notes summary meeting notes ai notes action items", "topic": "product",
         "a": "From any finished order choose Create AI notes. You get an editable note with an overview, key points and action items that you can download as Word or text. All notes live under Notes in the menu."},
        {"k": "vocabulary names terms spelling brand custom words", "topic": "product",
         "a": "Add names, brands and technical terms under Vocabulary. Add common wrong spellings after a vertical bar (for example: Kauli | cowley, cowlee) and new transcripts will use your spelling."},
        {"k": "meeting bot zoom meet teams record live join", "topic": "product",
         "a": "Under Meetings, paste a Zoom, Google Meet or Teams link and Kauli joins, records and transcribes it, then you can make notes from the result."},
        {"k": "api webhook webhooks zapier make integration developer website podcast automatically post", "topic": "product",
         "a": "Kauli has a REST API and signed webhooks, so finished transcripts can be posted to your website or other tools automatically. Zapier and Make work through them too. See Integrations in the menu and the API docs."},
        {"k": "team invite members workspace collaborate share account colleagues", "topic": "product",
         "a": "Invite teammates from Workspace. They sign up with the invited email and see the same orders, notes and billing."},
        {"k": "referral reward invite friend free minutes earn", "topic": "billing",
         "a": f"Share your link from Rewards. You earn {SIGNUP_REWARD_MINUTES} minutes when a friend signs up and {UPGRADE_REWARD_MINUTES:,} when they upgrade to a paid plan. Earned minutes roll over until you use them."},
        {"k": "cancel downgrade stop subscription renew refund", "topic": "billing",
         "a": "Go to Plans. Use Cancel auto-renewal and you keep your plan until the period you paid for ends, then move to Pay as you go. For refund questions, choose Talk to the team."},
        {"k": "close delete account data privacy remove", "topic": "billing",
         "a": "You can close your account at the bottom of the Plans page. For deleting your stored files and records as well, use Settings, then Security, and request deletion."},
        {"k": "dubbing dub voice captions burned subtitles video", "topic": "product",
         "a": "Dubbing and burned-in captions are delivered as video files on Premium or as an add-on. Choose the service when you start the order."},
        {"k": "call center calls contact center agent qa", "topic": "product",
         "a": "Kauli transcribes recorded calls with speaker labels, summaries and searchable text for quality and compliance review. See the Call centers page on our website, or ask the team for a pilot."},
        {"k": "human team contact support email talk person agent help", "topic": "support",
         "a": "I can pass your question to the Kauli team. Choose Talk to the team and they will reply here and by email."},
    ]


_WORD = re.compile(r"[a-z0-9']+")


def kb_answer(question: str) -> dict | None:
    q = set(_WORD.findall(question.lower())) - _STOP
    best, score = None, 0
    for e in _kb():
        keys = set(_WORD.findall(e["k"]))
        s = len(q & keys)
        if s > score:
            best, score = e, s
    return best if best and score >= 1 else None


ASSISTANT_PROMPT = """You are Kauli's help assistant inside the customer's account. Kauli turns audio and video into transcripts, \
translations, subtitles and dubbing. Answer the customer's question using ONLY the help material below. Be brief, friendly and \
practical (2-5 sentences), and point to the exact place in the app. If the material does not answer it, say you are not sure \
and offer to pass it to the team, ending with the exact token [ESCALATE]. Never invent prices, features or policies.

HELP MATERIAL:
{kb}"""


def assistant_reply_ex(question: str, history: list[dict], context: str = "", extra_docs: list[str] | None = None) -> dict:
    """{"text", "handoff", "escalate"}. Claude answers from the help material and the customer's own account facts when a key is set;
    otherwise the best match. Questions about how Kauli runs are refused before any model is asked, and company and model names are
    scrubbed from whatever comes back. "escalate" means the assistant itself decided a person has to take over."""
    if assistant_core.ops_probe(question):
        return {"text": assistant_core.REFUSAL, "handoff": False, "escalate": False}
    wants_human = bool(re.search(r"\b(human|person|agent|someone|staff|team|representative|talk to)\b", question.lower()))
    if order_ai_assist.ai_assist_configured():
        try:
            kb = "\n".join(f"- {e['a']}" for e in _kb())
            msgs = [{"role": h["role"], "content": h["content"][:1500]} for h in history[-6:] if h.get("role") in ("user", "assistant")]
            msgs.append({"role": "user", "content": question[:1500]})
            resp = order_ai_assist._client().messages.create(model="claude-haiku-4-5-20251001", max_tokens=450,
                                                            system=assistant_core.system_prompt(kb, context, extra_docs or []), messages=msgs)
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            if text:
                esc = "[ESCALATE]" in text
                return {"text": assistant_core.scrub(text.replace("[ESCALATE]", "").strip()), "handoff": esc or wants_human, "escalate": esc}
        except Exception:  # noqa: BLE001 - fall through to the built-in answers
            pass
    hit = kb_answer(question)
    if hit and not (wants_human and len(set(_WORD.findall(question.lower())) - _STOP) <= 3):
        return {"text": hit["a"], "handoff": wants_human, "escalate": False}
    return {"text": "I am not sure about that one. I can pass it to the Kauli team and they will reply here and by email.", "handoff": True, "escalate": False}


def assistant_reply(question: str, history: list[dict]) -> tuple[str, bool]:
    """(answer, offer_handoff) - the plain form of assistant_reply_ex."""
    r = assistant_reply_ex(question, history)
    return r["text"], r["handoff"]


def _thread_messages(thread_id: str) -> list[dict]:
    conn = db.get_conn()
    rows = conn.execute("SELECT sender, body, created_at FROM support_messages WHERE thread_id = ? ORDER BY created_at", (thread_id,)).fetchall()
    conn.close()
    return [{"sender": r["sender"], "body": r["body"], "at": r["created_at"]} for r in rows]


def _own_thread(user, thread_id: str | None):
    if not thread_id:
        return None
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM support_threads WHERE id = ? AND user_id = ?", (thread_id, user["id"])).fetchone()
    conn.close()
    return row


def _add_message(thread_id: str, sender: str, body: str) -> None:
    now = time.time()
    conn = db.get_conn()
    conn.execute("INSERT INTO support_messages (id, thread_id, sender, body, created_at) VALUES (?, ?, ?, ?, ?)", (uuid.uuid4().hex, thread_id, sender, body, now))
    conn.execute("UPDATE support_threads SET updated_at = ? WHERE id = ?", (now, thread_id))
    conn.commit()
    conn.close()


def _own_order_id(user, order_id) -> str | None:
    """An order id only when it is really this customer's order."""
    if not order_id:
        return None
    order = db.get_order(str(order_id))
    return order["id"] if order and order["client_id"] == user["client_scope_id"] else None


@router.get("/client/assistant/threads")
def assistant_threads(request: Request):
    user = _client(request)
    if not user:
        return JSONResponse({"error": "Sign in to continue."}, status_code=401)
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM support_threads WHERE user_id = ? ORDER BY updated_at DESC LIMIT 10", (user["id"],)).fetchall()
    conn.close()
    return JSONResponse({"threads": [{"id": r["id"], "subject": r["subject"], "escalated": bool(r["escalated"]), "status": r["status"],
                                      "order_id": r["order_id"], "messages": _thread_messages(r["id"])} for r in rows]})


@router.get("/client/assistant/feed")
def assistant_feed(request: Request):
    """Everything addressed to this customer in one list with one unread count: see assistant_core.feed."""
    user = _client(request)
    if not user:
        return JSONResponse({"error": "Sign in to continue."}, status_code=401)
    return JSONResponse(assistant_core.feed(user), headers={"Cache-Control": "no-store"})


@router.post("/client/assistant/feed/read")
async def assistant_feed_read(request: Request):
    user = _client(request)
    if not user:
        return JSONResponse({"error": "Sign in to continue."}, status_code=401)
    db.mark_all_notifications_read(user["id"])
    conn = db.get_conn()
    conn.execute("UPDATE support_threads SET client_seen_at = ? WHERE user_id = ?", (time.time(), user["id"]))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


@router.post("/client/assistant/seen")
async def assistant_seen(request: Request):
    user = _client(request)
    if not user:
        return JSONResponse({"error": "Sign in to continue."}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Bad request."}, status_code=400)
    thread = _own_thread(user, body.get("thread_id"))
    if thread is not None:
        conn = db.get_conn()
        conn.execute("UPDATE support_threads SET client_seen_at = ? WHERE id = ?", (time.time(), thread["id"]))
        conn.commit()
        conn.close()
    return JSONResponse({"ok": True})


def _escalate(request: Request, user, thread, announce: bool = True) -> None:
    """Hand a conversation to the team: flag it, tell staff once, and (for a conversation about an order) put the customer's words on that
    order's message thread, where the team already works. The customer is told the team has it, and that they will reply here and by email."""
    track_feature(request, "assistant_escalated")
    conn = db.get_conn()
    conn.execute("UPDATE support_threads SET escalated = 1, status = 'open', updated_at = ? WHERE id = ?", (time.time(), thread["id"]))
    last_user = conn.execute("SELECT body FROM support_messages WHERE thread_id = ? AND sender = 'user' ORDER BY created_at DESC LIMIT 1", (thread["id"],)).fetchone()
    conn.commit()
    conn.close()
    if announce:
        _add_message(thread["id"], "bot", "I have passed this to the Kauli team. They will reply here and by email.")
    who = user["display_name"] or user["email"]
    link = f"/staff/support/{thread['id']}"
    if thread["order_id"]:
        order = db.get_order(thread["order_id"])
        if order and last_user and order["review_tier"] != "ai":          # an Instant AI order has no reviewer to read it
            db.create_message(order["id"], user["id"], "client", last_user["body"])
            link = f"/staff/orders/{order['id']}"
    db.notify_all_staff("support", f"Help request from {who}: {thread['subject'][:60]}", link)
    try:
        if mailer.email_configured():
            contact = getattr(A, "CONTACT_EMAIL", None) or "info@kauli-forgemedia.com"
            inner = f"<p><strong>{html.escape(user['email'])}</strong> asked for a person in the Kauli assistant:</p><p>{html.escape(thread['subject'])}</p>"
            mailer.send_email(contact, "Kauli help request", mailer.wrap_email_html(inner, cta_text="Open the thread", cta_url=f"{A._site_base(request)}{link}"), tags=["support"])
    except Exception:  # noqa: BLE001 - the in-app notification already went out
        pass


@router.post("/client/assistant/message")
async def assistant_message(request: Request):
    user = _client(request)
    if not user:
        return JSONResponse({"error": "Sign in to continue."}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Bad request."}, status_code=400)
    text = str(body.get("text") or "").strip()[:1500]
    if not text:
        return JSONResponse({"error": "Type a question first."}, status_code=400)
    allowed, retry_after = rate_limit.check(f"assistant:{user['id']}", limit=30, window_s=600)
    if not allowed:
        return JSONResponse({"error": f"Slow down a little. Try again in {retry_after}s."}, status_code=429)
    order_id = _own_order_id(user, body.get("order_id"))
    thread = _own_thread(user, body.get("thread_id"))
    if thread is None:
        tid, now = uuid.uuid4().hex[:12], time.time()
        conn = db.get_conn()
        conn.execute("INSERT INTO support_threads (id, client_id, user_id, subject, created_at, updated_at, order_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (tid, user["client_scope_id"], user["id"], text[:80], now, now, order_id))
        conn.commit()
        conn.close()
    else:
        tid = thread["id"]
        order_id = order_id or thread["order_id"]
    history = [{"role": "user" if m["sender"] == "user" else "assistant", "content": m["body"]} for m in _thread_messages(tid) if m["sender"] != "staff"]
    _add_message(tid, "user", text)
    if thread is not None and thread["escalated"]:            # the team has it: do not talk over them
        if order_id:                                          # ... but what the customer adds goes to the order too
            db.create_message(order_id, user["id"], "client", text)
        return JSONResponse({"thread_id": tid, "reply": "Thanks, I have added that for the team. They will reply here.", "handoff": False, "escalated": True})
    order_row = db.get_order(order_id) if order_id else None
    if order_row and order_row["review_tier"] == "ai" and assistant_core.revision_request(text):
        # Instant AI is not reviewed by anyone, so there is no one to ask for a revision: say how to fix it, and do not bother the team
        _add_message(tid, "bot", assistant_core.INSTANT_AI_REVISION_REPLY)
        return JSONResponse({"thread_id": tid, "reply": assistant_core.INSTANT_AI_REVISION_REPLY, "handoff": False, "escalated": False})
    topic = str(body.get("topic") or "")
    r = assistant_reply_ex((topic + ": " if topic and not history else "") + text, history,
                           context=assistant_core.account_context(user, order_id), extra_docs=assistant_core.docs_entries(A, text))
    human = assistant_core.needs_human(text)
    if human or r["escalate"]:                                # only a person can do this one: hand it over now, and say so
        reply = ("That one needs a person, so I have passed it to the Kauli team with your details. They will reply here and by email."
                 if human else r["text"])
        _add_message(tid, "bot", reply)
        _escalate(request, user, _own_thread(user, tid), announce=False)
        if thread is None:
            track_feature(request, "assistant_question")
        return JSONResponse({"thread_id": tid, "reply": reply, "handoff": False, "escalated": True})
    _add_message(tid, "bot", r["text"])
    if thread is None:
        track_feature(request, "assistant_question")
    return JSONResponse({"thread_id": tid, "reply": r["text"], "handoff": r["handoff"], "escalated": False})


@router.post("/client/assistant/escalate")
async def assistant_escalate(request: Request):
    user = _client(request)
    if not user:
        return JSONResponse({"error": "Sign in to continue."}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Bad request."}, status_code=400)
    thread = _own_thread(user, body.get("thread_id"))
    if thread is None:
        return JSONResponse({"error": "Ask a question first so the team has something to read."}, status_code=400)
    note = str(body.get("note") or "").strip()[:1000]
    if note:
        _add_message(thread["id"], "user", note)
    if not thread["escalated"]:
        _escalate(request, user, thread)
    return JSONResponse({"ok": True, "thread_id": thread["id"]})


# ---- staff side
def _staff(request: Request):
    user = A.current_user(request)
    return user if user and user["role"] == "staff" else None


@router.get("/staff/support", response_class=HTMLResponse)
def staff_support_list(request: Request):
    user = _staff(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    conn = db.get_conn()
    threads = conn.execute(
        "SELECT t.*, u.email AS email, u.display_name AS name, (SELECT body FROM support_messages m WHERE m.thread_id = t.id ORDER BY created_at DESC LIMIT 1) AS last_body "
        "FROM support_threads t LEFT JOIN users u ON u.id = t.user_id WHERE t.escalated = 1 ORDER BY (t.status = 'open') DESC, t.updated_at DESC LIMIT 100").fetchall()
    requests = conn.execute("SELECT r.*, u.email AS email FROM integration_requests r LEFT JOIN users u ON u.id = r.user_id ORDER BY r.created_at DESC LIMIT 30").fetchall()
    interest = conn.execute("SELECT provider, COUNT(*) AS n FROM calendar_interest GROUP BY provider").fetchall()
    conn.close()
    return A.templates.TemplateResponse(request, "staff_support.html", {"user": user, "threads": threads, "requests": requests, "interest": interest,
                                                                         "theme": "dark", "thread": None, "messages": [],
                                                                         "when": lambda ts: datetime.fromtimestamp(ts).strftime("%b %d, %H:%M")})


@router.get("/staff/support/{thread_id}", response_class=HTMLResponse)
def staff_support_thread(request: Request, thread_id: str):
    user = _staff(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    conn = db.get_conn()
    thread = conn.execute("SELECT t.*, u.email AS email, u.display_name AS name FROM support_threads t LEFT JOIN users u ON u.id = t.user_id WHERE t.id = ?", (thread_id,)).fetchone()
    conn.close()
    if not thread:
        return HTMLResponse("Not found.", status_code=404)
    return A.templates.TemplateResponse(request, "staff_support.html", {"user": user, "threads": [], "requests": [], "interest": [], "theme": "dark", "thread": thread,
                                                                         "messages": _thread_messages(thread_id),
                                                                         "when": lambda ts: datetime.fromtimestamp(ts).strftime("%b %d, %H:%M")})


@router.post("/staff/support/{thread_id}/reply")
def staff_support_reply(request: Request, thread_id: str, body: str = Form(...), close: str = Form("")):
    user = _staff(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    conn = db.get_conn()
    thread = conn.execute("SELECT * FROM support_threads WHERE id = ?", (thread_id,)).fetchone()
    conn.close()
    if not thread:
        return HTMLResponse("Not found.", status_code=404)
    text = body.strip()[:4000]
    if text:
        _add_message(thread_id, "staff", text)
        db.create_notification(thread["user_id"], "support_reply", "The Kauli team replied to your question", "/client/home")
        target = db.get_user(thread["user_id"])
        try:
            if target and mailer.email_configured():
                inner = f"<p>{html.escape(text)}</p><p>You can reply from the help assistant in your Kauli account.</p>"
                mailer.send_email(target["email"], "Reply from the Kauli team", mailer.wrap_email_html(inner, cta_text="Open Kauli", cta_url=f"{A._site_base(request)}/client/home"), tags=["support"])
        except Exception:  # noqa: BLE001
            pass
    if close:
        conn = db.get_conn()
        conn.execute("UPDATE support_threads SET status = 'closed' WHERE id = ?", (thread_id,))
        conn.commit()
        conn.close()
    return RedirectResponse(f"/staff/support/{thread_id}", status_code=303)


# ================================================================================================ SITE ANALYTICS (staff)
def track_feature(request: Request, name: str, **params) -> None:
    """A server-side GA4 event for something a customer did in the app (never raises, and sends nothing without their analytics cookie).
    Only counts and kinds go in the parameters: no names, emails, file names or transcript text."""
    try:
        ga_events.send_event(ga_events.client_id_from_request(request), name, params)
    except Exception:  # noqa: BLE001 - measuring must never break the feature being measured
        pass


def _q(conn, sql: str, args: tuple = ()):
    try:
        return conn.execute(sql, args).fetchall()
    except Exception:  # noqa: BLE001 - a missing table on an old database must not take the page down
        return []


def internal_site_stats(days: int) -> dict:
    """What Kauli itself knows, with no Google involved: the real orders, payments, upload steps and blog readers in the window."""
    since = time.time() - days * 86400
    conn = db.get_conn()
    try:
        orders = _q(conn, "SELECT date(created_at, 'unixepoch') AS d, COUNT(*) AS n FROM orders WHERE created_at >= ? GROUP BY d ORDER BY d", (since,))
        first_orders = _q(conn, "SELECT COUNT(*) AS n FROM (SELECT client_id, MIN(created_at) AS m FROM orders GROUP BY client_id) WHERE m >= ?", (since,))
        paid = _q(conn, "SELECT COUNT(*) AS n, COALESCE(SUM(amount_usd), 0) AS usd FROM payments WHERE status = 'completed' AND completed_at >= ?", (since,))
        wizard = _q(conn, "SELECT step, COUNT(DISTINCT client_id) AS n FROM wizard_step_events WHERE created_at >= ? GROUP BY step", (since,))
        leads = _q(conn, "SELECT COUNT(*) AS n FROM leads WHERE created_at >= ?", (since,))
        views = _q(conn, "SELECT COUNT(*) AS n, COUNT(DISTINCT visitor_id) AS u FROM blog_views WHERE viewed_at >= ?", (since,))
        posts = _q(conn, "SELECT p.title AS title, p.slug AS slug, COUNT(*) AS n, COUNT(DISTINCT v.visitor_id) AS u FROM blog_views v "
                         "JOIN blog_posts p ON p.id = v.post_id WHERE v.viewed_at >= ? GROUP BY p.id ORDER BY n DESC LIMIT 8", (since,))
        countries = _q(conn, "SELECT COALESCE(country, '?') AS c, COUNT(*) AS n FROM blog_views WHERE viewed_at >= ? GROUP BY c ORDER BY n DESC LIMIT 8", (since,))
    finally:
        conn.close()
    order = ["1", "2", "3", "submitted"]
    steps = {r["step"]: r["n"] for r in wizard}
    first = max(steps.values(), default=0) or 1
    return {"orders": sum(r["n"] for r in orders), "orders_by_day": [(r["d"], r["n"]) for r in orders],
            "new_clients_ordering": first_orders[0]["n"] if first_orders else 0, "paid": paid[0]["n"] if paid else 0,
            "revenue": paid[0]["usd"] if paid else 0.0, "leads": leads[0]["n"] if leads else 0,
            "blog_views": views[0]["n"] if views else 0, "blog_readers": views[0]["u"] if views else 0,
            "posts": [dict(r) for r in posts], "countries": [dict(r) for r in countries],
            "wizard": [{"label": {"1": "Chose the file", "2": "Chose the service", "3": "Reached review", "submitted": "Submitted"}[k],
                        "count": steps.get(k, 0), "share": 100.0 * steps.get(k, 0) / first} for k in order if k in steps]}


def _fmt_day(raw: str) -> str:
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    try:
        return f"{months[int(raw[4:6]) - 1]} {int(raw[6:8])}"
    except (ValueError, IndexError):
        return raw


@router.get("/staff/analytics", response_class=HTMLResponse)
def site_analytics(request: Request, days: int = 28, refresh: int = 0):
    user = _staff(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    days = days if days in (7, 28, 90) else 28
    ga, error, tiles, line = None, None, [], ""
    if ga_reports.configured():
        try:
            ga = ga_reports.overview(days, force=bool(refresh))
        except ga_reports.GAError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001
            error = f"Could not read Google Analytics just now ({exc.__class__.__name__}). Try again in a minute."
    if ga:
        t, p = ga["totals"]["this"], ga["totals"]["previous"]

        def tile(label, key, fmt):
            now, before = t.get(key, 0.0), p.get(key, 0.0)
            ch = ga_reports.pct_change(now, before)
            return {"label": label, "value": fmt(now), "change": None if ch is None else round(ch)}
        mmss = lambda s: f"{int(s // 60)}:{int(s % 60):02d}"      # noqa: E731
        tiles = [tile("Visitors", "activeUsers", lambda v: f"{v:,.0f}"), tile("New visitors", "newUsers", lambda v: f"{v:,.0f}"),
                 tile("Visits", "sessions", lambda v: f"{v:,.0f}"), tile("Engagement rate", "engagementRate", lambda v: f"{v * 100:.0f}%"),
                 tile("Average visit", "averageSessionDuration", mmss), tile("Key events", "keyEvents", lambda v: f"{v:,.0f}")]
        line = _line_chart([(_fmt_day(r["date"]), r["activeUsers"]) for r in ga["daily"]])
    internal = internal_site_stats(days)
    return A.templates.TemplateResponse(request, "staff_site_analytics.html", {
        "dash": analytics_engine.dashboard(days), "an": analytics_engine, "user": user, "theme": "light", "days": days, "configured": ga_reports.configured(), "error": error, "ga": ga, "tiles": tiles, "line": line,
        "insights": ga_reports.insights(ga) if ga else [], "funnel": ga_reports.funnel(ga) if ga else [], "internal": internal,
        "internal_line": _line_chart([(d[5:], n) for d, n in internal["orders_by_day"]]) if len(internal["orders_by_day"]) > 1 else "",
        "property": ga_reports.property_id(), "measurement_id": ga_events.measurement_id(),
        "fetched": datetime.fromtimestamp(ga["fetched_at"]).strftime("%b %d, %H:%M") if ga else None})


@router.post("/staff/analytics/ad-spend")
def set_ad_spend(request: Request, spend: str = Form("")):
    """What is spent on ads in a month, so the dashboard can work out the cost of winning a customer from the paid channels."""
    user = _staff(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    try:
        value = float(spend.strip().lstrip("$").replace(",", "") or 0)
    except ValueError:
        value = -1.0
    if not (0 <= value <= 10_000_000):
        return RedirectResponse("/staff/analytics", status_code=303)
    db.set_flag("ad_spend_usd_month", str(value), by=user["id"])
    return RedirectResponse("/staff/analytics", status_code=303)


# ================================================================================================ RESOURCES
# Videos and guides for customers, inside the account only. Add a video here once it is on YouTube: {"id": "<the YouTube id>", ...}.
VIDEOS = [
    {"id": "i6R3flz8V1I", "title": "Kauli in 78 seconds", "about": "What Kauli does: transcripts, translation, dubbing and human review.", "length": "1:18", "thumb": "/static/promo-thumbnail.jpg"},
]


@router.get("/client/resources", response_class=HTMLResponse)
def resources_page(request: Request):
    user = _client(request)
    if not user:
        return _to_login(request)
    posts = []
    for post in list(db.list_blog_posts(published_only=True))[:8]:
        row = blog_audio.ready_for(post["id"])
        posts.append({"post": post, "audio": row})
    return _render(request, "account_resources.html", user, videos=[v for v in VIDEOS if v.get("id")], posts=posts,
                   podcast_ready=any(p["audio"] and p["audio"]["in_podcast"] for p in posts), base=A._site_base(request))
