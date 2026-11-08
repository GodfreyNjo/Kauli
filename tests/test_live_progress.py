"""A running order shows which step it is on, with a percentage that only moves forward."""
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401


def _running(env, wav, stage=None, **place):
    order = env.order_from(env.place(wav, **place))
    env.db.update_order_status(order["id"], "processing")
    if stage:
        env.db.set_processing_stage(order["id"], stage)
    return order


def test_a_transcript_goes_through_fewer_steps_than_a_dub(env, wav_bytes):
    t = _running(env, wav_bytes, "transcribe")
    state = env.app._processing_state(env.db.get_order(t["id"]))
    assert [s["key"] for s in state["steps"]] == ["prepare", "transcribe", "finalize"]
    assert [s["state"] for s in state["steps"]] == ["done", "current", "pending"] and state["what"] == "transcript"
    d = _running(env, wav_bytes, "voice")
    conn = env.db.get_conn()
    conn.execute("UPDATE orders SET service_level = 'dub' WHERE id = ?", (d["id"],))
    conn.commit()
    conn.close()
    keys = [s["key"] for s in env.app._processing_state(env.db.get_order(d["id"]))["steps"]]
    assert keys == ["prepare", "transcribe", "translate", "voice", "mix", "finalize"]


def test_the_percentage_moves_forward_with_the_stage_and_never_claims_to_be_done(env, wav_bytes):
    order = _running(env, wav_bytes)
    seen = []
    for stage in ("prepare", "transcribe", "finalize"):
        env.db.set_processing_stage(order["id"], stage)
        seen.append(env.app._processing_state(env.db.get_order(order["id"]))["pct"])
    assert seen == sorted(seen) and seen[0] < seen[-1] < 100
    env.db.update_order_status(order["id"], "ready_for_delivery")
    done = env.app._processing_state(env.db.get_order(order["id"]))
    assert done["done"] and done["pct"] == 100 and {s["state"] for s in done["steps"]} == {"done"}


def test_the_progress_endpoint_is_private_to_the_client_who_owns_the_order(env, wav_bytes):
    order = _running(env, wav_bytes, "transcribe")
    r = env.client.get(f"/account/orders/{order['id']}/progress")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json()["steps"][1]["state"] == "current"
    assert env.client.get("/account/orders/not-a-real-order/progress").status_code == 404


def test_the_order_page_shows_the_live_panel_while_running_and_not_after(env, wav_bytes):
    order = _running(env, wav_bytes, "transcribe")
    page = env.client.get(f"/account/orders/{order['id']}").text
    assert 'id="live-progress"' in page and "Turning speech into text" in page and "you can close this page" in page.lower()
    assert "updates itself automatically" not in page
    env.db.update_order_status(order["id"], "ready_for_delivery")
    assert 'id="live-progress"' not in env.client.get(f"/account/orders/{order['id']}").text


def test_the_pipeline_reports_each_stage_it_enters():
    src = (Path(__file__).parent.parent / "kauli" / "pipeline.py").read_text(encoding="utf-8")
    for key in ("prepare", "clean", "transcribe", "translate", "voice", "mix", "finalize"):
        assert f'stage("{key}")' in src, key
    worker = (Path(__file__).parent.parent / "webapp" / "worker.py").read_text(encoding="utf-8")
    assert "on_stage=lambda key: db.set_processing_stage(order_id, key)" in worker


def test_a_finished_order_ticks_every_step_including_delivered(env):
    steps = env.app._order_progress_steps
    ai = steps("ready_for_delivery", "ai")
    assert [s["key"] for s in ai] == ["submitted", "processing", "delivered"] and {s["state"] for s in ai} == {"done"}
    assert {s["state"] for s in steps("delivered", "human")} == {"done"}
    # while it is still running nothing is ticked early, and trouble is still flagged
    assert [s["state"] for s in steps("processing", "ai")] == ["done", "current", "pending"]
    assert [s["state"] for s in steps("awaiting_review", "human")] == ["done", "done", "current", "pending"]
    assert steps("failed", "ai")[1]["state"] == "attention"


def test_instant_ai_reaches_the_ticked_state_with_no_staff_approval(env, wav_bytes):
    from test_order_flow import _run_worker
    ai = env.order_from(env.place(wav_bytes))
    done = _run_worker(env, ai)
    assert done["status"] == "ready_for_delivery"                      # nobody approved anything
    page = env.client.get(f"/account/orders/{ai['id']}").text
    assert page.count("order-progress-done") == 3 and "order-progress-current" not in page


def test_the_progress_panel_styles_cannot_leak_onto_other_elements_called_live():
    """The "Available" badges on the home page carry a class named live; an unscoped .live rule once turned each into a
    large padded card."""
    import re
    css = (Path(__file__).parent.parent / "webapp/static/style.css").read_text(encoding="utf-8")
    assert not re.search(r"^\.live[\s,{.:]", css, re.M), "unscoped .live rule"      # unscoped
    assert re.search(r"^section\.live\s*\{", css, re.M)
