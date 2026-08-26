"""Translation providers.

The important idea: translation is NOT a single call. We ask for several
candidates at different lengths, then pick the one that fits the timing budget
without losing meaning. See kauli/timing.py:choose_candidate.
"""
from __future__ import annotations

import json
import os
import re

# Names shown to Claude in the prompt, and used to decide which
# source-language-specific rules below actually apply. Kept next to
# app.py's SOURCE_LANGUAGES rather than importing it - webapp shouldn't be
# a required dependency of this package.
SOURCE_LANG_NAMES = {"sw": "Kenyan Kiswahili", "ki": "Kikuyu (Gikuyu)", "en": "English"}


def _build_system_prompt(source_lang: str) -> str:
    lang_name = SOURCE_LANG_NAMES.get(source_lang, source_lang)
    # The Swahili-clock offset ("saa kumi na moja jioni" = 17:00) is a
    # Swahili-specific convention - applying it to a different source
    # language would actively introduce a wrong-time bug, not just a
    # missed nuance, so it's gated on source_lang instead of being a
    # blanket rule.
    clock_rule = ""
    if source_lang == "sw":
        clock_rule = (
            '\n- SWAHILI CLOCK: Swahili hours run 6 hours offset from the international clock. '
            '"saa moja asubuhi"=07:00, "saa nne"=10:00, "saa kumi na moja jioni"=17:00. '
            'Convert every "saa ..." expression. Getting this wrong is the single worst error you can make.'
        )
    # Kikuyu (and any other language added to MANUAL_TRANSCRIPTION_LANGUAGES
    # in app.py) is much lower-resource for you than Kiswahili - real
    # translation quality here is unverified. Say so explicitly rather than
    # let a fluent-sounding guess hide a wrong translation from review.
    low_resource_note = ""
    if source_lang not in ("sw", "en"):
        low_resource_note = (
            f"\n- {lang_name} is a lower-resource language for you than Kiswahili - if a phrase's meaning "
            "is genuinely uncertain, reflect that with a LOWER confidence score and the \"unclear\" flag "
            "instead of producing a fluent-sounding guess. A human reviews anything you flag as uncertain; "
            "a confident-looking wrong translation is what actually reaches the client unreviewed."
        )
    return f"""You translate {lang_name} speech into natural spoken English for dubbing.

Rules:
- Kenyan context. Expect English borrowings (order, store, deposit) and Sheng or code-switching with Kiswahili where natural. These are normal speech, not errors.{clock_rule}{low_resource_note}
- Preserve meaning and register. Never invent facts to fill time. Never drop facts to save time.
- Keep names, places and brands as-is.
- Output will be SPOKEN by a voice actor, so it must sound like speech, not prose.

Return ONLY a JSON object, no markdown fences, no preamble:
{{"literal": "...", "spoken": "...", "shorter": "...", "longer": "...", "notes": "...", "confidence": 0.0-1.0, "flags": ["sheng","time_expression","numbers","unclear"]}}

"literal" = faithful and complete.
"spoken"  = natural, aimed at the target character count given.
"shorter" = ~25% tighter than spoken, same meaning.
"longer"  = ~20% fuller than spoken, same meaning.
"notes"   = only when a non-obvious choice was made (idiom, clock conversion, cultural reference).
"""


# Kept for backward compatibility with anything importing the old constant
# directly (e.g. existing tests) - always the Kiswahili prompt, same text
# as before this change.
SYSTEM_PROMPT = _build_system_prompt("sw")


class MTProvider:
    name = "base"

    def translate(self, text: str, target_chars: int, source_lang: str = "sw",
                  target_lang: str = "en") -> dict:
        raise NotImplementedError


