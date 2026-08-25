"""Direct-to-storage client uploads via Cloudflare R2, presigned URLs.

Real problem this fixes: every client upload used to go client -> Cloudflare
Tunnel -> this app -> local disk. Cloudflare's own documented limit for a
Tunnel-proxied hostname on the Free/Pro plan is 100MB per request body -
above that, the upload doesn't cleanly error, it can just hang. Confirmed
live: a real client's larger file stalled forever with zero trace of the
request ever reaching this app's own logs - it never got past Cloudflare's
edge at all.

The fix: the client's browser uploads straight to R2 using a short-lived
presigned URL this app hands out (see generate_presigned_put) - that PUT
never touches the Tunnel or this app's request-handling at all, so the
100MB proxy cap simply doesn't apply. This app then downloads the object
from R2 to local disk (download_object) and runs the EXACT SAME real
security validation (upload_security.validate_media_upload) it always
has, via a tiny adapter (see app.py's _LocalFileAdapter) - never a
second, parallel copy of that logic for the R2 path.

Uses the SAME R2 bucket/credentials already set up for nightly backups
(see the launch runway's backup automation) - client uploads live under
a client-uploads/ prefix, deleted once safely landed on local disk. This
is deliberately transient staging, not permanent storage; the real,
permanent copy is the local file (itself backed up nightly under a
different prefix), matching how every other order file already works.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

R2_BUCKET = os.environ.get("R2_BUCKET", "kauli-backups")
_UPLOAD_PREFIX = "client-uploads"


def r2_configured() -> bool:
    return bool(
        os.environ.get("R2_ACCOUNT_ID")
        and os.environ.get("R2_ACCESS_KEY_ID")
        and os.environ.get("R2_SECRET_ACCESS_KEY")
    )


def _client():
    import boto3
    account_id = os.environ["R2_ACCOUNT_ID"]
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def new_upload_key(original_filename: str) -> str:
    """A random, non-guessable object key - never the client's own
    filename alone, so one client can never overwrite or probe for
    another's in-flight upload key."""
    safe_name = Path(original_filename).name.replace("/", "_").replace("\\", "_")[-120:]
    return f"{_UPLOAD_PREFIX}/{uuid.uuid4().hex}/{safe_name}"


def generate_presigned_put(key: str, content_type: str, expires_s: int = 900) -> str | None:
    """A 15-minute-default write-only URL for exactly one object key -
    the client can PUT to this and nothing else; it grants no access to
    any other object in the bucket, and expires whether or not it's used."""
    if not r2_configured():
        return None
    try:
        return _client().generate_presigned_url(
            "put_object",
            Params={"Bucket": R2_BUCKET, "Key": key, "ContentType": content_type or "application/octet-stream"},
            ExpiresIn=expires_s,
        )
    except Exception:
        return None


def download_object(key: str, dest_path: Path) -> bool:
    """Pulls a just-uploaded object down to local disk so the real
    validation/processing pipeline (which needs a real local file path -
    ffmpeg, faster-whisper, etc.) can run exactly as it always has."""
    if not r2_configured():
        return False
    try:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        _client().download_file(R2_BUCKET, key, str(dest_path))
        return True
    except Exception:
        return False


def delete_object(key: str) -> None:
    """Best-effort cleanup of the transient staging copy once it's safely
    landed on local disk - never raises, since a leftover staged object
    is a minor storage-cost annoyance, not a correctness problem, and
    shouldn't fail an otherwise-successful order submission."""
    if not r2_configured():
        return
    try:
        _client().delete_object(Bucket=R2_BUCKET, Key=key)
    except Exception:
        pass


def head_object_size(key: str) -> int | None:
    """Real size of what actually landed in R2, checked server-side
    before ever downloading it - the client's own reported size (used to
    pick the content-type/validate up front) is not trusted on its own,
    same "never trust the client" principle upload_security.py already
    applies to a direct multipart upload."""
    if not r2_configured():
        return None
    try:
        resp = _client().head_object(Bucket=R2_BUCKET, Key=key)
        return resp.get("ContentLength")
    except Exception:
        return None
