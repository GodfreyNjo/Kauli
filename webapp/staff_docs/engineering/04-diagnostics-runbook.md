# Diagnostics runbook

When something is wrong, work from the evidence in this order. Do not guess.

## 1. Start from a reference number

Every unexpected failure shows the visitor a short page with a **reference** (16 characters). It is the request's trace id, so it is also in the response header `X-Trace-Id`
and on **every log line of that request**.

1. Open **Diagnostics** (staff admins, in the sidebar under Admin) and paste the reference. You get the time, the exact line of code (`webapp/app.py:1234 in create_order`),
   the request, the account involved, and the stack trace.
2. The same list, grouped by bug with a count, is on that page: one bug repeating shows as one row with "Times".
3. For the full story of the request, search the server log (it is JSON, one object per line):

```bash
docker logs kauli --since 1h 2>&1 | grep 3fa85f6457174562          # everything that request did
docker logs kauli --since 10m 2>&1 | grep '"level": "error"'      # recent errors
```

If a customer has no reference (the page loaded but something looks wrong), go to the section below that matches what they describe.

## 2. Look at the data, read-only

Most questions are answered by the database. Open it **read-only** so nothing can change by accident:

```bash
docker exec -i kauli python3 - <<'PY'
import sqlite3
db = sqlite3.connect("file:/app/webapp/data/kauli_demo.db?mode=ro", uri=True)
db.row_factory = sqlite3.Row
for row in db.execute("SELECT id, status, cost_usd, created_at FROM orders WHERE client_id LIKE ? ORDER BY created_at DESC LIMIT 5", ("abcd12%",)):
    print(dict(row))
PY
```

Never run a write against the live file by hand. If data must change, do it through a code path with a test, or take a backup first (see Deploying).

## 3. Symptom to cause

| Symptom | Where to look | Usual causes and what to do |
|---|---|---|
| **A customer says their payment did not work** | Paystack dashboard (status: `abandoned`, `failed`, `success`); the `payments` row for the account (`status`, `meta`); the account's staff notes; `/staff/payment-failures` | *Abandoned* means they opened checkout and left: ask why, offer M-Pesa or a bank transfer. *Failed* has a reason: read it. If Paystack says success and ours says `pending`, the webhook did not arrive: search the log for `/webhooks/paystack` and signature errors. |
| **An order sits in "Awaiting payment"** | The order's `status`, its `payments` rows | Normal until paid. Check whether a payment is `pending` (still open at the provider) or none exists. Large Instant AI orders are paid *before* upload (`/client/pay-first`), so a waiting order here usually has an add-on or is human-reviewed. |
| **Upload never finishes, or "upload wasn't found"** | `upload_audit_log` (`rejected`, `reject_reason`); the browser console; `Diagnostics` | Files over 100 MB go to R2 first: is R2 configured (Diagnostics, "Outside services")? "Wasn't found, may have expired" means the object left R2 before the order was created. Very slow connections can time out: ask them to retry. |
| **An order is stuck in `queued` or `processing`** | `docker logs kauli \| grep <order id>`; `/staff/providers`; the order's `error` column | The worker may have been restarted by a deploy (re-submit the order from the staff order page), or a provider is down (the pipeline falls back to local models; check `/staff/providers`). `failed` and `dead_letter` have an error to read. |
| **Emails are not arriving** | `Diagnostics` > Outside services > Email; `onboarding_messages.email_send_detail`; `receipts.email_send_detail` | If Email is off, no Brevo key is set. A `network error: ... timed out` in `email_send_detail` is a provider hiccup: the row stays `pending_send` and can be re-sent. Ask the customer to check spam. |
| **"Email not confirmed" at sign-in** | `security_events` (`login_failed`, detail) | The person signed up with email and password and never confirmed. Resend the confirmation, or ask them to use Google. They may have no account row at all. |
| **A signed-up client never ordered** | Staff > Clients > **Analyse sign-ups** | Shows where each client stopped and drafts an email for you to review and send. |
| **The site is down or slow** | `docker ps` (is `kauli` up and healthy?); `df -h` (disk); `docker logs kauli --since 10m` | The tunnel or the container stopped (restart per Deploying). A full disk is usually the Docker build cache: prune it. Never touch other apps' containers on the shared server. |
| **A page shows the wrong text or price** | The template and the module it calls | Prices come from `billing.py` only. Page text for other languages is translated automatically at response time. |

## 4. After you fix it

1. Write the test that failed, and make it pass (see Coding standards).
2. Say in the commit message what the cause was, so the next person searching finds it.
3. Deploy only when asked, then check the live site and watch the Diagnostics page for the same failure.
4. If the cause was a gap in the runbook (no way to tell what happened), add the missing column, log line or check. **Making the next failure easier to see is part of the fix.**

## 5. Optional: Sentry

Set `SENTRY_DSN` in the server's `.env` and install `sentry-sdk` to also send failures to Sentry. Without it, everything above still works: the ledger and the logs are the primary record.