class StubMT(MTProvider):
    name = "stub"

    FIXTURE = {
        "Habari yako, naitwa Wanjiru kutoka Duka Bora.": {
            "literal": "How are you, I am called Wanjiru from Duka Bora.",
            "spoken": "Hi, I'm Wanjiru calling from Duka Bora.",
            "shorter": "Hi, Wanjiru from Duka Bora.",
            "longer": "Hello there, my name is Wanjiru and I'm calling from Duka Bora.",
            "notes": "'Habari yako' is a greeting, not a question about health.",
            "confidence": 0.93, "flags": [],
        },
        "Nimepiga simu kuhusu ile order uliweka Jumatatu, imefika kwa store yetu ya Kilimani.": {
            "literal": "I have called about that order you placed on Monday, it has arrived at our store in Kilimani.",
            "spoken": "I'm calling about the order you placed Monday. It's arrived at our Kilimani branch.",
            "shorter": "Your Monday order has arrived at our Kilimani branch.",
            "longer": "I'm calling about that order you placed on Monday - it has now arrived at our store in Kilimani.",
            "notes": "'order' and 'store' are established borrowings, not ASR noise.",
            "confidence": 0.90, "flags": [],
        },
        "Kama uko sawa, unaweza kuja kuipick leo kabla ya saa kumi na moja jioni.": {
            "literal": "If you are okay, you can come and pick it today before the eleventh hour of the evening.",
            "spoken": "If that works, you can collect it today before five p.m.",
            "shorter": "You can collect it today before five.",
            "longer": "If that works for you, you're welcome to come and pick it up today, any time before five in the evening.",
            "notes": "SWAHILI CLOCK: 'saa kumi na moja jioni' = 17:00, not 11:00.",
            "confidence": 0.79, "flags": ["sheng", "time_expression"],
        },
    }

    def translate(self, text: str, target_chars: int, source_lang="sw", target_lang="en") -> dict:
        return self.FIXTURE.get(text, {
            "literal": text, "spoken": text, "shorter": text, "longer": text,
            "notes": None, "confidence": 0.5, "flags": ["unclear"],
        })


class ClaudeMT(MTProvider):
    """Quality option. Costs cents per hour of audio at Haiku prices, and it is
    far better than generic NMT at register, Sheng and length-targeted paraphrase.
    Set ANTHROPIC_API_KEY."""
    name = "claude"

    # Best current understanding of Haiku-tier pricing per the Anthropic API,
    # $ per token (not per million) so per-call math below stays simple -
    # verify against console.anthropic.com/settings/billing before trusting
    # this for real budgeting; pricing can change and this isn't fetched live.
    PRICE_PER_INPUT_TOKEN = 1.0 / 1_000_000
    PRICE_PER_OUTPUT_TOKEN = 5.0 / 1_000_000

    def __init__(self, model: str = "claude-haiku-4-5-20251001", max_tokens: int = 900):
        self.model = model
        self.max_tokens = max_tokens
        self._client = None
        self.total_cost_usd = 0.0  # accumulates across every translate() call on this instance
        self.total_calls = 0

    def _c(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        return self._client

    def translate(self, text: str, target_chars: int, source_lang="sw", target_lang="en") -> dict:
        lang_name = {"en": "English", "sw": "Kiswahili"}.get(target_lang, target_lang)
        user = (
            f"Source ({source_lang}): {text}\n"
            f"Target language: {lang_name}\n"
            f"Target length for 'spoken': about {target_chars} characters "
            f"(this is a hard timing budget for dubbing)."
        )
        resp = self._c().messages.create(
            model=self.model, max_tokens=self.max_tokens,
            system=_build_system_prompt(source_lang), messages=[{"role": "user", "content": user}],
        )
        # Real usage from the response, not an estimate - this is what
        # feeds the ops dashboard's daily-spend figure (see worker.py /
        # staff_ops.html), which is the actual protection against a
        # bugged retry loop or malicious automation quietly running up a
        # bill unnoticed.
        if resp.usage:
            self.total_cost_usd += (resp.usage.input_tokens * self.PRICE_PER_INPUT_TOKEN +
                                     resp.usage.output_tokens * self.PRICE_PER_OUTPUT_TOKEN)
        self.total_calls += 1
        raw = "".join(b.text for b in resp.content if b.type == "text").strip()
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"literal": raw, "spoken": raw, "shorter": raw, "longer": raw,
                    "notes": "MT returned unparseable output - review required.",
                    "confidence": 0.3, "flags": ["unclear"]}


class AwsTranslateMT(MTProvider):
    """$15/million chars, 2M chars/month free for 12 months on new accounts.
    Cheap and fine for the literal variant, but it cannot paraphrase to length -
    so it gives you one candidate, not four. Use it as a cost fallback."""
    name = "aws-translate"

    def __init__(self, region: str = "eu-west-1"):
        self.region = region
        self._client = None

    def _c(self):
        if self._client is None:
            import boto3
            self._client = boto3.client("translate", region_name=self.region)
        return self._client

    def translate(self, text: str, target_chars: int, source_lang="sw", target_lang="en") -> dict:
        r = self._c().translate_text(Text=text, SourceLanguageCode=source_lang,
                                     TargetLanguageCode=target_lang)
        t = r["TranslatedText"]
        return {"literal": t, "spoken": t, "shorter": None, "longer": None,
                "notes": None, "confidence": 0.7, "flags": []}


