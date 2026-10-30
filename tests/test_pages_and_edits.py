"""Numbered pages on the file lists, and a client correcting their own Instant AI transcript."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from test_media_preview import _finished  # noqa: E402


# ------------------------------------------------------------------------------------------------ page numbers
def _home(env, query=""):
    return env.client.get("/client/home" + query).text


def _rows(html):
    return len(re.findall(r'<tr data-name=', html))


def test_the_recent_files_have_numbered_pages_instead_of_view_all(env, wav_bytes):
    for _ in range(19):
        env.order_from(env.place(wav_bytes))
    first = _home(env)
    assert "View all" not in first and 'class="ch-all"' not in first
    assert _rows(first) == 8 and 'class="pager"' in first
    assert 'aria-current="page">1<' in first and 'href="/client/home?page=2"' in first and 'href="/client/home?page=3"' in first
    second = _home(env, "?page=2")
    assert _rows(second) == 8 and 'aria-current="page">2<' in second and 'rel="prev"' in second and 'rel="next"' in second
    third = _home(env, "?page=3")
    assert _rows(third) == 3 and 'rel="next"' not in third
    assert _rows(_home(env, "?page=99")) == 3 and 'aria-current="page">3<' in _home(env, "?page=99")      # past the end shows the last page
    assert _rows(_home(env, "?page=0")) == 8 and _rows(_home(env, "?page=-4")) == 8


def test_a_short_list_has_no_pager_and_search_covers_every_page(env, wav_bytes):
    env.order_from(env.place(wav_bytes))
    assert 'class="pager"' not in _home(env)
    for _ in range(12):
        env.order_from(env.place(wav_bytes))
    hit = _home(env, "?q=clip")
    assert _rows(hit) == 8 and "q=clip" in hit and 'href="/client/home?q=clip&amp;page=2"' in hit or 'href="/client/home?q=clip&page=2"' in hit
    assert _rows(_home(env, "?q=clip&page=2")) == 5
    nothing = _home(env, "?q=zzz")
    assert _rows(nothing) == 0 and "No files match that search." in nothing and 'class="pager"' not in nothing


def test_long_page_lists_show_the_ends_and_the_neighbours_with_gaps(env):
    from jinja2 import Environment, FileSystemLoader
    jenv = Environment(loader=FileSystemLoader(str(Path(__file__).parent.parent / "webapp" / "templates")), autoescape=True)
    html = jenv.from_string('{% from "_pager.html" import pager %}{{ pager(10, 20, "/x", {"q": "a b"}) }}').render()
    nums = re.findall(r'class="pager-num[^"]*"[^>]*>(\d+)<', html)
    assert nums == ["1", "9", "10", "11", "20"] and html.count("pager-gap") == 2 and "q=a%20b&amp;page=11" in html


def test_the_pending_list_is_paginated_too(env, wav_bytes):
    for _ in range(13):
        env.order_from(env.place(wav_bytes, review_tier="human"))                         # all wait for payment
    page1 = env.client.get("/client/pending").text
    assert len(re.findall(r"/pay\"", page1)) == 10 and 'href="/client/pending?page=2"' in page1
    assert len(re.findall(r"/pay\"", env.client.get("/client/pending?page=2").text)) == 3


# ------------------------------------------------------------------------------------------------ editing the transcript
def _edit(env, order, edits):
    return env.client.post(f"/client/orders/{order['id']}/transcript", json={"edits": edits})


def test_a_client_can_correct_an_instant_ai_transcript_and_every_download_follows(env, wav_bytes):
    order = _finished(env, wav_bytes)
    page = env.client.get(f"/client/orders/{order['id']}").text
    assert 'id="tx-edit"' in page and 'data-edit="source"' in page and 'data-segment-id="s1"' in page and 'class="tx-same"' in page
    r = _edit(env, order, [{"segment_id": "s1", "source": "  Hello   there,  everyone.  "}])
    assert r.status_code == 200 and r.json() == {"ok": True, "changed": 1}
    page = env.client.get(f"/client/orders/{order['id']}").text
    assert "Hello there, everyone." in page and "edited" in page and "Second line" in page
    outdir = Path(order["outdir"])
    assert "Hello there, everyone." in (outdir / "subs_en.srt").read_text(encoding="utf-8")
    assert "Hello there, everyone." in (outdir / "subs_en.vtt").read_text(encoding="utf-8")
    assert "Hello there, everyone." in (outdir / "transcript_en.srt").read_text(encoding="utf-8")
    txt = env.client.get(f"/client/orders/{order['id']}/download/txt")
    assert txt.status_code == 200 and "Hello there, everyone." in txt.text and "Hello there." not in txt.text.replace("everyone.", "")
    assert not env.db.get_order(order["id"])["ai_summary_text"]                             # the summary is written again from the new text


def test_an_empty_text_puts_the_line_back_as_the_machine_wrote_it(env, wav_bytes):
    order = _finished(env, wav_bytes)
    _edit(env, order, [{"segment_id": "s2", "source": "Changed line"}])
    assert "Changed line" in env.client.get(f"/client/orders/{order['id']}").text
    assert _edit(env, order, [{"segment_id": "s2", "source": ""}]).json()["changed"] == 1
    page = env.client.get(f"/client/orders/{order['id']}").text
    assert "Changed line" not in page and "Second line" in page
    assert _edit(env, order, [{"segment_id": "s2", "source": ""}]).json()["changed"] == 0     # nothing to undo


def test_in_a_translated_order_editing_either_side_is_kept_and_the_source_edit_flags_the_translation(env, wav_bytes):
    order = _finished(env, wav_bytes)
    conn = env.db.get_conn()
    conn.execute("UPDATE orders SET service_level = 'translate', target_lang = 'es' WHERE id = ?", (order["id"],))
    conn.commit()
    conn.close()
    order = env.db.get_order(order["id"])
    from kauli.models import Job
    manifest = Path(order["outdir"]) / "manifest.json"
    assert _edit(env, order, [{"segment_id": "s1", "source": "Hello again"}]).json()["changed"] == 1
    seg = Job.load(str(manifest)).segments[0]
    assert seg.source_edited_transcript == "Hello again" and seg.translation_stale is True and seg.edited_text is None
    assert _edit(env, order, [{"segment_id": "s1", "target": "Hola otra vez"}]).json()["changed"] == 1
    seg = Job.load(str(manifest)).segments[0]
    assert seg.edited_text == "Hola otra vez" and seg.translation_stale is False and seg.source_transcript == "Hello there"   # the original is kept
    page = env.client.get(f"/client/orders/{order['id']}").text
    assert 'class="tx-same"' not in page and 'data-edit="target"' in page


def test_human_reviewed_transcripts_are_not_edited_here(env, wav_bytes):
    order = _finished(env, wav_bytes)
    conn = env.db.get_conn()
    conn.execute("UPDATE orders SET review_tier = 'human' WHERE id = ?", (order["id"],))
    conn.commit()
    conn.close()
    r = _edit(env, order, [{"segment_id": "s1", "source": "x"}])
    assert r.status_code == 403 and "revision request" in r.json()["error"]
    assert 'id="tx-edit"' not in env.client.get(f"/client/orders/{order['id']}").text


def test_edits_are_checked_and_limited(env, wav_bytes):
    order = _finished(env, wav_bytes)
    assert env.client.post(f"/client/orders/{order['id']}/transcript", json={"edits": "no"}).status_code == 400
    assert _edit(env, order, [{"segment_id": "s1", "source": "x" * 5001}]).status_code == 400
    assert _edit(env, order, [{"segment_id": "nope", "source": "x"}]).json()["changed"] == 0           # unknown lines are ignored
    assert _edit(env, order, [{"segment_id": "s1", "source": 5}]).status_code == 400
    assert env.client.post("/client/orders/not-an-order/transcript", json={"edits": []}).status_code == 404


def test_only_the_owner_signed_in_with_a_plan_can_edit(env, wav_bytes):
    order = _finished(env, wav_bytes)
    env.db.get_or_create_user("u2", "other@example.com", "client")
    mine = env.app.current_user
    env.monkeypatch.setattr(env.app, "current_user", lambda request: {**dict(env.db.get_user("u2")), "client_scope_id": "u2"})
    assert _edit(env, order, [{"segment_id": "s1", "source": "x"}]).status_code == 404                  # someone else's order
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)
    assert _edit(env, order, [{"segment_id": "s1", "source": "x"}]).status_code == 401
    env.monkeypatch.setattr(env.app, "current_user", mine)
    conn = env.db.get_conn()
    conn.execute("UPDATE users SET plan_chosen_at = NULL WHERE id = 'u1'")
    conn.commit()
    conn.close()
    r = _edit(env, order, [{"segment_id": "s1", "source": "x"}])
    assert r.status_code == 403 and "Choose a plan" in r.json()["error"]


def test_the_page_script_edits_only_changed_lines_and_never_pastes_formatting(env, wav_bytes):
    order = _finished(env, wav_bytes)
    page = env.client.get(f"/client/orders/{order['id']}").text
    assert 'contenteditable", on ? "plaintext-only"' in page and 'getData("text/plain")' in page and "beforeunload" in page
    assert "if (plain(c) !== c.dataset.orig)" in page and "window.txEditing" in page


# ------------------------------------------------------------------------------------------------ editor-only options
def test_the_editor_instructions_are_hidden_for_instant_ai_and_ignored_by_the_server(env, wav_bytes):
    page = env.client.get("/client").text
    for needle in ('id="verbatim-fieldset"', 'id="advanced-settings"', 'id="ai-clean-note"', 'id="verbatim-note"'):
        assert needle in page, needle
    js = env.client.get("/static/client_wizard.js").text
    assert '["verbatim-fieldset", "verbatim-note", "advanced-settings", "speaker-label-field"]' in js and "el.hidden = isAiTier" in js
    order = env.order_from(env.place(wav_bytes, review_tier="ai", instr_verbatim_level="verbatim", instr_existing_subs="match_existing",
                                     instr_no_audio="return", instr_transcribe_lyrics="1", instr_use_italics="1", instr_notes="keep this note"))
    row = env.db.get_order(order["id"])
    assert row["instr_verbatim_level"] == "clean_read" and row["instr_existing_subs"] == "ignore" and row["instr_no_audio"] == "tag"
    assert not row["instr_transcribe_lyrics"] and not row["instr_use_italics"] and row["instr_notes"] == "keep this note"        # a note is not an editor-only choice


def test_human_review_orders_still_keep_their_editor_instructions(env, wav_bytes):
    order = env.order_from(env.place(wav_bytes, review_tier="human", instr_verbatim_level="verbatim", instr_existing_subs="match_existing",
                                     instr_transcribe_lyrics="1"))
    row = env.db.get_order(order["id"])
    assert row["instr_verbatim_level"] == "verbatim" and row["instr_existing_subs"] == "match_existing" and row["instr_transcribe_lyrics"]


def test_the_service_step_keeps_extras_in_one_collapsed_section_and_names_no_vendors(env):
    page = env.client.get("/client").text
    assert 'id="extras"' in page and "Extras and extra charges (optional)" in page
    extras = page.split('id="extras"', 1)[1].split("</details>", 1)[0]
    for ident in ("human-voice-field", "voice-clone-consent-field", "rush-checkbox"):
        assert ident in extras, ident                                                         # every optional charge lives inside it
    step = page.split('data-pane="2"', 1)[1].split('data-pane="3"', 1)[0]
    for vendor in ("ElevenLabs", "Claude", "Anthropic", "Transkriptor", "Groq", "AssemblyAI", "Godfrey"):
        assert vendor not in step, vendor
    assert "+$" in extras and "/min" in extras
    assert "extrasBox.open = true" in env.client.get("/static/client_wizard.js").text         # a dub opens its own extras


# ------------------------------------------------------------------------------------------------ the order list
def _orders_page(env, query=""):
    return env.client.get("/client" + query).text


def _order_rows(html):
    return len(re.findall(r'<tr class="order-row"', html))


def test_the_order_list_has_numbered_pages_that_keep_the_filters(env, wav_bytes):
    for _ in range(23):
        env.order_from(env.place(wav_bytes, folder_name="Podcast"))
    for _ in range(4):
        env.order_from(env.place(wav_bytes, folder_name="Calls"))
    first = _orders_page(env)
    assert _order_rows(first) == 10 and 'class="pager"' in first and 'aria-current="page">1<' in first
    assert 'href="/client?page=2"' in first and 'href="/client?page=3"' in first
    assert _order_rows(_orders_page(env, "?page=3")) == 7 and _order_rows(_orders_page(env, "?page=99")) == 7      # past the end: the last page
    assert _order_rows(_orders_page(env, "?page=0")) == 10 and "View all" not in first
    calls = _orders_page(env, "?folder=Calls")
    assert _order_rows(calls) == 4 and 'class="pager"' not in calls
    podcast = _orders_page(env, "?folder=Podcast&page=2")
    assert _order_rows(podcast) == 10 and "folder=Podcast" in podcast and 'aria-current="page">2<' in podcast
    assert _order_rows(_orders_page(env, "?status=in_progress")) == 10 and "status=in_progress" in _orders_page(env, "?status=in_progress")
    assert _order_rows(_orders_page(env, "?status=ready")) == 0 and "No orders match" in _orders_page(env, "?status=ready")
    assert _order_rows(_orders_page(env, "?q=clip&folder=Calls")) == 4 and _order_rows(_orders_page(env, "?q=zzz")) == 0
    assert _order_rows(_orders_page(env, "?status=bogus")) == 10                              # an unknown filter is ignored


def test_the_order_rows_are_short(env, wav_bytes):
    from test_media_preview import _finished
    _finished(env, wav_bytes)
    html = _orders_page(env)
    row = html.split('<tr class="order-row"', 1)[1].split("</tr>", 1)[0]
    assert "Transcription" in row and "AI" in row and "Ready" in row and "clip.wav" in row
    table = html.split('class="orders-table orders-compact"', 1)[1].split("</table>", 1)[0]
    assert "your order is ready" not in table.lower()                                             # the short status, not the sentence
    for dropped in ("Due date", "Transcription only", "Trial credit", "Analysis in progress"):
        assert dropped not in table, dropped
    assert re.search(r"[A-Z][a-z]{2} \d{2}, 20\d\d", row)                                       # a short date such as Oct 29, 2026
