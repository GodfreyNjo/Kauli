"""Measure our own models, language by language, against real recordings and human translations (Google's FLEURS: the same sentences
read aloud and translated by people in about 100 languages).

    python scripts/eval_languages.py results.jsonl [--clips 6] [--only sw,fr,de]

For each of Kauli's languages (the ones Kauli can transcribe) it records:
  speech   our own transcription model on the first few real recordings: word error rate (character error rate for languages written
           without spaces), the model's own confidence, and how fast it ran compared with the audio
  from_en  our own translation model, English into that language, scored (chrF++) against the human translation of the same sentence
  to_en    the same, that language into English
Progress is written one line per language, so a stop is never a loss and a rerun skips what is done. `scripts/approve_languages.py`
turns the results into the approved lists the app reads.
"""
import argparse
import io
import json
import os
import re
import sys
import tarfile
import tempfile
import time
import unicodedata
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = "https://huggingface.co/datasets/google/fleurs/resolve/main/data/{cfg}/"
CHAR_LANGS = {"zh", "yue", "ja", "th", "lo", "km", "my"}                 # written without spaces between words: judged by characters
SPECIAL_CFG = {"zh": "cmn_hans_cn", "yue": "yue_hant_hk", "tl": "fil_ph", "no": "nb_no", "es": "es_419", "pt": "pt_br", "en": "en_us", "sw": "sw_ke"}
WHISPER_CODE = {"jv": "jw"}
MADLAD_TAGS = {"he": ["he", "iw"], "jv": ["jv", "jw"], "tl": ["tl", "fil"], "zh": ["zh", "zh_Hans"], "id": ["id", "in"], "yue": ["yue", "zh_Hant"]}


def get(url: str) -> bytes:
    return urllib.request.urlopen(url, timeout=90).read()


def configs() -> list[str]:
    api = json.loads(get("https://datasets-server.huggingface.co/parquet?dataset=google/fleurs"))
    return sorted({f["config"] for f in api["parquet_files"] if f["split"] == "test" and f["config"] != "all"})


def config_for(code: str, available: list[str]) -> str | None:
    if SPECIAL_CFG.get(code) in available:
        return SPECIAL_CFG[code]
    return next((c for c in available if c.split("_")[0] == code), None)


def tsv(cfg: str) -> list[list[str]]:
    return [r.split("\t") for r in get(BASE.format(cfg=cfg) + "test.tsv").decode("utf-8").strip().split("\n")]


def norm(text: str) -> str:
    text = "".join(" " if unicodedata.category(c)[0] in "PS" else c for c in text.lower())
    return " ".join(text.split())


def edit_distance(a: list, b: list) -> int:
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def error_rate(ref: str, hyp: str, by_char: bool) -> float:
    r, h = norm(ref), norm(hyp)
    r, h = (list(r.replace(" ", "")), list(h.replace(" ", ""))) if by_char else (r.split(), h.split())
    return edit_distance(r, h) / max(len(r), 1)


def first_clips(cfg: str, rows: list[list[str]], n: int) -> list[tuple[bytes, str]]:
    """The first n recordings in the archive, with their reference text. Only the start of the archive is downloaded."""
    text = {r[1]: r[3] for r in rows}
    out = []
    resp = urllib.request.urlopen(BASE.format(cfg=cfg) + "audio/test.tar.gz", timeout=90)
    with tarfile.open(fileobj=resp, mode="r|gz") as tf:
        for m in tf:
            name = Path(m.name).name
            if m.isfile() and name in text:
                out.append((tf.extractfile(m).read(), text[name]))
                if len(out) >= n:
                    break
    return out


