"""Transkriptor keys are tried in turn (primary, backup, second backup) before falling back to local Whisper."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from kauli.providers import asr as asr_mod  # noqa: E402


@pytest.fixture
def provider(monkeypatch):
    for name in ("TRANSKRIPTOR_API_KEY", "TRANSKRIPTOR_API_KEY_BACKUP", "TRANSKRIPTOR_API_KEY_BACKUP_2"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TRANSKRIPTOR_API_KEY", "k1")
    monkeypatch.setenv("TRANSKRIPTOR_API_KEY_BACKUP", "k2")
    monkeypatch.setenv("TRANSKRIPTOR_API_KEY_BACKUP_2", "k3")
    monkeypatch.setattr(asr_mod.FasterWhisperASR, "transcribe", lambda self, path, language="sw": ["whisper"])
    return asr_mod.TranskriptorASR()


def _fake(monkeypatch, working):
    calls = []

    def run(self, path, language, key):
        calls.append(key)
        if key not in working:
            raise RuntimeError(f"{key} refused")
        return [f"ok-{key}"]
    monkeypatch.setattr(asr_mod.TranskriptorASR, "_transcribe_via_api", run)
    return calls


def test_the_primary_key_is_used_first_and_alone(provider, monkeypatch):
    calls = _fake(monkeypatch, {"k1", "k2", "k3"})
    assert provider.transcribe("a.wav") == ["ok-k1"] and calls == ["k1"] and not provider.backup_key_used and not provider.fallback_used


def test_the_second_backup_is_the_last_stop_before_whisper(provider, monkeypatch):
    calls = _fake(monkeypatch, {"k3"})
    assert provider.transcribe("a.wav") == ["ok-k3"] and calls == ["k1", "k2", "k3"]
    assert provider.backup_key_used and "second backup" in provider.backup_key_note and "Primary" in provider.backup_key_note
    assert not provider.fallback_used


def test_when_every_key_fails_it_falls_back_to_whisper_and_says_why(provider, monkeypatch):
    _fake(monkeypatch, set())
    assert provider.transcribe("a.wav") == ["whisper"]
    assert provider.fallback_used and "every key" in provider.fallback_reason and "faster-whisper" in provider.fallback_reason


def test_with_no_keys_at_all_it_goes_straight_to_whisper(monkeypatch):
    for name in ("TRANSKRIPTOR_API_KEY", "TRANSKRIPTOR_API_KEY_BACKUP", "TRANSKRIPTOR_API_KEY_BACKUP_2"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(asr_mod.FasterWhisperASR, "transcribe", lambda self, path, language="sw": ["whisper"])
    p = asr_mod.TranskriptorASR()
    assert p.transcribe("a.wav") == ["whisper"] and "no API key configured" in p.fallback_reason
