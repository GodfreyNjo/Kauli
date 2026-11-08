"""Notes, analytics, workspace, vocabulary, rewards, plans, integrations and the help assistant in the customer account."""
import io
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from test_media_preview import _finished  # noqa: E402


def _as_user(env, user_id, email, role="client"):
    env.db.get_or_create_user(user_id, email, role)
    env.monkeypatch.setattr(env.app, "current_user", lambda request: {**dict(env.db.get_user(user_id)), "client_scope_id": user_id})


# ------------------------------------------------------------------------------------------------ pages and navigation
PAGES = ["/account/notes", "/account/analytics", "/account/workspace", "/account/vocabulary", "/account/rewards", "/account/integrations", "/account/resources"]


def test_every_account_page_opens_and_the_menu_links_to_them(env):
    for path in PAGES:
        r = env.client.get(path)
        assert r.status_code == 200, path
        for other in PAGES:
            assert f'href="{other}"' in r.text, (path, other)
    assert 'id="as-root"' in env.client.get("/account/home").text                             # the assistant is on every customer page


def test_signed_out_visitors_are_sent_to_log_in(env):
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)
    for path in PAGES + ["/staff/support"]:
        r = env.client.get(path)
        assert r.status_code in (303, 307) and "/login" in r.headers["location"], path
    assert env.client.post("/account/assistant/message", json={"text": "hi"}).status_code == 401


def test_the_menu_is_translated_for_other_languages(env):
    from webapp import ui_text
    for lang in ui_text.LANGS:
        for key in ("nav_notes", "nav_analytics", "nav_workspace", "nav_vocabulary", "nav_integrations", "nav_rewards", "nav_resources"):
            assert ui_text.translate(lang, key) != key


# ------------------------------------------------------------------------------------------------ notes
def _new_note(env):
    r = env.client.post("/account/notes/new")
    assert r.status_code == 303
    return r.headers["location"].split("id=")[1]


def test_a_note_is_created_saved_cleaned_listed_and_downloaded(env):
    nid = _new_note(env)
    page = env.client.get(f"/account/notes?id={nid}").text
    assert 'id="nt-body"' in page and "/static/notes_editor.js" in page
    dirty = '<h2>Plan</h2><p onclick="x()">Call <b>Amina</b><script>alert(1)</script></p><a href="javascript:evil()">bad</a><a href="https://ok.example/x">good</a><ul><li>one</li></ul><iframe src="x"></iframe>'
    r = env.client.post(f"/account/notes/{nid}/save", json={"title": "Weekly call", "body_html": dirty})
    assert r.status_code == 200 and r.json()["ok"]
    shown = env.client.get(f"/account/notes?id={nid}").text
    assert "Weekly call" in shown and "<b>Amina</b>" in shown and "<li>one</li>" in shown
    assert "alert(1)" not in shown and "onclick" not in shown and "javascript:" not in shown and "<iframe" not in shown
    assert 'href="https://ok.example/x"' in shown and 'rel="noopener noreferrer"' in shown
    assert "Weekly call" in env.client.get("/account/notes").text
    assert "Weekly call" in env.client.get("/account/notes?q=amina").text and "Weekly call" not in env.client.get("/account/notes?q=zzz").text
    txt = env.client.get(f"/account/notes/{nid}/download?format=txt")
    assert txt.status_code == 200 and "Call Amina" in txt.text and "• one" in txt.text
    docx = env.client.get(f"/account/notes/{nid}/download?format=docx")
    assert "Call Amina" in zipfile.ZipFile(io.BytesIO(docx.content)).read("word/document.xml").decode()
    assert env.client.get(f"/account/notes/{nid}/download?format=html").text.count("<h1>Weekly call</h1>") == 1
    assert env.client.post(f"/account/notes/{nid}/delete").status_code == 303
    assert "Weekly call" not in env.client.get("/account/notes").text


def test_notes_belong_to_their_owner(env):
    nid = _new_note(env)
    _as_user(env, "u2", "other@example.com")
    assert env.client.post(f"/account/notes/{nid}/save", json={"title": "x", "body_html": "y"}).status_code == 404
    assert env.client.get(f"/account/notes/{nid}/download").status_code == 404
    assert "Untitled" not in env.client.get("/account/notes").text
    env.client.post(f"/account/notes/{nid}/delete")
    _as_user(env, "u1", "client@example.com")
    assert "Untitled note" in env.client.get("/account/notes").text                           # the other account could not delete it


