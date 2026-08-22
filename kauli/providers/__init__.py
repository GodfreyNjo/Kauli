"""Swappable providers: each stage (ASR, MT, TTS) has a free local option and
a paid cloud option, selected by name via get_asr/get_mt/get_tts."""
from __future__ import annotations

from .asr import get_asr, ASRProvider
from .mt import get_mt, MTProvider
from .tts import get_tts, TTSProvider

__all__ = ["get_asr", "get_mt", "get_tts", "ASRProvider", "MTProvider", "TTSProvider"]
