# Kauli - single-container deployment. Matches the real architecture the
# app already has (webapp/worker.py is explicit: one background job at a
# time, no task queue "yet") - this image is meant to run as ONE instance,
# not behind an autoscaler. See docs/deploy-runway.md (or the "Kauli
# Launch Runway" artifact) for why that's a deliberate choice, not a
# limitation of this Dockerfile.
#
# Build:  docker build -t kauli .
# Run:    docker run -d -p 8000:8000 --env-file .env \
#           -v kauli_data:/app/webapp/data \
#           --name kauli kauli
#
# webapp/data/ is a VOLUME, not baked into the image - it holds the
# SQLite db, client uploads, and every order's generated output. Losing
# that volume without a backup means losing every real order on the box;
# see the launch runway doc's "automate backups" step before this ever
# runs with real client data.

FROM python:3.12-slim AS base

# ffmpeg: used throughout kauli/mixer.py (time-stretch, gap-audio
# extraction, video muxing) - not optional, most of the pipeline shells
# out to it directly. curl: fetches the Piper voice models below.
# build-essential: sentencepiece/some transformers deps need to compile.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# CPU-only torch, installed BEFORE the rest of requirements.txt and
# pinned to the CPU wheel index - see requirements.txt's own comment on
# this exact trap: a plain `pip install torch` (or letting coqui-tts pull
# it in as a transitive dep) grabs multi-GB CUDA libraries this box will
# never use, and torchcodec crashes at import time looking for
# libnvrtc.so on a machine with no GPU. Version pin matches what's
# actually been tested against this codebase this session (torch 2.13).
RUN pip install --no-cache-dir torch==2.13.0 torchaudio torchcodec \
    --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# XTTS voice cloning (--tts xtts) - commented out of requirements.txt
# because its install order matters (see that file's own comment); torch
# is already CPU-only above, so this is safe to add now. CPML license
# requires COQUI_TOS_AGREED=1 for any non-interactive run, including
# every request this container ever serves - set for real, not just at
# build time, since XTTSCloneTTS._load() runs inside the running app.
ENV COQUI_TOS_AGREED=1
RUN pip install --no-cache-dir coqui-tts>=0.27.5

# Real Piper voice models - gitignored (data/voices/ - see .gitignore),
# so they don't exist anywhere except this build step. All 5 the voice
# picker in Ereri actually offers (webapp/app.py's piper_voices dict
# filters to whichever of these exist on disk) - baking them into the
# image trades ~370MB of image size for never needing a first-request
# download stall or a separate startup script. KAULI_PIPER_VOICE (.env)
# must point at one of these - en_GB-alba-medium.onnx is the project's
# own default.
RUN mkdir -p data/voices && cd data/voices && \
    curl -LO https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alba/medium/en_GB-alba-medium.onnx && \
    curl -LO https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alba/medium/en_GB-alba-medium.onnx.json && \
    curl -LO https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium/en_GB-alan-medium.onnx && \
    curl -LO https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium/en_GB-alan-medium.onnx.json && \
    curl -LO https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx && \
    curl -LO https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx.json && \
    curl -LO https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/ryan/medium/en_US-ryan-medium.onnx && \
    curl -LO https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/ryan/medium/en_US-ryan-medium.onnx.json && \
    curl -LO https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/high/en_US-lessac-high.onnx && \
    curl -LO https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/high/en_US-lessac-high.onnx.json

# Real app code - copied last so a code-only change doesn't invalidate
# the (slow, multi-GB) dependency layers above during a rebuild.
COPY kauli/ kauli/
COPY webapp/ webapp/
# Real secrets come from --env-file at `docker run` time (or Secret
# Manager/a mounted file, depending on host) - never baked into the
# image. .env.example itself is excluded from the build context
# (.dockerignore) - it's documentation for a human setting up .env, not
# something the running app reads.

# webapp/data/ holds the SQLite db, uploads, and generated output - a
# volume, not image content (see the run command in this file's own
# header comment). Declaring it here documents that even if you forget
# to mount it, rather than silently writing into the container's
# throwaway layer.
VOLUME ["/app/webapp/data"]

EXPOSE 8000

# No --reload (that's dev-only, watches the filesystem and restarts on
# every change - real overhead and an unnecessary attack surface in
# production). Single worker: matches worker.py's own "one job at a
# time" design - see this file's header comment on why this image is
# meant to run as exactly one instance, not N behind a load balancer.
CMD ["uvicorn", "webapp.app:app", "--host", "0.0.0.0", "--port", "8000"]