class LaraMT(MTProvider):
    """Free-tier option: Lara Translate, by Translated (the company behind
    MyMemory and 25+ years of professional MT). Real adaptive NMT across
    200+ language pairs including Swahili - meaningfully better than
    LocalMT below, though not as good as ClaudeMT at Sheng/register/
    length-targeted paraphrase. 10,000 characters/month free, no credit
    card - see developers.laratranslate.com to sign up and
    support.laratranslate.com/en/api-key-for-laras-api to generate a key
    pair (Dashboard -> API credentials -> Create new credentials -> Download
    Credentials immediately, it's only shown once).

    Set LARA_ACCESS_KEY_ID and LARA_ACCESS_KEY_SECRET. Requires the
    lara-sdk package (pip install lara-sdk).

    Like AwsTranslateMT, this returns ONE translation, not four
    length-targeted candidates - duration fitting only ever has
    'literal'/'spoken' (identical here) to work with. The 10k/month quota
    is small: budget roughly a handful of dub orders a month before
    hitting it, not real production volume - a genuine stopgap until a
    paid provider makes sense, not a long-term default."""
    name = "lara"

    # Lara wants full BCP-47-ish codes, not the bare "sw"/"en" this app
    # uses everywhere else - mapped here only, never renamed system-wide.
    # Kikuyu deliberately has no entry - app.py's create_order already
    # refuses a Kikuyu order any MT provider but Claude (see
    # MANUAL_TRANSCRIPTION_LANGUAGES), so this never needs to guess at one.
    LANG_CODES = {"sw": "sw-KE", "en": "en-US"}

    def __init__(self):
        self._client = None
        self.total_chars_used = 0  # against the real 10,000/month free quota - track it,
        # don't just assume it's fine (see the class docstring on how little that actually covers)

    def _c(self):
        if self._client is None:
            from lara_sdk import Translator, Credentials
            creds = Credentials(
                access_key_id=os.environ["LARA_ACCESS_KEY_ID"],
                access_key_secret=os.environ["LARA_ACCESS_KEY_SECRET"],
            )
            self._client = Translator(creds)
        return self._client

    def translate(self, text: str, target_chars: int, source_lang="sw", target_lang="en") -> dict:
        src = self.LANG_CODES.get(source_lang, source_lang)
        tgt = self.LANG_CODES.get(target_lang, target_lang)
        res = self._c().translate(text, source=src, target=tgt)
        out = (res.translation or "").strip()
        self.total_chars_used += len(text)
        return {
            "literal": out, "spoken": out, "shorter": None, "longer": None,
            "notes": "Lara (free tier): one translation, no length-targeted paraphrase - "
                     "duration fitting only has this one candidate to work with.",
            "confidence": 0.75 if out else 0.0,
            "flags": [] if out else ["unclear"],
        }


