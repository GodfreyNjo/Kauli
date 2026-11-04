"""A client who arrived on a translated page keeps that language through sign-up and in their account.

The words live in webapp/ui_text.py. These tests guard the catalog (no language short of a key, no broken placeholder) and
the journey (cookie from the translated site, saved on the account, followed on every screen that is translated)."""
import re
import sys
from html import unescape
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from webapp import ui_text, locales  # noqa: E402

PLACEHOLDER = re.compile(r"\{(\w+)\}")


def test_every_language_has_every_key_and_the_same_placeholders():
    assert set(ui_text.CATALOG) == set(ui_text.LANGS) - {"en"}
    assert set(ui_text.CATALOG) == set(locales.LOCALES)                       # one account language per translated site
    for lang, words in ui_text.CATALOG.items():
        assert set(words) == set(ui_text.EN), (lang, sorted(set(ui_text.EN) ^ set(words))[:5])
        for key, text in words.items():
            assert text.strip(), (lang, key)
            assert set(PLACEHOLDER.findall(text)) == set(PLACEHOLDER.findall(ui_text.EN[key])), (lang, key)
            assert text != ui_text.EN[key] or key in ("minutes_n",) or len(text) < 14, (lang, key)       # nothing left in English


def test_every_t_call_in_the_templates_names_a_real_key():
    used = set()
    for path in Path(__file__).parent.parent.joinpath("webapp/templates").glob("*.html"):
        used |= set(re.findall(r"\bt\(\s*'([a-z0-9_]+)'", path.read_text(encoding="utf-8")))
    dynamic = {"svc_", "issue_"}                               # built in the template as 'svc_' ~ service level
    assert used - dynamic - set(ui_text.EN) == set(), used - dynamic - set(ui_text.EN)
    assert {"greet_morning", "greet_afternoon", "greet_evening", "svc_transcribe", "svc_translate", "svc_dub"} <= set(ui_text.EN)


def test_a_missing_key_or_language_falls_back_to_english():
    assert ui_text.translate("fr", "nav_orders") == "Mes commandes"
    assert ui_text.translate("xx", "nav_orders") == "Orders"
    assert ui_text.translate("fr", "no_such_key") == "no_such_key"
    assert ui_text.translate("de", "pay_btn", amount="$5.00") == "$5.00 bezahlen"
    assert ui_text.direction("ar") == "rtl" and ui_text.direction("fr") == "ltr"


def test_the_translated_site_hands_its_language_to_the_sign_in_page(env):
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)         # a visitor, not the signed-in stand-in
    r = env.client.get("/fr")
    assert "kauli_lang=fr" in r.headers.get("set-cookie", "")
    env.client.cookies.set("kauli_lang", "fr")
    page = unescape(env.client.get("/login?mode=signup").text)
    assert "Créer un compte gratuit" in page and 'lang="fr"' in page and "Create free account" not in page
    assert "Conditions d'utilisation" in page and "Au moins" in page


def test_the_sign_in_page_is_english_by_default_and_arabic_flips_direction(env):
    env.monkeypatch.setattr(env.app, "current_user", lambda request: None)
    env.client.cookies.clear()
    page = env.client.get("/login").text
    assert "Welcome back" in page and '<html lang="en"' in page and 'dir="rtl"' not in page
    env.client.cookies.set("kauli_lang", "ar")
    page = env.client.get("/login").text
    assert '<html lang="ar" dir="rtl"' in page and "مرحبًا بعودتك" in page


def test_the_dashboard_follows_the_language_on_the_account(env, wav_bytes):
    env.order_from(env.place(wav_bytes, review_tier="human"))
    env.db.set_ui_lang("u1", "es")
    home = env.client.get("/client/home").text
    assert '<html lang="es"' in home and "Subir archivos" in home and "Mis pedidos" in home and "Pendientes" in home
    assert "Pendiente de pago" in home and "Upload files" not in home and "Recent files" not in home
    pending = env.client.get("/client/pending").text
    assert "Pedidos pendientes" in pending and "Pagar ahora" in pending
    pay = env.client.get(f"/client/orders/{env.db.list_orders_for_client('u1')[0]['id']}/pay").text
    assert "Resumen del pedido" in pay and "Total a pagar" in pay and "Pagar $" in pay and "Order summary" not in pay


