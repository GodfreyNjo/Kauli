"""Order forms start on the language pair a client usually orders."""
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401  (the throwaway-database fixtures)


def _set_languages(env, rows):
    """Give already-placed orders the languages and ages a scenario needs."""
    conn = env.db.get_conn()
    now = time.time()
    for oid, source, target, age in rows:
        conn.execute("UPDATE orders SET source_lang = ?, target_lang = ?, created_at = ? WHERE id = ?", (source, target, now - age, oid))
    conn.commit()
    conn.close()


def _place(env, wav, n):
    return [env.order_from(env.place(wav))["id"] for _ in range(n)]


def test_no_orders_means_no_guess(env):
    assert env.db.usual_languages("u1") is None


def test_the_most_common_pair_wins_and_a_tie_goes_to_the_newest(env, wav_bytes):
    ids = _place(env, wav_bytes, 4)
    _set_languages(env, [(ids[0], "sw", "en", 40), (ids[1], "sw", "en", 30), (ids[2], "en", "sw", 20), (ids[3], "en", "sw", 10)])
    assert env.db.usual_languages("u1") == ("en", "sw")              # two each: the newest pair
    _set_languages(env, [(ids[3], "sw", "en", 10)])
    assert env.db.usual_languages("u1") == ("sw", "en")              # three beat one


def test_the_dashboard_starts_on_the_usual_pair_and_says_so(env, wav_bytes):
    ids = _place(env, wav_bytes, 3)
    _set_languages(env, [(i, "en", "sw", n) for i, n in zip(ids, (30, 20, 10))])
    page = env.client.get("/account").text
    assert "Languages are set to the ones you usually order" in page
    source = re.search(r'<select name="source_lang".*?</select>', page, re.S).group(0)
    target = re.search(r'<select name="target_lang">.*?</select>', page, re.S).group(0)
    assert re.search(r'<option value="en"\s+selected', source) and re.search(r'<option value="sw"\s+selected', target)


def test_explicit_settings_defaults_beat_what_was_learned(env, wav_bytes):
    ids = _place(env, wav_bytes, 2)
    _set_languages(env, [(i, "en", "sw", n) for i, n in zip(ids, (20, 10))])
    conn = env.db.get_conn()
    conn.execute("UPDATE users SET default_source_lang = 'sw', default_target_lang = 'en' WHERE id = 'u1'")
    conn.commit()
    conn.close()
    page = env.client.get("/account").text
    assert "usually order" not in page
    assert re.search(r'<option value="sw"\s+selected', re.search(r'<select name="source_lang".*?</select>', page, re.S).group(0))


def test_a_new_client_sees_the_normal_defaults_and_no_note(env):
    assert "usually order" not in env.client.get("/account").text
