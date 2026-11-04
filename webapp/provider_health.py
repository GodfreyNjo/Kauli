"""Which outside provider an order failed on, what else could do the job, and what each provider has left.

Staff see this on a failed order: the provider named in the error, why it failed (out of quota, bad key, down), the other providers that
are connected and able to take this order's languages, and a one-click switch that points the order at one of them and retries it.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

import httpx

# Which pipeline step each stage the worker reports belongs to.
STAGE_KIND = {"clean": "cleanup", "transcribe": "asr", "translate": "mt", "voice": "tts", "mix": "tts"}

# Words in an error that point at a provider, most specific first.
_SIGNATURES = (
    ("madlad", ("madlad", "ctranslate2")),
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
    "own": "Kauli's own transcription model", "madlad": "Kauli's own translation model", "azure": "Azure Speech", "piper": "Piper (our own server)", "elevenlabs": "ElevenLabs", "xtts": "XTTS", "mms": "MMS",
}

# The providers staff can switch on and off, by step. Each one that is off is passed over everywhere: new orders, automatic switching and
# the failed-order switch. ElevenLabs is the realistic-voice provider: turning it off takes away the premium voice, the automatic realistic
# voice on Premium dubs and the Studio tier (customers are never offered what cannot be delivered), and the standard voices carry on.
TOGGLEABLE = {
    "asr": ["transkriptor", "groq-whisper", "assemblyai", "own"],
    "mt": ["lara", "azure-translate", "madlad", "claude", "aws-translate"],
    "tts": ["piper", "azure", "elevenlabs"],
}
STEP_TITLES = {"asr": "Turning speech into text", "mt": "Translating", "tts": "Voices for dubbing"}
# What each provider costs us per audio-minute, in US dollars. Where the provider publishes a price it is filled in (an estimate: real bills depend
# on your plan and on how many characters a minute of speech has); None means we do not know, and staff enter the real figure on the switchboard.
COST_DEFAULTS = {
    "transkriptor": None, "groq-whisper": 0.0019, "assemblyai": 0.0025, "own": 0.0, "faster-whisper": 0.0, "elevenlabs-scribe": 0.0037,
    "lara": None, "azure-translate": 0.010, "claude": None, "aws-translate": 0.015, "madlad": 0.0, "local": 0.0,
    "piper": 0.0, "azure": 0.016, "elevenlabs": None,      # ElevenLabs: worked out from its published price, see cost_per_min
}
MODES = {
    "saver": ("Spend less", "The cheap providers go first for any order in the widely spoken languages, with Transkriptor behind them if they fail. "
                            "Swahili and the other languages they are not good at still go to the providers built for them."),
    "balanced": ("Balanced", "How it works today: Transkriptor for paying customers, the cheap providers for orders the free allowance covers."),
    "quality": ("Best quality", "Transkriptor first for every order, free ones too. Highest quality, highest cost."),
}


def _settings() -> dict:
    from . import db
    try:
        return db.get_provider_settings()
    except Exception:  # noqa: BLE001 - a missing table must never take orders down
        return {}


def is_enabled(name: str) -> bool:
    """False only when staff turned this provider off on the switchboard."""
    row = _settings().get(name)
    return True if row is None else bool(row["enabled"])


def usable(name: str) -> bool:
    """Connected on this server and not switched off by staff."""
    return bool(configured(name)) and is_enabled(name)


def blocked(name: str) -> bool:
    """Set aside after a failure, or switched off: new orders do not go to it."""
    return is_paused(name) or not is_enabled(name)


def cost_per_min(name: str) -> float | None:
    """Staff's figure if they entered one, else the published estimate, else None (not known)."""
    row = _settings().get(name)
    if row is not None and row["cost_per_min"] is not None:
        return float(row["cost_per_min"])
    if name == "elevenlabs":
        from . import billing
        return billing.premium_voice_unit_cost_usd()           # its characters per audio-minute at its list price, with retries allowed for
    return COST_DEFAULTS.get(name)


