"""AI summary and Q&A for a client's own delivered order - a real Claude
call grounded in that order's own final, human-reviewed transcript text,
not a feature "pulled from Transkriptor": Transkriptor's transcription API
(see kauli/providers/asr.py's TranskriptorASR - the real, documented
contract, confirmed live against the actual API) returns only
{text, StartTime, EndTime, Speaker} per segment - no summary field
anywhere in that response. The summary/chat experience some transcription
tools show is a feature of their own separate consumer app, not something
their transcription API exposes to an integrator. This module builds the
same real capability ourselves instead of quietly pretending it came from
a vendor that doesn't actually provide it.

Same honesty rules as blog_ai_assist.py: never invents facts, always
grounded in the real transcript text passed in, refuses to answer past
what that text actually says rather than guessing.
"""
import html as _html
import os
import re


def ai_assist_configured() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


SUMMARY_SYSTEM_PROMPT = """You summarize a real, human-reviewed transcript for the client who \
ordered it. You are given the ACTUAL final transcript text below - never invent content, names, \
numbers or claims that aren't in it. If the transcript is very short or thin, say so plainly \
rather than padding a summary out. Write 3-5 short sentences covering: what the recording is \
about, who's speaking if that's clear from the text, and the main points actually covered. Plain \
prose, no markdown, no preamble like "Here is a summary" - just the summary itself."""

ASK_SYSTEM_PROMPT = """You answer a client's question about their own real, human-reviewed \
transcript, given below. Answer ONLY from what the transcript actually says. If the transcript \
doesn't contain the answer, say so plainly ("The transcript doesn't cover that") rather than \
guessing or inventing an answer. Keep answers concise - a few sentences, not an essay. No \
markdown, plain prose."""


TEMPLATED_SYSTEM_PROMPT = """You write a structured summary of a real, human-reviewed transcript for the client who ordered it. \
You are given the ACTUAL transcript text. Never invent names, numbers, dates, decisions or quotes that are not in it, and copy any \
quote exactly as it was said. Write in the language the transcript is written in.

Use exactly this layout, in Markdown: for each section below, in this order, a line "## <section title>" followed by short bullet \
points ("- "). Keep each bullet to one or two sentences. If the recording does not cover a section, write one bullet saying it was \
not discussed in this recording. If the transcript is very short, say so plainly rather than padding. No preamble, no closing \
remarks, nothing outside the sections.

Sections:
{sections}"""


def _client():
    import anthropic
    return anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))


def generate_order_summary(transcript_text: str) -> dict:
    """Returns {"ok": True, "summary": "..."} or {"ok": False, "error": "..."}."""
    if not ai_assist_configured():
        return {"ok": False, "error": "AI summary isn't configured right now."}
    if not transcript_text or not transcript_text.strip():
        return {"ok": False, "error": "There's no reviewed transcript text yet to summarize."}
    try:
        resp = _client().messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=SUMMARY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"Transcript:\n\n{transcript_text[:12000]}"}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if not text:
            return {"ok": False, "error": "Couldn't generate a summary just now - try again shortly."}
        return {"ok": True, "summary": text}
    except Exception as exc:  # noqa: BLE001 - surface a real failure, never fabricate a summary instead
        return {"ok": False, "error": f"Summary request failed: {exc}"}


def generate_templated_summary(transcript_text: str, template: dict) -> dict:
    """A summary written in the chosen template's sections. Returns {"ok": True, "summary": "<markdown>"} or {"ok": False, "error": "..."}."""
    if not ai_assist_configured():
        return {"ok": False, "error": "AI summary isn't configured right now."}
    if not transcript_text or not transcript_text.strip():
        return {"ok": False, "error": "There's no reviewed transcript text yet to summarize."}
    from . import summary_templates
    try:
        resp = _client().messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1500,
            system=TEMPLATED_SYSTEM_PROMPT.format(sections=summary_templates.instructions(template)),
            messages=[{"role": "user", "content": f"Transcript:\n\n{transcript_text[:20000]}"}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if not text:
            return {"ok": False, "error": "Couldn't generate a summary just now - try again shortly."}
        return {"ok": True, "summary": text}
    except Exception as exc:  # noqa: BLE001 - surface a real failure, never fabricate a summary instead
        return {"ok": False, "error": f"Summary request failed: {exc}"}


def render_summary_html(markdown: str) -> str:
    """The summary's Markdown (## headings, - bullets, **bold**) as safe HTML: everything is escaped first, so nothing in the text can
    become markup."""
    out, in_list = [], False
    for raw in (markdown or "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", _html.escape(re.sub(r"^(#{1,6}\s+|[-*\u2022]\s+|\d+[.)]\s+)", "", line)))
        if line.startswith("#"):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h4>{text}</h4>")
        elif re.match(r"^([-*\u2022]|\d+[.)])\s+", line):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{text}</li>")
        else:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<p>{text}</p>")
    if in_list:
        out.append("</ul>")
    return "".join(out)


def plain_summary(markdown: str) -> str:
    """The same summary as plain lines, for places that cannot show Markdown (a Slack message, a Google Doc line)."""
    lines = []
    for raw in (markdown or "").split("\n"):
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", re.sub(r"^#{1,6}\s+", "", raw.strip()))
        if line:
            lines.append(line)
    return "\n".join(lines)


def answer_question_about_order(transcript_text: str, question: str) -> dict:
    """Returns {"ok": True, "answer": "..."} or {"ok": False, "error": "..."}."""
    if not ai_assist_configured():
        return {"ok": False, "error": "Ask-the-transcript isn't configured right now."}
    if not transcript_text or not transcript_text.strip():
        return {"ok": False, "error": "There's no reviewed transcript text yet to ask about."}
    question = (question or "").strip()
    if not question:
        return {"ok": False, "error": "Type an actual question first."}
    if len(question) > 500:
        return {"ok": False, "error": "That question's too long - keep it under 500 characters."}
    try:
        resp = _client().messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=ASK_SYSTEM_PROMPT,
            messages=[{"role": "user",
                       "content": f"Transcript:\n\n{transcript_text[:12000]}\n\nQuestion: {question}"}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if not text:
            return {"ok": False, "error": "Couldn't get an answer just now - try again shortly."}
        return {"ok": True, "answer": text}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Question failed: {exc}"}