def test_ai_notes_can_be_made_from_a_transcript(env, wav_bytes):
    order = _finished(env, wav_bytes)
    assert 'action="/account/orders/%s/notes"' % order["id"] in env.client.get(f"/account/orders/{order['id']}").text
    r = env.client.post(f"/account/orders/{order['id']}/notes")
    assert r.status_code == 303 and "/account/notes?id=" in r.headers["location"]
    page = env.client.get(r.headers["location"]).text
    assert "Notes: clip.wav" in page and "Overview" in page or "not enough spoken text" in page


def test_the_note_builder_uses_only_the_transcripts_own_words():
    from webapp import account_hub
    text = ("We will send the budget to Amina by Friday. The launch moved to March because the vendor is late. "
            "Who owns the press list? Mercy should draft the announcement. The team agreed the launch needs a second review.")
    html = account_hub.extractive_notes(text)
    assert "<h2>Overview</h2>" in html and "<h2>Key points</h2>" in html and "<h2>Action items</h2>" in html and "<h2>Open questions</h2>" in html
    assert "Who owns the press list?" in html and "budget to Amina" in html
    assert "not enough spoken text" in account_hub.extractive_notes("ok")


# ------------------------------------------------------------------------------------------------ analytics
def test_analytics_shows_minutes_languages_and_sentiment(env, wav_bytes):
    empty = env.client.get("/account/analytics").text
    assert "Nothing to chart yet" in empty
    order = _finished(env, wav_bytes)
    page = env.client.get("/account/analytics").text
    assert "Total transcription minutes" in page and "<svg" in page and "English" in page and "Distribution by language" in page
    assert "Conversation sentiment" in page
    for rng in ("3", "6", "12"):
        assert env.client.get(f"/account/analytics?range={rng}").status_code == 200
    assert env.db.get_conn().execute("SELECT COUNT(*) FROM order_sentiment WHERE order_id = ?", (order["id"],)).fetchone()[0] == 1


def test_sentiment_counts_follow_the_wording():
    from webapp import account_hub
    assert account_hub.sentiment_counts(["This is great, thank you", "That was a terrible problem", "The meeting is at noon"]) == (1, 1, 1)


# ------------------------------------------------------------------------------------------------ vocabulary
def test_vocabulary_is_added_listed_removed_and_applied(env):
    r = env.client.post("/account/vocabulary/add", data={"entries": "Kauli | cowley, kowli\nNjoroge\n  \nForge Media Services"})
    assert r.status_code == 303
    page = env.client.get("/account/vocabulary").text
    assert "Kauli" in page and "cowley, kowli" in page and "Njoroge" in page and "(3 of 200)" in page
    from webapp import account_hub
    fix = account_hub.vocabulary_fixer("u1")
    assert fix("I met cowley and KOWLI at njoroge's place, not forge media services.") == "I met Kauli and Kauli at Njoroge's place, not Forge Media Services."
    assert fix("cowleyfield stays") == "cowleyfield stays"                                      # whole words only
    assert account_hub.vocabulary_fixer("nobody") is None
    term_id = env.db.get_conn().execute("SELECT id FROM vocab_terms WHERE term = 'Njoroge'").fetchone()[0]
    env.client.post("/account/vocabulary/delete", data={"term_id": term_id})
    assert "<strong>Njoroge</strong>" not in env.client.get("/account/vocabulary").text
    assert "Type a word" in env.client.get(env.client.post("/account/vocabulary/add", data={"entries": "  "}).headers["location"]).text


def test_vocabulary_has_a_size_limit_and_is_private_to_the_account(env):
    env.client.post("/account/vocabulary/add", data={"entries": "\n".join(f"Term{i}" for i in range(230))})
    assert len(env.db.get_conn().execute("SELECT * FROM vocab_terms WHERE client_id = 'u1'").fetchall()) == 200
    _as_user(env, "u2", "other@example.com")
    assert "Term1" not in env.client.get("/account/vocabulary").text


def test_the_pipeline_applies_the_fixer_to_new_transcripts():
    from kauli import pipeline
    import inspect
    assert "fix_text" in inspect.signature(pipeline.run).parameters
    src = inspect.getsource(pipeline.run)
    assert "seg.source_transcript = fix_text(seg.source_transcript)" in src


