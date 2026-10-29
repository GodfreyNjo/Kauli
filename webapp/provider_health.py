"""Which outside provider an order failed on, what else could do the job, and what each provider has left.

Staff see this on a failed order: the provider named in the error, why it failed (out of quota, bad key, down), the other providers that
are connected and able to take this order's languages, and a one-click switch that points the order at one of them and retries it.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import httpx

# Which pipeline step each stage the worker reports belongs to.
STAGE_KIND = {"clean": "cleanup", "transcribe": "asr", "translate": "mt", "voice": "tts", "mix": "tts"}

# Words in an error that point at a provider, most specific first.
_SIGNATURES = (
    ("lara", ("api_translation_chars", "lara", "translated.com")),
    ("azure-translate", ("api.cognitive.microsofttranslator", "azure translator")),
    ("azure", ("speechsdk", "azure speech", "cognitiveservices.speech")),
    ("claude", ("anthropic", "claude")),
    ("aws-translate", ("aws translate", "botocore")),
    ("transkriptor", ("transkriptor",)),
    ("groq-whisper", ("groq",)),
    ("assemblyai", ("assemblyai",)),
    ("elevenlabs-scribe", ("elevenlabs-scribe", "scribe v")),
    ("elevenlabs", ("elevenlabs", "api.elevenlabs.io")),
    ("piper", ("piper",)),
    ("faster-whisper", ("whisper", "ctranslate2")),
)

NAMES = {
    "lara": "Lara Translate", "azure-translate": "Azure Translator", "claude": "Claude", "aws-translate": "AWS Translate",
    "local": "Local translation model", "stub": "None", "transkriptor": "Transkriptor", "faster-whisper": "Whisper (our own server)",
    "groq-whisper": "Groq Whisper", "assemblyai": "AssemblyAI", "elevenlabs-scribe": "ElevenLabs Scribe", "manual": "Typed by hand",
    "azure": "Azure Speech", "piper": "Piper (our own server)", "elevenlabs": "ElevenLabs", "xtts": "XTTS", "mms": "MMS",
}

# order column each kind of provider is stored in
COLUMN = {"asr": "asr", "mt": "mt", "tts": "tts"}


def _quota_reason(text: str) -> str | None:
    low = text.lower()
    if any(w in low for w in ("quota", "429", "rate limit", "credits", "exceeded", "plan limit")):
        return "out of quota or rate-limited"
    if any(w in low for w in ("401", "403", "unauthor", "invalid api key", "forbidden")):
        return "key rejected or not allowed"
    if any(w in low for w in ("timed out", "timeout", "connection", "502", "503", "504")):
        return "unreachable or timing out"
    return None


def _env(*names: str) -> bool:
    return any(os.environ.get(n) for n in names)


def configured(name: str) -> bool:
    """Whether the provider has what it needs to run here."""
    if name == "lara":
        return _env("LARA_ACCESS_KEY_ID") and _env("LARA_ACCESS_KEY_SECRET")
    if name == "azure-translate":
        return _env("AZURE_TRANSLATOR_KEY")
    if name == "claude":
        return _env("ANTHROPIC_API_KEY")
    if name == "aws-translate":
        return _env("AWS_ACCESS_KEY_ID")
    if name == "transkriptor":
        return _env("TRANSKRIPTOR_API_KEY", "TRANSKRIPTOR_API_KEY_BACKUP", "TRANSKRIPTOR_API_KEY_BACKUP_2")
    if name == "groq-whisper":
        return _env("GROQ_API_KEY")
    if name == "assemblyai":
        return _env("ASSEMBLYAI_API_KEY")
    if name in ("elevenlabs", "elevenlabs-scribe"):
        return _env("ELEVENLABS_API_KEY")
    if name == "azure":
        return _env("AZURE_SPEECH_KEY")
    if name == "piper":
        try:
            from kauli.providers.tts import PIPER_VOICES
            return any(Path(v["path"]).exists() for v in PIPER_VOICES.values())
        except Exception:  # noqa: BLE001
            return False
    return name in ("faster-whisper", "local", "stub")


def diagnose(order) -> dict | None:
    """What failed on this order: the step, the provider the error names (or the one the order was set to use for that step), why."""
    if order["status"] not in ("dead_letter", "failed"):
        return None
    error = order["error"] or ""
    stage = order["processing_stage"] or ""
    kind = STAGE_KIND.get(stage)
    named = next((name for name, words in _SIGNATURES if any(w in error.lower() for w in words)), None)
    if named and not kind:
        kind = ("mt" if named in ("lara", "azure-translate", "claude", "aws-translate") else
                "tts" if named in ("azure", "piper", "elevenlabs") else "asr")
    if not kind:
        return {"kind": None, "stage": stage, "provider": named, "reason": _quota_reason(error), "error": error}
    column = COLUMN.get(kind)
    # a provider the order is set to use for this step is the likely culprit when the error does not name one
    provider = named if named and (kind != "mt" or named in ("lara", "azure-translate", "claude", "aws-translate", "local")) else \
        (order[column] if column else None)
    return {"kind": kind, "stage": stage, "provider": provider, "reason": _quota_reason(error), "error": error}


def alternatives(order, kind: str) -> list[dict]:
    """Every provider that could take this step for this order, with whether it is connected and whether it fits the languages."""
    src, tgt = order["source_lang"], order["target_lang"]
    current = order[COLUMN[kind]] if kind in COLUMN else None
    rows: list[dict] = []
    if current == "stub":                                     # this order has no such step (a transcript has no voice)
        return rows

    def add(name: str, fits: bool = True, note: str = ""):
        rows.append({"name": name, "label": NAMES.get(name, name), "current": name == current, "connected": configured(name),
                     "fits": fits, "note": note})

    if kind == "asr":
        add("transkriptor")
        add("faster-whisper", note="Runs on our server; slower, no cost")
        add("groq-whisper", note="Cheap cloud Whisper")
        add("assemblyai")
        add("elevenlabs-scribe", fits={src} <= {"en", "sw"}, note="English and Swahili only")
    elif kind == "mt":
        add("lara")
        add("claude")
        add("azure-translate")
        add("aws-translate")
        add("local", fits=(src, tgt) == ("sw", "en"), note="Swahili to English only")
    elif kind == "tts":
        add("azure", fits=tgt in ("sw", "en"))
        add("piper", fits=tgt == "en", note="English only")
        if current == "elevenlabs":
            add("elevenlabs", note="The premium voice this order was sold with")
    return [r for r in rows if r["name"] != "stub"]


_credit_cache: dict = {"at": 0.0, "value": None}


def elevenlabs_credits() -> dict | None:
    """Characters left this billing period (cached for a minute)."""
    if not os.environ.get("ELEVENLABS_API_KEY"):
        return None
    if time.time() - _credit_cache["at"] < 60 and _credit_cache["value"] is not None:
        return _credit_cache["value"]
    try:
        r = httpx.get("https://api.elevenlabs.io/v1/user/subscription", headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]}, timeout=8)
        r.raise_for_status()
        d = r.json()
        value = {"limit": int(d["character_limit"]), "used": int(d["character_count"]),
                 "left": max(int(d["character_limit"]) - int(d["character_count"]), 0), "tier": d.get("tier", "")}
    except Exception:  # noqa: BLE001
        return None
    _credit_cache.update(at=time.time(), value=value)
    return value


def resources() -> list[dict]:
    """One line per outside provider: connected or not, and what is left where the provider can tell us."""
    rows = []
    for name in ("lara", "claude", "azure-translate", "aws-translate", "transkriptor", "groq-whisper", "assemblyai", "elevenlabs", "azure", "piper"):
        row = {"name": name, "label": NAMES.get(name, name), "connected": configured(name), "detail": ""}
        if name == "transkriptor" and row["connected"]:
            keys = sum(1 for n in ("TRANSKRIPTOR_API_KEY", "TRANSKRIPTOR_API_KEY_BACKUP", "TRANSKRIPTOR_API_KEY_BACKUP_2") if os.environ.get(n))
            row["detail"] = f"{keys} key{'s' if keys != 1 else ''} (a backup key is used when one runs out)"
        if name == "elevenlabs" and row["connected"]:
            c = elevenlabs_credits()
            if c:
                row["detail"] = f"{c['left']:,} of {c['limit']:,} characters left" + (f" ({c['tier']} plan)" if c["tier"] else "")
                row["low"] = c["left"] < max(c["limit"] * 0.05, 2000)
        rows.append(row)
    return rows


def failure_panel(order) -> dict | None:
    """Everything the failed-order panel shows, or None when the order has not failed."""
    d = diagnose(order)
    if not d:
        return None
    d["label"] = NAMES.get(d["provider"], d["provider"] or "")
    d["options"] = alternatives(order, d["kind"]) if d["kind"] else []
    d["resources"] = resources()
    return d


def valid_switch(order, kind: str, name: str) -> str | None:
    """None when the order can be pointed at this provider; otherwise why not."""
    if kind not in COLUMN:
        return "That step cannot be switched."
    row = next((a for a in alternatives(order, kind) if a["name"] == name), None)
    if not row:
        return "That provider is not an option for this step."
    if not row["connected"]:
        return f"{row['label']} is not connected on this server."
    if not row["fits"]:
        return f"{row['label']} cannot take this language pair."
    return None
