import sys, json, time, hmac, hashlib
sys.path.insert(0, "/mnt/c/Forge Project")
from webapp.app import _load_dotenv
_load_dotenv()
import os
import itsdangerous
from base64 import b64encode
import httpx
import sqlite3

secret = os.environ["KAULI_SESSION_SECRET"]
signer = itsdangerous.TimestampSigner(secret)
data = b64encode(json.dumps({"user_id": "ccfebc5a-f125-4f07-8328-fc30b8e03336", "last_seen": time.time()}).encode())
cookie = signer.sign(data).decode()

BASE = "https://mitsubishi-referred-warning-futures.trycloudflare.com"
client = httpx.Client(cookies={"session": cookie}, follow_redirects=False, timeout=20)

print("=== PAYSTACK: initiate real checkout through the real tunnel URL ===")
r = client.post(f"{BASE}/client/orders/paytest0002/pay", data={"provider": "paystack"})
print("HTTP", r.status_code, "->", r.headers.get("location"))

con = sqlite3.connect("/mnt/c/Forge Project/webapp/data/kauli_demo.db")
con.row_factory = sqlite3.Row
payment = con.execute("SELECT * FROM payments WHERE order_id='paytest0002' ORDER BY created_at DESC LIMIT 1").fetchone()
print("payment record:", dict(payment) if payment else None)
con.close()

if payment:
    reference = payment["id"]
    amount_kobo = int(round(payment["amount_local"] * 100))

    print()
    print("=== Simulating Paystack's real charge.success webhook, correctly HMAC-signed ===")
    event = {
        "event": "charge.success",
        "data": {
            "id": 999999,
            "reference": reference,
            "amount": amount_kobo,
            "currency": "KES",
            "status": "success",
        },
    }
    raw_body = json.dumps(event).encode()
    pkey = os.environ["PAYSTACK_SECRET_KEY"]
    signature = hmac.new(pkey.encode(), raw_body, hashlib.sha512).hexdigest()

    r2 = httpx.post(
        f"{BASE}/webhooks/paystack",
        content=raw_body,
        headers={"content-type": "application/json", "x-paystack-signature": signature},
        timeout=20,
    )
    print("webhook HTTP", r2.status_code, r2.text)

    con = sqlite3.connect("/mnt/c/Forge Project/webapp/data/kauli_demo.db")
    con.row_factory = sqlite3.Row
    order = con.execute("SELECT id, status FROM orders WHERE id='paytest0002'").fetchone()
    print("order status after webhook:", dict(order))
    payment2 = con.execute("SELECT status, provider_reference FROM payments WHERE id=?", (reference,)).fetchone()
    print("payment status after webhook:", dict(payment2))
    con.close()

    print()
    print("=== Negative test: wrong signature should be REJECTED ===")
    r3 = httpx.post(
        f"{BASE}/webhooks/paystack",
        content=raw_body,
        headers={"content-type": "application/json", "x-paystack-signature": "0" * 128},
        timeout=20,
    )
    print("wrong-signature HTTP", r3.status_code, r3.text)
