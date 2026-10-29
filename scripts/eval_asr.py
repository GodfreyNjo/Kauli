"""Measure transcription providers on the same recordings, so we know where our own model is good enough and where an outside provider is
still needed.

    python scripts/eval_asr.py en own,groq-whisper,transkriptor talk1.wav talk1.txt talk2.wav talk2.txt

Each recording comes with a reference text file (what was really said, typed or corrected by a person). For every provider it prints the
word error rate (lower is better; 0.10 means about one word in ten is wrong), how fast it ran compared with the recording
(0.5 = twice as fast as real time), and our own model's confidence. A provider with no key is skipped, not failed.

Rule of thumb for switching a language to our own model (KAULI_OWN_ASR_LANGUAGES): its word error rate on your real recordings is within
a couple of points of the paid provider's, and it runs faster than real time on the server.
"""
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def words(text: str) -> list[str]:
    return re.findall(r"[\w']+", text.lower())


def wer(reference: str, hypothesis: str) -> float:
    r, h = words(reference), words(hypothesis)
    if not r:
        return 0.0 if not h else 1.0
    prev = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        cur = [i]
        for j, hw in enumerate(h, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rw != hw)))
        prev = cur
    return prev[-1] / len(r)


def main() -> int:
    if len(sys.argv) < 5 or (len(sys.argv) - 3) % 2:
        print(__doc__)
        return 1
    language, providers = sys.argv[1], sys.argv[2].split(",")
    pairs = [(sys.argv[i], sys.argv[i + 1]) for i in range(3, len(sys.argv), 2)]
    from kauli.providers.asr import OwnASR, _ffprobe_duration_ms, get_asr
    print(f"{'provider':<18}{'WER':>8}{'speed':>9}{'confidence':>12}")
    for name in providers:
        errs, took, audio_s, conf = [], 0.0, 0.0, []
        try:
            provider = get_asr(name)
            for audio, ref in pairs:
                t0 = time.time()
                try:
                    segs = provider.transcribe(audio, language=language)
                except Exception as exc:  # noqa: BLE001 - a low-confidence draft still has text to score
                    segs = getattr(exc, "segments", None)
                    if segs is None:
                        raise
                took += time.time() - t0
                audio_s += _ffprobe_duration_ms(audio) / 1000
                errs.append(wer(Path(ref).read_text(encoding="utf-8"), " ".join(s.source_transcript for s in segs)))
                c = OwnASR.confidence(segs)
                if c is not None:
                    conf.append(c)
        except Exception as exc:  # noqa: BLE001
            print(f"{name:<18}skipped ({exc.__class__.__name__}: {str(exc)[:60]})")
            continue
        print(f"{name:<18}{sum(errs) / len(errs):>8.3f}{took / audio_s if audio_s else 0:>9.2f}{(sum(conf) / len(conf) if conf else 0):>12.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
