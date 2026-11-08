"""Several files at once, and the steps shown while an order is being set up."""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401

JS = (Path(__file__).parent.parent / "webapp" / "static" / "client_wizard.js").read_text(encoding="utf-8")


def test_the_upload_form_accepts_several_files_and_lists_them(env):
    page = env.client.get("/account").text
    assert re.search(r'<input type="file" name="audio" id="file-input"[^>]*\bmultiple\b', page)
    assert 'id="file-queue"' in page and "Up to 10 files at once" in page
    if os.environ.get("DASH_DUMP_PATH"):
        Path(os.environ["DASH_DUMP_PATH"]).write_text(page, encoding="utf-8")


def test_each_file_becomes_its_own_order_and_failures_are_kept_for_a_retry():
    assert "async function submitBatch" in JS and "MAX_FILES = 10" in JS
    assert JS.count('"/account/orders/presign-upload"') == 2          # the single-file path and the batch path
    assert 'fd.set("idempotency_key"' in JS                           # a fresh key per file, so none is treated as a duplicate
    assert 'okCount === 1 ? lastUrl : "/account/pending"' in JS and "window.location.href = dest" in JS
    assert "fileQueue = failed" in JS and "Fix or remove those and press Submit again" in JS


def test_after_the_upload_the_client_sees_what_is_happening():
    for step in ("Checking your audio", "Working out your price", "Starting your order", "Fetching your video from YouTube"):
        assert step in JS, step
    assert "Upload complete - processing your order" not in JS
    assert "startFinalizeSteps" in JS and "stopFinalizeSteps" in JS