# ------------------------------------------------------------------------------------------------ rewards
def _signup(env, uid, code):
    env.db.get_or_create_user(uid, f"{uid}@example.com", "client")
    from webapp import account_hub
    return account_hub.record_referral(env.db.get_user(uid), code)


def test_the_referral_link_sets_a_cookie_and_the_code_is_stable(env):
    page = env.client.get("/account/rewards").text
    code = re.search(r"/r/([a-z0-9]{8})", page).group(1)
    assert re.search(r"/r/([a-z0-9]{8})", env.client.get("/account/rewards").text).group(1) == code
    r = env.client.get(f"/r/{code}")
    assert r.status_code == 303 and r.headers["location"] == "/signup" and f"kauli_ref={code}" in r.headers["set-cookie"]
    assert "kauli_ref" not in env.client.get("/r/<script>").headers.get("set-cookie", "")


def test_a_friend_joining_earns_minutes_that_really_count(env):
    from webapp import account_hub, billing
    code = account_hub.referral_code_for(env.db.get_user("u1"))
    before = billing.free_minutes_remaining(env.db.get_subscription("u1"))
    assert _signup(env, "f1", code) is True
    assert billing.free_minutes_remaining(env.db.get_subscription("u1")) == before + 100
    assert _signup(env, "f1", code) is False                                                 # once per friend
    assert _signup(env, "f2", "nosuchcode") is False and _signup(env, "f3", None) is False
    own = env.db.get_user("u1")
    assert account_hub.record_referral(own, code) is False                                   # not yourself
    page = env.client.get("/account/rewards").text
    assert "Friends invited" in page
    assert env.db.list_recent_notifications("u1")[0]["title"].startswith("A friend joined")


def test_upgrading_earns_the_bigger_reward_once_and_both_are_capped(env):
    from webapp import account_hub, billing
    code = account_hub.referral_code_for(env.db.get_user("u1"))
    _signup(env, "f1", code)
    account_hub.on_plan_paid("f1", "free")
    assert env.db.get_subscription("u1")["reward_minutes"] == 100
    account_hub.on_plan_paid("f1", "pro")
    account_hub.on_plan_paid("f1", "pro")
    assert env.db.get_subscription("u1")["reward_minutes"] == 1100
    for i in range(account_hub.MAX_REWARDED_SIGNUPS + 3):
        _signup(env, f"x{i}", code)
    total = env.db.get_subscription("u1")["reward_minutes"]
    assert total == 100 * account_hub.MAX_REWARDED_SIGNUPS + 1000                           # sign-ups past the cap earn nothing
    assert len(env.db.get_conn().execute("SELECT * FROM referrals WHERE referrer_id = 'u1'").fetchall()) == account_hub.MAX_REWARDED_SIGNUPS + 4


def test_reward_minutes_roll_over_and_only_the_used_part_is_spent(env):
    from webapp import billing
    conn = env.db.get_conn()
    conn.execute("UPDATE subscriptions SET reward_minutes = 100, minutes_used_this_period = 50, period_started_at = 1 WHERE user_id = 'u1'")
    conn.commit()
    conn.close()
    sub = env.db.get_subscription_current("u1")
    # 50 minutes were used: the 30 free ones first, then 20 of the reward
    assert sub["minutes_used_this_period"] == 0 and sub["reward_minutes"] == 80
    assert billing.free_minutes_remaining(sub) == billing.FREE_MINUTES_PER_MONTH + 80
    conn = env.db.get_conn()
    conn.execute("UPDATE subscriptions SET minutes_used_this_period = 10, period_started_at = 1 WHERE user_id = 'u1'")
    conn.commit()
    conn.close()
    assert env.db.get_subscription_current("u1")["reward_minutes"] == 80                    # within the free 30: the reward is untouched


# ------------------------------------------------------------------------------------------------ plans
def test_the_plans_page_shows_usage_choices_and_closing(env):
    page = env.client.get("/account/billing").text
    assert "Pay as you go" in page and "2,500" in page and "6,500" in page and "Subscribe to Pro" in page and "Subscribe to Premium" in page
    assert 'action="/account/billing/checkout"' in page and 'href="/account/close-account"' in page and "close your account" in page
    assert "Included minutes left" in page
    env.db.set_subscription_plan("u1", "pro", 30)
    page = env.client.get("/account/billing").text
    assert "Your plan" in page and "Switch to Premium" in page and "2,500" in page
    assert "does not renew by itself" in page                                                   # no auto-renewing subscription behind it


