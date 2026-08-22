"""AI-assisted blog drafting for staff - the honest, human-reviewed version
of the "Research Specialist / Content Writer / SEO Optimizer / Social
Media Manager" pipeline a marketing doc asked for.

What this deliberately is NOT: an autonomous agent that researches
"trending topics" and auto-publishes across platforms. That's exactly the
kind of thing that produces fabricated or generic SEO spam, and this app's
whole design principle - AI drafts, a human verifies before anything ships
- applies here just as much as it does to a client's transcript. This
generates a DRAFT from a topic and notes a real staff member supplies;
nothing here ever calls create_blog_post or touches the database, let
alone publishes anywhere. The staff member reviews, edits, and explicitly
clicks Publish themselves, same as any other post.

Also generates a short tweet-length and LinkedIn-length teaser as plain
text for the same reason the share buttons on a published post are plain
links, not an auto-poster: a human decides what actually goes out under
Kauli's name.
"""
from __future__ import annotations

import json
import os
import re

SYSTEM_PROMPT = """You help draft blog posts for Kauli, a Swahili/Kikuyu/English \
dubbing and translation service run by Forge Media Services (a small, real, \
one-person-founded company in Kenya - not a large enterprise, don't write as if it were one).

Ground rules, followed strictly:
- Never invent statistics, client names, client counts, testimonials, or specific claims \
about Kauli's scale or customer base. If the topic needs a concrete example, use a plausible \
generic scenario ("a media team," "an NGO field report") rather than a fabricated named client.
- Never claim certifications, guarantees, or absolute claims (e.g. "0% errors", "SOC 2 certified") \
that would need real evidence to back up.
- Write in plain, confident, specific language - explain the real mechanics of a topic \
(like the "why", not just the "what"), the way a knowledgeable practitioner would, not \
generic marketing fluff.
- Kauli's real, true features you CAN reference: AI-drafted transcription/translation with \
mandatory human review before delivery, per-minute transparent pricing, consent-gated voice \
cloning, Swahili/Kikuyu/English support (Kikuyu uses human transcription since no ASR model \
supports it yet), free trial minutes (transcription-only, preview-only).

Return ONLY a JSON object, no markdown fences, no preamble:
{"title": "...", "description": "... (a real meta description, under 160 chars)", \
"body_html": "... (well-structured HTML using <p>, <h2>, <h3>, <ul>/<li> - NOT markdown, real HTML tags)", \
"tweet_teaser": "... (under 280 characters, a genuine hook, not clickbait)", \
"linkedin_teaser": "... (2-4 sentences, professional tone, a real insight not just a link)"}
"""


def ai_assist_configured() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def draft_blog_post(topic: str, notes: str) -> dict:
    """Returns {"ok": True, "title", "description", "body_html",
    "tweet_teaser", "linkedin_teaser"} or {"ok": False, "error": "..."}.
    Never writes anything anywhere - purely returns text for a human to
    review in the editor form before ever saving or publishing."""
    if not ai_assist_configured():
        return {"ok": False, "error": "ANTHROPIC_API_KEY isn't set - nothing to draft with."}
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        user_msg = f"Topic: {topic}\n\nNotes/angle from the team: {notes or '(none given - use your judgment)'}"
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = "".join(b.text for b in resp.content if b.type == "text").strip()
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
        data = json.loads(raw)
        return {
            "ok": True,
            "title": data.get("title", ""),
            "description": data.get("description", ""),
            "body_html": data.get("body_html", ""),
            "tweet_teaser": data.get("tweet_teaser", ""),
            "linkedin_teaser": data.get("linkedin_teaser", ""),
        }
    except json.JSONDecodeError:
        return {"ok": False, "error": "The AI's response wasn't valid JSON - try again, or write it by hand."}
    except Exception as exc:  # noqa: BLE001 - surface any real API failure to the staff member
        return {"ok": False, "error": f"Draft request failed: {exc}"}
