from __future__ import annotations

import argparse
import sys
from pathlib import Path


def cmd_run(a):
    from .pipeline import run
    run(audio_path=a.audio, outdir=a.out, asr=a.asr, mt=a.mt, tts=a.tts,
        source_lang=a.source, target_lang=a.target, voice_id=a.voice,
        skip_tts=a.skip_tts)


def cmd_calibrate(a):
    """Measure your TTS voice's real characters-per-second. Run this once per
    voice. The default constants in timing.py are guesses; yours won't be."""
    from .providers import get_tts
    from statistics import mean
    import tempfile, os

    sentences = [
        "Hello, my name is Wanjiru and I am calling from Duka Bora.",
        "Your order arrived at our Kilimani branch this morning.",
        "You can collect it today before five in the evening.",
        "Please bring your receipt and a form of identification.",
        "Thank you very much for your patience, we appreciate it.",
    ]
    tts = get_tts(a.tts)
    rates = []
    for i, s in enumerate(sentences):
        p = os.path.join(tempfile.gettempdir(), f"cal_{i}.wav")
        ms = tts.synthesize(s, p, voice_id=a.voice)
        r = len(s) / (ms / 1000)
        rates.append(r)
        print(f"  {len(s):3d} chars -> {ms:5d} ms  = {r:5.2f} cps")
    print(f"\nMean: {mean(rates):.2f} cps")
    print(f"Put this in kauli/timing.py DEFAULT_CPS for '{a.lang}'.")


def cmd_report(a):
    """What needs a human, and why. Run this before opening the editor."""
    from .models import Job
    job = Job.load(a.manifest)
    print(f"Job {job.job_id} | {job.status}")
    print(f"  {len(job.segments)} segments | fit rate {job.fit_rate:.0%} | "
          f"{job.flagged_count} flagged\n")
    for s in job.segments:
        if not s.review_flag:
            continue
        print(f"[{s.segment_id}] {s.start_ms/1000:6.2f}s  ({', '.join(s.review_reasons)})")
        print(f"   SW: {s.source_transcript}")
        print(f"   EN: {s.final_text}")
        if s.cultural_notes:
            print(f"   ! {s.cultural_notes}")
        print(f"   fit {s.fit_ratio:.2f} [{s.fit_status}]\n")


def main(argv=None):
    p = argparse.ArgumentParser(prog="kauli", description="Kiswahili <-> English dubbing")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="process an audio file end to end")
    r.add_argument("audio")
    r.add_argument("-o", "--out", default="./output")
    r.add_argument("--asr", default="faster-whisper",
                   choices=["stub", "faster-whisper", "aws-transcribe"])
    r.add_argument("--mt", default="claude", choices=["stub", "claude", "aws-translate", "local"])
    r.add_argument("--tts", default="piper", choices=["stub", "piper", "azure", "xtts"])
    r.add_argument("--source", default="sw")
    r.add_argument("--target", default="en")
    r.add_argument("--voice", default=None)
    r.add_argument("--skip-tts", action="store_true",
                   help="transcript + translation + subtitles only (cheapest mode)")
    r.set_defaults(func=cmd_run)

    c = sub.add_parser("calibrate", help="measure a TTS voice's chars-per-second")
    c.add_argument("--tts", default="piper")
    c.add_argument("--voice", default=None)
    c.add_argument("--lang", default="en")
    c.set_defaults(func=cmd_calibrate)

    rep = sub.add_parser("report", help="list segments needing human review")
    rep.add_argument("manifest")
    rep.set_defaults(func=cmd_report)

    a = p.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
