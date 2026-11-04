"""The editor can pick the provider for a redo (transcription, translation or dubbing), staff pin defaults on the Providers page, and a redo
touches only the chosen section."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from kauli.models import Job, Segment  # noqa: E402
from webapp import provider_health  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    provider_health._paused.clear()
    yield
    provider_health._paused.clear()


def _staff(env):
    env.db.get_or_create_user("s1", "staff@example.com", "staff")
    env.monkeypatch.setattr(env.app, "current_user", lambda request: {**dict(env.db.get_user("s1")), "client_scope_id": "s1"})


def _job():
    return Job(segments=[
        Segment(segment_id="s0", index=0, start_ms=0, end_ms=3000, source_transcript="Hello there everyone."),
        Segment(segment_id="s1", index=1, start_ms=3000, end_ms=6000, source_transcript="This part was misheard."),
        Segment(segment_id="s2", index=2, start_ms=6000, end_ms=9000, source_transcript="And a closing remark."),
    ], providers={"asr": "own", "mt": "lara", "tts": "piper"}, source_duration_ms=9000)


def _order(env, wav_bytes, **cols):
    o = env.order_from(env.place(wav_bytes))
    Path(o["outdir"]).mkdir(parents=True, exist_ok=True)
    _job().save(str(Path(o["outdir"]) / "manifest.json"))
    for kind, name in cols.items():
        env.db.set_order_provider(o["id"], kind, name)
    return env.db.get_order(o["id"])


def _keys(env, **keys):
    for k, v in keys.items():
        env.monkeypatch.setenv(k, v)


# ---------------------------------------------------------------------------------------------- the defaults
def test_only_staff_pin_a_default_and_only_a_provider_that_belongs_to_the_step(env):
    assert env.client.post("/staff/providers/default", data={"kind": "mt", "name": "lara"}, follow_redirects=False).status_code in (302, 303, 307)
    assert provider_health.default_name("mt") == provider_health.FACTORY_DEFAULTS["mt"]            # a customer changed nothing: still the house choice
    _staff(env)
    assert "error=" in env.client.post("/staff/providers/default", data={"kind": "mt", "name": "groq-whisper"}, follow_redirects=False).headers["location"]
    assert "error=" in env.client.post("/staff/providers/default", data={"kind": "nope", "name": "lara"}, follow_redirects=False).headers["location"]
    r = env.client.post("/staff/providers/default", data={"kind": "mt", "name": "azure-translate"}, follow_redirects=False)
    assert r.status_code == 303 and provider_health.default_name("mt") == "azure-translate"
    assert "Default providers" in env.client.get("/staff/providers").text and 'value="azure-translate" selected' in env.client.get("/staff/providers").text
    env.client.post("/staff/providers/default", data={"kind": "mt", "name": ""})
    assert provider_health.default_name("mt") == ""                                   # back to automatic


def test_a_pinned_default_steers_new_orders_only_when_it_can_take_the_job(env):
    _staff(env)
    env.client.post("/staff/providers/default", data={"kind": "mt", "name": "azure-translate"})
    env.client.post("/staff/providers/default", data={"kind": "asr", "name": "groq-whisper"})
    env.monkeypatch.delenv("AZURE_TRANSLATOR_KEY", raising=False)
    env.monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert provider_health.default_for("mt", "en", "de") is None and provider_health.default_for("asr", "en", "en") is None      # not connected: automatic carries on
    assert env.app._route_asr("transkriptor", source_lang="en", free_covered=False) != "groq-whisper"
    _keys(env, AZURE_TRANSLATOR_KEY="k", GROQ_API_KEY="g")
    assert env.app._pick_mt("en", "de", "local", "ai") == ("azure-translate", None)
    assert env.app._route_asr("transkriptor", source_lang="en", free_covered=False) == "groq-whisper"
    env.db.set_provider_setting("azure-translate", enabled=False)
    assert provider_health.default_for("mt", "en", "de") is None                         # switched off: not used even though pinned


# ---------------------------------------------------------------------------------------------- the popup's list
def test_the_popup_lists_providers_with_cost_default_and_why_one_cannot_be_chosen(env, wav_bytes):
    o = _order(env, wav_bytes, asr="own", mt="lara", tts="piper")
    assert env.client.get(f"/staff/orders/{o['id']}/provider-options?kind=asr").status_code == 401         # a customer cannot see it
    _staff(env)
    _keys(env, GROQ_API_KEY="g")
    env.monkeypatch.delenv("ASSEMBLYAI_API_KEY", raising=False)
    env.client.post("/staff/providers/default", data={"kind": "asr", "name": "groq-whisper"})
    data = env.client.get(f"/staff/orders/{o['id']}/provider-options?kind=asr").json()
    rows = {r["name"]: r for r in data["options"]}
    assert data["title"] == "Transcription" and data["default"] == "groq-whisper" and data["current"] == "own" and data["available"]
    assert rows["groq-whisper"]["default"] and rows["groq-whisper"]["selectable"] and rows["groq-whisper"]["cost_per_min"] == provider_health.COST_DEFAULTS["groq-whisper"]
    assert rows["own"]["current"] and not rows["assemblyai"]["selectable"] and rows["assemblyai"]["why"] == "Not connected on this server"
    assert env.client.get(f"/staff/orders/{o['id']}/provider-options?kind=bogus").status_code == 400
    assert [r["name"] for r in env.client.get(f"/staff/orders/{o['id']}/provider-options?kind=tts").json()["options"]]       # dubbing options exist for a dub


# ---------------------------------------------------------------------------------------------- redo one section with a chosen provider
class _FakeASR:
    def __init__(self, name):
        self.name = name

    def transcribe(self, path, language=None):
        word = SimpleNamespace(text="corrected", start_ms=0, end_ms=500, confidence=0.99)
        return [SimpleNamespace(words=[word], source_transcript=f"fixed by {self.name}", source_confidence=0.97)]


def test_a_section_is_re_transcribed_with_the_chosen_provider_and_not_with_a_disallowed_one(env, wav_bytes):
    o = _order(env, wav_bytes, asr="own", mt="stub", tts="stub")
    _staff(env)
    used = []
    env.monkeypatch.setattr(env.app, "get_asr", lambda name, **kw: (used.append(name), _FakeASR(name))[1])
    env.monkeypatch.setattr(env.app, "extract_audio_window", lambda *a, **k: True)
    url = f"/staff/orders/{o['id']}/segments/s1/retranscribe"
    bad = env.client.post(url, json={"provider": "assemblyai"})                          # not connected here
    assert bad.status_code == 400 and "not connected" in bad.json()["error"] and not used
    _keys(env, GROQ_API_KEY="g")
    ok = env.client.post(url, json={"provider": "groq-whisper"})
    assert ok.status_code == 200 and used == ["groq-whisper"] and ok.json()["source_final_text"] == "fixed by groq-whisper"
    again = env.client.post(url)                                                         # no body: the order's own provider, as before
    assert again.status_code == 200 and used[-1] == "own"
    from webapp import db as _db  # noqa: F401
    job = env.app._load_job(env.db.get_order(o["id"]))
    texts = {s.segment_id: s.source_final_text for s in job.segments}
    assert texts["s0"] == "Hello there everyone." and texts["s2"] == "And a closing remark."       # the neighbours were not touched
    conn = env.db.get_conn()
    logged = [r["detail"] for r in conn.execute("SELECT detail FROM security_events WHERE kind = 'editor_provider_redo'").fetchall()]
    conn.close()
    assert any("groq-whisper" in d and "s1" in d for d in logged)


def test_a_section_is_re_translated_with_the_chosen_provider(env, wav_bytes):
    o = _order(env, wav_bytes, asr="own", mt="lara", tts="stub")
    _staff(env)
    seen = []

    def fake_resync(order, job, seg, mt_provider=None):
        seen.append(getattr(mt_provider, "name", None) or "order-default")
        seg.literal = seg.spoken = "retranslated"
        seg.translation_stale = False

    env.monkeypatch.setattr(env.app, "_retranslate_and_resync", fake_resync)
    env.monkeypatch.setattr(env.app, "get_mt", lambda name, **kw: SimpleNamespace(name=name))
    url = f"/staff/orders/{o['id']}/segments/s1/retranslate"
    assert env.client.post(url, json={"provider": "claude"}).status_code == 400                                    # no key here
    _keys(env, ANTHROPIC_API_KEY="a")
    r = env.client.post(url, json={"provider": "claude"})
    assert r.status_code == 200 and seen == ["claude"]
    assert env.client.post(url).status_code == 200 and seen[-1] == "order-default"


# ---------------------------------------------------------------------------------------------- dubbing
def test_dubbing_is_redone_for_the_chosen_segments_only_and_the_whole_file_moves_the_order(env, wav_bytes):
    o = _order(env, wav_bytes, asr="own", mt="lara", tts="piper")
    _staff(env)
    rendered, made = [], []
    env.monkeypatch.setattr(env.app, "get_tts", lambda name, **kw: SimpleNamespace(name=name, sample_rate=16000))
    env.monkeypatch.setattr(env.app, "_render_segment_audio", lambda order, job, seg, prov, voice: rendered.append((seg.segment_id, prov.name)))
    env.monkeypatch.setattr(env.app, "build_timeline", lambda *a, **k: made.append("timeline") or [0.0])
    env.monkeypatch.setattr(env.app, "write_wav_mono", lambda *a, **k: None)
    url = f"/staff/orders/{o['id']}/redub"
    assert env.client.post(url, json={"provider": "azure", "segment_ids": ["s1"]}).status_code == 400            # azure is not connected here
    _keys(env, AZURE_SPEECH_KEY="k")
    assert env.client.post(url, json={"provider": "azure"}).status_code == 400                                   # nothing chosen
    r = env.client.post(url, json={"provider": "azure", "segment_ids": ["s1"]})
    assert r.status_code == 200 and rendered == [("s1", "azure")] and r.json()["redone"] == 1
    assert env.db.get_order(o["id"])["tts"] == "piper"                                                           # a section redo leaves the order on its provider
    rendered.clear()
    r = env.client.post(url, json={"provider": "azure", "all": True})
    assert r.status_code == 200 and {s for s, _ in rendered} == {"s0", "s1", "s2"}
    assert env.db.get_order(o["id"])["tts"] == "azure"                                                           # the whole file moves the order


def test_the_editor_can_move_the_whole_order_to_a_provider_and_only_a_valid_one(env, wav_bytes):
    o = _order(env, wav_bytes, asr="own", mt="lara", tts="stub")
    _staff(env)
    url = f"/staff/orders/{o['id']}/use-provider"
    assert env.client.post(url, json={"kind": "asr", "provider": "assemblyai"}).status_code == 400
    _keys(env, GROQ_API_KEY="g")
    assert env.client.post(url, json={"kind": "asr", "provider": "groq-whisper"}).status_code == 200
    assert env.db.get_order(o["id"])["asr"] == "groq-whisper"
    assert env.client.post(url, json={"kind": "bogus", "provider": "x"}).status_code == 400


def test_the_editor_page_has_the_provider_popup_and_the_shortcuts_open_it(env, wav_bytes):
    o = _order(env, wav_bytes, asr="own", mt="lara", tts="piper")
    _staff(env)
    page = env.client.get(f"/staff/orders/{o['id']}/editor").text
    assert 'id="provider-redo-modal"' in page and "Redo with a provider" in page
    js = env.client.get("/static/editor.js").text
    assert "openProviderRedo(" in js and "provider-options" in js and "/redub" in js and "intersectsNode" in js



# ---------------------------------------------------------------------------------------------- sound tags are translated in the editor too
def test_the_editors_retranslate_translates_a_sound_tag_in_a_silent_stretch(env, wav_bytes):
    o = _order(env, wav_bytes, asr="own", mt="lara", tts="stub")
    gap = Segment(segment_id="g1", index=3, start_ms=9000, end_ms=12000, segment_type="gap", source_edited_transcript="[MUSIC PLAYING]")

    class _Tags:
        def translate(self, text, target_chars, source_lang, target_lang):
            out = {"MUSIC PLAYING": "MUSIK SPIELT"}.get(text.strip(), text)
            return {"literal": out, "spoken": out, "confidence": 0.9}

    order = dict(env.db.get_order(o["id"]))
    order["source_lang"], order["target_lang"] = "en", "de"
    env.app._retranslate_and_resync(order, Job(segments=[gap]), gap, mt_provider=_Tags())
    assert gap.final_text == "[MUSIK SPIELT]" and gap.translation_stale is False
    same = dict(order, mt="stub")
    keep = Segment(segment_id="g2", index=4, start_ms=0, end_ms=1000, segment_type="gap", source_edited_transcript="[MUSIC PLAYING]")
    env.app._retranslate_and_resync(same, Job(segments=[keep]), keep, mt_provider=_Tags())
    assert keep.final_text == "[MUSIC PLAYING]"                                                               # a transcript has no translation step: the tag stays



# ---------------------------------------------------------------------------------------------- the popup offers only the steps this job has
def _kinds(env, o):
    page = env.client.get(f"/staff/orders/{o['id']}/editor").text
    return page.split('id="provider-redo-modal" data-kinds="')[1].split('"')[0], page


def test_the_popup_only_offers_the_steps_the_job_has(env, wav_bytes):
    o = _order(env, wav_bytes, asr="own", mt="stub", tts="stub")                              # a transcript
    _staff(env)

    def with_columns(**cols):
        for kind, name in cols.items():
            env.db.set_order_provider(o["id"], kind, name)
        return _kinds(env, o)

    kinds, page = with_columns()
    assert kinds == "asr" and 'id="pr-kinds" hidden' in page                                  # one step: no chooser at all
    assert with_columns(mt="lara")[0] == "asr,mt"                                             # subtitles
    kinds, page = with_columns(asr="stub")                                                    # translated from a transcript the customer already had
    assert kinds == "mt" and 'value="asr"' not in page.split('id="pr-kinds"')[1].split("</fieldset>")[0]
    assert env.client.get(f"/staff/orders/{o['id']}/provider-options?kind=asr").json()["available"] is False     # nothing to redo there either
    kinds, page = with_columns(asr="own", tts="piper")                                        # a dub
    assert kinds == "asr,mt,tts" and page.count('name="pr-kind"') == 3
    js = env.client.get("/static/editor.js").text
    assert "has no " in js and "is shown." in js and "dataset.kinds" in js                    # a shortcut for a step the job lacks opens the one it has