class LocalMT(MTProvider):
    """PLACEHOLDER - free, local, offline via HuggingFace transformers
    (Helsinki-NLP/opus-mt-swc-en, MarianMT). No API key, no cost, no network
    after the first model download (~300MB, cached forever).

    Read this before trusting its output for a paying client:
      - The model is trained on 'swc' (Congo Swahili), not Kenyan Swahili.
        Standard Swahili should come through fine; Sheng and Kenyan-specific
        idiom/loanwords will be noticeably weaker than ClaudeMT.
      - It returns ONE translation, not four length-targeted candidates -
        there is no 'spoken'/'shorter'/'longer' paraphrase, so duration
        fitting only ever has 'literal' to work with.
      - It has NO awareness of the Swahili clock offset rule ("saa kumi na
        moja jioni" = 17:00) that ClaudeMT's system prompt explicitly
        handles. Confidence is therefore capped low enough that every
        segment trips the pipeline's low_mt_confidence review flag - do
        NOT relax that just because this provider "seems fine" on a sample.
    Swap to --mt claude once ANTHROPIC_API_KEY is set. This exists purely so
    the pipeline can run end-to-end on real audio for zero cost until then.
    """
    name = "local"
    MODEL_NAME = "Helsinki-NLP/opus-mt-swc-en"

    def __init__(self):
        self._tokenizer = None
        self._model = None

    def _load(self):
        if self._model is None:
            from transformers import MarianMTModel, MarianTokenizer
            self._tokenizer = MarianTokenizer.from_pretrained(self.MODEL_NAME)
            # low_cpu_mem_usage=False is deliberate, not a default left in
            # place: newer transformers versions build the model on a
            # "meta" device first (shape/dtype only, no real data) as a
            # memory-saving fast-init path, then materialize real weights
            # afterward. MarianMT's tied embeddings (shared.weight /
            # encoder.embed_tokens.weight / decoder.embed_tokens.weight /
            # lm_head.weight all alias each other) can come out of that
            # path with one alias still stuck on meta - real, this is
            # exactly the "Tensor.item() cannot be called on meta tensors"
            # error a real re-translate hit in production. This ~300MB
            # model has no real memory pressure to save by using the fast
            # path anyway - forcing the old, fully-materialized-on-CPU-
            # from-the-start load path is strictly safer here, not slower
            # in any way that matters.
            self._model = MarianMTModel.from_pretrained(self.MODEL_NAME, low_cpu_mem_usage=False)
        return self._tokenizer, self._model

    def translate(self, text: str, target_chars: int, source_lang="sw", target_lang="en") -> dict:
        tokenizer, model = self._load()
        batch = tokenizer([text], return_tensors="pt", padding=True, truncation=True)
        generated = model.generate(**batch, max_new_tokens=256)
        out = tokenizer.decode(generated[0], skip_special_tokens=True).strip()
        return {
            "literal": out, "spoken": out, "shorter": None, "longer": None,
            "notes": "LocalMT placeholder (opus-mt-swc-en): no Swahili-clock "
                     "handling, no length-targeted paraphrase - verify against source.",
            "confidence": 0.55,
            "flags": ["local_mt_placeholder"],
        }


class AzureTranslateMT(MTProvider):
    """Real, commercially-licensed, direction-aware MT via Azure AI
    Translator - built to replace LocalMT below on Free/Pro tier.

    Real bug this fixes, not just a quality upgrade: LocalMT is
    Helsinki-NLP/opus-mt-swc-en, a Swahili-TO-English-ONLY model - it
    ignores source_lang/target_lang entirely and always runs text
    through that one fixed direction. On an en->sw order (Free/Pro
    tier), that meant feeding ENGLISH text into a model trained to
    output English FROM Swahili - not just weaker, actually nonsensical
    regardless of input. Azure Translator is genuinely bidirectional
    (confirmed: Swahili `sw` is a real supported language, both
    directions, via Microsoft's own current language-support docs) and
    commercially licensed - 2,000,000 characters/month free, real
    pay-as-you-go pricing beyond that (nowhere close to what a real
    order's transcript volume would need to worry about).

    Like AwsTranslateMT/LaraMT, this returns ONE translation, not four
    length-targeted candidates - duration fitting only has 'literal'/
    'spoken' (identical here) to work with. Set AZURE_TRANSLATOR_KEY
    and AZURE_TRANSLATOR_REGION (a Translator resource, NOT the same
    resource as AZURE_SPEECH_KEY/AZURE_SPEECH_REGION - see
    kauli/providers/tts.py:AzureTTS for that one)."""
    name = "azure-translate"
    ENDPOINT = "https://api.cognitive.microsofttranslator.com/translate"

    def translate(self, text: str, target_chars: int, source_lang="sw", target_lang="en") -> dict:
        import httpx
        import uuid as _uuid
        resp = httpx.post(
            self.ENDPOINT,
            params={"api-version": "3.0", "from": source_lang, "to": target_lang},
            headers={
                "Ocp-Apim-Subscription-Key": os.environ["AZURE_TRANSLATOR_KEY"],
                "Ocp-Apim-Subscription-Region": os.environ["AZURE_TRANSLATOR_REGION"],
                "Content-Type": "application/json",
                "X-ClientTraceId": str(_uuid.uuid4()),
            },
            json=[{"text": text}],
            timeout=15,
        )
        resp.raise_for_status()
        out = resp.json()[0]["translations"][0]["text"]
        return {
            "literal": out, "spoken": out, "shorter": None, "longer": None,
            "notes": "Azure Translator: one translation, no length-targeted paraphrase - "
                     "duration fitting only has this one candidate to work with.",
            "confidence": 0.75 if out else 0.0,
            "flags": [] if out else ["unclear"],
        }


