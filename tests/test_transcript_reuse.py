"""Nothing is transcribed twice: ordering a translation (or any next step) for a file we already transcribed starts from the delivered transcript."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from kauli.models import Job, Segment  # noqa: E402
from webapp import transcript_reuse  # noqa: E402


def _delivered(env, wav_bytes, heard="the commitee approved the budget", corrected="The committee approved the budget."):
    """A finished order whose delivered transcript has one line the customer corrected."""
    o = env.order_from(env.place(wav_bytes))
    segs = [Segment(segment_id="s0", index=0, start_ms=0, end_ms=3000, source_transcript=heard, source_edited_transcript=corrected,
                    spoken="old translation", speaker_id="SPK_1", source_confidence=0.9, translation_confidence=0.8, audio_path="/x.wav"),
            Segment(segment_id="s1", index=1, start_ms=3000, end_ms=6000, source_transcript="and then we moved on", speaker_id="SPK_2",
                    spoken="old translation two")]
    Path(o["outdir"]).mkdir(parents=True, exist_ok=True)
    Job(segments=segs, providers={"asr": "own", "mt": "stub", "tts": "stub"}).save(str(Path(o["outdir"]) / "manifest.json"))
    conn = env.db.get_conn()
    conn.execute("UPDATE orders SET status = 'delivered' WHERE id = ?", (o["id"],))
    conn.commit()
    conn.close()
    return env.db.get_order(o["id"])


def _order_translation(env, original, **extra):
    env.monkeypatch.setattr(env.app, "_lara_configured", lambda: True)
    env.monkeypatch.setattr(env.app, "_pick_mt", lambda *a, **k: ("local", None))
    data = {"reuse_order_id": original["id"], "source_lang": original["source_lang"], "target_lang": "sw", "service_level": "translate",
            "review_tier": "ai"}
    data.update(extra)
    return env.order_from(env.client.post("/account/orders", data=data, follow_redirects=False))


def test_a_translation_of_a_delivered_file_starts_from_the_delivered_transcript(env, wav_bytes):
    original = _delivered(env, wav_bytes)
    new = _order_translation(env, original)
    assert new["reuse_of"] == original["id"] and new["asr"] == "stub" and new["source_sha256"]
    assert transcript_reuse.seed_manifest(new) is True
    seeded = Job.load(str(Path(new["outdir"]) / "manifest.json"))
    first, second = seeded.segments
    assert first.source_transcript == "The committee approved the budget."          # the customer's correction carried over
    assert second.source_transcript == "and then we moved on"
    assert first.speaker_id == "SPK_1" and (first.start_ms, first.end_ms) == (0, 3000)
    assert first.spoken == "" and first.audio_path is None                                                # everything downstream starts clean
    assert first.source_edited_transcript == "The committee approved the budget." and second.source_edited_transcript is None   # the human correction stays a correction


def test_the_pipeline_skips_recognition_and_keeps_the_delivered_text(env, wav_bytes, monkeypatch):
    from kauli import pipeline
    original = _delivered(env, wav_bytes)
    new = _order_translation(env, original)
    assert transcript_reuse.seed_manifest(new)
    monkeypatch.setattr(pipeline, "get_asr", lambda name: pytest.fail("speech recognition must not run again"))
    job = pipeline.run(audio_path=new["audio_path"], outdir=new["outdir"], asr="stub", mt="stub", tts="stub", source_lang="en", target_lang="en",
                       verbose=False, resume=True, skip_tts=True)
    assert [s.source_transcript for s in job.segments] == ["The committee approved the budget.", "and then we moved on"]


def test_sending_the_same_file_again_is_recognised_without_pressing_anything(env, wav_bytes):
    original = _delivered(env, wav_bytes)
    again = env.order_from(env.place(wav_bytes))                                      # the same bytes, uploaded as a brand new order
    assert again["reuse_of"] == original["id"] and again["asr"] == "stub"


def test_a_different_file_or_language_or_account_is_never_reused(env, wav_bytes):
    original = _delivered(env, wav_bytes)
    other = bytearray(wav_bytes)
    other[-400:-200] = b"\x10\x20" * 100                                             # same length, different sound
    assert not env.order_from(env.place(bytes(other)))["reuse_of"]                   # not the same file
    sw = env.order_from(env.place(wav_bytes, source_lang="sw", target_lang="en"))
    assert not sw["reuse_of"]                                                          # a different spoken language would need new recognition
    env.db.get_or_create_user("other", "other@example.com", "client")
    conn = env.db.get_conn()
    conn.execute("UPDATE orders SET client_id = 'other' WHERE id = ?", (original["id"],))
    conn.commit()
    conn.close()
    assert not env.order_from(env.place(wav_bytes))["reuse_of"]                      # another account's transcript is never used


def test_an_unfinished_or_empty_transcript_is_not_reused(env, wav_bytes):
    original = _delivered(env, wav_bytes)
    conn = env.db.get_conn()
    conn.execute("UPDATE orders SET status = 'processing' WHERE id = ?", (original["id"],))
    conn.commit()
    conn.close()
    assert not env.order_from(env.place(wav_bytes))["reuse_of"]
    assert transcript_reuse.seed_manifest(env.db.get_order(original["id"])) is False                      # nothing to seed from


def test_the_page_says_the_transcript_was_reused_and_links_to_it(env, wav_bytes):
    original = _delivered(env, wav_bytes)
    new = _order_translation(env, original)
    page = env.client.get(f"/account/orders/{new['id']}").text
    assert "earlier transcript" in page and f"/account/orders/{original['id']}" in page
    more = env.client.get(f"/account/orders/{original['id']}/more").text
    assert "nothing to upload or transcribe again" in more


def test_without_a_plan_nobody_can_edit_or_download(env, wav_bytes):
    original = _delivered(env, wav_bytes)
    conn = env.db.get_conn()
    conn.execute("UPDATE users SET plan_chosen_at = NULL WHERE id = 'u1'")
    conn.execute("UPDATE orders SET is_free_preview = 1, duration_minutes = 0 WHERE id = ?", (original["id"],))      # a free-minutes order with nothing to pay
    conn.commit()
    conn.close()
    oid = original["id"]
    assert env.client.post(f"/account/orders/{oid}/transcript", json={"edits": []}).status_code == 403                 # edit
    for kind in ("docx", "txt", "srt", "source", "audio", "dubbed", "burned"):
        r = env.client.get(f"/account/orders/{oid}/download/{kind}", follow_redirects=False)
        assert r.status_code in (303, 403, 404) and "choose_plan" in r.headers.get("location", "choose_plan"), kind
    assert env.client.get(f"/account/orders/{oid}/export?format=docx").status_code == 403                              # export
    assert env.client.post(f"/account/orders/{oid}/share", json={"action": "create"}).status_code == 403             # share link
    env.client.post("/account/billing/choose-free")
    assert env.client.get(f"/account/orders/{oid}/export?format=txt").status_code == 200                              # the Free plan unlocks it



# ------------------------------------------------ the editor's work comes with the transcript
def _edited(env, wav_bytes):
    """A delivered order an editor worked on: speaker names typed in, a paragraph break, italics, a sound tag, a named speaker with a voice."""
    o = env.order_from(env.place(wav_bytes))
    segs = [Segment(segment_id="s0", index=0, start_ms=0, end_ms=3000, source_transcript="amara okay so we begin",
                    source_edited_transcript="AMARA: Okay, so <i>we</i> begin.\n\nThen the second paragraph [APPLAUSE]", speaker_id="SPK_1",
                    para_start_source=True, spoken="old", audio_path="/x.wav"),
            Segment(segment_id="s1", index=1, start_ms=3000, end_ms=6000, source_transcript="thank you for having me", speaker_id="SPK_2")]
    Path(o["outdir"]).mkdir(parents=True, exist_ok=True)
    Job(segments=segs, providers={"asr": "own", "mt": "stub", "tts": "stub"}, source_duration_ms=6000,
        speaker_voices={"SPK_1": "amy"}, speaker_voice_names={"SPK_1": "sw-KE-ZuriNeural"}, speaker_subtitle_included={"SPK_2": False}
        ).save(str(Path(o["outdir"]) / "manifest.json"))
    conn = env.db.get_conn()
    conn.execute("UPDATE orders SET status = 'delivered' WHERE id = ?", (o["id"],))
    conn.commit()
    conn.close()
    return env.db.get_order(o["id"])


def test_the_new_order_keeps_speaker_names_paragraphs_italics_and_who_is_who(env, wav_bytes):
    new = _order_translation(env, _edited(env, wav_bytes))
    assert transcript_reuse.seed_manifest(new)
    seeded = Job.load(str(Path(new["outdir"]) / "manifest.json"))
    first = seeded.segments[0]
    assert first.source_edited_transcript == "AMARA: Okay, so <i>we</i> begin.\n\nThen the second paragraph [APPLAUSE]"
    assert first.speaker_id == "SPK_1" and first.para_start_source and first.para_start_target             # a paragraph the editor started stays one
    assert seeded.speaker_voices == {"SPK_1": "amy"} and seeded.speaker_voice_names == {"SPK_1": "sw-KE-ZuriNeural"}
    assert seeded.speaker_subtitle_included == {"SPK_2": False} and seeded.source_duration_ms == 6000
    cells = env.app._build_source_cells(first)                                                            # what the new order's editor shows
    words = [c["text"].strip() for c in cells if c["type"] == "word"]
    assert "Okay," in words and "<i>we</i>" in words and "[APPLAUSE]" in words and "amara" not in " ".join(words)
    assert any(c.get("speaker_tag") for c in cells) and any(c.get("para_start") for c in cells)


def test_running_the_new_order_keeps_who_is_who_and_the_formatting_in_the_translation(env, wav_bytes, monkeypatch):
    from kauli import pipeline
    new = _order_translation(env, _edited(env, wav_bytes))
    assert transcript_reuse.seed_manifest(new)
    seen = []

    class _Echo:
        total_cost_usd = 0.0

        def translate(self, text, target_chars, source_lang, target_lang):
            seen.append(text)
            return {"literal": text, "spoken": text, "confidence": 0.95}

    monkeypatch.setattr(pipeline, "get_asr", lambda name: pytest.fail("speech recognition must not run again"))
    monkeypatch.setattr(pipeline, "get_mt", lambda name, **kw: _Echo())
    job = pipeline.run(audio_path=new["audio_path"], outdir=new["outdir"], asr="stub", mt="local", tts="stub", source_lang="en", target_lang="sw",
                       verbose=False, resume=True, skip_tts=True)
    assert job.speaker_voices == {"SPK_1": "amy"} and job.speaker_voice_names == {"SPK_1": "sw-KE-ZuriNeural"} and job.speaker_subtitle_included == {"SPK_2": False}
    final = job.segments[0].final_text
    assert final.startswith("AMARA:") and "<i>we</i>" in final and "\n\n" in final and "[APPLAUSE]" in final
    assert "<i>" not in " ".join(seen)                                                                    # the translator never saw the italics marks, only the words inside


def test_italics_marks_survive_a_translator_that_moves_or_spaces_them():
    from kauli import pipeline
    protected, holders = pipeline._protect_untranslatable("Okay, so <i>we</i> begin. >> Next speaker.")
    assert "<i>" not in protected and "</i>" not in protected and ">>" not in protected
    spaced = protected.replace("QQTAG0QQ", "QQTAG0QQ ")                                                     # a translator that adds spaces
    restored = pipeline._restore_untranslatable(spaced, holders)
    assert "<i>we</i>" in restored and ">> " in restored
