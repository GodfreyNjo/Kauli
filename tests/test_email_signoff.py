"""Every customer email is signed by the team, never by a person's name."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401


def _capture(env, monkeypatch):
    sent = []
    monkeypatch.setenv("BREVO_API_KEY", "k")
    monkeypatch.setenv("BREVO_FROM_EMAIL", "ops@example.com")
    monkeypatch.setattr(env.app.mailer, "send_email", lambda to, subject, html, text, **kw: sent.append((subject, html, text)) or (True, "ok"))
    return sent


def _assert_team_signed(subject, html, text):
    for part in (html, text):
        assert "Godfrey" not in part and "Njoroge" not in part and "Forge Media Services)" not in part, subject
        assert "Kauli Team" in part, subject
    assert " I'm " not in text and "tell me" not in text and "from me" not in text, subject            # speaks as "we", not "I"


def test_the_order_ready_email_is_signed_kauli_team(env, wav_bytes, monkeypatch):
    sent = _capture(env, monkeypatch)
    order = env.order_from(env.place(wav_bytes))
    env.app.worker.notify_client_order_ready(order, base_url="https://kauli.test")
    assert len(sent) == 1 and sent[0][0] == "Your Kauli order is ready"
    _assert_team_signed(*sent[0])


def test_the_welcome_and_first_payment_emails_are_signed_kauli_team(env, monkeypatch):
    sent = _capture(env, monkeypatch)
    user = env.db.get_user("u1")
    env.app._queue_welcome_message(user, base_url="https://kauli.test")
    env.app._queue_first_payment_message(user, base_url="https://kauli.test")
    assert len(sent) == 2
    for mail in sent:
        _assert_team_signed(*mail)
    assert "we're glad you're here" in sent[0][2] and "We'll be keeping an eye" in sent[1][2]