def test_team_members_cannot_change_or_close_the_account(env):
    env.db.get_or_create_user("m1", "member@example.com", "client")
    env.monkeypatch.setattr(env.app, "current_user", lambda request: {**dict(env.db.get_user("m1")), "client_scope_id": "u1"})
    page = env.client.get("/account/billing").text
    assert "Ask the account owner" in page and "close your account" not in page and 'action="/account/billing/checkout"' not in page


# ------------------------------------------------------------------------------------------------ integrations
def test_the_integrations_page_lists_real_options_and_takes_requests(env):
    page = env.client.get("/account/integrations").text
    for name in ("Slack", "Microsoft Teams", "Notion", "Zapier", "HubSpot", "Webhooks", "Google Drive"):
        assert name in page
    assert 'data-kind="built"' in page and 'data-kind="zapier"' in page and "Built in" in page and "Feedback &amp; requests" in page
    assert "Coming soon" in page and "Connect your calendar" in page and "Google Calendar" in page and "Outlook Calendar" in page
    r = env.client.post("/account/integrations/request", data={"body": "Please add Airtable"})
    assert r.status_code == 303
    assert env.db.get_conn().execute("SELECT body FROM integration_requests").fetchone()[0] == "Please add Airtable"
    assert env.client.post("/account/calendar/interest", data={"provider": "google_calendar"}).status_code == 303
    assert env.client.post("/account/calendar/interest", data={"provider": "bogus"}).status_code == 303
    assert env.db.get_conn().execute("SELECT COUNT(*) FROM calendar_interest").fetchone()[0] == 1
    assert 'href="/account/calendar"' in page


def test_nothing_in_the_live_list_points_nowhere():
    from webapp import account_hub
    for i in account_hub.INTEGRATIONS:
        assert (i["state"] == "live") == bool(i["href"]), i["id"]


# ------------------------------------------------------------------------------------------------ assistant
def _ask(env, text, thread_id=None, **extra):
    return env.client.post("/account/assistant/message", json={"text": text, "thread_id": thread_id, **extra})


def test_the_assistant_answers_from_the_help_material(env):
    r = _ask(env, "How do I download my transcript as a PDF?").json()
    assert "Download" in r["reply"] and "PDF" in r["reply"] and r["thread_id"] and r["handoff"] is False
    price = _ask(env, "what does the pro plan cost").json()["reply"]
    assert "$19" in price and "2,500" in price
    assert "100 minutes" in _ask(env, "how do I earn free minutes from a referral").json()["reply"]


def test_a_question_it_cannot_answer_offers_the_team_and_escalation_reaches_staff(env):
    env.db.get_or_create_user("s1", "staff@example.com", "staff")
    r = _ask(env, "zzzz qqqq xxxx").json()
    assert r["handoff"] is True and "Kauli team" in r["reply"]
    tid = r["thread_id"]
    assert env.client.post("/account/assistant/escalate", json={"thread_id": tid, "note": "Please call me about invoices"}).json()["ok"]
    assert env.client.post("/account/assistant/escalate", json={"thread_id": tid}).json()["ok"]                     # idempotent
    follow = _ask(env, "Any news?", tid).json()
    assert follow["escalated"] is True and "team" in follow["reply"]                                              # the bot stops talking over staff
    threads = env.client.get("/account/assistant/threads").json()["threads"]
    assert threads[0]["escalated"] is True and any("invoices" in m["body"] for m in threads[0]["messages"])
    # staff see it, answer it, and the customer sees the answer
    mine = env.app.current_user
    env.monkeypatch.setattr(env.app, "current_user", lambda request: {**dict(env.db.get_user("s1")), "client_scope_id": "s1"})
    assert "zzzz qqqq xxxx" in env.client.get("/staff/support").text
    assert "Please call me about invoices" in env.client.get(f"/staff/support/{tid}").text
    assert env.client.post(f"/staff/support/{tid}/reply", data={"body": "We will call you today.", "close": "1"}).status_code == 303
    env.monkeypatch.setattr(env.app, "current_user", mine)
    msgs = env.client.get("/account/assistant/threads").json()["threads"][0]
    assert msgs["status"] == "closed" and msgs["messages"][-1] == {**msgs["messages"][-1], "sender": "staff", "body": "We will call you today."}
    assert env.db.list_recent_notifications("u1")[0]["title"] == "The Kauli team replied to your question"
    assert any(n["kind"] == "support" for n in env.db.list_recent_notifications("s1"))