def mode() -> str:
    from . import db
    try:
        value = db.get_flag("provider_mode", "balanced")
    except Exception:  # noqa: BLE001
        value = "balanced"
    return value if value in MODES else "balanced"


def typical_route(level: str) -> dict:
    """The provider each step of a service normally uses under the current mode, for the cost estimate."""
    m = mode()
    asr_order = {"saver": ["groq-whisper", "assemblyai", "own", "transkriptor"], "quality": ["transkriptor", "assemblyai", "groq-whisper", "own"]}.get(
        m, ["transkriptor", "groq-whisper", "assemblyai", "own"])
    route = {"asr": next((n for n in asr_order if usable(n)), "faster-whisper")}
    if level in ("translate", "dub"):
        route["mt"] = next((n for n in ["lara", "azure-translate", "madlad", "claude", "aws-translate"] if usable(n)), None)
    if level == "dub":
        route["tts"] = next((n for n in ["piper", "azure"] if usable(n)), None)
    return route


def cost_estimates() -> list[dict]:
    """For each service: what one audio-minute of Instant AI costs us under the current setup, against what we charge for it."""
    from . import billing
    out = []
    for level, price in billing.AI_PRICES.items():
        route = typical_route(level)
        costs = [cost_per_min(n) if n else 0.0 for n in route.values()]
        known = all(c is not None for c in costs) and bool(route)
        total = sum(c for c in costs if c is not None)
        out.append({"level": level, "price": price, "route": [NAMES.get(n, n or "none") for n in route.values()],
                    "cost": total if known else None, "known": known,
                    "margin": ((price - total) / price * 100) if known and price else None})
    # the realistic voice: sold as an add-on, included on Premium, so its own margin is worth seeing
    voice_cost = cost_per_min("elevenlabs")
    add_on = billing.ADDONS["premium_voice"]["rate_per_min"]
    out.append({"level": "premium_voice", "price": add_on, "route": [NAMES["elevenlabs"]], "cost": voice_cost, "known": voice_cost is not None,
                "margin": ((add_on - voice_cost) / add_on * 100) if voice_cost is not None and add_on else None})
    return out


def own_model_status() -> dict:
    """Whether our own transcription model is on this server's disk yet (it downloads itself on first use, about half a gigabyte)."""
    try:
        from kauli.providers.asr import OwnASR
        root = Path(OwnASR.model_root())
        files = [f for f in root.rglob("model.bin")] if root.exists() else []
        size = sum(f.stat().st_size for f in files) // 1_000_000
    except Exception:  # noqa: BLE001
        files, size = [], 0
    return {"ready": bool(files), "mb": size, "downloading": _warming.is_set()}


import threading  # noqa: E402

_warming = threading.Event()


def warm_own_model() -> bool:
    """Download and load our own transcription model now, in the background, so the first customer does not wait for it."""
    if _warming.is_set():
        return False
    _warming.set()

    def run():
        try:
            from kauli.providers.asr import OwnASR
            OwnASR()._load()
        except Exception:  # noqa: BLE001 - the page shows whether it arrived
            pass
        finally:
            _warming.clear()
    threading.Thread(target=run, daemon=True, name="own-asr-warmup").start()
    return True


def switchboard() -> list[dict]:
    """Every provider staff can switch, grouped by step, with its state, cost and what is left where it can tell us."""
    detail = {r["name"]: r for r in resources()}
    steps = []
    for kind, names in TOGGLEABLE.items():
        rows = []
        for name in names:
            d = detail.get(name, {})
            rows.append({"name": name, "label": NAMES.get(name, name), "connected": bool(configured(name)), "enabled": is_enabled(name),
                         "paused": is_paused(name), "detail": d.get("detail", ""), "low": d.get("low", False),
                         "cost": cost_per_min(name), "default_cost": COST_DEFAULTS.get(name) if name != "elevenlabs" else cost_per_min(name),
                         "model": own_model_status() if name == "own" else None,
                         "custom_cost": (_settings().get(name) is not None and _settings()[name]["cost_per_min"] is not None)})
        steps.append({"kind": kind, "title": STEP_TITLES[kind], "rows": rows})
    return steps