_REGISTRY = {
    "stub": StubMT, "claude": ClaudeMT, "aws-translate": AwsTranslateMT,
    "local": LocalMT, "lara": LaraMT, "azure-translate": AzureTranslateMT,
}


class ResilientMT(MTProvider):
    """Same 'use X first, fall back to Y automatically' pattern
    kauli.providers.asr.TranskriptorASR already uses for ASR (see that
    class's own docstring) - here for MT. get_mt() returns one of these
    instead of the bare named provider whenever a real, already-configured
    fallback exists (see FALLBACK_CHAIN/_fallback_configured below), so a
    caller selecting mt="claude" gets "Claude, and if it's actually down
    fall back to Azure Translator" as ONE resolved behavior, not two
    providers it has to orchestrate itself. fallback_used/fallback_reason
    mirror TranskriptorASR's exact attribute names on purpose - callers
    that already know to check an ASR provider for these can check an MT
    provider the same way."""
    def __init__(self, primary: MTProvider, fallback: MTProvider, fallback_name: str):
        self.primary = primary
        self.fallback = fallback
        self.fallback_name = fallback_name
        self.name = primary.name
        self.fallback_used = False
        self.fallback_reason: str | None = None

    @property
    def total_cost_usd(self) -> float:
        # Real cost from BOTH providers, summed - a mid-job switch means
        # the primary genuinely already spent real money on whatever
        # segments it completed before failing; reporting only whichever
        # provider is "current" would silently drop that from
        # ops_ai_spend_today (webapp/db.py) the moment a fallback fires.
        return getattr(self.primary, "total_cost_usd", 0.0) + getattr(self.fallback, "total_cost_usd", 0.0)

    def translate(self, text: str, target_chars: int, source_lang="sw", target_lang="en") -> dict:
        # Once the primary has shown it's down, stay on the fallback for
        # the rest of THIS job rather than re-trying (and possibly
        # succeeding on) the primary per segment - a flaky primary
        # otherwise produces a translation stitched from two different
        # models' style/terminology choices within one delivered order,
        # which is worse than a consistent fallback throughout.
        if not self.fallback_used:
            try:
                return self.primary.translate(text, target_chars, source_lang, target_lang)
            except Exception as exc:  # noqa: BLE001 - any primary failure means "fall back", not "fail the order"
                self.fallback_used = True
                self.fallback_reason = f"{self.primary.name} failed ({exc}) - used {self.fallback_name} instead"
        return self.fallback.translate(text, target_chars, source_lang, target_lang)


# Real fallback pairing, not every provider paired with every other one -
# each entry is "if THIS fails, and a working alternative is actually
# configured, use THAT instead". Azure Translator is the fallback target
# across the board: it's the one MT provider here confirmed genuinely
# bidirectional and Kiswahili-capable independent of Claude being up (see
# that class's own docstring) - not just "some other provider happened to
# be available".
MT_FALLBACK_CHAIN = {"claude": "azure-translate", "aws-translate": "azure-translate", "lara": "azure-translate"}


def _mt_fallback_configured(name: str) -> bool:
    """Whether the fallback candidate actually has what it needs to run -
    never wrap a primary in a fallback that would just fail too (silently
    swapping one exception for a different, equally-broken one)."""
    if name == "azure-translate":
        return bool(os.environ.get("AZURE_TRANSLATOR_KEY"))
    if name == "claude":
        return bool(os.environ.get("ANTHROPIC_API_KEY"))
    if name == "aws-translate":
        return bool(os.environ.get("AWS_ACCESS_KEY_ID"))
    if name == "lara":
        return bool(os.environ.get("LARA_ACCESS_KEY_ID"))
    return True


def get_mt(name: str, **kwargs) -> MTProvider:
    if name not in _REGISTRY:
        raise ValueError(f"Unknown MT provider '{name}'. Options: {list(_REGISTRY)}")
    primary = _REGISTRY[name](**kwargs)
    fallback_name = MT_FALLBACK_CHAIN.get(name)
    if fallback_name and fallback_name != name and _mt_fallback_configured(fallback_name):
        return ResilientMT(primary, _REGISTRY[fallback_name](), fallback_name)
    return primary
