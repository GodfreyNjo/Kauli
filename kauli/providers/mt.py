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
            self._model = MarianMTModel.from_pretrained(self.MODEL_NAME)
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


_REGISTRY = {
    "stub": StubMT, "claude": ClaudeMT, "aws-translate": AwsTranslateMT,
    "local": LocalMT,
}


def get_mt(name: str, **kwargs) -> MTProvider:
    if name not in _REGISTRY:
        raise ValueError(f"Unknown MT provider '{name}'. Options: {list(_REGISTRY)}")
    return _REGISTRY[name](**kwargs)