# order column each kind of provider is stored in
COLUMN = {"asr": "asr", "mt": "mt", "tts": "tts"}

# What each step is called to a person, in the editor and on the Providers page.
KIND_LABELS = {"asr": "Transcription", "mt": "Translation", "tts": "Dubbing"}
DEFAULT_KEYS = {"asr": "provider_default_asr", "mt": "provider_default_mt", "tts": "provider_default_tts"}
# The house choice, in force until staff pick something else on the Providers, Models or Review tab: Transkriptor is the first choice for
# turning speech into text, and Azure Translator for translating the automatic (Instant AI) jobs. A step staff have set to Automatic stays
# Automatic: only a step nobody has ever touched uses these.
FACTORY_DEFAULTS = {"asr": "transkriptor", "mt": "azure-translate"}


def default_name(kind: str) -> str:
    """The provider staff pinned as the default for this step on the Providers page, or "" for automatic (the mode decides)."""
    from . import db
    if kind not in DEFAULT_KEYS:
        return ""
    try:
        value = db.get_flag(DEFAULT_KEYS[kind], None)
    except Exception:  # noqa: BLE001 - a missing table must never take orders down
        return ""
    if value is None:                                                       # never set by anyone: the house choice
        value = FACTORY_DEFAULTS.get(kind, "")
    return value if value in TOGGLEABLE.get(kind, []) else ""


def is_house_default(kind: str) -> bool:
    """True while this step is on the house choice because staff have never picked anything for it."""
    from . import db
    try:
        return kind in FACTORY_DEFAULTS and db.get_flag(DEFAULT_KEYS[kind], None) is None
    except Exception:  # noqa: BLE001
        return False


def switch_panel() -> list[dict]:
    """For the model switch on the Models and Review tabs: per step, what is in use now and every provider that can be picked."""
    out = []
    for kind in ("asr", "mt", "tts"):
        options = [{"name": n, "label": NAMES.get(n, n), "note": "" if configured(n) and is_enabled(n) else (" (turned off)" if configured(n) else " (not connected)")}
                   for n in TOGGLEABLE[kind]]
        out.append({"kind": kind, "label": KIND_LABELS[kind], "current": default_name(kind), "house": is_house_default(kind), "options": options})
    return out


def set_default(kind: str, name: str, by: str | None = None) -> str | None:
    """Pin (or, with an empty name, unpin) the default provider for a step. None when saved, otherwise why not."""
    from . import db
    if kind not in DEFAULT_KEYS:
        return "That step cannot have a default."
    if name and name not in TOGGLEABLE[kind]:
        return "That provider cannot be a default for this step."
    db.set_flag(DEFAULT_KEYS[kind], name, by=by)
    return None


def default_for(kind: str, source_lang: str, target_lang: str, explicit_only: bool = False) -> str | None:
    """The pinned default, but only when it can really take this job now: connected, switched on, not set aside after a failure and able to
    take the languages. Otherwise None, and the automatic routing carries on exactly as before. explicit_only ignores the house choice, for a
    job it should not decide (a paid plan's translation upgrade) while still honouring a provider staff picked on purpose."""
    name = default_name(kind)
    if not name or not usable(name) or is_paused(name) or (explicit_only and is_house_default(kind)):
        return None
    stub = {"source_lang": source_lang, "target_lang": target_lang, "asr": None, "mt": None, "tts": None}
    row = next((a for a in alternatives(stub, kind) if a["name"] == name), None)
    return name if row and row["fits"] else None


def editor_kinds(order) -> list[str]:
    """The steps an editor can redo with a provider on THIS order, in pipeline order. A step the order does not have is left out: a transcript
    has no translation or voice, a translation made from a transcript the customer already had was never transcribed again, and a subtitle
    job has no dub."""
    return [k for k in ("asr", "mt", "tts") if alternatives(order, k)]


