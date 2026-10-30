"""The staff view of how the AI providers (our own models included) and the human reviewers are doing."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from kauli.models import Job, Segment  # noqa: E402
from webapp import model_stats  # noqa: E402


def test_word_errors_count_what_a_reviewer_changed():
    assert model_stats.word_errors("the cat sat", "the cat sat") == (0, 3)
    assert model_stats.word_errors("the cat sat", "the dog sat down") == (2, 4)       # one word swapped, one added
    assert model_stats.word_errors("Hello, World!", "hello world") == (0, 2)           # case and punctuation are not corrections
    assert model_stats.word_errors("", "one two") == (2, 2)


def _reviewed_order(env, wav_bytes, asr="groq-whisper", mt="lara", source_edits=0, words=400, status="delivered", tier="human"):
    o = env.order_from(env.place(wav_bytes, review_tier=tier))
    sentence = " ".join(f"word{i}" for i in range(words // 4))
    segs = []
    for i in range(4):
        s = Segment(segment_id=f"s{i}", index=i, start_ms=i * 1000, end_ms=i * 1000 + 900, source_transcript=sentence, spoken="habari " + sentence,
                    source_confidence=0.9, translation_confidence=0.8)
        if i < source_edits:
            s.source_edited_transcript = sentence.replace("word1 ", "changed ")
        segs.append(s)
    job = Job(segments=segs, providers={"asr": asr, "mt": mt, "tts": "stub"})
    Path(o["outdir"]).mkdir(parents=True, exist_ok=True)
    job.save(str(Path(o["outdir"]) / "manifest.json"))
    conn = env.db.get_conn()
    conn.execute("UPDATE orders SET status = ?, asr = ?, mt = ?, target_lang = 'sw', duration_minutes = 4, status_changed_at = created_at + 7200 WHERE id = ?", (status, asr, mt, o["id"]))
    conn.commit()
    conn.close()
    model_stats._cache.clear()
    return o["id"]


def test_providers_are_counted_by_what_ran_and_scored_by_what_reviewers_kept(env, wav_bytes):
    _reviewed_order(env, wav_bytes, asr="groq-whisper", source_edits=0)
    _reviewed_order(env, wav_bytes, asr="own", source_edits=2)
    s = model_stats.snapshot(30)
    asr = {r["name"]: r for r in s["asr"]}
    assert asr["groq-whisper"]["accuracy"] == 100.0 and asr["groq-whisper"]["orders"] == 1
    assert 90 < asr["own"]["accuracy"] < 100                                                     # a few words were corrected
    assert asr["own"]["confidence"] == 90.0 and asr["own"]["minutes"] == 4.0
    assert {r["name"] for r in s["mt"]} == {"lara"}


def test_too_little_reviewed_work_shows_no_percentage(env, wav_bytes):
    _reviewed_order(env, wav_bytes, words=40)
    row = model_stats.snapshot(30)["asr"][0]
    assert row["accuracy"] is None and row["reviewed_words"] < model_stats.MIN_WORDS_FOR_ACCURACY


def test_instant_ai_orders_count_in_usage_but_not_in_accuracy(env, wav_bytes):
    _reviewed_order(env, wav_bytes, tier="ai", asr="own")
    s = model_stats.snapshot(30)
    row = s["asr"][0]
    assert row["orders"] == 1 and row["accuracy"] is None and row["reviewed_words"] == 0 and s["instant_ai"] == 1


def test_a_provider_that_took_over_is_counted_as_a_fallback(env, wav_bytes):
    oid = _reviewed_order(env, wav_bytes, asr="own")
    conn = env.db.get_conn()
    conn.execute("UPDATE orders SET asr = 'groq-whisper' WHERE id = ?", (oid,))             # planned on Groq, but our own model ran
    conn.commit()
    conn.close()
    model_stats._cache.clear()
    s = model_stats.snapshot(30)
    assert s["fallbacks"] == 1 and s["asr"][0]["name"] == "own" and s["asr"][0]["fallbacks"] == 1


def test_human_review_numbers(env, wav_bytes):
    _reviewed_order(env, wav_bytes, source_edits=1)
    _reviewed_order(env, wav_bytes, status="awaiting_review")
    h = model_stats.human_review(30)
    assert h["orders"] == 2 and h["delivered"] == 1 and h["in_review"] == 1
    assert h["median_hours"] == 2.0 and h["within_day"] == 100.0
    assert h["segments"] == 4 and h["segments_changed"] == 1 and h["change_rate"] == 25.0
    assert h["pairs"][0]["source"] == "en" and h["pairs"][0]["target"] == "sw"


def test_the_page_is_for_staff_only_and_shows_the_tables(env, wav_bytes):
    _reviewed_order(env, wav_bytes, source_edits=1)
    assert env.client.get("/staff/models", follow_redirects=False).status_code in (302, 303, 307)
    env.db.get_or_create_user("s1", "staff@example.com", "staff")
    env.monkeypatch.setattr(env.app, "current_user", lambda request: {**dict(env.db.get_user("s1")), "client_scope_id": "s1"})
    page = env.client.get("/staff/models?days=90")
    assert page.status_code == 200
    for text in ("Models and review", "Transcription", "Translation", "Our own models", "Human-reviewed work", "Groq Whisper", "Lara Translate",
                 "Reviewer-kept accuracy", "Lines changed by reviewers"):
        assert text in page.text, text
