"""Lara Translate as the translation engine: language-code coverage, cost
tracking, and which engine an order gets (inert until Lara's keys are set)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from kauli.providers.asr import TRANSKRIPTOR_LANGUAGES  # noqa: E402
from kauli.providers.mt import LaraMT  # noqa: E402


def test_lara_codes_cover_every_language_we_offer():
    for code in list(TRANSKRIPTOR_LANGUAGES) + ["sw", "en", "ki"]:
        assert code in LaraMT.LANG_CODES, code
    assert LaraMT.LANG_CODES["ki"] == "ki-KE"      # Kikuyu is supported by Lara
    assert LaraMT.LANG_CODES["es"] == "es-419"     # Latin-American Spanish for a US audience
    assert LaraMT.LANG_CODES["mn"] == "khk-MN"     # Lara's code for Halh Mongolian


def test_lara_tracks_cost_per_source_character():
    class _Res:
        translation = "Hello world"

    class _Client:
        def translate(self, text, source, target):
            self.seen = (source, target)
            return _Res()

    lara = LaraMT()
    lara._client = _Client()
    out = lara.translate("a" * 1000, 0, source_lang="zh", target_lang="en")
    assert lara._client.seen == ("zh-CN", "en-US")
    assert out["spoken"] == "Hello world"
    assert lara.total_chars_used == 1000
    assert abs(lara.total_cost_usd - 0.025) < 1e-9  # $25 per 1M chars


def _clear(monkeypatch):
    for key in ("LARA_ACCESS_KEY_ID", "LARA_ACCESS_KEY_SECRET", "ANTHROPIC_API_KEY", "AZURE_TRANSLATOR_KEY"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def app_module():
    pytest.importorskip("fastapi")
    import os
    # Importing the web app loads the project's real .env into os.environ
    # (real API keys). Put the environment back afterwards so it can't leak
    # into other tests - e.g. one that asserts "no Transkriptor key".
    before = dict(os.environ)
    from webapp import app as app_mod
    for key in set(os.environ) - set(before):
        del os.environ[key]
    os.environ.update(before)
    return app_mod


def test_without_lara_nothing_changes(monkeypatch, app_module):
    _clear(monkeypatch)
    assert app_module._pick_mt("sw", "en", "local", "human") == ("local", None)
    mt, err = app_module._pick_mt("zh", "en", "local", "ai")
    assert mt is None and err                      # no provider connected -> refused, never garbage
    mt, err = app_module._pick_mt("ki", "en", "local", "human")
    assert mt is None and "Kikuyu" in err
    monkeypatch.setenv("AZURE_TRANSLATOR_KEY", "x")
    assert app_module._pick_mt("sw", "es", "local", "ai") == ("azure-translate", None)


def test_with_lara_it_is_the_engine(monkeypatch, app_module):
    _clear(monkeypatch)
    monkeypatch.setenv("LARA_ACCESS_KEY_ID", "id")
    monkeypatch.setenv("LARA_ACCESS_KEY_SECRET", "secret")
    assert app_module._pick_mt("zh", "en", "local", "ai") == ("lara", None)
    assert app_module._pick_mt("sw", "en", "local", "human") == ("lara", None)
    assert app_module._pick_mt("en", "sw", "local", "ai") == ("lara", None)
    # a priority plan's Claude upgrade on a human-reviewed order is kept
    assert app_module._pick_mt("sw", "en", "claude", "human") == ("claude", None)
    # ...and an Instant AI order uses Lara, unless the plan includes Claude and Claude is connected (see test_plan_allocations.py)
    assert app_module._pick_mt("sw", "en", "claude", "ai") == ("lara", None)
    # Kikuyu: Claude if connected, otherwise Lara (ki-KE) - both then human-reviewed
    assert app_module._pick_mt("ki", "en", "local", "human") == ("lara", None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert app_module._pick_mt("ki", "en", "local", "human") == ("claude", None)