def editor_options(order, kind: str) -> dict:
    """What the editor's provider popup shows for one step of one order: every provider that could do it, what it costs us per audio-minute,
    whether it can be chosen right now (and if not, why), which one the order is on, and which one staff pinned as the default."""
    rows = alternatives(order, kind) if kind in COLUMN else []
    pinned = default_name(kind)
    options = []
    for r in rows:
        if not r["connected"]:
            why = "Not connected on this server"
        elif r.get("off"):
            why = "Turned off on the Providers page"
        elif not r["fits"]:
            why = "Cannot take this language pair"
        elif is_paused(r["name"]):
            why = "Set aside after a recent failure"
        else:
            why = ""
        options.append({"name": r["name"], "label": r["label"], "note": r["note"], "current": r["current"], "default": r["name"] == pinned,
                        "cost_per_min": cost_per_min(r["name"]), "selectable": not why or why.startswith("Set aside"), "why": why})
    return {"kind": kind, "title": KIND_LABELS.get(kind, kind), "available": bool(options), "current": order[COLUMN[kind]] if kind in COLUMN else None,
            "default": pinned, "options": options}


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
    if name == "own":
        try:
            import faster_whisper  # noqa: F401
            return True
        except Exception:  # noqa: BLE001
            return False
    if name == "madlad":
        try:
            from kauli.providers.mt import MadladMT
            return MadladMT.available()
        except Exception:  # noqa: BLE001
            return False
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


def madlad_ok(source_lang: str, target_lang: str) -> bool:
    """Our own translation model is installed, switched on and can be trusted with this language pair."""
    from kauli.providers.mt import MadladMT
    return configured("madlad") and is_enabled("madlad") and MadladMT.pair_ok(source_lang, target_lang)


def diagnose(order) -> dict | None:
    """What failed on this order: the step, the provider the error names (or the one the order was set to use for that step), why."""
    if order["status"] not in ("dead_letter", "failed"):
        return None
    return diagnose_error(order, order["error"] or "", order["processing_stage"] or "")


def diagnose_error(order, error: str, stage: str = "") -> dict:
    kind = STAGE_KIND.get(stage)
    named = next((name for name, words in _SIGNATURES if any(w in error.lower() for w in words)), None)
    if named and not kind:
        kind = ("mt" if named in ("lara", "azure-translate", "claude", "aws-translate", "madlad") else
                "tts" if named in ("azure", "piper", "elevenlabs") else "asr")
    if not kind:
        return {"kind": None, "stage": stage, "provider": named, "named": bool(named), "reason": _quota_reason(error), "error": error}
    column = COLUMN.get(kind)
    # a provider the order is set to use for this step is the likely culprit when the error does not name one
    provider = named if named and (kind != "mt" or named in ("lara", "azure-translate", "claude", "aws-translate", "madlad", "local")) else \
        (order[column] if column else None)
    return {"kind": kind, "stage": stage, "provider": provider, "named": bool(named), "reason": _quota_reason(error), "error": error}


def alternatives(order, kind: str) -> list[dict]:
    """Every provider that could take this step for this order, with whether it is connected and whether it fits the languages."""
    src, tgt = order["source_lang"], order["target_lang"]
    current = order[COLUMN[kind]] if kind in COLUMN else None
    rows: list[dict] = []
    if current == "stub":                                     # this order has no such step (a transcript has no voice)
        return rows

    def add(name: str, fits: bool = True, note: str = ""):
        rows.append({"name": name, "label": NAMES.get(name, name), "current": name == current, "connected": configured(name),
                     "fits": fits, "note": note, "off": not is_enabled(name)})

    if kind == "asr":
        add("transkriptor")
        add("own", fits=src in {"en", "es", "fr", "de", "pt", "it", "nl"}, note="Our own model on our server: no cost, no quota")
        add("faster-whisper", note="Runs on our server; slower, no cost")
        add("groq-whisper", note="Cheap cloud Whisper")
        add("assemblyai")
        add("elevenlabs-scribe", fits={src} <= {"en", "sw"}, note="English and Swahili only")
    elif kind == "mt":
        add("lara")
        add("claude")
        add("azure-translate")
        add("aws-translate")
        from kauli.providers.mt import MadladMT
        add("madlad", fits=MadladMT.pair_ok(src, tgt), note="Our own model, no quota; slower, and not for English to Swahili or Kikuyu")
        add("local", fits=(src, tgt) == ("sw", "en"), note="Swahili to English only")
    elif kind == "tts":
        add("azure", fits=tgt in ("sw", "en"))
        add("piper", fits=tgt == "en", note="English only")
        if current == "elevenlabs":
            add("elevenlabs", note="The premium voice this order was sold with")
    return [r for r in rows if r["name"] != "stub"]


