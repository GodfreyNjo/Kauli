import pytest
"""Run with: python -m pytest tests/test_mt_providers.py -v

Separate from test_pipeline.py because this one loads a real ~300MB
HuggingFace model (already cached locally after first use - no network
needed here) rather than the StubMT/stub-everything path the rest of the
suite deliberately stays fast with.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kauli.providers.mt import LocalMT


def test_local_mt_translate_does_not_hit_meta_tensor_bug():
    """Real bug caught in production: a live re-translate through LocalMT
    failed with "Tensor.item() cannot be called on meta tensors" - newer
    transformers versions build a model on the "meta" device (shape/dtype
    only, no real data) as a fast-init memory-saving path, and MarianMT's
    tied embeddings can come out of that path with one alias still stuck
    on meta. low_cpu_mem_usage=False (see LocalMT._load) forces the old,
    fully-materialized-from-the-start load path instead. This is a real
    end-to-end call through the actual provider, not a mock - a
    regression here would otherwise only show up live, the same way it
    did the first time."""
    mt = LocalMT()
    result = mt.translate("Habari yako", target_chars=50, source_lang="sw", target_lang="en")
    assert result["literal"]  # real, non-empty translation came back
    assert result["spoken"] == result["literal"]
    assert result["confidence"] == 0.55



# ------------------------------------------------ a model's commentary never becomes the translation
def test_the_prompt_names_the_target_language_so_the_model_is_not_told_to_write_english():
    from kauli.providers.mt import _build_system_prompt
    sw = _build_system_prompt("en", "sw")
    assert "into natural spoken Kiswahili" in sw and "into natural spoken English" not in sw and "never add commentary" in sw
    assert "into natural spoken English" in _build_system_prompt("sw")                    # the default is unchanged
    assert "German" in _build_system_prompt("en", "de")


def test_a_reply_with_a_note_after_the_json_is_still_read_as_a_translation():
    from kauli.providers.mt import parse_model_json
    reply = ('{ "literal": "Third, names.", "spoken": "Tatu, majina.", "shorter": "Tatu, majina.", "longer": "Tatu, majina yote.", '
             '"notes": "kept as is", "confidence": 0.75, "flags": ["unclear"] }\n\n**Note:** You specified Kiswahili. Please clarify.')
    got = parse_model_json(reply)
    assert got["spoken"] == "Tatu, majina." and got["confidence"] == 0.75 and got["flags"] == ["unclear"]
    assert parse_model_json("Sure! Here is the translation: " + reply)["spoken"] == "Tatu, majina."
    assert parse_model_json("```json\n" + reply.split("\n\n")[0] + "\n```")["literal"] == "Third, names."
    assert parse_model_json("I cannot translate this, could you clarify?") is None


class _FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.messages = self

    def create(self, **kw):
        from types import SimpleNamespace
        self.calls += 1
        text = self.replies.pop(0)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], usage=SimpleNamespace(input_tokens=10, output_tokens=10))


def test_claude_retries_once_and_never_stores_a_reply_that_is_not_a_translation():
    from kauli.providers.mt import ClaudeMT
    good = '{"literal": "a", "spoken": "Habari yako", "confidence": 0.9, "flags": []}'
    mt = ClaudeMT()
    mt._client = _FakeClient(["**Note:** You specified Kiswahili but...", good])
    assert mt.translate("Hello", 12, "en", "sw")["spoken"] == "Habari yako" and mt._client.calls == 2
    broken = ClaudeMT()
    broken._client = _FakeClient(["I need clarification.", "Please tell me the target language."])
    with pytest.raises(ValueError):
        broken.translate("Hello", 12, "en", "sw")                                          # not stored: the caller falls back or retries


def test_commentary_already_stored_in_a_translation_is_cleaned_out():
    from kauli.providers.mt import clean_model_leak
    leaked = ('Karibu Kauli Guides. Second, fixing the problem. { "literal": "Third, names.", "spoken": "Tatu, majina.", "shorter": "T", "longer": "L", '
              '"notes": "n", "confidence": 0.75, "flags": ["x"] }\n\n**Note:** You specified Kiswahili. Please clarify if you need: 1. English, or 2. Kiswahili\n\n'
              'Nne, manukuu.')
    cleaned = clean_model_leak(leaked)
    assert cleaned == "Karibu Kauli Guides. Second, fixing the problem. Tatu, majina.\n\nNne, manukuu."
    assert "{" not in cleaned and "Note" not in cleaned and "literal" not in cleaned
    assert clean_model_leak("A normal sentence {with braces}.") == "A normal sentence {with braces}."          # nothing to clean: unchanged
    assert clean_model_leak(None) == "" and clean_model_leak("") == ""
