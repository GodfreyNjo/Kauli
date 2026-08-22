# Kauli — Platform Roadmap (from one-person pipeline to client-facing ops)

**Read this before opening Claude Code.** It's the map. Claude Code is where you cut trail one segment at a time — this document decides which segment is next so you don't build the wrong thing well.

---

## The honest scope check

What you described — client accounts, plan selection, order dashboard, a contractor correction tool, a separate QA tool, delivery to YouTube/social/download, plus back-office ops — is the *entire* 3Play platform. That's the target architecture, correctly. It is not the MVP. Building all of it before your first paying client touches it is the most common way a solo bootstrapped build stalls for a year.

The actual MVP is: **one job, moving through defined stages, with you filling every role a piece of software will eventually automate.** You already have the engine (the `kauli` pipeline). The next question is never "what feature next" — it's "what is currently costing me the most time or turning away a client, and is software or a manual workaround the faster fix this week."

---

## 1. The target system (what you're eventually building toward)

### Roles
| Role | Who, for now | What they do |
|---|---|---|
| **Client** | Your customer | Signs up, picks a plan, uploads/orders a job, tracks status, downloads or approves delivery |
| **Contractor / Editor** | You, later hired freelancers | Corrects ASR transcript + dubbed translation against source audio |
| **QA** | You, later a second person | Final listen, clears flags, checks against client spec, approves for delivery |
| **Admin / Ops** | You | Assigns jobs, watches SLAs, handles billing, talks to clients when something's wrong |

Note the design intent: **one person can hold multiple roles**, and the system should never assume otherwise. You'll be contractor + QA + admin for a long time. The roles are there so that when you hire your first freelance editor, you flip a permission, not rebuild the schema.

### Order state machine

This is the backbone. Get this right early — everything else (UI screens, notifications, dashboards) is just a view onto this state machine.

```
submitted
   │  (client uploads/orders, payment or plan confirmed)
   ▼
queued
   │  (engine picks it up)
   ▼
processing            ← kauli pipeline: ASR → MT → TTS → mix
   │
   ▼
contractor_review      ← human fixes transcript/translation/timing
   │
   ▼
qa_review               ← second pass: flags cleared, inaudibles removed, spec check
   │
   ├─► needs_revision ──► back to contractor_review (with QA's notes attached)
   │
   ▼
ready_for_delivery
   │
   ▼
delivering              ← push to YouTube / render final file / post to socials
   │
   ▼
delivered
   │
   ▼
completed                ← client confirms or auto-confirms after N days
```

Side states that cut across the happy path: `on_hold` (waiting on the client for something — missing consent, unclear audio, unpaid invoice), `cancelled`, `failed` (pipeline error, needs manual intervention).

Every state transition should be logged with a timestamp and actor. That log *is* your SLA report and your dispute record later — cheap to build now, expensive to reconstruct later.

### Core entities (this becomes your database schema)

- **Client** — account, plan, billing contact, delivery preferences (YouTube channel ID, social handles, download-only)
- **Order** — belongs to a client, has a state (above), a source file, a target language pair, a due date, a plan tier
- **Job manifest** — the `kauli` pipeline output for that order (you already have this schema from the earlier planning docs)
- **Review pass** — one per contractor or QA touch: who, when, what changed, what's still flagged
- **Delivery** — one per output channel: type (YouTube / file / social), destination, status, timestamp
- **Message** — client ↔ ops communication thread, scoped to an order

### Delivery channels (build in this order, cheapest first)
1. **Download link** — trivial, you already have this (files land in an output folder)
2. **Direct file delivery via email/WhatsApp** — no integration needed, manual for a long time
3. **YouTube upload** — YouTube Data API, needs OAuth per client channel, real integration work
4. **Social post** — different API per platform, genuinely the most expensive to build and the least likely to be worth it early. Push back on this feature until a client actually asks and pays for it.

---

## 2. Phased build plan

Each phase has a **stop condition** — the thing that has to be true before you're allowed to start the next phase. This is the discipline that keeps you from building the ops dashboard while you have zero paying clients.

### Phase 0 — Engine (done)
The `kauli` CLI pipeline: ASR → MT → TTS → mix → subtitles → manifest.
**Stop condition met:** it runs end to end on stub providers, tests pass.
**Next:** get it running on real Swahili audio on your laptop (this is your current Claude Code task).

### Phase 1 — Manual ops, zero platform code
Run the business with **no client-facing software at all.**
- Client contacts you (WhatsApp, email, a simple Google Form)
- You run `kauli` locally, review the output yourself using `kauli report`
- You deliver via email or WhatsApp file share
- You track orders in a spreadsheet: client, file, status, due date, price, paid Y/N
- Payment via M-Pesa Paybill/Till, manually reconciled

**Why this matters:** every hour spent building a client portal before you have 3 paying clients is an hour not spent finding out whether people will pay for this at all, and what they actually complain about. The spreadsheet also tells you exactly which manual step to automate first — you'll feel the pain directly.

**Stop condition to leave this phase:** you've delivered ≥5 real paid jobs manually, and you can name the single step that wastes the most of your time (this is usually the review/correction pass, not intake).

