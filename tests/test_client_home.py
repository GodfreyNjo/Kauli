"""The client dashboard is a short, clear home: greeting, four actions, order counts, recent files."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401


def test_home_is_short_and_clear(env, wav_bytes):
    env.order_from(env.place(wav_bytes))
    page = env.client.get("/account/home").text
    for action in ("Upload", "From YouTube", "Try a sample", "Connect tools"):
        assert action in page
    assert "Recent projects" in page and 'id="ch-search"' in page and "clip.wav" in page
    assert "Processing" in page and "In review" in page and "Needs input" in page
    for column in ("File / Project", "Service", "Languages", "Updated", "Status", "Actions"):
        assert column in page
    # none of the old wordy cards
    for gone in ("Here's what's happening with your orders", "All-time, confirmed payments", "Payment or a file we need from you"):
        assert gone not in page
    assert "free minutes left this month" in page
    import os
    if os.environ.get("HOME_DUMP_PATH"):
        Path(os.environ["HOME_DUMP_PATH"]).write_text(page, encoding="utf-8")


def test_home_labels_use_short_status_words(env, wav_bytes):
    order = env.order_from(env.place(wav_bytes, review_tier="human"))
    page = env.client.get("/account/home").text
    assert "Awaiting payment" in page and "YOUR ORDER" not in page.upper().split("RECENT PROJECTS", 1)[1]


def test_a_new_client_gets_a_friendly_empty_state(env):
    page = env.client.get("/account/home").text
    assert "Nothing here yet" in page


def test_pending_tab_lists_unfinished_orders_and_shows_a_count(env, wav_bytes):
    assert "Nothing is waiting" in env.client.get("/account/pending").text
    assert 'class="nav-badge"' not in env.client.get("/account/home").text
    order = env.order_from(env.place(wav_bytes, review_tier="human"))           # waits for payment
    page = env.client.get("/account/pending").text
    assert "clip.wav" in page and "Awaiting payment" in page and f"/account/orders/{order['id']}/pay" in page
    home = env.client.get("/account/home").text
    assert 'href="/account/pending"' in home and 'class="nav-badge"' in home
    assert env.db.pending_orders_count("u1") == 1
    done = env.order_from(env.place(wav_bytes))
    conn = env.db.get_conn()
    conn.execute("UPDATE orders SET status = 'delivered' WHERE id = ?", (done["id"],))
    conn.commit()
    conn.close()
    assert env.db.pending_orders_count("u1") == 1 and "clip.wav" in env.client.get("/account/pending").text


def test_the_greeting_uses_a_first_name():
    from webapp import app as app_mod
    f = app_mod._first_name
    assert f({"display_name": "Edwin G K", "email": "gkedwin9@gmail.com"}) == "Edwin"
    assert f({"display_name": "amina yusuf", "email": "a@x.com"}) == "Amina"
    assert f({"display_name": "esirachewww6", "email": "esirachewww6@gmail.com"}) == "Esirachewww"     # only an email start: no digits
    assert f({"display_name": "jane.doe42", "email": "jane.doe42@x.com"}) == "Jane"
    assert f({"display_name": None, "email": "wanjiku_k@x.com"}) == "Wanjiku"
    assert f({"display_name": "", "email": "42@x.com"}) == ""                                           # nothing usable: no name


def test_the_home_greeting_has_no_username_and_no_digits(env, wav_bytes):
    conn = env.db.get_conn()
    conn.execute("UPDATE users SET display_name = 'Edwin G K' WHERE id = 'u1'")
    conn.commit()
    conn.close()
    page = env.client.get("/account/home").text
    assert ", Edwin</h1>" in page and "G K" not in page.split("<h1>", 1)[1].split("</h1>", 1)[0]
