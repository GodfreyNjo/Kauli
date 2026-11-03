"""A speaker name the human editor typed is the only speaker label a customer sees - never SPK_1 beside it."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from kauli.models import Job, Segment, label_speakers  # noqa: E402
from webapp import transcript_export  # noqa: E402


def _job():
    return Job(segments=[
        Segment(segment_id="s0", index=0, start_ms=0, end_ms=3000, source_transcript="AMARA: Okay, so we begin.", speaker_id="SPK_1"),
        Segment(segment_id="s1", index=1, start_ms=3000, end_ms=6000, source_transcript="Thank you for having me.", speaker_id="SPK_2"),
        Segment(segment_id="s2", index=2, start_ms=6000, end_ms=9000, source_transcript="And the second point is simple.", speaker_id="SPK_1"),
    ], providers={"asr": "own", "mt": "stub", "tts": "stub"})


def test_an_editors_name_replaces_the_machine_label_and_is_not_repeated_in_the_text():
    out = label_speakers([("SPK_1", "AMARA: Okay, so we begin."), ("SPK_2", "Thank you."), ("SPK_1", "And the second point.")])
    assert out[0][:2] == ("AMARA", "Okay, so we begin.") and out[0][2].startswith("AMARA:")
    assert out[1][0] == ""                                          # others are named in this transcript, so a leftover SPK_2 is not shown among them
    assert out[2][0] == "AMARA"                                     # the same speaker, named once, is named everywhere
    assert [o[0] for o in label_speakers([("SPK_1", "Hello."), ("SPK_2", "Hi.")])] == ["SPK_1", "SPK_2"]       # nobody named: the labels stay


def test_two_different_names_for_one_label_are_not_guessed_across_lines():
    out = label_speakers([("SPK_1", "AMARA: Hello."), ("SPK_1", "JOHN: Hi."), ("SPK_1", "No name here.")])
    assert [o[0] for o in out] == ["AMARA", "JOHN", ""]


def test_exports_show_the_name_once():
    rows = transcript_export.rows(_job())
    assert rows[0]["speaker"] == "AMARA" and rows[0]["text"] == "Okay, so we begin."
    assert rows[1]["speaker"] == ""
    text = transcript_export.to_txt(_job(), "T", timestamps=False).decode()
    assert "AMARA: Okay, so we begin." in text and "SPK_1" not in text and "AMARA: AMARA" not in text
    assert "AMARA,Okay, so we begin." in transcript_export.to_csv(_job(), timestamps=False).decode("utf-8-sig").replace('"', "")


def test_the_order_page_shows_only_the_editors_name(env, wav_bytes):
    o = env.order_from(env.place(wav_bytes))
    Path(o["outdir"]).mkdir(parents=True, exist_ok=True)
    _job().save(str(Path(o["outdir"]) / "manifest.json"))
    conn = env.db.get_conn()
    conn.execute("UPDATE orders SET status = 'delivered' WHERE id = ?", (o["id"],))
    conn.commit()
    conn.close()
    env.db.set_order_free_preview(o["id"], False)                    # paid for, so the full transcript shows rather than a teaser
    job = Job.load(str(Path(o["outdir"]) / "manifest.json"))
    for seg in job.segments:
        seg.spoken = seg.source_transcript                          # a same-language order: the delivered text is the transcript itself
    job.save(str(Path(o["outdir"]) / "manifest.json"))
    page = env.client.get(f"/client/orders/{o['id']}").text
    assert ">AMARA</span>" in page and "SPK_1" not in page          # the name is the label, and the machine label is gone
    assert 'data-edit="source" data-tag="AMARA:" style="font-size:0.85rem;">Okay, so we begin.</td>' in page    # the name is not repeated inside the line
    assert "SPK_2" not in page                                      # nor is a leftover machine label shown among the named speakers
    # (the stored text keeps the tag: the dubbing step reads it, and a saved edit puts it back)


def test_action_messages_are_a_coloured_toast_loaded_on_every_page(env):
    page = env.client.get("/login").text
    assert "/static/toast.js" in page
    js = (Path(__file__).parent.parent / "webapp" / "static" / "toast.js").read_text(encoding="utf-8")
    for kind in ("success", "error", "warning", "info"):
        assert f"{kind}:" in js
    css = (Path(__file__).parent.parent / "webapp" / "static" / "style.css").read_text(encoding="utf-8")
    for kind in ("success", "error", "warning", "info"):
        assert f".kp-toast-{kind}" in css


# ------------------------------------------------------------------------------------------------ Ereri: paragraph breaks survive a save


def _staff(env):
    env.db.get_or_create_user("staff1", "staff@example.com", "staff")
    env.monkeypatch.setattr(env.app, "current_user", lambda request: {**dict(env.db.get_user("staff1")), "role": "staff"})


def _editable(env, wav_bytes):
    o = env.order_from(env.place(wav_bytes))
    Path(o["outdir"]).mkdir(parents=True, exist_ok=True)
    Job(segments=[Segment(segment_id="s0", index=0, start_ms=0, end_ms=3000, source_transcript="Okay so we begin", spoken="Okay so we begin", approved=True),
                  Segment(segment_id="s1", index=1, start_ms=3000, end_ms=6000, source_transcript="thank you for having me", spoken="thank you for having me", approved=True)],
        providers={"asr": "own", "mt": "stub", "tts": "stub"}).save(str(Path(o["outdir"]) / "manifest.json"))
    return o


def test_a_break_before_a_segments_first_word_survives_saving_and_reloading(env, wav_bytes):
    o = _editable(env, wav_bytes)
    _staff(env)
    assert env.client.post(f"/staff/orders/{o['id']}/segments/s1/source", json={"text": "AMARA: thank you for having me", "para_start": True}).status_code == 200
    assert env.client.post(f"/staff/orders/{o['id']}/segments/s1/save", json={"text": "AMARA: thank you for having me", "para_start": True}).status_code == 200
    job = Job.load(str(Path(o["outdir"]) / "manifest.json"))
    s1 = job.segments[1]
    assert s1.para_start_source and s1.para_start_target
    assert env.app._build_source_cells(s1)[0]["para_start"] is True and env.app._build_target_cells(s1)[0]["para_start"] is True
    assert not env.app._build_target_cells(job.segments[0])[0].get("para_start")
    env.client.post(f"/staff/orders/{o['id']}/segments/s1/save", json={"text": "AMARA: thank you for having me", "para_start": False})     # Shift+Backspace removed it
    assert Job.load(str(Path(o["outdir"]) / "manifest.json")).segments[1].para_start_target is False


def test_a_break_inside_a_segment_keeps_its_speaker_tag_as_one_cell(env, wav_bytes):
    o = _editable(env, wav_bytes)
    _staff(env)
    env.client.post(f"/staff/orders/{o['id']}/segments/s0/save", json={"text": "Okay so\n\nAMARA: we begin"})
    seg = Job.load(str(Path(o["outdir"]) / "manifest.json")).segments[0]
    cells = env.app._build_target_cells(seg)
    tag = [c for c in cells if c.get("speaker_tag")]
    assert tag and tag[0]["text"] == "AMARA:" and tag[0]["para_start"] is True


def test_the_editor_script_has_the_macro_paragraph_option_and_shift_backspace():
    js = (Path(__file__).parent.parent / "webapp" / "static" / "editor.js").read_text(encoding="utf-8")
    assert "macro-para" in js and "macro.para" in js and "SPEAKER_MACRO_RE" in js
    assert 'e.shiftKey && !e.ctrlKey && !e.altKey && e.key === "Backspace"' in js
    assert '(e.ctrlKey || e.shiftKey) && !e.altKey && key === "enter"' in js                    # Shift+Enter starts a paragraph
    assert "para_start: paraStart" in js
