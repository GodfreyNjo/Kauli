"""Kauli's own translation model: how it is asked, when an order falls back to it, and that staff can switch a failed order to it."""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from kauli.providers import mt  # noqa: E402
from test_order_flow import _set_lara, env, wav_bytes  # noqa: E402,F401
from webapp import provider_health  # noqa: E402


class FakeSP:
    def encode(self, text, out_type=str):
        return text.split(" ")

    def decode(self, pieces):
        return " ".join(pieces)


class FakeTranslator:
    def __init__(self):
        self.calls = []

    def translate_batch(self, batch, **kw):
        self.calls.append((batch, kw))
        return [SimpleNamespace(hypotheses=[["Hello", "there"]])]


def _fake_model(monkeypatch):
    t = FakeTranslator()
    monkeypatch.setattr(mt.MadladMT, "_load", classmethod(lambda cls: (t, FakeSP())))
    import threading
    monkeypatch.setattr(mt.MadladMT, "_lock", threading.Lock())
    return t


def test_the_text_is_tagged_with_the_target_language_and_the_answer_is_one_translation(monkeypatch):
    t = _fake_model(monkeypatch)
    out = mt.MadladMT().translate("Habari yako", 20, source_lang="sw", target_lang="en")
    batch, kw = t.calls[0]
    assert batch[0][0] == "<2en>" and batch[0][-1] == "</s>" and kw["beam_size"] == 4
    assert out["literal"] == "Hello there" and out["spoken"] == out["literal"] and out["shorter"] is None and out["flags"] == []
    assert mt.MadladMT().translate("   ", 5)["flags"] == ["unclear"] and len(t.calls) == 1        # nothing to translate: the model is not asked


def test_the_model_is_only_available_when_the_converted_folder_is_there(monkeypatch, tmp_path):
    monkeypatch.setenv("KAULI_MADLAD_DIR", str(tmp_path))
    assert not mt.MadladMT.available()
    (tmp_path / "model.bin").write_bytes(b"x")
    assert not mt.MadladMT.available()                                                          # the tokenizer is missing too
    (tmp_path / "spiece.model").write_bytes(b"x")
    assert mt.MadladMT.available()


def test_a_failed_paid_engine_falls_back_to_azure_first_then_to_our_own_model(monkeypatch):
    monkeypatch.setenv("LARA_ACCESS_KEY_ID", "i")
    monkeypatch.setenv("LARA_ACCESS_KEY_SECRET", "s")
    monkeypatch.setattr(mt.MadladMT, "available", classmethod(lambda cls: True))
    monkeypatch.delenv("AZURE_TRANSLATOR_KEY", raising=False)
    assert mt.get_mt("lara").fallback_name == "madlad"
    monkeypatch.setenv("AZURE_TRANSLATOR_KEY", "k")
    assert mt.get_mt("lara").fallback_name == "azure-translate"                                  # a connected paid engine still comes first
    monkeypatch.delenv("AZURE_TRANSLATOR_KEY")
    monkeypatch.setattr(mt.MadladMT, "available", classmethod(lambda cls: False))
    assert not isinstance(mt.get_mt("lara"), mt.ResilientMT)                                     # nothing to fall back to: unchanged


def test_when_lara_runs_out_of_quota_the_order_finishes_on_our_own_model(monkeypatch):
    monkeypatch.setenv("LARA_ACCESS_KEY_ID", "i")
    monkeypatch.setenv("LARA_ACCESS_KEY_SECRET", "s")
    monkeypatch.delenv("AZURE_TRANSLATOR_KEY", raising=False)
    monkeypatch.setattr(mt.MadladMT, "available", classmethod(lambda cls: True))
    _fake_model(monkeypatch)

    def boom(self, *a, **k):
        raise RuntimeError('(HTTP 429) ApiQuotaExceededError: You have exceeded your "api_translation_chars" quota')
    monkeypatch.setattr(mt.LaraMT, "translate", boom)
    provider = mt.get_mt("lara")
    assert provider.translate("Habari yako", 20)["literal"] == "Hello there"
    assert provider.fallback_used and "madlad" in provider.fallback_reason and "api_translation_chars" in provider.fallback_reason


def test_staff_can_switch_a_failed_order_to_our_own_model_once_it_is_installed(env, wav_bytes, monkeypatch):
    _set_lara(env)
    order = env.order_from(env.place(wav_bytes, source_lang="sw", service_level="translate", review_tier="human"))
    env.db.set_processing_stage(order["id"], "translate")
    env.db.update_order_status(order["id"], "dead_letter", error="(HTTP 429) ApiQuotaExceededError: api_translation_chars quota")
    order = env.db.get_order(order["id"])
    monkeypatch.setattr(mt.MadladMT, "available", classmethod(lambda cls: False))
    assert provider_health.valid_switch(order, "mt", "madlad") == "Kauli's own translation model is not connected on this server."
    monkeypatch.setattr(mt.MadladMT, "available", classmethod(lambda cls: True))
    assert provider_health.valid_switch(order, "mt", "madlad") is None
    assert any(o["name"] == "madlad" and o["connected"] for o in provider_health.alternatives(order, "mt"))
    assert provider_health.diagnose(env.db.get_order(order["id"]))["provider"] == "lara"
