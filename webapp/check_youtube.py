"""Checks whether this server can fetch a YouTube link right now, and says which mitigations are configured.

    docker exec kauli python3 -m webapp.check_youtube "https://www.youtube.com/watch?v=VIDEO_ID"

It reads the video's details first (no download), then fetches the audio into a temporary folder, using exactly the options the
web app uses. Nothing is kept, and no keys or cookies are ever printed.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def main(url: str) -> int:
    from . import app
    import httpx

    cookies = os.environ.get("KAULI_YOUTUBE_COOKIES_FILE", "").strip()
    proxy = os.environ.get("KAULI_YOUTUBE_PROXY", "").strip()
    print("cookies file:", "set and found" if cookies and Path(cookies).exists() else ("set but MISSING" if cookies else "not set"))
    print("residential proxy:", "set" if proxy else "not set")
    base = app.YT_DLP_EXTRACTOR_ARGS["extractor_args"]["youtubepot-bgutilhttp"]["base_url"][0]
    try:
        httpx.get(base + "/ping", timeout=5.0)
        print("proof-of-origin token service: reachable")
    except Exception as exc:  # noqa: BLE001
        print("proof-of-origin token service: NOT reachable (", exc.__class__.__name__, ")")
    print("YOUTUBE_API_KEY:", "set" if os.environ.get("YOUTUBE_API_KEY") else "not set")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path, name, video_id = app._download_youtube(url, Path(tmp))
            print(f"OK: fetched '{name}' ({path.stat().st_size // 1024} KB), video id {video_id}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print("FAILED:", str(exc)[:400])
        print("A client would see:", app.explain_youtube_failure(exc))
        return 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    raise SystemExit(main(sys.argv[1]))
