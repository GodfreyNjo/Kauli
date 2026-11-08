# Architecture

## The big picture

```
Visitor / customer / staff  (browser)
        |
   Cloudflare (DNS, tunnel, TLS)
        |
   Docker container "kauli": FastAPI app (webapp/app.py) + background worker (webapp/worker.py)
        |                         |
   SQLite file (data volume)      kauli/ pipeline -> speech recognition, translation, voices
        |                         |
   nightly backup to R2           outside providers (Transkriptor, Anthropic, Lara, ElevenLabs, Azure ...)
```

- **One container** serves the site and runs the jobs. It listens only on the server's loopback address; the Cloudflare tunnel is the only way in.
- **One SQLite database** on a data volume that survives redeploys. It is backed up every night, and a second job checks the backup.
- **Large uploads** go straight from the browser to R2 object storage (a presigned address), because the tunnel limits a request to 100 MB; the server
  then fetches the file from R2 when the order is created.
- **Sign-in** is Supabase (email and password) and Google. Passwords are never stored by us.
- **Email** goes out through Brevo. **Payments** go through Paystack (card and mobile money), M-Pesa direct (still in test mode) and bank transfer.

## How a request is handled

1. A middleware gives the request a **trace id** (16 characters). It is returned in the `X-Trace-Id` header and written on every log line of the request.
2. The route runs. Anything unexpected is caught by the global error handler, which stores the failure with that trace id and shows the visitor a page quoting it.
3. The request is logged as one JSON line: method, path, status, duration, trace id.

## An order, from upload to delivery

| Status | Meaning | Next |
|---|---|---|
| `pending_payment` | Created, and money is due before work starts | Payment confirmed, then `queued` |
| `queued` / `processing` | The worker is running the pipeline | `awaiting_review` (human review) or `ready_for_delivery` (Instant AI) |
| `awaiting_review` / `editor_returned` | A person is checking it | `returned_to_client` or `ready_for_delivery` |
| `returned_to_client` | Waiting on the customer | Back to the editor or delivered |
| `ready_for_delivery` / `delivered` | Finished | |
| `failed` / `dead_letter` | The pipeline could not finish it | Staff investigate (see the runbook) |

An order that is fully covered by free minutes skips `pending_payment` and starts at once.

## Money

- **Prices** are per audio-minute and come from the price card in `billing.py`. `billing.order_cost_usd()` is the single place a price is worked out:
  free minutes come off first, then any plan discount (none are offered now), then prepaid credits, then add-ons.
- **Free minutes**: the first 30 minutes of Instant AI transcription each month. They are reserved when an order is created (so two quick orders cannot both use them)
  and the customer is told about them only once the work has started.
- **Credits**: prepaid value in the customer's wallet (1 credit is $0.10). They are what a top-up buys, and what *pay first* uses.
- **Pay first, then upload** (large Instant AI orders): the order form reads the recording's length in the browser and asks `/client/orders/quote` what it will
  cost. If money is due, the customer pays on `/client/pay-first` for exactly that amount; it lands in the wallet as credits. When they return, the file uploads
  and the order uses those credits, so it starts straight away. Orders with add-ons, human review, dubbing or rush keep the older flow (upload, then pay).
- **Paying customers can download and edit.** Any real payment opens downloads and editing on every order of that account. Before that, an order made with
  free minutes can be read but not downloaded.
- **Currency**: customers in Kenya are charged in shillings at checkout (the page says the shilling amount first); everyone else in dollars.
- A confirmed payment becomes something real in exactly one place, `_activate_payment()` in `app.py`: it releases an order to the worker, adds credits, or starts a plan.
  It is safe to run twice for the same payment.

## Data

The tables you will meet most: `users`, `orders`, `payments`, `subscriptions` (plan, free minutes used, wallet credits), `receipts`, `onboarding_messages`
(every email we queue or send, with the provider's answer), `security_events` (sign-ins, sign-ups), `upload_audit_log`, `wizard_step_events` (how far people get in the
order form), `site_hits` (our own visit counter, no cookies), and `app_errors` (unexpected failures, see the runbook). `db.py` holds the full schema.

## Where things are decided

- Which provider does the speech recognition or translation for an order: `_route_asr()` and `_pick_mt()` in `app.py`, with fallbacks inside `kauli/providers/`.
- Who can see what: `current_user()` and the role checks at the top of each route (`client`, `staff`, `voice_actor`; admins are staff with `is_admin`).
- What the customer-facing assistant may talk about: `assistant_core.py` (only the customer's own account and orders).