### Phase 2 — Self-serve intake, still manual delivery
The smallest piece of "client-facing website": a form where a client uploads a file, picks sw→en or en→sw, sees a price, and pays.
- Simple upload form (file → your S3/cloud storage or even just email-in for now)
- Static pricing (per audio-minute), Stripe or a Kenyan payment gateway (Pesapal/Flutterwave support M-Pesa) for payment
- On submit: creates an `Order` row, notifies you (email/Slack/WhatsApp webhook)
- You still run the pipeline manually and deliver manually

**Stop condition:** intake and payment no longer require you to be present to say "yes I got your file, let me send an invoice."

### Phase 3 — Client status visibility
Client can log in and see: order state, estimated completion, download the result when ready. No contractor/QA UI yet — you're still doing corrections in `kauli report` output or a plain text editor.
- Basic auth (magic link email is easiest, no password reset support burden)
- Order list + detail page, reads straight from the state machine
- Automatic status emails on state change (submitted → processing → ready)

**Stop condition:** clients stop messaging you to ask "is it done yet."

### Phase 4 — Contractor review tool (the one that matters most)
This is the highest-leverage build in the whole roadmap, because it's the stage where your actual editing time is spent. Build this before the QA-specific tool or delivery integrations.
- Waveform + segment list, source transcript next to translated text
- Inline edit of transcript/translation, duration-fit indicator (green/amber/red — you already have the data for this from `fit_status`)
- "Re-synthesize this segment" button (calls Piper again with the edited text)
- Flag reasons shown per segment (already computed by the pipeline — `review_reasons`)

**Stop condition:** your own per-audio-minute review time drops measurably (track it — this is the number that tells you whether the tool is working).

### Phase 5 — QA pass + role separation
Same tool, second lens: a `qa_review` state, a checklist (inaudibles removed, flags cleared, spec followed), and an approve/reject-to-contractor action. This is also the point where, *if* you bring on a second person, you assign them the contractor role and keep QA for yourself.

**Stop condition:** you've had at least one job where a second pair of eyes caught something you missed — that's the proof this stage earns its keep.

### Phase 6 — Delivery integrations
YouTube upload first (most requested for dubbing clients), then whatever else clients are actually asking for and willing to pay a premium for. Don't build a channel nobody's requested.

**Stop condition:** a paying client has explicitly asked for it twice.

### Phase 7 — Ops dashboard
SLA tracking, contractor workload, revenue reporting, panel/rater rotation if you ever run human QA panels at scale. This is 3Play's back office. You will know you need this because you'll be manually doing something in a spreadsheet that's now too big to manage by eye.

---

## 3. Tech stack for a solo dev with no budget

Bias toward things with generous free tiers and that you can run/deploy without DevOps overhead. Don't optimize for "what would scale to 10,000 clients" — optimize for "what can one person maintain."

| Layer | Pick | Why |
|---|---|---|
| Backend + API | **FastAPI** (Python) | Same language as your pipeline, small, you already half-know it from the CLI code |
| Database | **Postgres**, hosted free on **Supabase** or **Neon** | Free tier is enough for years at your scale; Supabase also gives you auth and storage for free, which kills three separate integrations |
| Auth | **Supabase Auth** (magic link) | Don't build your own auth. Ever. |
| File storage | **Supabase Storage** or Cloudflare R2 free tier | S3-compatible, cheap egress |
| Frontend | **Next.js** on **Vercel** free tier | Deploys in minutes, huge ecosystem, AI tools (including Claude Code) are strong at it |
| Background jobs (running `kauli`) | A simple polling worker to start; Celery/RQ only once you have real concurrency problems | Don't add a task queue before you need one |
| Payments | **Pesapal** or **Flutterwave** (M-Pesa native) for Kenyan clients; Stripe later for international | Match the payment method to where your clients actually are |
| Hosting for the worker (running Whisper/Piper) | Your laptop, honestly, until volume forces a cloud box | A $5-6/month VPS (Hetzner, DigitalOcean) is the first real spend, and only once your laptop can't keep up |

This whole stack is free until you have paying revenue to justify the first VPS.

---

## 4. What goes where — Claude Code vs. this chat

**Claude Code (hands on your machine, writing and running real code):**
- Everything in Phases 0–7 above — actual implementation
- Setting up FastAPI/Next.js/Supabase locally
- Debugging real errors on real audio, real deploys
- Writing the contractor review UI, wiring the state machine, integrations

**This chat (planning, decisions, non-code work):**
- Deciding what phase to build next and whether a stop condition is actually met
- Pricing, contracts, client communication drafts
- Researching a specific API (YouTube Data API quotas, Pesapal fees, a competitor's pricing) before you commit Claude Code time to integrating it
- Revisiting this roadmap when reality disagrees with it — it will

---

## 5. Your next two weeks, concretely

1. **Finish Phase 0 in Claude Code**: get `kauli` running on a real Swahili file on your laptop, calibrate the TTS voice, confirm the output is something you'd actually hand to a client.
2. **Do NOT open a code editor for Phase 2 yet.** Find three real people or organizations who need Swahili↔English dubbing or transcription — church media, a YouTube creator, an NGO — and get one paid job through the pipeline manually, end to end, delivered by WhatsApp or email.
3. Only after that first paid delivery, come back to this document and confirm the Phase 1 stop condition, then move to Phase 2 with Claude Code.

The platform is real and worth building. It's just not what week one is for.