# ------------------------------------------------------------------------------------------------ automatic switching
# When a provider fails it is set aside for a while, so new orders go straight to the next one instead of failing first, and an order that
# fails on it is moved to the next connected provider that can take its languages and retried. Cheapest and most reliable first; our own
# model before the paid AI, the placeholder local model last.
AUTO_ORDER = {"asr": ["transkriptor", "groq-whisper", "assemblyai", "own", "faster-whisper"],
              "mt": ["lara", "azure-translate", "madlad", "claude", "aws-translate", "local"],
              "tts": ["piper", "azure"]}
PAUSE_SECONDS = {"out of quota or rate-limited": 30 * 60, "key rejected or not allowed": 30 * 60, "unreachable or timing out": 5 * 60}
_paused: dict[str, float] = {}


def mark_failed(name: str, error: str) -> None:
    """Set a provider aside after a failure that is plainly its own (quota, rejected key, outage)."""
    seconds = PAUSE_SECONDS.get(_quota_reason(error) or "")
    if name and seconds:
        _paused[name] = time.time() + seconds


def is_paused(name: str) -> bool:
    until = _paused.get(name)
    if until and until > time.time():
        return True
    _paused.pop(name, None)
    return False


def paused_for(name: str) -> int:
    return max(int(_paused.get(name, 0) - time.time()), 0)


def auto_switch(order, error: str):
    """After a failed attempt: if the error is a provider's own (it names one, or says quota/key/outage), pause that provider and point the
    order at the next one. Returns (step, failed provider, new provider), or None when there is nothing sensible to switch to."""
    from . import db
    d = diagnose_error(order, error, order["processing_stage"] or "")
    if not d["kind"] or d["kind"] not in COLUMN or not d["provider"] or not (d["named"] or d["reason"]):
        return None
    mark_failed(d["provider"], error)
    if order[COLUMN[d["kind"]]] != d["provider"]:
        return None
    options = {a["name"]: a for a in alternatives(order, d["kind"])}
    for name in AUTO_ORDER.get(d["kind"], []):
        a = options.get(name)
        if a and not a["current"] and a["connected"] and a["fits"] and not a["off"] and not is_paused(name):
            db.set_order_provider(order["id"], d["kind"], name)
            return d["kind"], d["provider"], name
    return None


_FALLBACK_NOTE = re.compile(r"^([\w-]+) failed \((.*)\) - used ", re.S)


def note_job_fallbacks(job) -> None:
    """A run that finished by falling back inside itself (Lara down, Azure used) still tells us the first provider failed: pause it too."""
    for text in getattr(job, "warnings", None) or []:
        m = _FALLBACK_NOTE.match(str(text))
        if m:
            mark_failed(m.group(1), m.group(2))


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
    for name in ("lara", "claude", "azure-translate", "aws-translate", "madlad", "transkriptor", "groq-whisper", "assemblyai", "elevenlabs", "azure", "piper"):
        row = {"name": name, "label": NAMES.get(name, name), "connected": configured(name), "detail": ""}
        if is_paused(name):
            row["detail"] = f"set aside for {paused_for(name) // 60 + 1} more minutes after a failure; orders use the next provider"
            row["low"] = True
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
    if row.get("off"):
        return f"{row['label']} is turned off on the Providers page. Turn it on there first."
    if not row["fits"]:
        return f"{row['label']} cannot take this language pair."
    return None