def eval_speech(code: str, cfg: str, rows: list[list[str]], clips: int) -> dict:
    from kauli.providers.asr import LowQualityTranscript, OwnASR, _ffprobe_duration_ms
    from kauli.providers.asr_cloud import WHISPER_LANGUAGES
    lang = WHISPER_CODE.get(code, code)
    if lang not in WHISPER_LANGUAGES:
        return {"status": "model does not support this language"}
    asr = EVAL["asr"]
    errs, confs, took, audio_s = [], [], 0.0, 0.0
    for data, ref in first_clips(cfg, rows, clips):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(data)
        try:
            t0 = time.time()
            try:
                segs = asr.transcribe(f.name, language=lang)
            except LowQualityTranscript as exc:
                segs = exc.segments
            took += time.time() - t0
            audio_s += _ffprobe_duration_ms(f.name) / 1000
        finally:
            os.unlink(f.name)
        errs.append(error_rate(ref, " ".join(s.source_transcript for s in segs), code in CHAR_LANGS))
        c = OwnASR.confidence(segs)
        if c is not None:
            confs.append(c)
    return {"status": "ok", "metric": "CER" if code in CHAR_LANGS else "WER", "error": round(sum(errs) / len(errs), 3), "clips": len(errs),
            "confidence": round(sum(confs) / len(confs), 3) if confs else None, "speed": round(took / audio_s, 2) if audio_s else None}


def madlad_tag(sp, code: str) -> str | None:
    for tag in MADLAD_TAGS.get(code, [code]):
        if sp.piece_to_id(f"<2{tag}>") != sp.unk_id():
            return tag
    return None


def translate(text: str, tag: str) -> str:
    translator, sp = EVAL["mt"]
    tokens = sp.encode(f"<2{tag}> {text}", out_type=str) + ["</s>"]
    res = translator.translate_batch([tokens], beam_size=4, max_decoding_length=max(64, min(512, len(tokens) * 3)), no_repeat_ngram_size=4)
    return sp.decode(res[0].hypotheses[0]).strip()


def eval_translation(code: str, rows: list[list[str]], en_rows: list[list[str]], n: int) -> dict:
    from sacrebleu.metrics import CHRF
    _t, sp = EVAL["mt"]
    tag = madlad_tag(sp, code)
    if not tag:
        return {"status": "model does not support this language"}
    en = {r[0]: r[2] for r in en_rows}
    pairs = [(en[r[0]], r[2]) for r in rows if r[0] in en][:n]                 # (English, same sentence in this language), human-made
    if not pairs:
        return {"status": "no matching sentences"}
    chrf = CHRF(word_order=2)
    from_en = [translate(e, tag) for e, _x in pairs]
    to_en = [translate(x, "en") for _e, x in pairs]
    return {"status": "ok", "sentences": len(pairs),
            "from_en": round(chrf.corpus_score(from_en, [[x for _e, x in pairs]]).score, 1),
            "to_en": round(chrf.corpus_score(to_en, [[e for e, _x in pairs]]).score, 1),
            "sample_from_en": [(pairs[0][0], from_en[0], pairs[0][1])], "sample_to_en": [(pairs[0][1], to_en[0], pairs[0][0])]}


EVAL: dict = {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("--clips", type=int, default=6)
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    from kauli.providers.asr import TRANSKRIPTOR_LANGUAGES, OwnASR
    from kauli.providers.mt import MadladMT
    EVAL["asr"] = OwnASR()
    EVAL["mt"] = MadladMT._load()
    available = configs()
    en_rows = tsv("en_us")
    done = set()
    out = Path(a.results)
    if out.exists():
        done = {json.loads(line)["code"] for line in out.read_text(encoding="utf-8").splitlines() if line.strip()}
    codes = [c for c in (a.only.split(",") if a.only else sorted(set(TRANSKRIPTOR_LANGUAGES) | {"sw", "en"})) if c and c not in done]
    for code in codes:
        cfg = config_for(code, available)
        row = {"code": code, "config": cfg, "model": EVAL["asr"].model_size}
        t0 = time.time()
        if not cfg:
            row["speech"] = row["translation"] = {"status": "no test data for this language"}
        else:
            rows = tsv(cfg)
            for key, fn in (("speech", lambda: eval_speech(code, cfg, rows, a.clips)),
                            ("translation", lambda: eval_translation(code, rows, en_rows, a.clips) if code != "en" else {"status": "source language"})):
                try:
                    row[key] = fn()
                except Exception as exc:  # noqa: BLE001 - one language failing must not stop the run
                    row[key] = {"status": f"error: {exc.__class__.__name__}: {str(exc)[:80]}"}
        row["seconds"] = round(time.time() - t0, 1)
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        sp, tr = row.get("speech", {}), row.get("translation", {})
        print(f"{code:>4} {row['seconds']:>6}s  speech: {sp.get('error', sp.get('status'))}  from_en: {tr.get('from_en', tr.get('status'))}  to_en: {tr.get('to_en', '-')}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
