"""AI summaries are made only when the customer asks, in the template they choose, and kept."""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from webapp import order_ai_assist, summary_templates  # noqa: E402

MARKDOWN = "## Discussion Topics\n- The **budget** was reviewed.\n- A <script>alert(1)</script> idea came up.\n\n## Decisions Made\n- Not discussed in this recording."


def _job():
    segs = [SimpleNamespace(final_text="Hello there.", source_final_text="Habari.", start_ms=500, end_ms=2500, speaker_id=None, segment_type="speech",
                            segment_id="s1")]
    return SimpleNamespace(segments=segs)


def _ready(env, wav_bytes):
    order = env.order_from(env.place(wav_bytes, source_lang="sw", target_lang="en", service_level="translate", review_tier="ai"))
    env.db.update_order_status(order["id"], "ready_for_delivery")
    env.monkeypatch.setattr(env.app, "_plan_chosen", lambda client_id: True)
    env.monkeypatch.setattr(env.app, "_load_job", lambda o: _job())
    return env.db.get_order(order["id"])


def _writer(env, calls):
    def fake(text, template):
        calls.append(template["id"])
        return {"ok": True, "summary": MARKDOWN}
    env.monkeypatch.setattr(order_ai_assist, "generate_templated_summary", fake)


def test_the_library_has_the_categories_and_the_general_templates():
    cat = summary_templates.catalog()
    assert [c["name"] for c in cat["categories"]] == ["General", "Sales", "HR", "Health", "Customer Success", "Media & Podcasting", "Marketing",
                                                      "Education", "Consulting", "Product"]
    general = [t["name"] for t in cat["categories"][0]["templates"]]
    assert general == ["General Meeting", "Sprint Planning", "Basic Summary", "Board Meeting", "Standard", "Brainstorming", "Kick off Meeting"]
    ids = [t["id"] for t in summary_templates.TEMPLATES]
    assert len(ids) == len(set(ids)) and all(len(t["sections"]) >= 3 for t in summary_templates.TEMPLATES)
    assert all(c["templates"] for c in cat["categories"]) and summary_templates.get("nonsense")["id"] == summary_templates.DEFAULT_ID
    assert summary_templates.get("general-meeting")["sections"][2]["title"] == "Action Items"


def test_the_summary_is_shown_as_safe_html():
    html = order_ai_assist.render_summary_html(MARKDOWN)
    assert "<h4>Discussion Topics</h4>" in html and "<strong>budget</strong>" in html and html.count("<li>") == 3
    assert "<script>" not in html and "&lt;script&gt;" in html                                  # nothing in the text can become markup
    assert order_ai_assist.plain_summary(MARKDOWN).startswith("Discussion Topics\n- The budget was reviewed.")


def test_opening_an_order_makes_no_summary_and_asks_for_none(env, wav_bytes, monkeypatch):
    order = _ready(env, wav_bytes)
    calls = []
    _writer(env, calls)
    html = env.client.get(f"/account/orders/{order['id']}").text
    assert calls == [] and env.db.list_order_summaries(order["id"]) == []
    assert 'id="sm-open"' in html and "No summary yet" in html and "Create summary" in html
    assert "ai-summary-loading" not in html and "Generating a summary" not in html               # the old automatic request is gone
    assert "General Meeting" in html                                                              # the picker's choices are on the page; nothing is written


def test_choosing_a_template_writes_it_once_and_keeps_it(env, wav_bytes):
    order = _ready(env, wav_bytes)
    calls = []
    _writer(env, calls)
    r = env.client.post(f"/account/orders/{order['id']}/ai-summary", data={"template": "general-meeting"})
    data = r.json()
    assert data["ok"] and data["template"] == "general-meeting" and data["name"] == "General Meeting" and "<h4>Decisions Made</h4>" in data["html"]
    assert calls == ["general-meeting"]
    again = env.client.post(f"/account/orders/{order['id']}/ai-summary", data={"template": "general-meeting"}).json()
    assert again["ok"] and again["summary"] == MARKDOWN and calls == ["general-meeting"]          # kept: no second paid request
    env.client.post(f"/account/orders/{order['id']}/ai-summary", data={"template": "standard"})
    assert calls == ["general-meeting", "standard"] and len(env.db.list_order_summaries(order["id"])) == 2
    html = env.client.get(f"/account/orders/{order['id']}").text
    assert 'data-template="general-meeting"' in html and 'data-template="standard"' in html and "Create another summary" in html
    assert 'id="sm-empty" hidden' in html                                                        # the "no summary yet" note goes away
    plain = env.db.get_order(order["id"])["ai_summary_text"]
    assert plain.startswith("Discussion Topics") and "##" not in plain                           # integrations get a plain first summary


def test_no_template_given_uses_the_basic_summary_and_a_failure_saves_nothing(env, wav_bytes, monkeypatch):
    order = _ready(env, wav_bytes)
    calls = []
    _writer(env, calls)
    assert env.client.post(f"/account/orders/{order['id']}/ai-summary").json()["template"] == "basic-summary"
    env.monkeypatch.setattr(order_ai_assist, "generate_templated_summary", lambda t, tpl: {"ok": False, "error": "Summary request failed: down"})
    bad = env.client.post(f"/account/orders/{order['id']}/ai-summary", data={"template": "board-meeting"}).json()
    assert not bad["ok"] and [r["template_id"] for r in env.db.list_order_summaries(order["id"])] == ["basic-summary"]


def test_correcting_the_transcript_clears_the_summaries_written_from_the_old_text(env, wav_bytes):
    order = _ready(env, wav_bytes)
    env.db.save_order_summary(order["id"], "standard", MARKDOWN)
    env.db.clear_order_summaries(order["id"])
    assert env.db.list_order_summaries(order["id"]) == []
