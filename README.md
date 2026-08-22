# Kauli — Kiswahili ↔ English dubbing pipeline

Audio in → time-aligned transcript, translation, dubbed audio track, SRT/VTT out.
Built to run on one laptop for zero shillings until a paying client forces otherwise.

## The rule this repo is built around

**Spend nothing you don't have to.** Every component has a free local option and a
paid cloud option. Default to local. Only move a stage to the cloud when you can
show, on your own audio, that the paid version is better — not because a vendor
benchmark says so.

## Setup (30 minutes, no cloud account needed)

```bash
git clone <your-repo> kauli && cd kauli
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in ANTHROPIC_API_KEY
```

Check it works with zero models installed:

```bash
python -m kauli.cli run /dev/null -o ./output --asr stub --mt stub --tts stub
python -m kauli.cli report ./output/manifest.json
python tests/test_pipeline.py
```

Then get the real ASR and voice:

```bash
# ASR: first run downloads the model (~500MB for 'small'), then it's offline forever
python -c "from faster_whisper import WhisperModel; WhisperModel('small')"

# TTS voice: download one Piper voice into data/voices/
# https://github.com/rhasspy/piper  → en_GB-alba-medium (.onnx + .onnx.json)
```

Real run:

```bash
python -m kauli.cli run ./data/audio/job001.wav -o ./output/job001 \
    --asr faster-whisper --mt claude --tts piper
```

**Calibrate before your first paid job.** The chars-per-second constants in
`kauli/timing.py` are guesses. Yours will be different, and a wrong value costs
you 10–15% of your fit rate:

```bash
python -m kauli.cli calibrate --tts piper --voice ./data/voices/en_GB-alba-medium.onnx
```

## What each stage costs

| Stage | Free option | Paid option | When to switch |
|---|---|---|---|
| ASR | faster-whisper, local | AWS Transcribe ~$0.024/min | Only if local loses on your own test set |
| Translate | — | Claude Haiku (cents/hour) or AWS Translate ($15/M chars, 2M/mo free for 12mo) | Start paid here — this is where quality lives |
| TTS → English | Piper, local | Polly / ElevenLabs | When a client rejects the voice |
| TTS → Swahili | **none good** | Azure `sw-KE-ZuriNeural`, Google Cloud TTS | Required for en→sw. Polly has no Swahili. |

Practical figure: an hour of audio through local ASR + Claude translation + Piper
costs roughly **one US dollar**, nearly all of it translation. That is your entire
cost of goods. Everything else is your time.

## Why sw→en first

The output is English, where free local TTS is genuinely good. English→Swahili
needs a paid Azure account *and* is the harder direction for timing, because
Kiswahili renders longer than English and overruns the original timing. Do the
easy, free, profitable direction first.

## Architecture

```
audio ──► ASR ──► translate + fit ──► TTS ──► mix ──► deliverables
            │           │              │        │
            └───────────┴──────────────┴────────┴──► manifest.json
                                                      (single source of truth,
                                                       written after every stage)
```

`manifest.json` is the whole system's memory. Any stage can be re-run against it.
The review editor reads and writes it. Don't add a database until this genuinely hurts.

### The part that matters: duration fitting

Translation is not one call. We ask for four candidates at different lengths
(`literal`, `spoken`, `shorter`, `longer`), estimate how long each will take to
speak, and pick the one that lands inside 0.90–1.10× the source duration while
keeping meaning. If nothing fits, the segment is flagged for a human rather than
silently squashed. Time-stretching is capped at ±8% — beyond that it sounds cheap
and it's the first thing a client notices.

### The Swahili clock

`saa kumi na moja jioni` is **17:00**, not 11:00. Swahili hours run six hours
offset. Every literal translator gets this wrong, and in call-centre or health
content it's a real-world failure, not a style complaint. It's in the translation
prompt, it's flagged for review, and there's a test that fails if the flagging
ever stops working.

## Layout

```
kauli/
  models.py       Job + Segment. The data model.
  timing.py       Duration estimation and candidate selection.
  providers/      asr.py / mt.py / tts.py — swappable, each with a free stub
  mixer.py        Timeline assembly, time-stretch, normalisation
  subtitles.py    SRT / WebVTT
  pipeline.py     Orchestration
  cli.py          run / calibrate / report
tests/            Run these before every client delivery
webapp/           Local demo UI - see below. NOT the real Phase 2/3 platform.
```

## Local demo UI (webapp/)

A clickable prototype on top of the pipeline above - client upload, staff
review/correction (in **Ereri**, the word-synced transcript editor - see
below), approve, client download. Built to *experience* the flow and demo
it to early pilot clients, not to onboard real ones:

- Real auth (Supabase email/password) - see webapp/supabase_auth.py
- SQLite, single machine, not deployed anywhere
- One "staff" role covers contractor + QA + admin (per the roadmap: that's
  correct until there's a second person to actually split it across)

### Ereri (webapp/templates/editor.html + static/editor.js)

The transcript correction workspace: audio player, word-level cells tied to
real timestamps, click-to-seek. Two-step workflow - correct the Swahili ASR
source first (real per-word timing), then the English translation (approximate
timing, optionally re-translated from your corrected source). Sound-tag gap
cells at real silence intervals, cell merging (ctrl+m), speaker-ID macros
(ctrl+1..0), paragraph breaks (ctrl+enter), prose preview (ctrl+p). See the
in-app Shortcuts tab for the full list.

Run it:
```bash
uvicorn webapp.app:app --reload --port 8000
```
Then open http://localhost:8000 and log in as either:
- `client_demo@example.com` / `demo` - upload, track status, download
- `staff_demo@forgemedia.example` / `demo` - queue, review flagged segments,
  edit + re-synthesize, approve for delivery

This is genuinely Phase 2-5 territory from the platform roadmap, built early
and deliberately scoped down (no real accounts, not deployed, single tenant)
specifically so it doesn't cost the "premature platform" trap the roadmap
warns about. Treat it as a demo tool, not a decision that Phase 1 is done.

## Working practice

- `--skip-tts` gives transcript + translation + subtitles only. Many early clients
  want exactly that and it's your cheapest, fastest product.
- `kauli report` before opening the editor — it tells you which segments need you
  and why, so you're not scrubbing through the whole file.
- Every human edit is training data. Keep every manifest. In a year that archive
  is worth more than the code.
- Never commit `.env`, client audio, or output. See `.gitignore`.

## Not built yet

- Review editor (next)
- Speaker diarization (`pyannote`) — only needed for multi-speaker content
- en→sw direction — needs an Azure key
- Voice cloning — needs written consent per speaker before you touch it
