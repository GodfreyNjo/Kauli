"""Transcript tools in the client account: download with options, share links, and the toolbar on the order page."""
import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from test_media_preview import _finished  # noqa: E402


def _export(env, order, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    return env.client.get(f"/account/orders/{order['id']}/export?{q}")


# ------------------------------------------------------------------------------------------------ download
def test_every_format_downloads_with_the_right_type(env, wav_bytes):
    order = _finished(env, wav_bytes)
    txt = _export(env, order, format="txt")
    assert txt.status_code == 200 and txt.headers["content-type"].startswith("text/plain")
    assert "Hello there" in txt.text and "[00:00]" in txt.text and "_transcript.txt" in txt.headers["content-disposition"]
    docx = _export(env, order, format="docx")
    assert docx.status_code == 200 and "Hello there" in zipfile.ZipFile(io.BytesIO(docx.content)).read("word/document.xml").decode()
    pdf = _export(env, order, format="pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF-1.4") and pdf.content.rstrip().endswith(b"%%EOF") and b"Hello there" in pdf.content
    csv_ = _export(env, order, format="csv")
    assert csv_.status_code == 200 and csv_.content.startswith(b"\xef\xbb\xbf") and "start,end" in csv_.text
    srt = _export(env, order, format="srt")
    assert srt.status_code == 200 and "-->" in srt.text and "Hello there" in srt.text


def test_download_options_change_the_file(env, wav_bytes):
    order = _finished(env, wav_bytes)
    plain = _export(env, order, format="txt", timestamps=0, speakers=0).text
    assert "[00:" not in plain and "Hello there" in plain
    single = _export(env, order, format="txt", split="single", timestamps=0, speakers=0).text
    body = single.split("\n\n", 1)[1].strip()
    assert "Hello there" in body and "Second line" in body and "\n\n" not in body                    # one paragraph
    no_speaker = _export(env, order, format="csv", speakers=0).text
    assert "speaker" not in no_speaker.splitlines()[0]
    assert _export(env, order, format="txt", split="nonsense").status_code == 200                     # unknown option falls back


def test_a_translated_order_can_download_either_side(env, wav_bytes):
    order = _finished(env, wav_bytes)
    conn = env.db.get_conn()
    conn.execute("UPDATE orders SET service_level = 'translate', target_lang = 'es' WHERE id = ?", (order["id"],))
    conn.commit()
    conn.close()
    order = env.db.get_order(order["id"])
    assert env.client.post(f"/account/orders/{order['id']}/transcript", json={"edits": [{"segment_id": "s1", "target": "Hola a todos"}]}).status_code == 200
    source = _export(env, order, format="txt", side="source").text
    target = _export(env, order, format="txt", side="target").text
    assert "Hello there" in source and "Hola a todos" not in source
    assert "Hola a todos" in target and "Hello there" not in target


def test_bad_formats_and_foreign_orders_are_refused(env, wav_bytes):
    order = _finished(env, wav_bytes)
    assert _export(env, order, format="exe").status_code == 400
    env.db.get_or_create_user("u2", "other@example.com", "client")
    mine = env.app.current_user
    env.monkeypatch.setattr(env.app, "current_user", lambda request: {**dict(env.db.get_user("u2")), "client_scope_id": "u2"})
    assert _export(env, order, format="txt").status_code == 404
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)
    assert _export(env, order, format="txt").status_code == 401
    env.monkeypatch.setattr(env.app, "current_user", mine)
    conn = env.db.get_conn()
    conn.execute("UPDATE orders SET is_free_preview = 1, duration_minutes = 10 WHERE id = ?", (order["id"],))
    conn.commit()
    conn.close()
    r = _export(env, order, format="txt")
    assert r.status_code == 403 and "Subscribe to the Free plan" in r.json()["error"]


def test_a_script_the_pdf_cannot_draw_says_so_instead_of_failing(env, wav_bytes):
    order = _finished(env, wav_bytes)
    r = env.client.post(f"/account/orders/{order['id']}/transcript", json={"edits": [{"segment_id": "s1", "source": "你好世界"}]})
    assert r.status_code == 200
    pdf = _export(env, order, format="pdf")
    assert pdf.status_code == 400 and "Word" in pdf.json()["error"]
    assert "你好" in _export(env, order, format="txt").text


