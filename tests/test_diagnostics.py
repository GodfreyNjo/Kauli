"""Diagnostics: an unexpected failure is recorded under a reference number, shown to the visitor, and findable by staff.

Also the engineering guide pages, and the generated module map staying in step with the code.
"""
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env  # noqa: E402,F401
from webapp import diagnostics, engineering_docs  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def break_on_purpose(env):
    """Adds a page that always fails, and a client that returns the error page instead of re-raising it."""
    @env.app.app.get("/__boom")
    def boom():
        raise ValueError("price for ana@example.com failed, key abcdefghijklmnopqrstuvwxyz0123456789")
    return TestClient(env.app.app, raise_server_exceptions=False)


def staff(env, admin=True):
    person = {**dict(env.db.get_user("u1")), "role": "staff", "is_admin": int(admin)}
    env.monkeypatch.setattr(env.app, "current_user", lambda request: person)


def test_an_unexpected_failure_shows_a_reference_and_is_recorded_without_secrets(env):
    client = break_on_purpose(env)
    page = client.get("/__boom")
    reference = page.headers["X-Trace-Id"]
    assert page.status_code == 500 and reference in page.text and "Something went wrong on our side" in page.text
    saved = diagnostics.find_error(reference)
    assert saved["error_type"] == "ValueError" and saved["path"] == "/__boom"
    assert "test_diagnostics.py" in saved["where_raised"]
    assert "ana@example.com" not in saved["message"]
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in saved["message"]
    assert "[email]" in saved["message"] and "[token]" in saved["message"]


def test_a_caller_that_expects_json_gets_json_with_the_reference(env):
    client = break_on_purpose(env)
    reply = client.get("/__boom", headers={"accept": "application/json"})
    assert reply.status_code == 500 and reply.json()["reference"] == reply.headers["X-Trace-Id"]


def test_repeats_of_one_bug_group_into_one_row_with_a_count(env):
    client = break_on_purpose(env)
    for _ in range(3):
        client.get("/__boom")
    groups = [g for g in diagnostics.recent_groups() if "test_diagnostics" in g["where_raised"]]
    assert len(groups) == 1 and groups[0]["times"] == 3


def test_only_admins_see_the_diagnostics_page_and_it_finds_a_failure_by_reference(env):
    client = break_on_purpose(env)
    reference = client.get("/__boom").headers["X-Trace-Id"]
    staff(env, admin=False)
    assert env.client.get("/staff/diagnostics").status_code in (302, 303, 307)
    staff(env, admin=True)
    page = env.client.get(f"/staff/diagnostics?ref={reference}").text
    assert "Stack trace" in page and "ValueError" in page and "Outside services" in page
    assert "No failure is recorded" in env.client.get("/staff/diagnostics?ref=0000000000000000").text


def test_recording_a_failure_can_never_cause_another_one(env, monkeypatch):
    def locked(row):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(diagnostics, "_insert", locked)
    diagnostics.record_error("ref123", "GET", "/x", None, ValueError("boom"))      # must simply return


def test_the_engineering_guide_is_readable_by_staff_and_rejects_unknown_pages(env):
    staff(env, admin=False)
    home = env.client.get("/staff/engineering")
    assert home.status_code == 200 and "Start here" in home.text and "Coding standards" in home.text
    assert "Diagnostics (recent failures)" not in home.text
    assert env.client.get("/staff/engineering/04-diagnostics-runbook").status_code == 200
    assert env.client.get("/staff/engineering/..%2F..%2Fapp").status_code == 404
    assert env.client.get("/staff/engineering/nope").status_code == 404


def test_every_page_of_the_guide_renders_and_has_a_title():
    pages = engineering_docs.available()
    assert len(pages) >= 8
    for page in pages:
        rendered = engineering_docs.render(page["slug"])
        assert rendered["html"] and rendered["title"] and not rendered["title"].startswith(page["slug"][:2]), page


def test_the_guide_contains_no_secrets_or_server_addresses():
    text = "\n".join(path.read_text(encoding="utf-8") for path in engineering_docs.DOCS_DIR.glob("*.md"))
    for forbidden in ("169.58.", "BEGIN PRIVATE KEY", "sk_live", "api-key", "kauli_deploy"):
        assert forbidden not in text, forbidden


def test_the_module_map_is_in_step_with_the_code():
    command = [sys.executable, "scripts/generate_module_map.py", "--check"]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    assert result.returncode == 0, "Run: python scripts/generate_module_map.py"


SCRUB_CASES = [("mail ana@example.com now", "mail [email] now"), ("k=" + "a" * 30, "k=[token]")]


@pytest.mark.parametrize("text,expected", SCRUB_CASES)
def test_scrub_removes_emails_and_tokens(text, expected):
    assert diagnostics.scrub(text) == expected