def test_chinese_dates_numbers_and_arabic_layout_on_the_dashboard(env, wav_bytes):
    env.order_from(env.place(wav_bytes))
    env.db.set_ui_lang("u1", "zh")
    home = env.client.get("/client/home").text
    assert '<html lang="zh-Hans"' in home and "上传文件" in home and re.search(r"\d+月\d+日 \d\d:\d\d", home)
    env.db.set_ui_lang("u1", "ar")
    assert '<html lang="ar" dir="rtl"' in env.client.get("/client/home").text


def test_the_switcher_saves_the_choice_for_every_device_and_cannot_be_abused(env):
    r = env.client.get("/lang/de?next=/client/home", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/client/home" and "kauli_lang=de" in r.headers["set-cookie"]
    assert env.db.get_user("u1")["ui_lang"] == "de"
    assert "Übersicht" in env.client.get("/client/home").text
    assert env.client.get("/lang/xx", follow_redirects=False).status_code == 404
    assert env.client.get("/lang/fr?next=https://evil.example/", follow_redirects=False).headers["location"] == "/"
    assert env.client.get("/lang/fr?next=//evil.example", follow_redirects=False).headers["location"] == "/"
    assert env.client.get("/lang/fr", follow_redirects=False).headers["x-robots-tag"] == "noindex, nofollow"
    env.client.get("/lang/en")
    assert ">Home<" in env.client.get("/client/home").text


def test_the_account_menu_lists_every_language(env):
    page = env.client.get("/client/home").text
    for name in ui_text.NATIVE.values():
        assert name in page
    assert "/lang/ko?next=/client/home" in page


def test_staff_screens_stay_in_english_whatever_the_cookie_says(env):
    request = type("R", (), {"cookies": {"kauli_lang": "fr"}})()
    assert env.app._ui_lang(request, {"role": "staff", "ui_lang": "fr"}) == "en"
    assert env.app._ui_lang(request, {"role": "client", "ui_lang": None}) == "fr"
    assert env.app._ui_lang(request, {"role": "client", "ui_lang": "de"}) == "de"
    assert env.app._ui_lang(request, None) == "fr"


def test_the_live_progress_panel_speaks_the_clients_language(env, wav_bytes):
    order = env.order_from(env.place(wav_bytes))
    conn = env.db.get_conn()
    conn.execute("UPDATE orders SET status = 'processing' WHERE id = ?", (order["id"],))
    conn.commit()
    conn.close()
    en = env.client.get(f"/client/orders/{order['id']}/progress").json()
    assert en["title"] == "Your transcript is being made" and en["steps"][0]["label"] == "Preparing your file"
    env.db.set_ui_lang("u1", "fr")
    fr = env.client.get(f"/client/orders/{order['id']}/progress").json()
    assert fr["title"] == "Nous préparons votre transcription" and fr["steps"][0]["label"] == "Préparation de votre fichier"
    assert fr["what"] == "transcript"                                          # the stable key is unchanged
    page = env.client.get(f"/client/orders/{order['id']}").text
    assert "Nous préparons votre transcription" in page and "Vous pouvez fermer cette page" in page


def test_signing_in_keeps_the_language_from_the_translated_site(env):
    from types import SimpleNamespace
    env.client.cookies.set("kauli_lang", "pt")
    session = SimpleNamespace(user=SimpleNamespace(id="u1", email="client@example.com"))
    request = SimpleNamespace(cookies={"kauli_lang": "pt"}, session={}, headers={}, client=None,
                              base_url="http://t/", state=SimpleNamespace())
    resp = env.app._complete_auth_session(request, session, "/client/home")
    assert env.db.get_user("u1")["ui_lang"] == "pt" and "kauli_lang=pt" in resp.headers["set-cookie"]
    request.cookies = {}
    env.app._complete_auth_session(request, session, "/client/home")
    assert env.db.get_user("u1")["ui_lang"] == "pt"                            # a new device with no cookie keeps the account's language
