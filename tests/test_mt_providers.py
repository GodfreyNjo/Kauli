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
