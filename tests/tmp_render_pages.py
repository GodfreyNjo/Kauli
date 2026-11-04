import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent)); sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa
OUT = Path("/mnt/c/Users/HP/AppData/Local/Temp/claude/5ae7a5a31775b7c4/531e3874-a34d-4a3f-aaed-77d7b8de9b87/scratchpad")
def test_render(env, wav_bytes):
    ids = []
    for i, st in enumerate(["awaiting_review", "ready_for_delivery", "delivered", "processing", "editor_returned", "delivered"]):
        o = env.order_from(env.place(wav_bytes + bytes([i]) * 8))
        env.db.update_order_status(o["id"], st); ids.append(o["id"])
    env.db.create_payment("pp1", "u1", "pro", 18.0, None, "USD", "paystack"); env.db.complete_payment("pp1", "r1", 30)
    for n, (nm, src) in enumerate([("Amina", "website"), ("Brian", "research"), ("Chen", "referral"), ("Dara", "website")]):
        env.db.create_lead(nm, nm.lower() + "@example.org", None, nm + " Co", "hello", None, source=src)
    env.db.get_or_create_user("staff1", "staff@example.com", "staff")
    env.monkeypatch.setattr(env.app, "current_user", lambda request: {**dict(env.db.get_user("staff1")), "role": "staff", "display_name": "Godfrey Njoroge"})
    for name, path in (("q", "/staff/jobs"), ("rep", "/staff/ops"), ("crm", "/staff/leads"), ("cl", "/staff/clients"), ("cd", "/staff/clients/u1")):
        r = env.client.get(path); assert r.status_code == 200, path
        (OUT / (name + ".html")).write_text(r.text, encoding="utf-8")
