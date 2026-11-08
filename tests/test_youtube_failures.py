"""A YouTube link we cannot fetch gives the client a plain sentence and a next step, never the downloader's raw error."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401

RAW_BOT = ("ERROR: [youtube] x5B_I8h-4Oc: Sign in to confirm you’re not a bot. Use --cookies-from-browser or --cookies for the "
           "authentication. See https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp for how to manually pass cookies.")


def test_each_cause_gets_a_plain_explanation(env):
    explain = env.app.explain_youtube_failure
    bot = explain(Exception(RAW_BOT))
    assert "blocking automatic downloads" in bot and "upload the file" in bot and "cookies" not in bot and "yt-dlp" not in bot
    assert "private" in explain(Exception("ERROR: Private video. Sign in if you've been granted access")).lower()
    assert "unavailable" in explain(Exception("ERROR: Video unavailable. This video is not available")).lower()
    assert "age-restricted" in explain(Exception("ERROR: Sign in to confirm your age. This video may be inappropriate for some users; age-restricted"))
    assert "live stream" in explain(Exception("ERROR: This live event will begin in 2 hours"))
    assert "single video" in explain(Exception("ERROR: Unsupported URL: https://example.com"))
    other = explain(RuntimeError("socket hang up"))
    assert "fetch that YouTube link" in other and "socket" not in other


def test_the_client_sees_the_plain_message_and_the_real_error_is_logged(env, wav_bytes, caplog):
    def fail(url, dest):
        raise RuntimeError(RAW_BOT)
    env.monkeypatch.setattr(env.app, "_download_youtube", fail)
    with caplog.at_level("WARNING"):
        r = env.client.post("/account/orders", data={"source_lang": "en", "target_lang": "en", "service_level": "transcribe",
                                                     "review_tier": "ai", "youtube_url": "https://www.youtube.com/watch?v=x5B_I8h-4Oc"})
    assert r.status_code == 200 and "blocking automatic downloads" in r.text and "--cookies" not in r.text
    assert env.db.list_orders_for_client("u1") == []                                      # nothing was created or charged
    assert any("youtube_fetch_failed" in rec.getMessage() or rec.getMessage() == "youtube_fetch_failed" for rec in caplog.records)


def test_the_diagnostic_script_exists_and_never_prints_keys():
    src = (Path(__file__).parent.parent / "webapp" / "check_youtube.py").read_text(encoding="utf-8")
    assert "set and found" in src and "ever printed" in src and "print(os.environ" not in src and "print(cookies)" not in src and "print(proxy)" not in src
