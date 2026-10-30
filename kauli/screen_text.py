"""Keeping captions clear of text that is already on the screen (lower-thirds, speaker names, titles, graphics).

The rule of good captioning is that captions never cover burned-in text or a speaker's face. So for a video we look for the stretches of time
when the caption zone (the lower part of the picture) is taken by something that was not there a moment ago, and the captions that fall in
those stretches are moved to the top of the picture instead: in WebVTT with line/position settings, in TTML with a top region, in burned-in
captions with a top-aligned style, and in the live preview over the player.

How a lower-third is found, without reading it: the picture is sampled twice a second at low resolution, and in the caption zone we measure
how much sharp, text-like detail there is (many strong horizontal edges along the same row, which is what lettering looks like). A graphic
shows up as a sudden jump of that measure against what the video has been doing, stays for a moment and drops away. A scene cut also makes
the measure jump, but it changes the upper part of the picture at the same time, so those are ignored. Anything permanent (a logo, a
studio wall) is part of the baseline and is not mistaken for a graphic.

The result is a list of (start_ms, end_ms) ranges, saved as screen_text.json beside the order's other files.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
from collections import deque
from pathlib import Path

W, H = 320, 180
FPS = 2.0
ZONE = (int(H * 0.64), int(H * 0.95))          # the rows captions sit in
UPPER = (int(H * 0.06), int(H * 0.52))         # the rows that should NOT change at the same moment unless the whole picture changed
EDGE = 38                                       # how sharp a step in brightness has to be to count as an edge
TEXT_ROW_EDGES = 8                              # a row with this many sharp edges looks like lettering
MIN_RISE = 0.05                                 # how far above the baseline the measure must climb to count
MIN_SECONDS = 1.0
MERGE_GAP_S = 1.0
MAX_ANALYSE_SECONDS = 3 * 3600
CACHE_NAME = "screen_text.json"


def _measure(frame, lo: int, hi: int) -> float:
    """How much text-like detail is in these rows: the share of rows that are full of sharp edges, plus half the share of edge pixels."""
    import numpy as np
    band = frame[lo:hi].astype(np.int16)
    edges = np.abs(band[:, 1:] - band[:, :-1]) > EDGE
    rows = edges.sum(axis=1) >= TEXT_ROW_EDGES
    return float(rows.mean() * 0.7 + edges.mean() * 3.0 * 0.3)


def frames(video_path: str, timeout: int = 600):
    """Grey 320x180 frames, two a second, straight from ffmpeg."""
    if not shutil.which("ffmpeg"):
        return
    cmd = ["ffmpeg", "-v", "error", "-i", str(video_path), "-an", "-vf", f"fps={FPS},scale={W}:{H},format=gray", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size = W * H
    timer = threading.Timer(timeout, proc.kill)
    timer.start()
    try:
        import numpy as np
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield np.frombuffer(buf, dtype=np.uint8).reshape(H, W)
    finally:
        timer.cancel()
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait()


def ranges_from_measures(zone: list[float], upper: list[float], fps: float = FPS) -> list[tuple[int, int]]:
    """The state machine over the two measures; kept apart from the video so it can be tested on plain numbers."""
    import statistics
    base_hist: deque = deque(maxlen=int(fps * 14))
    up_hist: deque = deque(maxlen=int(fps * 14))
    flagged, start, rise, calm = False, 0, 0, 0
    out: list[tuple[float, float]] = []
    for i, (z, u) in enumerate(zip(zone, upper)):
        if len(base_hist) < 3:
            base_hist.append(z)
            up_hist.append(u)
            continue
        base = statistics.median(base_hist)
        mad = statistics.median(abs(x - base) for x in base_hist)
        thr = max(MIN_RISE, 5 * mad)
        up_base = statistics.median(up_hist)
        delta = z - base
        scene_cut = abs(u - up_base) > 0.6 * max(delta, 1e-9) and abs(u - up_base) > MIN_RISE      # the whole picture changed, not just the zone
        if not flagged:
            if delta > thr and not scene_cut:
                rise += 1
                if rise >= 2:
                    flagged, start, calm = True, i - 1, 0
            else:
                rise = 0
                base_hist.append(z)
                up_hist.append(u)
        else:
            if delta < 0.5 * thr:
                calm += 1
                if calm >= 2:
                    out.append((start / fps, (i - 1) / fps))
                    flagged, rise = False, 0
                    base_hist.append(z)
                    up_hist.append(u)
            else:
                calm = 0
    if flagged:
        out.append((start / fps, len(zone) / fps))
    merged: list[list[float]] = []
    for s, e in out:
        if e - s < MIN_SECONDS:
            continue
        if merged and s - merged[-1][1] <= MERGE_GAP_S:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(int(s * 1000), int(e * 1000)) for s, e in merged]


def detect(video_path: str, timeout: int = 600) -> list[tuple[int, int]]:
    """The stretches of time (milliseconds) when the caption zone of this video is taken by on-screen text or graphics."""
    zone, upper = [], []
    for n, frame in enumerate(frames(video_path, timeout)):
        zone.append(_measure(frame, *ZONE))
        upper.append(_measure(frame, *UPPER))
        if n / FPS > MAX_ANALYSE_SECONDS:
            break
    return ranges_from_measures(zone, upper) if zone else []


# ------------------------------------------------------------------------------------------------------------- the saved result
def _cache(outdir: str | Path) -> Path:
    return Path(outdir) / CACHE_NAME


def cached(outdir: str | Path) -> list[tuple[int, int]] | None:
    """The saved ranges, or None when this video has not been looked at (an empty list means it was, and nothing was found)."""
    try:
        data = json.loads(_cache(outdir).read_text(encoding="utf-8"))
        return [(int(a), int(b)) for a, b in data["ranges"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save(outdir: str | Path, ranges: list[tuple[int, int]]) -> None:
    _cache(outdir).write_text(json.dumps({"ranges": [list(r) for r in ranges], "fps": FPS}), encoding="utf-8")


def analyse(video_path: str, outdir: str | Path, force: bool = False) -> list[tuple[int, int]]:
    """Look at the video once and remember what was found."""
    if not force:
        known = cached(outdir)
        if known is not None:
            return known
    ranges = detect(video_path)
    save(outdir, ranges)
    return ranges


def analyse_in_background(video_path: str, outdir: str | Path, then=None) -> None:
    """Look at the video beside the order, never in front of it. `then` is called with the ranges when it is done."""
    def run():
        try:
            ranges = analyse(video_path, outdir)
            if then:
                then(ranges)
        except Exception:  # noqa: BLE001 - captions simply stay at the bottom
            pass
    threading.Thread(target=run, daemon=True, name="screen-text").start()


# ---------------------------------------------------------------------------------------------------------- which captions move
def is_top(start_ms: int, end_ms: int, ranges) -> bool:
    """A caption moves to the top when a graphic is up for at least a third of the time it is on screen."""
    if not ranges:
        return False
    span = max(end_ms - start_ms, 1)
    overlap = sum(max(0, min(end_ms, b) - max(start_ms, a)) for a, b in ranges)
    return overlap >= span / 3.0
