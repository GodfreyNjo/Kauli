"""A customer's own names and terms are given to the speech recognizer, so it hears them right instead of only being corrected afterwards."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env  # noqa: E402,F401
from kauli.providers import asr, asr_cloud  # noqa: E402
from webapp import account_hub  # noqa: E402


def test_hints_are_cleaned_and_capped():
    p = asr.ASRProvider()
    p.set_hints(["Kauli", " ", "Forge Media", None] + [f"t{i}" for i in range(100)])
    assert p.hint_terms[:2] == ["Kauli", "Forge Media"] and len(p.hint_terms) == 60
    assert p._hint_text().startswith("Kauli, Forge Media")


def test_whisper_gets_the_terms_as_hotwords_and_nothing_extra_without_them(monkeypatch):
    seen = {}

    class Model:
        def transcribe(self, path, **kw):
            seen.update(kw)
            return iter([]), None

    w = asr.FasterWhisperASR()
    w._model = Model()
    w.transcribe("x.wav", language="en")
    assert "hotwords" not in seen
    w.set_hints(["Kauli", "Forge Media"])
    w.transcribe("x.wav", language="en")
    assert seen["hotwords"] == "Kauli, Forge Media"


def test_groq_gets_the_terms_as_a_prompt(monkeypatch):
    sent = {}

    class Resp:
        status_code = 200

        def json(self):
            return {"segments": [], "words": []}

    def fake_post(url, **kw):
        sent.update(kw.get("data", {}))
        return Resp()

    monkeypatch.setattr(asr_cloud.httpx, "post", fake_post)
    monkeypatch.setattr(asr_cloud, "prepare_chunks", lambda path, tmp, chunk_s: [(str(Path(__file__)), 0)])
    g = asr_cloud.GroqWhisperASR(api_key="test")
    g.transcribe("x.wav", language="en")
    assert "prompt" not in sent
    g.set_hints(["Kauli"])
    g.transcribe("x.wav", language="en")
    assert sent["prompt"] == "Names and terms: Kauli"


def test_a_chain_hands_the_terms_to_each_provider_it_tries(monkeypatch):
    got = []

    class Fake(asr.ASRProvider):
        name = "fake"

        def transcribe(self, audio_path, language="sw"):
            got.append(list(self.hint_terms))
            return []

    monkeypatch.setattr(asr_cloud, "get_asr", lambda name, **kw: Fake())
    chain = asr_cloud.ChainASR(["fake"])
    chain.set_hints(["Kauli"])
    chain.transcribe("x.wav", language="en")
    assert got == [["Kauli"]]


def test_a_customers_vocabulary_becomes_hints_for_the_recognizer(env):
    conn = env.db.get_conn()
    conn.execute("INSERT INTO vocab_terms (id, client_id, term, variants, created_at) VALUES ('v1', 'u1', 'Kauli', 'kohli, cowley', 1)")
    conn.commit()
    conn.close()
    fix = account_hub.vocabulary_fixer("u1")
    assert fix.terms == ["Kauli"]
    assert fix("Start at kohli forgemedia and Cowley.") == "Start at Kauli forgemedia and Kauli."
    assert account_hub.vocabulary_fixer("nobody") is None