# ------------------------------------------------------------------------------------------------ share
def _share(env, order, **body):
    return env.client.post(f"/account/orders/{order['id']}/share", json=body or {"action": "create"})


def test_a_share_link_shows_the_transcript_read_only_and_can_be_switched_off(env, wav_bytes):
    order = _finished(env, wav_bytes)
    made = _share(env, order).json()
    assert made["ok"] and "/s/" in made["url"] and made["whatsapp"].startswith("https://wa.me/?text=")
    token = made["url"].rsplit("/s/", 1)[1]
    assert _share(env, order).json()["url"] == made["url"]                                  # a second click reuses the live link
    page = env.client.get(f"/s/{token}")
    assert page.status_code == 200 and "Hello there" in page.text and "noindex" in page.text
    assert page.headers["x-robots-tag"].startswith("noindex")
    assert env.db.get_order_share(token)["views"] == 1
    assert _share(env, order, action="revoke").json()["revoked"] == 1
    gone = env.client.get(f"/s/{token}")
    assert gone.status_code == 404 and "Hello there" not in gone.text


def test_unknown_or_expired_links_show_nothing(env, wav_bytes):
    order = _finished(env, wav_bytes)
    assert env.client.get("/s/not-a-token").status_code == 404
    token = _share(env, order).json()["url"].rsplit("/s/", 1)[1]
    conn = env.db.get_conn()
    conn.execute("UPDATE order_shares SET expires_at = 1 WHERE token = ?", (token,))
    conn.commit()
    conn.close()
    assert env.client.get(f"/s/{token}").status_code == 404


def test_sharing_by_email_checks_the_address_and_needs_the_mailer(env, wav_bytes, monkeypatch):
    order = _finished(env, wav_bytes)
    assert _share(env, order, action="email", to="nope").status_code == 400
    monkeypatch.setattr(env.app.mailer, "email_configured", lambda: False)
    assert _share(env, order, action="email", to="a@example.com").status_code == 503
    sent = []
    monkeypatch.setattr(env.app.mailer, "email_configured", lambda: True)
    monkeypatch.setattr(env.app.mailer, "send_email", lambda to, subject, html, text_body=None, tags=None, sender_name="": (sent.append((to, text_body)) or (True, "id")))
    r = _share(env, order, action="email", to="a@example.com")
    assert r.status_code == 200 and sent and sent[0][0] == "a@example.com" and "/s/" in sent[0][1]


def test_only_the_owner_can_share(env, wav_bytes):
    order = _finished(env, wav_bytes)
    env.db.get_or_create_user("u2", "other@example.com", "client")
    env.monkeypatch.setattr(env.app, "current_user", lambda request: {**dict(env.db.get_user("u2")), "client_scope_id": "u2"})
    assert _share(env, order).status_code == 404


def test_share_pages_stay_out_of_search(env):
    robots = env.client.get("/robots.txt").text
    assert "Disallow: /s/" in robots


# ------------------------------------------------------------------------------------------------ the page
def test_the_order_page_has_the_toolbar_and_hooks_for_it(env, wav_bytes):
    order = _finished(env, wav_bytes)
    page = env.client.get(f"/account/orders/{order['id']}").text
    for needle in ('id="tx-tools"', 'id="tx-share-dialog"', 'id="tx-download-dialog"', 'id="tx-find-panel"', 'id="tx-keys-dialog"',
                   "/static/transcript_tools.js", 'class="tx-time"', 'id="tx-hide-speaker"', 'id="tx-hide-time"', 'id="tx-undo"'):
        assert needle in page, needle
    assert "window.txStartEditing" in page and "window.txAutoScroll" in page
    js = env.client.get("/static/transcript_tools.js").text
    assert "/export?" in js and "CSS.highlights" in js and "navigator.clipboard" in js


def test_a_human_reviewed_order_still_gets_download_and_share_but_not_the_edit_tools(env, wav_bytes):
    order = _finished(env, wav_bytes)
    conn = env.db.get_conn()
    conn.execute("UPDATE orders SET review_tier = 'human' WHERE id = ?", (order["id"],))
    conn.commit()
    conn.close()
    page = env.client.get(f"/account/orders/{order['id']}").text
    assert 'id="tx-tools"' in page and 'id="tx-undo"' not in page and 'id="tx-replace-all"' not in page
