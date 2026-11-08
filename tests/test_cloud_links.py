"""Transcribe from Cloud by pasting a share link (no keys), and the richer Record & Transcribe control."""
import base64
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401
from webapp import cloud_import, cloud_links  # noqa: E402

STATIC = Path(__file__).parent.parent / "webapp" / "static"


# ----------------------------------------------------------------------------------------------- the resolver
def test_a_dropbox_link_becomes_a_direct_download_and_keeps_its_key():
    found = cloud_links.resolve("https://www.dropbox.com/scl/fi/abc123/Team%20call.mp3?rlkey=KEY&dl=0")
    assert found.provider == "dropbox" and found.name == "Team call.mp3"
    assert "dl=1" in found.url and "rlkey=KEY" in found.url and "dl=0" not in found.url


def test_a_google_drive_link_becomes_a_direct_download_from_the_id():
    for link in ("https://drive.google.com/file/d/1AbCdEfGhIjKlMnOp/view?usp=sharing",
                 "https://drive.google.com/open?id=1AbCdEfGhIjKlMnOp", "https://drive.google.com/uc?id=1AbCdEfGhIjKlMnOp&export=download"):
        found = cloud_links.resolve(link)
        assert found.provider == "gdrive" and "id=1AbCdEfGhIjKlMnOp" in found.url and found.url.startswith("https://drive.usercontent.google.com/")
    assert cloud_links.resolve("https://drive.google.com/file/d/short/view") is None


def test_a_onedrive_link_goes_through_the_public_share_api_and_sharepoint_gets_download_on():
    link = "https://1drv.ms/u/s!AbCdEf"
    found = cloud_links.resolve(link)
    token = found.url.split("/shares/u!", 1)[1].split("/", 1)[0]
    assert found.provider == "onedrive" and found.url.startswith("https://api.onedrive.com/v1.0/shares/u!")
    assert base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode() == link
    sharepoint = cloud_links.resolve("https://contoso.sharepoint.com/:v:/g/personal/me/clip.mp4?e=x")
    assert sharepoint.provider == "onedrive" and "download=1" in sharepoint.url and sharepoint.name == "clip.mp4"


@pytest.mark.parametrize("link", [
    "http://www.dropbox.com/s/abc/a.mp3", "https://evil.example/a.mp3", "https://drive.google.com.evil.example/file/d/1AbCdEfGhIjKlMnOp/view",
    "https://dropbox.com.evil.example/s/abc/a.mp3", "https://169.254.169.254/latest/meta-data",
    "https://localhost/a.mp3", "ftp://x/y",
    "", "not a link", "https://www.dropbox.com/" + "a" * 2100])
def test_anything_that_is_not_a_share_link_on_those_services_is_refused(link):
    assert cloud_links.resolve(link) is None


def test_every_address_we_build_is_on_the_services_own_hosts():
    for link in ("https://www.dropbox.com/s/abc/a.mp3?dl=0", "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOp/view",
                 "https://1drv.ms/u/s!AbCdEf", "https://contoso.sharepoint.com/:v:/g/x/clip.mp4"):
        found = cloud_links.resolve(link)
        assert cloud_import.host_allowed(found.provider, found.url), link


def test_the_file_name_comes_from_the_servers_header_or_the_content_type():
    assert cloud_links.name_from_headers('attachment; filename="Interview 1.m4a"') == "Interview 1.m4a"
    assert cloud_links.name_from_headers("attachment; filename*=UTF-8''Caf%C3%A9.mp3") == "Café.mp3"
    assert cloud_links.name_from_headers(None) == ""
    assert cloud_links.suffix_from_type("audio/mpeg; charset=x") == ".mp3"
    assert cloud_links.suffix_from_type("text/html") == ""


# --------------------------------------------------------------------------------------------- the route
def _fake_download(content=b"RIFFdata", kind="audio/mpeg", disposition=""):
    def fake(url, dest, headers=None, provider=None, limit=None, meta=None):
        dest.write_bytes(content)
        if meta is not None:
            meta.update(type=kind, disposition=disposition)
        return len(content)
    return fake


def _post(env, link):
    return env.client.post("/account/import/link", json={"url": link})


def _staged(env):
    return list(cloud_import.imports_dir("u1").glob("*"))


