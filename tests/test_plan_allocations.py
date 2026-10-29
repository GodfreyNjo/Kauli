"""The top plan's promise is kept without the customer doing anything: realistic voices and high-quality translations are allocated
automatically. Plus the wording of the plan cards."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from tests.test_order_flow import env, wav_bytes  # noqa: E402,F401
from tests.test_premium_voice import VOICE, premium  # noqa: E402,F401
from webapp import billing, provider_health  # noqa: E402


def _plan(env, plan):
    env.db.set_subscription_plan("u1", plan, 30)


# ---------------------------------------------------------------------------------------------------------------- voices
def test_a_premium_plan_dub_gets_the_realistic_voice_automatically_at_no_extra_charge(premium):
    _plan(premium, "premium")
    order = premium.order_from(premium.dub())                                              # the customer did not ask for it
    assert order["tts"] == "elevenlabs" and order["dub_voice"] == f"elevenlabs:{VOICE}"
    assert not premium.db.order_has_addon(order, "premium_voice")                          # not charged for
    assert abs(order["cost_usd"] - billing.rate_for("dub", "intl", "ai") * order["duration_minutes"]) < 0.005


def test_a_voice_the_customer_picks_is_kept_and_still_free_on_premium(premium):
    _plan(premium, "premium")
    order = premium.order_from(premium.dub(addon_premium_voice="1", premium_voice_id=VOICE))
    assert order["tts"] == "elevenlabs" and abs(order["cost_usd"] - 0.80 * order["duration_minutes"]) < 0.005


def test_the_voice_also_comes_with_swahili_and_human_reviewed_dubs(premium):
    _plan(premium, "premium")
    assert premium.order_from(premium.dub(source_lang="en", target_lang="sw"))["tts"] == "elevenlabs"
    assert premium.order_from(premium.dub(review_tier="human"))["tts"] == "elevenlabs"


def test_other_plans_do_not_get_it_for_free(premium):
    for plan in ("free", "pro"):
        _plan(premium, plan)
        order = premium.order_from(premium.dub())
        assert order["tts"] != "elevenlabs", plan


def test_a_premium_order_is_never_refused_because_the_voice_provider_is_out_of_credits(premium, monkeypatch):
    _plan(premium, "premium")
    monkeypatch.setattr(premium.app, "_premium_capacity_ok", lambda minutes: False)
    order = premium.order_from(premium.dub())
    assert order["tts"] != "elevenlabs" and not premium.db.order_has_addon(order, "premium_voice")


def test_without_the_voice_provider_connected_premium_orders_use_the_standard_voice(premium, monkeypatch):
    _plan(premium, "premium")
    monkeypatch.delenv("ELEVENLABS_API_KEY")
    assert premium.order_from(premium.dub())["tts"] != "elevenlabs"


# ---------------------------------------------------------------------------------------------------------- translation
def _mt(env, plan_mt, tier):
    return env.app._pick_mt("sw", "en", plan_mt, tier)


def test_premium_translation_goes_to_claude_automatically_and_everyone_else_to_lara(env, monkeypatch):
    monkeypatch.setenv("LARA_ACCESS_KEY_ID", "id")
    monkeypatch.setenv("LARA_ACCESS_KEY_SECRET", "secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    provider_health._paused.clear()
    assert _mt(env, "claude", "ai") == ("claude", None) and _mt(env, "claude", "human") == ("claude", None)
    assert _mt(env, "local", "ai") == ("lara", None)                                       # Pro and the free plan keep the standard engine


def test_if_claude_is_down_off_or_not_connected_premium_translation_still_works_through_lara(env, monkeypatch):
    monkeypatch.setenv("LARA_ACCESS_KEY_ID", "id")
    monkeypatch.setenv("LARA_ACCESS_KEY_SECRET", "secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    provider_health._paused.clear()
    provider_health.mark_failed("claude", "429 quota exceeded")
    assert _mt(env, "claude", "ai") == ("lara", None)
    provider_health._paused.clear()
    env.db.set_provider_setting("claude", enabled=False)
    assert _mt(env, "claude", "ai") == ("lara", None)
    env.db.set_provider_setting("claude", enabled=True)
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert _mt(env, "claude", "ai") == ("lara", None)
    provider_health._paused.clear()


# --------------------------------------------------------------------------------------------------------------- wording
def test_the_plan_cards_use_the_agreed_wording(env):
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)
    html = env.client.get("/").text
    section = html.split('id="pz-plans"')[1].split("</section>")[0].split("<script>")[0]
    for old in ("Early access to new languages", "Invoicing and bank transfer", "A named contact at Kauli"):
        assert old not in section, old
    for new in ("All benefits in Free", "All benefits in Pro", "All benefits in Pro and Premium", "Realistic voices and high-quality translations",
                "Managed billing and invoicing", "Dedicated account manager"):
        assert new in section, new
    pay = html.split('class="pz-cards"')[1].split("</section>")[0].split('class="pz-fine"')[0]
    assert "Accurate AI transcription and translations" in pay and "Clearly labelled as machine-generated" not in pay
    assert "A dedicated editor checks it against your audio" in pay and "A real editor checks it against your audio" not in pay


def test_the_account_plans_page_uses_the_same_wording(env):
    page = env.client.get("/client/plans").text
    assert "Managed billing and invoicing" in page and "Dedicated account manager" in page and "All benefits in Pro and Premium" in page
    assert "Invoicing and bank transfer" not in page and "A named contact" not in page
    assert "All benefits in Free" in page and "All benefits in Pro" in page


def test_the_kenya_shilling_line_is_only_shown_to_visitors_in_kenya(env):
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)
    assert "In Kenya you can pay in KES with M-Pesa at checkout." not in env.client.get("/", headers={"cf-ipcountry": "US"}).text
    assert "In Kenya you can pay in KES with M-Pesa at checkout." not in env.client.get("/").text
    assert "In Kenya you can pay in KES with M-Pesa at checkout." in env.client.get("/", headers={"cf-ipcountry": "KE"}).text
