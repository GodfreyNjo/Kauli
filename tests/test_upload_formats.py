"""Upload-format rules: Opus voice notes are accepted, and raw AAC (.aac) is no
longer an allowed extension the content sniffer would reject."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from webapp import upload_security as u  # noqa: E402


def test_opus_and_aac_are_allowed_extensions():
    assert ".opus" in u.ALLOWED_MEDIA_EXTENSIONS
    assert ".aac" in u.ALLOWED_MEDIA_EXTENSIONS


def test_sniffer_recognises_ogg_opus_and_raw_aac():
    assert u.sniff_container(b"OggS" + b"\x00" * 28) == "ogg"          # .opus / .ogg
    assert u.sniff_container(b"\xff\xf1" + b"\x50" * 30) == "aac"      # ADTS AAC (MPEG-4)
    assert u.sniff_container(b"\xff\xf9" + b"\x50" * 30) == "aac"      # ADTS AAC (MPEG-2)
    assert u.sniff_container(b"ID3" + b"\x00" * 30) == "mp3"
    assert u.sniff_container(b"not media at all") is None


def test_every_allowed_extension_has_a_matching_sniffable_container():
    """Each accepted extension must be a type the magic-byte check can
    recognise - otherwise it is accepted by name and rejected by content."""
    sniffable = {"mp3", "riff", "flac", "ogg", "ebml", "asf", "aac", "iso-bmff"}
    by_ext = {".mp3": "mp3", ".wav": "riff", ".avi": "riff", ".flac": "flac", ".ogg": "ogg", ".opus": "ogg",
              ".webm": "ebml", ".mkv": "ebml", ".wma": "asf", ".aac": "aac",
              ".m4a": "iso-bmff", ".mp4": "iso-bmff", ".mov": "iso-bmff"}
    assert set(by_ext) == u.ALLOWED_MEDIA_EXTENSIONS
    assert set(by_ext.values()) <= sniffable