def test_escalating_needs_a_question_and_threads_are_private(env):
    assert env.client.post("/account/assistant/escalate", json={}).status_code == 400
    tid = _ask(env, "How do I edit a transcript?").json()["thread_id"]
    _as_user(env, "u2", "other@example.com")
    assert env.client.get("/account/assistant/threads").json()["threads"] == []
    assert env.client.post("/account/assistant/escalate", json={"thread_id": tid}).status_code == 400
    again = _ask(env, "hello there", tid).json()
    assert again["thread_id"] != tid                                                              # a stranger's id starts a fresh thread
    assert _ask(env, "   ").status_code == 400


def test_non_staff_cannot_open_the_support_desk(env):
    r = env.client.get("/staff/support")
    assert r.status_code == 303 and "/login" in r.headers["location"]
    assert env.client.post("/staff/support/abc/reply", data={"body": "x"}).status_code == 303


def test_the_assistant_uses_claude_when_a_key_is_set_and_falls_back_when_it_fails(env, monkeypatch):
    from webapp import account_hub
    monkeypatch.setattr(account_hub.order_ai_assist, "ai_assist_configured", lambda: True)

    class Resp:
        content = [type("B", (), {"type": "text", "text": "Open Plans in the menu. [ESCALATE]"})()]

    class Fake:
        class messages:
            @staticmethod
            def create(**kw):
                assert "HELP MATERIAL" in kw["system"] and "2,500" in kw["system"]
                return Resp()

    monkeypatch.setattr(account_hub.order_ai_assist, "_client", lambda: Fake())
    reply, handoff = account_hub.assistant_reply("how do plans work", [])
    assert reply == "Open Plans in the menu." and handoff is True

    class Boom:
        class messages:
            @staticmethod
            def create(**kw):
                raise RuntimeError("down")

    monkeypatch.setattr(account_hub.order_ai_assist, "_client", lambda: Boom())
    assert "$19" in account_hub.assistant_reply("what does the pro plan cost", [])[0]


def test_the_transcript_can_be_opened_as_an_editable_note(env, wav_bytes):
    order = _finished(env, wav_bytes)
    assert 'action="/account/orders/%s/notes?as=transcript"' % order["id"] in env.client.get(f"/account/orders/{order['id']}").text
    r = env.client.post(f"/account/orders/{order['id']}/notes?as=transcript")
    assert r.status_code == 303
    page = env.client.get(r.headers["location"]).text
    assert "Transcript: clip.wav" in page and "<p>Hello there</p>" in page and "<p>Second line</p>" in page


def test_billing_and_plans_are_one_page_matching_the_website(env):
    nav = env.client.get("/account/home").text
    assert 'href="/account/billing"' in nav and 'href="/account/plans"' not in nav and ">Billing<" in nav
    moved = env.client.get("/account/plans", follow_redirects=False)
    assert moved.status_code == 303 and moved.headers["location"] == "/account/billing#change-plan"
    page = env.client.get("/account/billing").text
    for text in ("Billing and plans", "Pay as you go", "For occasional projects", "For steady, smaller projects", "Best for regular premium work",
                 "For premium work, small or in bulk", "For heavy users and organisations", "All benefits in Free", "All benefits in Pro included",
                 "All benefits in Pro and Premium", "Anything beyond that is the standard per-minute price", "Downloads and delivery unlocked",
                 "Dedicated account manager", "Managed billing and invoicing"):
        assert text in page or text.replace(" included", "") in page, text
    for slug_text in (f"${env.app.billing.PLANS['pro']['price_usd']}", f"${env.app.billing.PLANS['premium']['price_usd']}"):
        assert slug_text in page                                  # the same prices as the website's plan cards


def test_every_plan_line_on_the_website_is_on_the_account_page_too(env):
    real = env.app.current_user
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)
    site = env.client.get("/").text
    env.monkeypatch.setattr(env.app, "current_user", real)
    import re
    from html import unescape
    bullets = [unescape(re.sub(r"<[^>]+>", "", m)).strip() for m in re.findall(r'<li>(.*?)</li>', site.split('id="pz-plans"', 1)[1].split("</section>", 1)[0], flags=re.S)]
    assert len(bullets) >= 15
    page = unescape(re.sub(r"<[^>]+>", "", env.client.get("/account/billing").text))
    page = re.sub(r"\s+", " ", page)
    for line in bullets:
        assert re.sub(r"\s+", " ", line) in page, line
