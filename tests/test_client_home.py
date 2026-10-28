"""The client dashboard is a short, clear home: greeting, four actions, order counts, recent files."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401


def test_home_is_short_and_clear(env, wav_bytes):
    env.order_from(env.place(wav_bytes))
    page = env.client.get("/client/home").text
    for action in ("Upload files", "YouTube link", "Hear a demo", "Plans &amp; billing"):
        assert action in page
    assert "Recent files" in page and 'id="ch-search"' in page and "clip.wav" in page
    assert "Processing" in page and "In review" in page and "Needs you" in page
    # none of the old wordy cards
    for gone in ("Here's what's happening with your orders", "All-time, confirmed payments", "Payment or a file we need from you"):
        assert gone not in page
    assert "free minutes left this month" in page
    import os
    if os.environ.get("HOME_DUMP_PATH"):
        Path(os.environ["HOME_DUMP_PATH"]).write_text(page, encoding="utf-8")


def test_home_labels_use_short_status_words(env, wav_bytes):
    order = env.order_from(env.place(wav_bytes, review_tier="human"))
    page = env.client.get("/client/home").text
    assert "Awaiting payment" in page and "YOUR ORDER" not in page.upper().split("RECENT FILES", 1)[1]


def test_a_new_client_gets_a_friendly_empty_state(env):
    page = env.client.get("/client/home").text
    assert "Nothing here yet" in page


def test_pending_tab_lists_unfinished_orders_and_shows_a_count(env, wav_bytes):
    assert "Nothing is waiting" in env.client.get("/client/pending").text
    assert 'class="nav-badge"' not in env.client.get("/client/home").text
    order = env.order_from(env.place(wav_bytes, review_tier="human"))           # waits for payment
    page = env.client.get("/client/pending").text
    assert "clip.wav" in page and "Awaiting payment" in page and f"/client/orders/{order['id']}/pay" in page
    home = env.client.get("/client/home").text
    assert 'href="/client/pending"' in home and 'class="nav-badge"' in home
    assert env.db.pending_orders_count("u1") == 1
    done = env.order_from(env.place(wav_bytes))
    conn = env.db.get_conn()
    conn.execute("UPDATE orders SET status = 'delivered' WHERE id = ?", (done["id"],))
    conn.commit()
    conn.close()
    assert env.db.pending_orders_count("u1") == 1 and "clip.wav" in env.client.get("/client/pending").text