def test_a_pasted_dropbox_link_is_downloaded_and_becomes_a_staged_file(env):
    env.monkeypatch.setattr(cloud_import, "_download", _fake_download())
    reply = _post(env, "https://www.dropbox.com/s/abc/talk.mp3?dl=0")
    data = reply.json()
    assert reply.status_code == 200 and data["filename"] == "talk.mp3" and data["upload_key"].startswith("import:")
    assert cloud_import.staged_file(data["upload_key"], "u1") is not None
    assert not [f for f in _staged(env) if f.suffix == ".part"]                  # no half-finished file left


def test_a_drive_link_takes_its_name_and_type_from_the_servers_answer(env):
    named = _fake_download(kind="application/octet-stream", disposition='attachment; filename="Interview 1.m4a"')
    env.monkeypatch.setattr(cloud_import, "_download", named)
    data = _post(env, "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOp/view").json()
    assert data["filename"] == "Interview 1.m4a"
    env.monkeypatch.setattr(cloud_import, "_download", _fake_download(kind="video/mp4"))
    assert _post(env, "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOp/view").json()["filename"].endswith(".mp4")


def test_a_sign_in_page_instead_of_the_file_says_to_set_sharing_to_anyone_with_the_link(env):
    page = _fake_download(content=b"<html>sign in</html>", kind="text/html; charset=utf-8")
    env.monkeypatch.setattr(cloud_import, "_download", page)
    reply = _post(env, "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOp/view")
    assert reply.status_code == 400 and "Anyone with the link" in reply.json()["error"]
    assert _staged(env) == []


def test_a_refused_download_gets_the_same_plain_advice_and_leaves_nothing_behind(env):
    def refused(url, dest, headers=None, provider=None, limit=None, meta=None):
        dest.write_bytes(b"x")
        raise ValueError("The provider refused the download (403). Pick the file again.")
    env.monkeypatch.setattr(cloud_import, "_download", refused)
    reply = _post(env, "https://www.dropbox.com/s/abc/talk.mp3?dl=0")
    assert reply.status_code == 400 and "Anyone with the link" in reply.json()["error"] and _staged(env) == []


def test_something_that_is_not_audio_or_video_is_refused(env):
    env.monkeypatch.setattr(cloud_import, "_download", _fake_download(kind="application/octet-stream"))
    reply = _post(env, "https://www.dropbox.com/s/abc/notes.pdf?dl=0")
    assert reply.status_code == 400 and "not an audio or video file" in reply.json()["error"] and _staged(env) == []


def test_a_link_to_another_site_never_reaches_the_downloader(env):
    called = []
    env.monkeypatch.setattr(cloud_import, "_download", lambda *a, **k: called.append(a) or 0)
    reply = _post(env, "https://evil.example/a.mp3")
    assert reply.status_code == 400 and "share link" in reply.json()["error"] and called == []


# ----------------------------------------------------------------------------------------- the page and scripts
def test_the_cloud_card_is_always_live_and_the_card_names_are_clear(env):
    env.monkeypatch.setenv("RECALL_API_KEY", "key")
    page = env.client.get("/account/home").text
    assert "/account?source=cloud#order-form" in page and "is-soon" not in page
    for label in ("Upload &amp; Process", "Record &amp; Process", "Process from YouTube", "Process Meeting",
                  "Process from Cloud"):
        assert f"<strong>{label}</strong>" in page, label


def test_the_upload_step_always_offers_the_link_box_and_a_recorder_with_a_meter_and_playback(env):
    page = env.client.get("/account").text
    assert 'id="cloud-import"' in page
    script = (STATIC / "cloud_import.js").read_text(encoding="utf-8")
    assert "/account/import/link" in script and 'id="ci-link"' in script and "Anyone with the link" in script
    for ident in ("rec-toggle", "rec-pause", "rec-meter", "rec-preview", "rec-continue"):
        assert f'id="{ident}"' in page, ident
    recorder = (STATIC / "recorder.js").read_text(encoding="utf-8")
    assert "createAnalyser" in recorder and "recorder.pause()" in recorder and "wizard-next" in recorder


def test_every_language_has_the_five_card_names():
    from webapp import ui_text
    for code, catalog in ui_text.CATALOG.items():
        for key in ("act_upload_t", "act_rec_t", "act_yt_t", "act_meet_t", "act_cloud_t"):
            assert catalog.get(key), (code, key)
    assert re.search(r"process", ui_text.EN["act_cloud_t"], re.I)
