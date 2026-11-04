"""Everything a script shows is in the reader's language too: the upload steps, "5 min ago", the assistant's greeting, the order summary and the
titles in the notification bell."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from webapp import js_strings, page_translate  # noqa: E402

STATIC = Path(__file__).parent.parent / "webapp" / "static"


def _fake_translate(strings, lang, budget=8.0):
    return {s: f"[{lang}] {s}" for s in strings if s.strip()}, True


def test_every_string_the_scripts_show_is_found_by_itself():
    found = js_strings.english_strings()
    for text in ("Checking your audio", "Working out your price", "Starting your order", "Upload complete", "Setting up your order",
                 "Hi {name}, how can we help?", "{n} min ago", "{n} h ago", "{n} d ago", "just now", "Uploading {n} files", "Sending file {i} of {n}",
                 "Instant AI", "Human-reviewed", "Transcription only", "Where is my latest order?", "Dismiss message", "Live demo"):
        assert text in found, text


def test_the_scripts_never_write_those_words_without_asking_for_a_translation():
    wizard = (STATIC / "client_wizard.js").read_text(encoding="utf-8")
    for text in ("Checking your audio", "Working out your price", "Upload complete", "Setting up your order"):
        assert all(f'kT("{text}"' in line or text not in line for line in wizard.splitlines()), text
    assert 'kT("Instant AI")' in wizard and 'kT(svc.dataset.name)' in wizard                 # the order summary names the service in the reader's language
    assistant = (STATIC / "assistant.js").read_text(encoding="utf-8")
    assert 'kT("Hi {name}, how can we help?"' in assistant and 'kT("{n} min ago"' in assistant
    base = (Path(__file__).parent.parent / "webapp" / "templates" / "base.html").read_text(encoding="utf-8")
    assert 'window.kauliT("{n} min ago"' in base and "window.KAULI_I18N" in base


def test_english_readers_get_nothing_extra_and_other_readers_get_the_translations(env, monkeypatch):
    assert js_strings.for_lang("en") == {}
    monkeypatch.setattr(page_translate, "translate_many", _fake_translate)
    hindi = js_strings.for_lang("hi")
    assert hindi["Checking your audio"] == "[hi] Checking your audio" and hindi["{n} min ago"] == "[hi] {n} min ago"


def test_the_page_hands_the_scripts_the_readers_language(env, monkeypatch):
    monkeypatch.setattr(page_translate, "translate_many", _fake_translate)
    monkeypatch.setattr(page_translate, "configured", lambda: True)
    monkeypatch.setattr(page_translate, "localize_html", lambda html, lang: (html, True))        # the page itself is not under test
    page = env.client.get("/client/home", cookies={"kauli_lang": "hi"}).text
    assert "window.KAULI_I18N" in page and "[hi] Checking your audio" in page and "window.kauliT = function" in page
    english = env.client.get("/client/home", cookies={"kauli_lang": "en"}).text
    assert "[hi]" not in english and "window.KAULI_I18N = {}" in english


def test_the_gaps_in_a_sentence_are_kept_for_the_translator():
    fenced = page_translate._fence("Sending file {i} of {n}")
    assert fenced.count('class="notranslate"') == 2 and "{i}" in fenced and "{n}" in fenced
    assert page_translate._unfence(fenced) == "Sending file {i} of {n}"


def test_notification_titles_are_translated_with_the_file_name_kept(env, monkeypatch):
    seen = []

    def fake(strings, lang, budget=8.0):
        seen.extend(strings)
        return {s: ("{name} तैयार है" if s == "{name} is ready" else f"[{lang}] {s}") for s in strings}, True

    monkeypatch.setattr(page_translate, "translate_many", fake)
    monkeypatch.setattr(page_translate, "configured", lambda: True)
    out = env.app._notification_titles(["kauli-guides-ep01.mp3 is ready", "The Kauli team replied to your question"], "hi")
    assert out["kauli-guides-ep01.mp3 is ready"] == "kauli-guides-ep01.mp3 तैयार है"                # the name is never sent to the translator
    assert out["The Kauli team replied to your question"] == "[hi] The Kauli team replied to your question"
    assert not any("kauli-guides" in s for s in seen)
    assert env.app._notification_titles(["x is ready"], "en") == {}
