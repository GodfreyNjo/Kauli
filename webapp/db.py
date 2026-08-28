"""SQLite storage for the local demo platform.

Auth itself is real now: Supabase Auth owns credentials (email/password),
verified via webapp/supabase_auth.py. This table never sees a password -
it only maps a Supabase user id -> our local role ('client'/'staff') and
display name, and is the join target for orders. Still a local, single-
machine SQLite demo otherwise - not the real Phase 2/3 platform.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "kauli_demo.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,         -- Supabase auth user id (auth.users.id)
    email TEXT UNIQUE NOT NULL,
    role TEXT NOT NULL,          -- 'client' | 'staff'
    display_name TEXT NOT NULL,
    -- Onboarding journey stage: 'new' (just signed up) -> 'activated' (first
    -- payment/order went through) -> 'nudged' (48h+ with no first job, a
    -- reminder message got queued). See onboarding_messages above and
    -- queue_onboarding_message() below.
    onboarding_status TEXT NOT NULL DEFAULT 'new',
    account_status TEXT NOT NULL DEFAULT 'active',  -- 'active' | 'closed' - see close_account()
    -- Only true for the env-var-listed founder/admin account(s) - see
    -- app.py's _resolve_role_and_admin. Staff added later via the admin
    -- panel (add_staff_invite/promote_to_staff) are real staff but not
    -- admins - they can't invite more staff themselves.
    is_admin INTEGER NOT NULL DEFAULT 0
);

-- Pre-signup staff invites: an admin adds an email here before that person
-- ever creates an account, and get_or_create_user grants the 'staff' role
-- the moment they actually sign up - see app.py's _resolve_role_and_admin.
CREATE TABLE IF NOT EXISTS staff_invites (
    email TEXT PRIMARY KEY,
    invited_by TEXT,
    invited_at REAL NOT NULL
);

-- A client's self-service "delete my data" request. Never auto-executed -
-- payment/order records may have real accounting or dispute-resolution
-- reasons to keep around for a while, so a human reviews and acts on this
-- by hand, same "real staff task, not fake automation" pattern as
-- onboarding_messages above.
CREATE TABLE IF NOT EXISTS data_deletion_requests (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    requested_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'done' | 'declined'
    resolved_at REAL,
    resolved_by TEXT,
    notes TEXT
);

-- Real posts, staff-authored only (no client-submitted content, so
-- body_html can be trusted and rendered directly - see /staff/blog).
-- Public /blog and /blog/{slug} only ever show status='published' rows.
CREATE TABLE IF NOT EXISTS blog_posts (
    id TEXT PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    description TEXT,           -- meta description / card excerpt
    body_html TEXT NOT NULL,
    author_id TEXT,
    status TEXT NOT NULL DEFAULT 'draft',  -- 'draft' | 'published'
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    published_at REAL,
    medium_url TEXT,  -- set once cross-posted to Medium - see webapp/medium_publish.py
    devto_url TEXT    -- set once cross-posted to DEV.to - see webapp/devto_publish.py
);

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    audio_path TEXT NOT NULL,
    source_lang TEXT NOT NULL,
    target_lang TEXT NOT NULL,
    tier TEXT NOT NULL,          -- 'free' | 'pro'
    asr TEXT NOT NULL,
    mt TEXT NOT NULL,
    tts TEXT NOT NULL,
    outdir TEXT NOT NULL,
    status TEXT NOT NULL,        -- mirrors the order state machine (roadmap section 1)
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (client_id) REFERENCES users(id)
);

-- Internal communication (see webapp/app.py's message routes for the
-- server-side visibility enforcement - 'internal' rows must never reach a
-- client-facing query, not just be hidden by the template).
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    visibility TEXT NOT NULL,    -- 'client' (both sides see it) | 'internal' (staff only)
    body TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders(id),
    FOREIGN KEY (sender_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS message_reads (
    user_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    last_read_at REAL NOT NULL,
    PRIMARY KEY (user_id, order_id)
);

-- Billing. See webapp/billing.py for pricing/provider logic - this table
-- only ever reflects a PAID, CONFIRMED plan; provisioning happens on
-- get_or_create_user() (defaults to 'free') so every user always has a row.
CREATE TABLE IF NOT EXISTS subscriptions (
    user_id TEXT PRIMARY KEY,
    plan TEXT NOT NULL DEFAULT 'free',       -- 'free' | 'pro' | 'premium' | 'enterprise'
    status TEXT NOT NULL DEFAULT 'active',   -- 'active' | 'expired' | 'cancelled'
    current_period_end REAL,                 -- NULL for free (never expires)
    minutes_used_this_period REAL NOT NULL DEFAULT 0,
    period_started_at REAL,
    bonus_minutes REAL NOT NULL DEFAULT 0,   -- staff-granted trial extension, see grant_bonus_minutes
    bonus_note TEXT,                          -- who granted it and why - account-manager audit trail
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- id is OUR OWN reference, generated and stored BEFORE ever calling a
-- payment provider, and is what we send them as the transaction reference
-- (Paystack's `reference`, M-Pesa's `AccountReference`). provider_reference
-- is THEIRS, filled in once confirmed, and is UNIQUE - that constraint is
-- Team accounts: a client can invite teammates to see and act on the
-- SAME orders/billing, without any of orders/payments/subscriptions
-- changing what client_id they point to. member_user_id is NULL until
-- the invite is accepted (the invitee may not have a Kauli account yet -
-- see accept_team_invite, called right after their real Supabase
-- signup/login); status stays a real audit trail rather than deleting
-- rows on removal. A user can be an accepted member of at most one
-- owner (enforced in code, not a DB constraint - see
-- get_team_owner_for_member's ORDER BY/LIMIT 1, kept simple since this
-- is a real but small feature, not multi-org SaaS).
CREATE TABLE IF NOT EXISTS team_members (
    id TEXT PRIMARY KEY,
    owner_client_id TEXT NOT NULL,
    member_user_id TEXT,
    invited_email TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- 'pending' | 'accepted' | 'removed'
    invited_at REAL NOT NULL,
    accepted_at REAL,
    FOREIGN KEY (owner_client_id) REFERENCES users(id)
);

-- the actual double-payment guard: even if a webhook fires twice for the
-- same provider transaction, the second insert/update is rejected at the
-- database level, not just "trusted" to only happen once.
CREATE TABLE IF NOT EXISTS payments (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    plan TEXT NOT NULL,                      -- for order payments: the plan used for pricing/discount
    order_id TEXT,                           -- set for a per-order usage charge; NULL for a plan subscription purchase
    amount_usd REAL NOT NULL,
    amount_local REAL,
    currency TEXT NOT NULL,
    provider TEXT NOT NULL,                  -- 'mpesa' | 'paystack' | 'bank'
    provider_reference TEXT UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'completed' | 'failed'
    meta TEXT,                                -- small JSON blob (phone number, checkout id, etc.)
    created_at REAL NOT NULL,
    completed_at REAL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- Marketing-site contact/callback requests (public "/" landing page,
-- POST /contact/request-callback). No account or login required to submit
-- one - this is the top of the funnel, before anyone has a Kauli account.
-- Staff triage these by hand into real client accounts.
CREATE TABLE IF NOT EXISTS leads (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    phone TEXT,
    company TEXT,
    message TEXT,
    preferred_time TEXT,
    -- pipeline stage: 'new' -> 'contacted' -> 'qualified' -> 'proposal' -> 'won' | 'lost'.
    -- 'won' is set automatically the moment someone with a matching email
    -- actually signs up (see get_or_create_user) - that's the real
    -- "converted" moment, not something staff have to notice and flag.
    status TEXT NOT NULL DEFAULT 'new',
    source TEXT NOT NULL DEFAULT 'website',   -- 'website' | 'instagram' | 'facebook' | 'tiktok' | 'whatsapp' | 'referral' | 'other'
    assigned_to TEXT,                          -- staff user id handling this lead, if any
    converted_user_id TEXT,                    -- set once this lead becomes a real account
    created_at REAL NOT NULL
);

-- Activity timeline per lead: calls made, notes, status changes - a CRM
-- without a paper trail is just a spreadsheet with extra steps.
CREATE TABLE IF NOT EXISTS lead_notes (
    id TEXT PRIMARY KEY,
    lead_id TEXT NOT NULL,
    author_id TEXT,             -- staff user id; NULL for system-generated notes (e.g. auto-conversion)
    body TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY (lead_id) REFERENCES leads(id)
);

-- A real, itemized receipt for every completed payment - not just the
-- staff bank-confirmation note above, this is Kauli's own receipt (what
-- Paystack/M-Pesa email is proof you paid THEM; this is proof of what you
-- bought FROM us), auto-created the moment a payment completes (see
-- _activate_payment in app.py). email_status stays 'queued' rather than
-- 'sent' - there's no real transactional email provider wired up
-- anywhere in this app, so nothing here claims to have been delivered
-- that wasn't. Real, viewable, printable receipt page exists regardless
-- (see /receipts/{id}); this field is honest about the one part
-- (automatic delivery) that isn't real yet.
CREATE TABLE IF NOT EXISTS receipts (
    id TEXT PRIMARY KEY,
    receipt_number TEXT UNIQUE NOT NULL,
    payment_id TEXT NOT NULL,
    client_id TEXT NOT NULL,
    description TEXT NOT NULL,
    amount_usd REAL NOT NULL,
    amount_local REAL,
    currency TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_reference TEXT,
    email_status TEXT NOT NULL DEFAULT 'queued',   -- 'queued' (not sent - no mailer configured, or awaiting staff) | 'sent' (auto or by staff) | 'failed' (auto-send attempted and Brevo rejected/errored - see email_send_detail)
    issued_at REAL NOT NULL,
    FOREIGN KEY (payment_id) REFERENCES payments(id),
    FOREIGN KEY (client_id) REFERENCES users(id)
);

-- A one-click satisfaction rating from an email link (see app.py's
-- _queue_first_payment_message) - real, low-stakes, not signed/tokenized
-- the way a payment or auth action would be (same trust level as an
-- order ID or receipt ID elsewhere in this app: a real, non-sequential
-- id in the URL, not a security boundary). 'needs_work' notifies staff
-- immediately - this is the churn-risk signal worth acting on same-day.
CREATE TABLE IF NOT EXISTS client_feedback (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    context TEXT NOT NULL,        -- e.g. 'first_payment'
    rating TEXT NOT NULL,         -- 'great' | 'good' | 'needs_work'
    created_at REAL NOT NULL
);

-- A real, staff-composed newsletter (see app.py's /staff/newsletter) -
-- every field here is content a staff member actually typed or picked,
-- not AI-generated and auto-sent. Kept as a real record for the same
-- reason receipts/onboarding_messages are: an audit trail of what
-- actually went out and to how many people, not just a fire-and-forget
-- action with no history.
CREATE TABLE IF NOT EXISTS newsletters (
    id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    blog_post_id TEXT,             -- the "Signal-First" highlight, if one was picked
    feature_update TEXT,           -- the "Kauli Scoop" - real, staff-written text
    industry_trend_text TEXT,
    industry_trend_url TEXT,
    sent_by TEXT NOT NULL,
    recipient_count INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

-- A client pushing back on a limit the system applied to their account
-- (a rate limit, a plan restriction) - "Think this is a mistake?" on the
-- error/limit message itself, not a generic contact form. 'open' ->
-- 'granted' (staff actually lifted something, see grant_trusted_submitter)
-- | 'declined' (staff looked at it and said no, with a reason).
CREATE TABLE IF NOT EXISTS limit_exception_requests (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    context TEXT NOT NULL,        -- which limit, e.g. 'order_submission_rate_limit'
    client_note TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    staff_note TEXT,
    resolved_by TEXT,
    created_at REAL NOT NULL,
    resolved_at REAL,
    FOREIGN KEY (client_id) REFERENCES users(id)
);

-- A client-tracked YouTube channel/playlist - polled periodically (see
-- webapp/youtube_poll.py) for new public uploads using a read-only API
-- key, not OAuth. This is deliberately the "detect it for you" half of
-- automation, not the "process and bill it for you automatically" half -
-- new videos land in youtube_pending_imports for a human to turn into a
-- real order, same as pasting the link manually would. That's the actual
-- safeguard against the doc's own "accidental giant processing bill" risk,
-- not a rule about which videos get flagged.
CREATE TABLE IF NOT EXISTS youtube_watches (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    playlist_id TEXT NOT NULL,   -- resolved uploads-playlist id, not the raw channel id
    label TEXT,                  -- client's own name for it, e.g. "Main channel"
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    last_checked_at REAL,
    last_error TEXT,             -- most recent poll failure, if any - surfaced to the client, not silent
    FOREIGN KEY (client_id) REFERENCES users(id)
);

-- One row per new public video found on a watch - never auto-becomes a
-- real order. 'new' -> 'imported' (a real order now references this
-- video_id) | 'dismissed' (client said no thanks).
CREATE TABLE IF NOT EXISTS youtube_pending_imports (
    id TEXT PRIMARY KEY,
    watch_id TEXT NOT NULL,
    video_id TEXT NOT NULL,
    title TEXT NOT NULL,
    published_at REAL,
    status TEXT NOT NULL DEFAULT 'new',
    found_at REAL NOT NULL,
    FOREIGN KEY (watch_id) REFERENCES youtube_watches(id),
    UNIQUE (watch_id, video_id)
);

-- The client-onboarding message queue. A real transactional email
-- provider (Brevo, see webapp/mailer.py) is now wired up for receipts,
-- but not yet plugged into this queue - every onboarding trigger still
-- writes a real, fully-rendered message here with status 'pending_send'
-- rather than assume it went out. Staff see it on the CRM page today and
-- can copy it into an email or WhatsApp by hand; wiring mailer.send_email
-- into this queue too (same pattern as _activate_payment's receipt send)
-- is a small follow-up, not yet done - the trigger logic below never
-- changes, only how the last step is fulfilled.
CREATE TABLE IF NOT EXISTS onboarding_messages (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL,          -- 'welcome' | 'first_payment' | 'inactivity_nudge'
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_send',   -- 'pending_send' | 'sent' | 'skipped'
    created_at REAL NOT NULL,
    sent_at REAL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- Immutable trail of every upload attempt (accepted or rejected) - see
-- webapp/upload_security.py. Not editable from anywhere in the app on
-- purpose; this is the audit record, not a working table.
-- Human voice-over talent roster. Staff-managed for now, by design - see
-- webapp/app.py's staff_voice_actors routes. No self-service actor
-- login/portal exists yet because there are no real actors onboarded to
-- build one around; add that the moment recruiting produces real people
-- who'd actually use it, not speculatively ahead of them.
CREATE TABLE IF NOT EXISTS voice_actors (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT,
    phone TEXT,                -- for arranging the real payout, not stored as a payment credential
    languages TEXT NOT NULL,   -- comma-separated, e.g. "sw,en" - matches order source/target_lang values
    bio TEXT,
    rate_per_min_usd REAL,     -- negotiated per actor - see billing.ADDONS['human_voice_over']'s own
                                -- comment on why this is NOT a single platform-wide rate
    status TEXT NOT NULL DEFAULT 'active',  -- 'active' | 'inactive'
    notes TEXT,                -- internal only, staff-visible
    created_at REAL NOT NULL,
    user_id TEXT                -- linked once this actor actually signs up (see is_invited_voice_actor /
                                 -- link_voice_actor_user) - NULL means staff added them to the roster but
                                 -- they haven't created a real login yet.
);

-- What's owed/paid to a voice actor for one order's work - a real ledger
-- staff maintains by hand, not something a payment API writes to. No
-- M-Pesa/Paystack Transfer integration exists here on purpose (moving
-- real money to a third party needs a human to actually send it and a
-- real payout-capable account, neither of which this build has) - a row
-- here is created once an actor's work on an order is ready to be paid
-- for, and marked 'paid' once staff has actually sent that real transfer
-- outside this system. See webapp/app.py's create_payout / mark_payout_paid.
CREATE TABLE IF NOT EXISTS voice_actor_payouts (
    id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    minutes REAL NOT NULL,
    rate_per_min_usd REAL NOT NULL,
    amount_usd REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'owed',  -- 'owed' | 'paid'
    paid_at REAL,
    paid_by TEXT,               -- staff user id who confirmed the real transfer was sent
    paid_reference TEXT,        -- e.g. an M-Pesa transaction code, for their own records
    created_at REAL NOT NULL,
    FOREIGN KEY (actor_id) REFERENCES voice_actors(id),
    FOREIGN KEY (order_id) REFERENCES orders(id),
    FOREIGN KEY (paid_by) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS upload_audit_log (
    id TEXT PRIMARY KEY,
    user_id TEXT,
    order_id TEXT,                -- NULL if rejected before an order was created
    original_filename TEXT,
    stored_filename TEXT,
    sha256 TEXT,
    size_bytes INTEGER,
    magic_sniff TEXT,
    ffprobe_ok INTEGER,
    clamav_clean INTEGER,
    clamav_detail TEXT,
    metadata_stripped INTEGER,
    content_safety_flagged INTEGER,     -- see upload_security.scan_video_for_explicit_content
    content_safety_detail TEXT,
    rejected INTEGER NOT NULL DEFAULT 0,
    reject_reason TEXT,
    ip_address TEXT,
    user_agent TEXT,
    created_at REAL NOT NULL
);

-- Real, persistent in-app notifications (the staff overview page's bell) -
-- separate from notifications.py's notify_staff*, which only ever sends an
-- EMAIL. Both fire from the same real events; this is what lets a staff
-- member see "5 things happened" without checking their inbox. link is a
-- same-origin path (an order/lead page, etc.), never rendered as raw HTML.
CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY,
    recipient_id TEXT NOT NULL,
    kind TEXT NOT NULL,          -- 'needs_review' | 'dead_letter' | 'payment_received' | 'ops_decision'
    title TEXT NOT NULL,
    link TEXT,
    created_at REAL NOT NULL,
    read_at REAL,
    FOREIGN KEY (recipient_id) REFERENCES users(id)
);

-- Lets a client's own systems (or Zapier/Make) call Kauli's read-only API
-- instead of the browser UI. One key per client account (client_id here is
-- always the team-owner account, same scope team_members shares) - only the
-- SHA-256 hash is ever stored; the real key is shown once, at generation
-- time, in app.py's settings route and never written to the database or a
-- log. key_prefix is just enough of the real value (never secret on its
-- own) for the settings page to show "which key is this" without being
-- able to show the real thing again.
CREATE TABLE IF NOT EXISTS client_api_keys (
    client_id TEXT PRIMARY KEY,
    key_hash TEXT NOT NULL UNIQUE,
    key_prefix TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_used_at REAL,
    FOREIGN KEY (client_id) REFERENCES users(id)
);

-- A client-configured URL that gets a real, signed POST the moment one of
-- their orders reaches ready_for_delivery (see worker.notify_client_order_ready).
-- Unlike the API key's hash-only storage, `secret` is kept in plain text
-- here on purpose - the client needs the same value back to verify our
-- signature (HMAC-SHA256 over the request body) on their end, the same way
-- Stripe/GitHub webhook secrets work.
CREATE TABLE IF NOT EXISTS client_webhooks (
    client_id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    secret TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (client_id) REFERENCES users(id)
);

-- A real, visible log of every webhook attempt - so a client (or Godfrey,
-- debugging on their behalf) can see whether a delivery actually happened
-- rather than just hoping. One attempt per event, no retry queue yet (see
-- app.py's _fire_client_webhook docstring) - ok is 0/1, status_code is NULL
-- on a connection-level failure (timeout, DNS, refused).
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    event TEXT NOT NULL,
    sent_at REAL NOT NULL,
    ok INTEGER NOT NULL DEFAULT 0,
    status_code INTEGER,
    error TEXT,
    FOREIGN KEY (client_id) REFERENCES users(id)
);
"""

def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Off by default in SQLite, per-connection - without this, FK clauses
    # in the schema (leads.lead_id -> leads(id), etc.) are silently
    # decorative and never actually enforced. Older tables here (orders,
    # messages, payments) predate any FK declarations and would need a
    # full table rebuild (SQLite can't ALTER TABLE to add a constraint) to
    # get real ones - a real migration tool's job, not a quick patch here,
    # so this only starts enforcing what's already declared.
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL (write-ahead log) instead of SQLite's default rollback journal -
    # readers no longer block behind a writer (or vice versa), which
    # matters the moment this is a real deployment with a client browsing
    # their order while staff corrects a segment in Ereri, instead of one
    # person on one laptop. A real, persistent, file-level setting (not
    # per-connection like foreign_keys above) - this line just makes sure
    # it's actually on rather than assuming a prior connection already set
    # it. Free, no schema change, safe to flip any time.
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    conn = get_conn()
    conn.executescript(SCHEMA)
    # CREATE TABLE IF NOT EXISTS above is a no-op against a users table that
    # already exists (yours does, with real accounts in it) - new columns
    # need an explicit migration, not just adding them to SCHEMA.
    existing_user_cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    if "avatar_path" not in existing_user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN avatar_path TEXT")
    if "onboarding_status" not in existing_user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN onboarding_status TEXT NOT NULL DEFAULT 'new'")
    if "account_status" not in existing_user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN account_status TEXT NOT NULL DEFAULT 'active'")
    if "is_admin" not in existing_user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
    if "created_at" not in existing_user_cols:
        # Backfilled to "now" for accounts that already exist (this
        # migration only runs once) - not real history for those, but
        # accurate for every account created from here on, which is what
        # clients_needing_activation_nudge() below actually needs.
        #
        # Moved up here from further down in this function - a real bug
        # caught by the FIRST deploy against a genuinely fresh database:
        # old_clients_missing_leads (below) reads users.created_at, but
        # this migration used to run after that query, so it only ever
        # worked by accident on a dev database that already had the
        # column from an earlier code version. A brand-new database has
        # no such history to hide behind.
        conn.execute("ALTER TABLE users ADD COLUMN created_at REAL")
        conn.execute("UPDATE users SET created_at = ? WHERE created_at IS NULL", (time.time(),))
    if "trusted_submitter_until" not in existing_user_cols:
        # Staff-granted, time-boxed lift on the order-submission rate limit
        # (see rate_limit.check's "submit:{user_id}" key in app.py) -
        # granted from a real limit_exception_request, not a blanket
        # account flag with no expiry or paper trail.
        conn.execute("ALTER TABLE users ADD COLUMN trusted_submitter_until REAL")
    if "marketing_consent" not in existing_user_cols:
        # Granular, GDPR/Kenya DPA-style consent - separate from the
        # transactional emails (order status, receipts) every account gets
        # regardless, which aren't "marketing" and don't need opt-in.
        # _at/_ip are the real audit trail: when and from where consent was
        # actually given (or withdrawn - see set_marketing_consent), not
        # just the current true/false.
        conn.execute("ALTER TABLE users ADD COLUMN marketing_consent INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE users ADD COLUMN marketing_consent_at REAL")
        conn.execute("ALTER TABLE users ADD COLUMN marketing_consent_ip TEXT")
    if "default_source_lang" not in existing_user_cols:
        # Personal, not account-wide (unlike marketing_consent/API keys/
        # webhooks, which are the account owner's call) - a teammate on a
        # shared team account might genuinely work a different language
        # pair than the owner, so these live on the individual user row and
        # only ever prefill the NEW-order wizard's own fields (see
        # app.py's client_dashboard) - never silently applied to an order
        # itself, which always uses whatever the wizard actually submitted.
        conn.execute("ALTER TABLE users ADD COLUMN default_source_lang TEXT")
        conn.execute("ALTER TABLE users ADD COLUMN default_target_lang TEXT")
        conn.execute("ALTER TABLE users ADD COLUMN default_service_level TEXT")
        conn.execute("ALTER TABLE users ADD COLUMN default_rush INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE users ADD COLUMN default_addon_video INTEGER NOT NULL DEFAULT 0")
    if "staff_notes" not in existing_user_cols:
        # Internal-only context a staff member jots about this client
        # account (a real CRM basic - "prefers WhatsApp", "sensitive
        # deadline last time", whatever's actually worth remembering) -
        # never shown to the client themselves, only on /staff/clients.
        conn.execute("ALTER TABLE users ADD COLUMN staff_notes TEXT")
    if "trial_verified_at" not in existing_user_cols:
        # Real-person gate before a client's free minutes can be spent -
        # see billing.TRIAL_VERIFICATION_FEE_USD. Every account that
        # already exists at the moment this migration runs is grandfathered
        # in (set to right now, not left NULL) - they already used the free
        # allowance under the old rules, or are staff/voice-actor rows this
        # was never meant to gate in the first place; retroactively
        # blocking real, already-onboarded accounts the first time this
        # ships would be a real regression, not a fraud fix.
        conn.execute("ALTER TABLE users ADD COLUMN trial_verified_at REAL")
        conn.execute("UPDATE users SET trial_verified_at = ? WHERE trial_verified_at IS NULL", (time.time(),))
    if "signup_ip" not in existing_user_cols:
        # Real, honest fraud-triage signal - see ip_intel.py's own module
        # docstring for exactly what this does and doesn't prove.
        # signup_ip_is_datacenter is nullable on purpose: NULL means "not
        # checked yet / couldn't check", never treated the same as 0
        # ("checked, looks like a real residential IP") - see
        # staff_clients.html for how the three states render differently.
        conn.execute("ALTER TABLE users ADD COLUMN signup_ip TEXT")
        conn.execute("ALTER TABLE users ADD COLUMN signup_ip_is_datacenter INTEGER")
    if "onboarding_tour_seen_at" not in existing_user_cols:
        # Real server-side flag for the first-visit dashboard walkthrough
        # (see client_dashboard.html/static/tour.js) - server-side, not
        # just localStorage, so it survives a cleared browser and shows
        # the same "already seen it" state on any device they log in
        # from. Every existing account is grandfathered as already-seen
        # for the same reason trial_verified_at is: a tour aimed at a
        # brand-new user popping up for someone who's used Kauli for
        # weeks already would be a regression, not onboarding help.
        conn.execute("ALTER TABLE users ADD COLUMN onboarding_tour_seen_at REAL")
        conn.execute(
            "UPDATE users SET onboarding_tour_seen_at = ? WHERE onboarding_tour_seen_at IS NULL", (time.time(),))
    existing_payment_cols = {row["name"] for row in conn.execute("PRAGMA table_info(payments)")}
    if "receipt_path" not in existing_payment_cols:
        # A real uploaded receipt image/PDF for an off-platform (bank
        # transfer, cash) payment, attached when staff confirms it - see
        # staff_confirm_bank_payment. Local disk, same pattern as every
        # other upload in this app (audio, style guides, avatars) - no S3
        # or cloud storage exists here.
        conn.execute("ALTER TABLE payments ADD COLUMN receipt_path TEXT")
        conn.execute("ALTER TABLE payments ADD COLUMN staff_note TEXT")
    if "refunded_at" not in existing_payment_cols:
        # A refund is a real, separate event layered on top of a real
        # completed payment - status stays 'completed' (the charge really
        # did happen), this just records that it was later reversed.
        # Built for the $1 trial-verification charge specifically (see
        # staff_refund_trial_verification) - a genuine last resort when a
        # client's onboarding hasn't worked out despite staff actually
        # trying, not an automated/client-facing refund button.
        conn.execute("ALTER TABLE payments ADD COLUMN refunded_at REAL")
        conn.execute("ALTER TABLE payments ADD COLUMN refund_reference TEXT")
    existing_receipt_cols = {row["name"] for row in conn.execute("PRAGMA table_info(receipts)")}
    if "line_items_json" not in existing_receipt_cols:
        # Real per-service breakdown (see orders.cost_breakdown_json above),
        # frozen onto the receipt the moment it's issued - an immutable
        # record of what was actually charged, not something that changes
        # if the order or client's plan changes later. NULL for
        # non-order payments (plan subscriptions, wallet top-ups), which
        # only ever have one real line anyway.
        conn.execute("ALTER TABLE receipts ADD COLUMN line_items_json TEXT")
    if "email_send_detail" not in existing_receipt_cols:
        # Set the moment an automatic send is attempted (see mailer.py /
        # app.py's _activate_payment) - Brevo's messageId on success, or
        # a human-readable reason on failure, so staff can see WHY one
        # didn't go out instead of just that it's stuck 'queued'.
        conn.execute("ALTER TABLE receipts ADD COLUMN email_send_detail TEXT")
    existing_onboarding_cols = {row["name"] for row in conn.execute("PRAGMA table_info(onboarding_messages)")}
    if "email_send_detail" not in existing_onboarding_cols:
        # Same pattern as receipts.email_send_detail above, now that a real
        # mailer (Brevo, see webapp/mailer.py) exists - 'pending_send' still
        # means exactly what it always did (no send attempted, or one that
        # failed - see detail), 'sent' now covers both an automatic send and
        # a staff member confirming they forwarded it by hand.
        conn.execute("ALTER TABLE onboarding_messages ADD COLUMN email_send_detail TEXT")
    # Reconciled on EVERY startup, not gated behind the one-time column-add
    # above - deliberately, after finding the hard way that a migration
    # helper invoked without .env loaded (KAULI_STAFF_EMAILS empty in that
    # process) silently backfills nothing and, being one-time, never gets
    # a second chance. Re-running this each start is cheap, idempotent, and
    # self-heals that case - and also means adding a new email to
    # KAULI_STAFF_EMAILS later promotes them to admin on the next restart
    # with no manual DB surgery needed.
    env_staff = {e.strip().lower() for e in os.environ.get("KAULI_STAFF_EMAILS", "").split(",") if e.strip()}
    for email in env_staff:
        conn.execute("UPDATE users SET is_admin = 1 WHERE lower(email) = ?", (email,))
    # Backfill for clients who signed up BEFORE the auto-add-to-CRM logic
    # above existed - same "reconcile on every startup" reasoning as the
    # staff-promotion block above it: idempotent (WHERE NOT EXISTS means a
    # client only ever gets backfilled once), and self-heals if this ever
    # needs to run again for any reason.
    old_clients_missing_leads = conn.execute(
        """SELECT id, email, display_name, created_at FROM users
           WHERE role = 'client' AND NOT EXISTS (
               SELECT 1 FROM leads WHERE lower(leads.email) = lower(users.email)
           )"""
    ).fetchall()
    for u in old_clients_missing_leads:
        new_lead_id = uuid.uuid4().hex[:12]
        conn.execute(
            "INSERT INTO leads (id, name, email, status, source, converted_user_id, created_at) "
            "VALUES (?, ?, ?, 'won', 'signup', ?, ?)",
            (new_lead_id, u["display_name"] or u["email"].split("@")[0], u["email"], u["id"], u["created_at"]),
        )
        conn.execute(
            "INSERT INTO lead_notes (id, lead_id, author_id, body, created_at) VALUES (?, ?, NULL, ?, ?)",
            (uuid.uuid4().hex[:12], new_lead_id,
             "Backfilled - this account existed before clients were auto-added to the CRM.", time.time()),
        )
    existing_blog_cols = {row["name"] for row in conn.execute("PRAGMA table_info(blog_posts)")}
    if "medium_url" not in existing_blog_cols:
        conn.execute("ALTER TABLE blog_posts ADD COLUMN medium_url TEXT")
    if "devto_url" not in existing_blog_cols:
        conn.execute("ALTER TABLE blog_posts ADD COLUMN devto_url TEXT")
    if "category" not in existing_blog_cols:
        conn.execute("ALTER TABLE blog_posts ADD COLUMN category TEXT")
    if "views" not in existing_blog_cols:
        # Real page-view count, incremented on every real GET /blog/{slug}
        # (see increment_blog_post_views) - not unique-visitor tracking,
        # just "how many times has this page actually loaded", the same
        # honest scope as a plain server-side hit counter. Staff viewing
        # their own post while editing/QA-ing it isn't excluded - a small,
        # known overcount, not worth a session-tracking system to avoid.
        conn.execute("ALTER TABLE blog_posts ADD COLUMN views INTEGER NOT NULL DEFAULT 0")
    existing_upload_audit_cols = {row["name"] for row in conn.execute("PRAGMA table_info(upload_audit_log)")}
    if "content_safety_flagged" not in existing_upload_audit_cols:
        conn.execute("ALTER TABLE upload_audit_log ADD COLUMN content_safety_flagged INTEGER")
        conn.execute("ALTER TABLE upload_audit_log ADD COLUMN content_safety_detail TEXT")
    existing_order_cols = {row["name"] for row in conn.execute("PRAGMA table_info(orders)")}
    if "content_safety_flagged" not in existing_order_cols:
        # See upload_security.scan_video_for_explicit_content - set when a
        # video upload's sampled frames trip the NSFW classifier. Holds the
        # order for a human's call (worker.py's final-status check) rather
        # than blocking upload outright - see that module's docstring for
        # why (a real false-positive on legitimate medical footage, found
        # during testing before this shipped).
        conn.execute("ALTER TABLE orders ADD COLUMN content_safety_flagged INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE orders ADD COLUMN content_safety_detail TEXT")
    if "voice_clone_consent_given_at" not in existing_order_cols:
        # An explicit, client-initiated, logged action - see
        # set_voice_clone_consent() and app.py's staff_set_dub_voice, which
        # refuses to run XTTS on an order without this set REGARDLESS of
        # what the staff-side form submits. No UI trick (hiding a dropdown
        # option, etc.) is what actually enforces this - the server route
        # checking this column is. Real hardening, not a guarantee that
        # nobody lies when they check the box - see the client-facing copy
        # this is attached to for the honest framing of that limit.
        conn.execute("ALTER TABLE orders ADD COLUMN voice_clone_consent_given_at REAL")
        conn.execute("ALTER TABLE orders ADD COLUMN voice_clone_consent_ip TEXT")
    if "source_youtube_id" not in existing_order_cols:
        # Set only for YouTube-sourced orders - lets Ereri embed the actual
        # YouTube player for visual context instead of a downloaded video
        # file (see app.py's _download_youtube: audio-only fetch).
        conn.execute("ALTER TABLE orders ADD COLUMN source_youtube_id TEXT")
    if "service_level" not in existing_order_cols:
        # Billing fields - see billing.order_cost_usd. duration_minutes is
        # the real probed length (what's actually charged for); cost_usd is
        # the final amount after free allowance + plan discount, frozen at
        # order-creation time so a later price change never retroactively
        # changes what an existing order owes.
        conn.execute("ALTER TABLE orders ADD COLUMN service_level TEXT")
        conn.execute("ALTER TABLE orders ADD COLUMN duration_minutes REAL")
        conn.execute("ALTER TABLE orders ADD COLUMN cost_usd REAL")
    if "addons" not in existing_order_cols:
        # Paid upgrades this order bought above its plan's default features
        # (see billing.ADDONS) - a JSON list, e.g. '["video_deliverables"]'.
        conn.execute("ALTER TABLE orders ADD COLUMN addons TEXT")
    if "instr_verbatim_level" not in existing_order_cols:
        # Client job instructions - set at order creation, read by staff/
        # Ereri throughout the job, never changed after submission (if a
        # client wants different handling, that's a new order or a message,
        # not a silent edit to what was already agreed for this one).
        conn.execute("ALTER TABLE orders ADD COLUMN instr_speaker_ids INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE orders ADD COLUMN instr_verbatim_level TEXT NOT NULL DEFAULT 'clean_read'")
        conn.execute("ALTER TABLE orders ADD COLUMN instr_transcribe_lyrics INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE orders ADD COLUMN instr_use_italics INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE orders ADD COLUMN instr_existing_subs TEXT NOT NULL DEFAULT 'ignore'")
        conn.execute("ALTER TABLE orders ADD COLUMN instr_no_audio TEXT NOT NULL DEFAULT 'tag'")
        conn.execute("ALTER TABLE orders ADD COLUMN instr_wrong_language TEXT NOT NULL DEFAULT 'tag'")
        conn.execute("ALTER TABLE orders ADD COLUMN instr_instrumental_only TEXT NOT NULL DEFAULT 'tag'")
        conn.execute("ALTER TABLE orders ADD COLUMN instr_notes TEXT")
        conn.execute("ALTER TABLE orders ADD COLUMN style_guide_path TEXT")
        conn.execute("ALTER TABLE orders ADD COLUMN style_guide_filename TEXT")
    if "return_reason" not in existing_order_cols:
        # The editor-return / ops-triage workflow: an editor who hits
        # no-audio, wrong-language, instrumental-only (etc.) content that
        # the client's own instructions say to return, or anything else
        # that needs a human decision, sends the order to 'editor_returned'
        # with a reason here - staff then either contacts the client or
        # formally returns the job ('returned_to_client').
        conn.execute("ALTER TABLE orders ADD COLUMN return_reason TEXT")
        conn.execute("ALTER TABLE orders ADD COLUMN return_note TEXT")
    if "workflow_steps" not in existing_order_cols:
        # Manual "I've actually finished this stage" checkmarks for the
        # Ereri workflow stepper - a JSON dict of step key -> bool. Only the
        # steps that exist for this job's service level are ever shown or
        # settable (see billing.workflow_steps_for_order); "deliverables" is
        # computed from real files on disk, not stored here.
        conn.execute("ALTER TABLE orders ADD COLUMN workflow_steps TEXT")
    if "difficulty_surcharge_status" not in existing_order_cols:
        # Some files are genuinely harder to process than others - heavy
        # background noise, overlapping speakers, strong accents - and
        # that's only knowable AFTER ASR actually runs, which is after the
        # base price was already quoted and paid. Rather than auto-charge
        # more than was agreed to, staff can propose a surcharge once the
        # real difficulty is known (see app.py's audio_difficulty_rate);
        # the client has to see it and pay it before delivery unlocks -
        # nothing here ever charges without that explicit approval.
        # status: NULL (never proposed) / 'pending_approval' / 'approved' / 'waived'.
        conn.execute("ALTER TABLE orders ADD COLUMN difficulty_surcharge_status TEXT")
        conn.execute("ALTER TABLE orders ADD COLUMN difficulty_surcharge_pct REAL")
        conn.execute("ALTER TABLE orders ADD COLUMN difficulty_surcharge_usd REAL")
        conn.execute("ALTER TABLE orders ADD COLUMN difficulty_surcharge_note TEXT")
    if "difficulty_surcharge_reason" not in existing_order_cols:
        # Generalizes the surcharge above from "only audio difficulty" to
        # "any additional charge this order's workflow needed" (rush
        # processing, an extra revision round, a format not covered by the
        # original order, etc.) - same propose -> client approves -> pay
        # via Paystack/M-Pesa/bank -> unlocks delivery flow either way,
        # just labeled by what actually happened. NULL/'difficult_audio'
        # (the original, still auto-suggested from real ASR confidence)
        # for anything proposed before this existed. See EXTRA_CHARGE_REASONS.
        conn.execute("ALTER TABLE orders ADD COLUMN difficulty_surcharge_reason TEXT")
    if "ai_cost_usd" not in existing_order_cols:
        # Real dollar cost of this order's MT calls, read back from
        # kauli's Job.cost_usd once processing finishes (see
        # kauli/pipeline.py and providers/mt.py's ClaudeMT.total_cost_usd) -
        # 0.0 for jobs on local/stub MT, which don't have a real marginal
        # cost per call. Feeds the ops dashboard's spend-spike check.
        conn.execute("ALTER TABLE orders ADD COLUMN ai_cost_usd REAL NOT NULL DEFAULT 0.0")
    if "is_free_preview" not in existing_order_cols:
        # True only when this order's ENTIRE cost was covered by the free
        # monthly transcription allowance (never true for a wallet- or
        # real-money-covered $0 order - see app.py's create_order). Gates
        # the download routes and order_detail.html's deliverables block:
        # preview the transcript on-screen, no file download, until a real
        # paid order covers it. See billing.FREE_MINUTES_SERVICE_LEVEL.
        conn.execute("ALTER TABLE orders ADD COLUMN is_free_preview INTEGER NOT NULL DEFAULT 0")
    if "folder_name" not in existing_order_cols:
        # Client-set free-text grouping label ("Q3 campaign", "NGO field
        # reports") - lets a client with many jobs find "everything for
        # this project" without us building real folders/projects as their
        # own entity. Optional, defaults to ungrouped.
        conn.execute("ALTER TABLE orders ADD COLUMN folder_name TEXT")
    if "idempotency_key" not in existing_order_cols:
        # A slow connection + an impatient double-click on Submit used to
        # mean two real orders (and two real charges) for one upload - the
        # client generates one UUID per page load (see client_dashboard.html)
        # and it rides along with the form; create_order() checks for an
        # existing order with the same (client_id, idempotency_key) before
        # creating a new one, the same idea the doc's Idempotency-Key
        # header describes, just backed by a UNIQUE index instead of Redis.
        conn.execute("ALTER TABLE orders ADD COLUMN idempotency_key TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_idempotency "
            "ON orders(client_id, idempotency_key) WHERE idempotency_key IS NOT NULL")
    if "retry_count" not in existing_order_cols:
        # How many times worker.py has retried this job after a transient-
        # looking failure - see worker._run_job. Exhausting the retry
        # budget lands the order in 'dead_letter' (added to
        # TERMINAL_FAIL_STATUSES below) instead of endlessly re-queueing a
        # file that's genuinely never going to process - the local,
        # SQLite-and-threading equivalent of a message queue's dead-letter
        # queue, without needing Redis/SQS to get the same protection.
        conn.execute("ALTER TABLE orders ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0")
    if "dub_voice" not in existing_order_cols:
        # Which voice actually rendered the current dub_{lang}.wav - NULL
        # means "never re-picked, still whatever the order was created
        # with" (kauli.providers.tts's default Piper voice). See
        # kauli.providers.tts.PIPER_VOICES for the picker's real options,
        # or "xtts" for an actual clone of the source speaker.
        # dub_voice_job_status tracks an in-progress XTTS clone (real
        # cloning is slow - minutes, not seconds - so it runs on a
        # background thread; NULL = idle, 'running', or 'failed:<message>').
        conn.execute("ALTER TABLE orders ADD COLUMN dub_voice TEXT")
        conn.execute("ALTER TABLE orders ADD COLUMN dub_voice_job_status TEXT")
    if "deadline_at" not in existing_order_cols:
        # See webapp/tat.py. tat_start_at is when the clock actually
        # started (processing start - payment confirmed, or immediately
        # for a $0 order), not order submission time. internal_deadline_at
        # is the real hard stop; deadline_at is the client-facing promise
        # (later, on purpose - see tat.CLIENT_BUFFER_PCT). The two
        # *_sent_at columns dedupe the deadline-watch alerts (see
        # deadline_watch.py) so each one only ever fires once per order.
        conn.execute("ALTER TABLE orders ADD COLUMN tat_start_at REAL")
        conn.execute("ALTER TABLE orders ADD COLUMN internal_deadline_at REAL")
        conn.execute("ALTER TABLE orders ADD COLUMN deadline_at REAL")
        conn.execute("ALTER TABLE orders ADD COLUMN deadline_warning_sent_at REAL")
        conn.execute("ALTER TABLE orders ADD COLUMN deadline_missed_alert_sent_at REAL")
    if "cost_breakdown_json" not in existing_order_cols:
        # The full billing.order_cost_usd() breakdown (rate, billable
        # minutes, free/wallet minutes applied, plan discount, addon
        # lines), frozen at order-creation time same as cost_usd above -
        # lets a receipt show real per-service line items instead of one
        # flat "Order <id> - <plan> plan" figure, without recomputing
        # against whatever the client's plan/rates happen to be later.
        conn.execute("ALTER TABLE orders ADD COLUMN cost_breakdown_json TEXT")
    if "is_rush" not in existing_order_cols:
        # A client-chosen surcharge for real, manual queue priority - see
        # billing.RUSH_SURCHARGE_PCT and tat.RUSH_TAT_MULTIPLIER. Frozen at
        # order-creation time same as the rest of the billing snapshot
        # above; also what gates the client-facing "Call us" phone option
        # on a rush order that's actually in trouble (see order_detail.html).
        conn.execute("ALTER TABLE orders ADD COLUMN is_rush INTEGER NOT NULL DEFAULT 0")
    if "status_changed_at" not in existing_order_cols:
        # When the order LAST actually changed status, not just any field -
        # set alongside status in every one of the few places that assign
        # it (update_order_status, flag_order_for_return,
        # resume_returned_order). Powers the staff queue's "time in stage"
        # column (see staff_dashboard.html) - the real bottleneck signal
        # updated_at alone can't give you, since that also bumps on a
        # message reply or a segment edit that never changed the status at
        # all. Backfilled to created_at for existing rows - the honest
        # "we don't actually know when it last changed, so treat it as
        # since creation" default, not a fabricated timestamp.
        conn.execute("ALTER TABLE orders ADD COLUMN status_changed_at REAL")
        conn.execute("UPDATE orders SET status_changed_at = created_at WHERE status_changed_at IS NULL")
    if "sla_credit_issued_at" not in existing_order_cols:
        # Dedupe flag for billing.ENTERPRISE_SLA_CREDIT_PCT (see
        # webapp/app.py's _deadline_watch_once) - same pattern as
        # deadline_missed_alert_sent_at, just for "already credited",
        # not "already alerted staff" - the two are independent so an
        # order can't get double-credited on a later sweep.
        conn.execute("ALTER TABLE orders ADD COLUMN sla_credit_issued_at REAL")
    if "wants_human_voice_over" not in existing_order_cols:
        # Client-selected addon (billing.ADDONS['human_voice_over']) - the
        # AI dub still gets made regardless (nothing about the pipeline
        # changes), this just marks that staff should also cast and
        # deliver a real human voice-over for it. See voice_actor_id below
        # for who's actually doing it.
        conn.execute("ALTER TABLE orders ADD COLUMN wants_human_voice_over INTEGER NOT NULL DEFAULT 0")
    if "voice_actor_id" not in existing_order_cols:
        conn.execute("ALTER TABLE orders ADD COLUMN voice_actor_id TEXT")
    existing_actor_cols = {row["name"] for row in conn.execute("PRAGMA table_info(voice_actors)")}
    if existing_actor_cols and "user_id" not in existing_actor_cols:
        # existing_actor_cols is empty only on a genuinely fresh DB where
        # the CREATE TABLE above (which already includes user_id) just
        # ran for the first time - PRAGMA table_info on a table that
        # doesn't exist yet returns no rows, not an error, so this guard
        # skips the redundant ALTER rather than running it against a
        # table that already has the column.
        conn.execute("ALTER TABLE voice_actors ADD COLUMN user_id TEXT")
    if "orders_status_idx" not in {row["name"] for row in conn.execute("PRAGMA index_list(orders)")}:
        # The staff queue's filter tabs, list_orders_needing_ops_triage,
        # list_orders_needing_deadline_check, and failure_rate all filter
        # by status - a real, cheap win once order volume grows past what
        # a full table scan handles instantly.
        conn.execute("CREATE INDEX orders_status_idx ON orders(status)")
    existing_sub_cols = {row["name"] for row in conn.execute("PRAGMA table_info(subscriptions)")}
    if "wallet_minutes" not in existing_sub_cols:
        # A prepaid balance, bought in bulk via the SAME Paystack/M-Pesa/bank
        # rails as everything else (see billing.WALLET_PACKAGES) - never
        # expires, unlike the monthly free allowance. Applied in
        # order_cost_usd's waterfall right after free minutes and before
        # anything gets billed at the per-minute rate.
        conn.execute("ALTER TABLE subscriptions ADD COLUMN wallet_minutes REAL NOT NULL DEFAULT 0")
    if "wallet_credits" not in existing_sub_cols:
        # Replaces wallet_minutes (kept, unused, rather than dropped -
        # SQLite column drops are a heavier migration than this needs) -
        # see billing.py's CREDITS_PER_DOLLAR comment for why raw minutes
        # was the wrong unit to track a prepaid balance in. One-time
        # migration below converts any real existing balance to its true
        # dollar-equivalent credits (it was purchased at the dub rate, see
        # the old WALLET_PACKAGES) rather than just zeroing it out - 15 =
        # dub's $1.50/min * CREDITS_PER_DOLLAR's 10, hardcoded rather than
        # imported from billing.py to keep this file a pure data layer.
        conn.execute("ALTER TABLE subscriptions ADD COLUMN wallet_credits REAL NOT NULL DEFAULT 0")
        conn.execute("UPDATE subscriptions SET wallet_credits = wallet_minutes * 15 WHERE wallet_minutes > 0")
    if "wallet_low_alert_sent_at" not in existing_sub_cols:
        # Dedupes the low-balance email (see app.py's create_order) so it
        # fires once when the balance actually crosses the threshold, not
        # again on every order after - add_wallet_credits clears this on
        # any real top-up, so the next time it drops low again, it alerts
        # again.
        conn.execute("ALTER TABLE subscriptions ADD COLUMN wallet_low_alert_sent_at REAL")
    existing_lead_cols = {row["name"] for row in conn.execute("PRAGMA table_info(leads)")}
    if "source" not in existing_lead_cols:
        # CRM fields - the leads table originally only tracked the website
        # callback form. source lets staff log a lead from anywhere (an
        # Instagram DM, a WhatsApp inquiry, a referral) even without a real
        # API integration for that platform; converted_user_id is set
        # automatically the moment a matching email actually signs up.
        conn.execute("ALTER TABLE leads ADD COLUMN source TEXT NOT NULL DEFAULT 'website'")
        conn.execute("ALTER TABLE leads ADD COLUMN assigned_to TEXT")
        conn.execute("ALTER TABLE leads ADD COLUMN converted_user_id TEXT")
    if "volume_estimate" not in existing_lead_cols:
        # Lead-qualification fields from the callback form - staff-side
        # triage signal, not an automated reject/route decision (see
        # is_personal_email_domain's docstring for why the email-domain
        # check flags rather than blocks).
        conn.execute("ALTER TABLE leads ADD COLUMN volume_estimate TEXT")
        conn.execute("ALTER TABLE leads ADD COLUMN org_type TEXT")
        conn.execute("ALTER TABLE leads ADD COLUMN personal_email_flag INTEGER NOT NULL DEFAULT 0")
    # Backfill: users created before the subscriptions table existed (yours
    # included) never went through get_or_create_user's INSERT path, so
    # they'd otherwise have no subscription row at all.
    conn.execute(
        """INSERT INTO subscriptions (user_id, plan, status, period_started_at)
           SELECT id, 'free', 'active', ? FROM users
           WHERE id NOT IN (SELECT user_id FROM subscriptions)""",
        (time.time(),),
    )
    conn.commit()
    conn.close()


def log_upload_audit(user_id: str | None, order_id: str | None, audit: dict,
                      ip_address: str | None, user_agent: str | None) -> None:
    """One row per upload attempt, accepted or rejected - see
    webapp/upload_security.py's validate_media_upload for what `audit`
    contains. Never raises on a bad/missing key - a logging bug should
    never be the thing that breaks a real upload."""
    conn = get_conn()
    conn.execute(
        """INSERT INTO upload_audit_log
           (id, user_id, order_id, original_filename, stored_filename, sha256, size_bytes,
            magic_sniff, ffprobe_ok, clamav_clean, clamav_detail, metadata_stripped,
            content_safety_flagged, content_safety_detail,
            rejected, reject_reason, ip_address, user_agent, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            uuid.uuid4().hex, user_id, order_id,
            audit.get("original_filename"), audit.get("stored_filename"), audit.get("sha256"),
            audit.get("size_bytes"), audit.get("magic_sniff"),
            audit.get("ffprobe_ok"), audit.get("clamav_clean"), audit.get("clamav_detail"),
            audit.get("metadata_stripped"),
            audit.get("content_safety_flagged"), audit.get("content_safety_detail"),
            int(bool(audit.get("rejected"))), audit.get("reject_reason"),
            ip_address, user_agent, time.time(),
        ),
    )
    conn.commit()
    conn.close()


def get_user_by_email(email: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    conn.close()
    return row


def get_user(user_id: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return row


# --------------------------------------------------------- team accounts ----
def get_team_owner_for_member(user_id: str) -> str | None:
    """The real client account whose orders/billing this user should see,
    if they're an ACCEPTED team member of someone else's account - None
    if they're not on anyone's team (the overwhelmingly common case,
    checked on every client-portal request via app.py's current_user, so
    this stays a single cheap indexed lookup, never a join across every
    route that needs it)."""
    conn = get_conn()
    row = conn.execute(
        "SELECT owner_client_id FROM team_members WHERE member_user_id = ? AND status = 'accepted' "
        "ORDER BY accepted_at DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    conn.close()
    return row["owner_client_id"] if row else None


def create_team_invite(owner_client_id: str, invited_email: str) -> str:
    """Real client-initiated invite (Settings page) - no account required
    yet for the invitee; accept_team_invite links it up the moment they
    actually sign up or log in with this email."""
    invite_id = uuid.uuid4().hex[:12]
    conn = get_conn()
    conn.execute(
        "INSERT INTO team_members (id, owner_client_id, member_user_id, invited_email, status, invited_at) "
        "VALUES (?, ?, NULL, ?, 'pending', ?)",
        (invite_id, owner_client_id, invited_email.strip().lower(), time.time()),
    )
    conn.commit()
    conn.close()
    return invite_id


def accept_team_invite(user_id: str, email: str) -> bool:
    """Called right after a real Supabase login/signup (same spot
    get_or_create_user's CRM-conversion check runs) - links this user's
    real account to any pending invite sent to their email, so they get
    access the moment they actually have a Kauli login, not before.
    Returns True if a real invite was accepted."""
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM team_members WHERE invited_email = ? AND status = 'pending' "
        "ORDER BY invited_at DESC LIMIT 1",
        (email.strip().lower(),),
    ).fetchone()
    if not row:
        conn.close()
        return False
    conn.execute(
        "UPDATE team_members SET member_user_id = ?, status = 'accepted', accepted_at = ? WHERE id = ?",
        (user_id, time.time(), row["id"]),
    )
    conn.commit()
    conn.close()
    return True


def list_team_members(owner_client_id: str):
    """Everyone on this account's team, pending or accepted, newest
    first - the real roster shown on Settings. 'removed' rows are
    excluded, not just hidden, so a removed-then-reinvited email doesn't
    show two confusing rows for what the owner sees as one relationship."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT team_members.*, users.email AS member_email, users.display_name AS member_name "
        "FROM team_members LEFT JOIN users ON team_members.member_user_id = users.id "
        "WHERE owner_client_id = ? AND status != 'removed' ORDER BY invited_at DESC",
        (owner_client_id,),
    ).fetchall()
    conn.close()
    return rows


# --------------------------------------------------- staff: client roster ----
def list_clients_for_staff(search: str | None = None):
    """Every real, top-level client account (never a teammate - those live
    under their owner's own Team tab, same scope rule client_scope_id
    already uses everywhere else) with real aggregate stats, for
    /staff/clients. Search matches display_name or email, case-insensitive,
    substring - deliberately simple, this is a handful of real accounts,
    not thousands."""
    conn = get_conn()
    query = """
        SELECT users.*,
               COUNT(DISTINCT orders.id) AS order_count,
               COALESCE(SUM(CASE WHEN payments.status = 'completed' THEN payments.amount_usd END), 0) AS total_spent_usd,
               MAX(orders.created_at) AS last_order_at
        FROM users
        LEFT JOIN orders ON orders.client_id = users.id
        LEFT JOIN payments ON payments.user_id = users.id
        WHERE users.role = 'client'
          AND users.id NOT IN (
              SELECT member_user_id FROM team_members
              WHERE status = 'accepted' AND member_user_id IS NOT NULL
          )
    """
    params: list = []
    if search:
        query += " AND (users.display_name LIKE ? OR users.email LIKE ?)"
        like = f"%{search.strip()}%"
        params += [like, like]
    query += " GROUP BY users.id ORDER BY users.created_at DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows


def set_client_staff_notes(client_id: str, notes: str) -> None:
    conn = get_conn()
    conn.execute("UPDATE users SET staff_notes = ? WHERE id = ?", (notes.strip() or None, client_id))
    conn.commit()
    conn.close()


def append_client_staff_note(client_id: str, note: str) -> None:
    """One dated, auto-generated line added to the TOP of a client's
    staff notes, never replacing whatever a human already typed there -
    see set_client_staff_notes for the manual-edit path (the textarea on
    /staff/clients/{id}?tab=settings) this must never clobber. Used for
    real automatic events worth a staff member seeing next time they look
    at this account (see app.py's _note_payment_failure), not a full
    activity log - keep call sites to genuinely note-worthy real events."""
    conn = get_conn()
    row = conn.execute("SELECT staff_notes FROM users WHERE id = ?", (client_id,)).fetchone()
    existing = (row["staff_notes"] or "").strip() if row else ""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    line = f"[{stamp}] {note}"
    combined = f"{line}\n{existing}" if existing else line
    conn.execute("UPDATE users SET staff_notes = ? WHERE id = ?", (combined, client_id))
    conn.commit()
    conn.close()


def count_recent_failed_payments(user_id: str, hours: float = 48) -> int:
    conn = get_conn()
    cutoff = time.time() - hours * 3600
    n = conn.execute(
        "SELECT COUNT(*) FROM payments WHERE user_id = ? AND status = 'failed' AND created_at >= ?",
        (user_id, cutoff),
    ).fetchone()[0]
    conn.close()
    return n


def set_signup_ip(user_id: str, ip: str) -> None:
    conn = get_conn()
    conn.execute("UPDATE users SET signup_ip = ? WHERE id = ?", (ip, user_id))
    conn.commit()
    conn.close()


def set_signup_ip_datacenter_flag(user_id: str, is_datacenter: bool) -> None:
    """Set once, from the background check ip_intel.is_datacenter_ip runs
    right after signup - see app.py's callers. Never called with an
    unknown (None) result; that's just left as the column's real NULL
    default, not written as False."""
    conn = get_conn()
    conn.execute("UPDATE users SET signup_ip_is_datacenter = ? WHERE id = ?", (int(is_datacenter), user_id))
    conn.commit()
    conn.close()


def set_tour_seen(user_id: str) -> None:
    """Set the first time the client dashboard tour finishes OR is
    skipped - either way it's "don't show this again", not just
    "completed". Never cleared."""
    conn = get_conn()
    conn.execute(
        "UPDATE users SET onboarding_tour_seen_at = ? WHERE id = ? AND onboarding_tour_seen_at IS NULL",
        (time.time(), user_id),
    )
    conn.commit()
    conn.close()


def set_trial_verified(user_id: str) -> None:
    """Only ever set once, never cleared - see the trial_verified_at
    migration's own comment. Idempotent on purpose: a duplicate webhook
    delivery for the same completed payment just sets the same value
    again, not an error."""
    conn = get_conn()
    conn.execute(
        "UPDATE users SET trial_verified_at = ? WHERE id = ? AND trial_verified_at IS NULL",
        (time.time(), user_id),
    )
    conn.commit()
    conn.close()


def reopen_account(user_id: str) -> None:
    """The staff-side counterpart to close_account - there was previously
    no way back in once a client closed their own account, even if they
    emailed asking to be reopened."""
    conn = get_conn()
    conn.execute("UPDATE users SET account_status = 'active' WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()


def remove_team_member(owner_client_id: str, team_member_row_id: str) -> None:
    """Scoped to owner_client_id in the WHERE clause itself, not just
    checked by the caller first - a removal request can never touch a
    row belonging to a different account no matter what id someone
    passes in."""
    conn = get_conn()
    conn.execute(
        "UPDATE team_members SET status = 'removed' WHERE id = ? AND owner_client_id = ?",
        (team_member_row_id, owner_client_id),
    )
    conn.commit()
    conn.close()


def get_or_create_user(user_id: str, email: str, default_role: str, display_name: str | None = None,
                        marketing_consent: bool = False, consent_ip: str | None = None):
    """Called right after a real Supabase login. First time we see this
    Supabase user id, provision a local row for them (role decided by the
    KAULI_STAFF_EMAILS allowlist - see webapp/supabase_auth.py) plus a
    default 'free' subscription row (see webapp/billing.py). Every later
    login just reuses the existing rows.

    Also the real CRM "conversion" moment: if this email matches an
    open lead, that lead is the thing that actually turned into a
    customer - mark it 'won' and link it, right here, rather than relying
    on staff to notice and update it by hand.

    Returns (row, was_new) - was_new is True only the instant this account
    is first provisioned, which is what app.py's login()/signup() use to
    decide whether to queue the onboarding welcome message. Login (an
    existing Supabase account with no local row yet, e.g. after a DB reset)
    can hit the was_new=True path too, same as signup - either way it's a
    genuinely first-time local account, which is the thing that actually
    matters here."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    was_new = row is None
    if row is None:
        now = time.time()
        conn.execute(
            "INSERT INTO users (id, email, role, display_name, created_at, marketing_consent, "
            "marketing_consent_at, marketing_consent_ip) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, email, default_role, display_name or email.split("@")[0], now,
             int(marketing_consent), now if marketing_consent else None,
             consent_ip if marketing_consent else None),
        )
        conn.execute(
            "INSERT INTO subscriptions (user_id, plan, status, period_started_at) "
            "VALUES (?, 'free', 'active', ?)",
            (user_id, now),
        )
        matching_leads = conn.execute(
            "SELECT id FROM leads WHERE lower(email) = ? AND status NOT IN ('won', 'lost')",
            (email.strip().lower(),),
        ).fetchall()
        for lead in matching_leads:
            conn.execute(
                "UPDATE leads SET status = 'won', converted_user_id = ? WHERE id = ?",
                (user_id, lead["id"]),
            )
            conn.execute(
                "INSERT INTO lead_notes (id, lead_id, author_id, body, created_at) VALUES (?, ?, NULL, ?, ?)",
                (uuid.uuid4().hex[:12], lead["id"], "Automatically marked won - this email signed up for an account.", now),
            )
        # No tracked lead ever matched this email - most real signups won't
        # have come through a tracked lead-gen channel at all, and staff
        # need a full, real client list in the CRM, not just the subset
        # that happened to arrive via a form. Only for client accounts -
        # a staff account isn't a CRM contact. source='signup' keeps this
        # visibly distinct from an actual marketing-sourced lead in the
        # "by source" breakdown, rather than silently inflating some
        # other channel's numbers.
        if not matching_leads and default_role == "client":
            new_lead_id = uuid.uuid4().hex[:12]
            conn.execute(
                "INSERT INTO leads (id, name, email, status, source, converted_user_id, created_at) "
                "VALUES (?, ?, ?, 'won', 'signup', ?, ?)",
                (new_lead_id, display_name or email.split("@")[0], email, user_id, now),
            )
            conn.execute(
                "INSERT INTO lead_notes (id, lead_id, author_id, body, created_at) VALUES (?, ?, NULL, ?, ?)",
                (uuid.uuid4().hex[:12], new_lead_id,
                 "Auto-added - this account signed up directly, no tracked lead existed for this email.", now),
            )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return row, was_new


def set_marketing_consent(user_id: str, consent: bool, ip: str | None) -> None:
    """The "Preference Center" action - opting in or out later, from
    Settings. Every real change gets its own fresh timestamp/IP (the audit
    trail is "when was consent last actually given or withdrawn", not just
    a static value from signup that might be years stale)."""
    conn = get_conn()
    conn.execute(
        "UPDATE users SET marketing_consent = ?, marketing_consent_at = ?, marketing_consent_ip = ? WHERE id = ?",
        (int(consent), time.time(), ip, user_id),
    )
    conn.commit()
    conn.close()


def set_order_defaults(user_id: str, source_lang: str | None, target_lang: str | None,
                        service_level: str | None, rush: bool, addon_video: bool) -> None:
    """Only ever prefills app.py's client_dashboard wizard - see
    default_source_lang's schema comment above for why this is per-user,
    not account-wide."""
    conn = get_conn()
    conn.execute(
        """UPDATE users SET default_source_lang = ?, default_target_lang = ?, default_service_level = ?,
           default_rush = ?, default_addon_video = ? WHERE id = ?""",
        (source_lang, target_lang, service_level, int(rush), int(addon_video), user_id),
    )
    conn.commit()
    conn.close()


def update_profile(user_id: str, display_name: str, avatar_path: str | None = None) -> None:
    """avatar_path=None leaves the existing photo alone (this is 'update
    display name, optionally also replace the photo', not a full overwrite -
    a plain profile-name edit shouldn't silently clear an uploaded photo)."""
    conn = get_conn()
    if avatar_path is not None:
        conn.execute("UPDATE users SET display_name = ?, avatar_path = ? WHERE id = ?",
                     (display_name, avatar_path, user_id))
    else:
        conn.execute("UPDATE users SET display_name = ? WHERE id = ?", (display_name, user_id))
    conn.commit()
    conn.close()


def set_user_admin(user_id: str, is_admin: bool) -> None:
    conn = get_conn()
    conn.execute("UPDATE users SET is_admin = ? WHERE id = ?", (int(is_admin), user_id))
    conn.commit()
    conn.close()


def close_account(user_id: str) -> None:
    """Blocks future login (see app.py's current_user) without touching
    order/payment history - those stay exactly as they are unless the
    client separately requests deletion (see create_deletion_request)."""
    conn = get_conn()
    conn.execute("UPDATE users SET account_status = 'closed' WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()


def create_deletion_request(user_id: str) -> str:
    """Never auto-executed - see data_deletion_requests' own comment in
    SCHEMA. Idempotent-ish: doesn't stop someone from filing a second
    request, but list_deletion_requests only surfaces 'pending' ones so a
    resolved one doesn't clutter the staff queue."""
    request_id = uuid.uuid4().hex[:12]
    conn = get_conn()
    conn.execute(
        "INSERT INTO data_deletion_requests (id, user_id, requested_at, status) VALUES (?, ?, ?, 'pending')",
        (request_id, user_id, time.time()),
    )
    conn.commit()
    conn.close()
    return request_id


def has_pending_deletion_request(user_id: str) -> bool:
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM data_deletion_requests WHERE user_id = ? AND status = 'pending' LIMIT 1", (user_id,)
    ).fetchone()
    conn.close()
    return row is not None


def list_deletion_requests(status: str | None = "pending"):
    conn = get_conn()
    query = ("SELECT data_deletion_requests.*, users.email AS user_email, users.display_name AS user_display_name "
              "FROM data_deletion_requests JOIN users ON users.id = data_deletion_requests.user_id WHERE 1=1")
    params: list = []
    if status:
        query += " AND data_deletion_requests.status = ?"
        params.append(status)
    query += " ORDER BY data_deletion_requests.requested_at ASC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows


def resolve_deletion_request(request_id: str, resolved_by: str, status: str = "done", notes: str | None = None) -> None:
    assert status in ("done", "declined")
    conn = get_conn()
    conn.execute(
        "UPDATE data_deletion_requests SET status = ?, resolved_at = ?, resolved_by = ?, notes = ? WHERE id = ?",
        (status, time.time(), resolved_by, notes, request_id),
    )
    conn.commit()
    conn.close()


# ------------------------------------------------------------------ blog ----
# Small, fixed set matching what's actually been written - not a full
# taxonomy speculatively built ahead of having enough posts to need one.
BLOG_CATEGORIES = ("Accessibility & Compliance", "Localization", "Process & Quality", "Trust & Safety")


def create_blog_post(slug: str, title: str, description: str, body_html: str,
                      author_id: str, status: str = "draft", category: str | None = None) -> str:
    assert status in ("draft", "published")
    post_id = uuid.uuid4().hex[:12]
    now = time.time()
    conn = get_conn()
    conn.execute(
        """INSERT INTO blog_posts (id, slug, title, description, body_html, author_id, status,
           created_at, updated_at, published_at, category)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (post_id, slug, title, description, body_html, author_id, status, now, now,
         now if status == "published" else None, category),
    )
    conn.commit()
    conn.close()
    return post_id


def update_blog_post(post_id: str, slug: str, title: str, description: str, body_html: str,
                      category: str | None = None) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE blog_posts SET slug = ?, title = ?, description = ?, body_html = ?, category = ?, "
        "updated_at = ? WHERE id = ?",
        (slug, title, description, body_html, category, time.time(), post_id),
    )
    conn.commit()
    conn.close()


def set_blog_post_status(post_id: str, status: str) -> None:
    assert status in ("draft", "published")
    conn = get_conn()
    post = conn.execute("SELECT published_at FROM blog_posts WHERE id = ?", (post_id,)).fetchone()
    # published_at is set the FIRST time a post goes live, never reset by a
    # later unpublish/republish cycle - it's "when this first went public",
    # not "when it's currently visible", which matters for anything citing
    # a real publish date (the JSON-LD Article schema, an RSS reader, etc.).
    published_at = post["published_at"] if post and post["published_at"] else time.time()
    conn.execute(
        "UPDATE blog_posts SET status = ?, published_at = ?, updated_at = ? WHERE id = ?",
        (status, published_at if status == "published" else (post["published_at"] if post else None),
         time.time(), post_id),
    )
    conn.commit()
    conn.close()


def set_blog_post_medium_url(post_id: str, medium_url: str) -> None:
    conn = get_conn()
    conn.execute("UPDATE blog_posts SET medium_url = ? WHERE id = ?", (medium_url, post_id))
    conn.commit()
    conn.close()


def set_blog_post_devto_url(post_id: str, devto_url: str) -> None:
    conn = get_conn()
    conn.execute("UPDATE blog_posts SET devto_url = ? WHERE id = ?", (devto_url, post_id))
    conn.commit()
    conn.close()


def delete_blog_post(post_id: str) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM blog_posts WHERE id = ?", (post_id,))
    conn.commit()
    conn.close()


def get_blog_post(post_id: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM blog_posts WHERE id = ?", (post_id,)).fetchone()
    conn.close()
    return row


def get_blog_post_by_slug(slug: str, published_only: bool = True):
    conn = get_conn()
    query = "SELECT * FROM blog_posts WHERE slug = ?"
    params: list = [slug]
    if published_only:
        query += " AND status = 'published'"
    row = conn.execute(query, params).fetchone()
    conn.close()
    return row


def increment_blog_post_views(post_id: str) -> None:
    conn = get_conn()
    conn.execute("UPDATE blog_posts SET views = views + 1 WHERE id = ?", (post_id,))
    conn.commit()
    conn.close()


def list_blog_posts(published_only: bool = True):
    conn = get_conn()
    query = "SELECT * FROM blog_posts"
    if published_only:
        query += " WHERE status = 'published' ORDER BY published_at DESC"
    else:
        query += " ORDER BY created_at DESC"
    rows = conn.execute(query).fetchall()
    conn.close()
    return rows


def slug_exists(slug: str, exclude_post_id: str | None = None) -> bool:
    conn = get_conn()
    if exclude_post_id:
        row = conn.execute("SELECT 1 FROM blog_posts WHERE slug = ? AND id != ?", (slug, exclude_post_id)).fetchone()
    else:
        row = conn.execute("SELECT 1 FROM blog_posts WHERE slug = ?", (slug,)).fetchone()
    conn.close()
    return row is not None


# ------------------------------------------------------------- staff mgmt ----
def is_invited_staff(email: str) -> bool:
    conn = get_conn()
    row = conn.execute("SELECT 1 FROM staff_invites WHERE email = ?", (email.strip().lower(),)).fetchone()
    conn.close()
    return row is not None


def add_staff_invite(email: str, invited_by: str) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO staff_invites (email, invited_by, invited_at) VALUES (?, ?, ?)",
        (email.strip().lower(), invited_by, time.time()),
    )
    conn.commit()
    conn.close()


def remove_staff_invite(email: str) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM staff_invites WHERE email = ?", (email.strip().lower(),))
    conn.commit()
    conn.close()


def list_staff_invites():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM staff_invites ORDER BY invited_at DESC").fetchall()
    conn.close()
    return rows


def list_staff_users():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM users WHERE role = 'staff' ORDER BY is_admin DESC, display_name ASC").fetchall()
    conn.close()
    return rows


def staff_search(query: str, limit: int = 8):
    """Real cross-entity search - orders (by id, filename, or client name/
    email), clients (by name/email), leads (by name/email). Plain SQL LIKE,
    not a search index - fine at this order volume (see the "one staff
    role for now" note elsewhere), and it's a real query against real
    data, not a fabricated typeahead over static text."""
    q = f"%{query.strip()}%"
    conn = get_conn()
    orders = conn.execute(
        """SELECT orders.id, orders.original_filename, orders.status, users.display_name AS client_name
           FROM orders JOIN users ON orders.client_id = users.id
           WHERE orders.id LIKE ? OR orders.original_filename LIKE ?
              OR users.display_name LIKE ? OR users.email LIKE ?
           ORDER BY orders.created_at DESC LIMIT ?""",
        (q, q, q, q, limit),
    ).fetchall()
    clients = conn.execute(
        """SELECT id, display_name, email FROM users
           WHERE role = 'client' AND (display_name LIKE ? OR email LIKE ?)
           ORDER BY display_name ASC LIMIT ?""",
        (q, q, limit),
    ).fetchall()
    leads = conn.execute(
        """SELECT id, name, email, status FROM leads
           WHERE name LIKE ? OR email LIKE ? ORDER BY created_at DESC LIMIT ?""",
        (q, q, limit),
    ).fetchall()
    conn.close()
    return {"orders": orders, "clients": clients, "leads": leads}


def create_notification(recipient_id: str, kind: str, title: str, link: str | None = None) -> None:
    """Single-recipient version of notify_all_staff - the client-facing
    half of the same bell/notifications table (base.html renders it for
    every logged-in role, staff and client alike)."""
    conn = get_conn()
    conn.execute(
        "INSERT INTO notifications (id, recipient_id, kind, title, link, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (uuid.uuid4().hex, recipient_id, kind, title, link, time.time()),
    )
    conn.commit()
    conn.close()


def notify_all_staff(kind: str, title: str, link: str | None = None) -> None:
    """Real in-app notification for every current staff account - the bell's
    actual data source. Fired from the same real events notifications.py's
    notify_staff*/notify_staff_needs_review email helpers already fire
    from, not a new trigger of its own - see those call sites."""
    conn = get_conn()
    now = time.time()
    for staff in list_staff_users():
        conn.execute(
            "INSERT INTO notifications (id, recipient_id, kind, title, link, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, staff["id"], kind, title, link, now),
        )
    conn.commit()
    conn.close()


def list_recent_notifications(user_id: str, limit: int = 10):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM notifications WHERE recipient_id = ? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    conn.close()
    return rows


def count_unread_notifications(user_id: str) -> int:
    conn = get_conn()
    n = conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE recipient_id = ? AND read_at IS NULL", (user_id,)
    ).fetchone()[0]
    conn.close()
    return n


def mark_notification_read(notification_id: str, user_id: str) -> None:
    """user_id scopes this to the recipient's OWN notification - without it
    a guessed/enumerated id could mark (or reveal the existence of)
    someone else's notification as read."""
    conn = get_conn()
    conn.execute(
        "UPDATE notifications SET read_at = ? WHERE id = ? AND recipient_id = ? AND read_at IS NULL",
        (time.time(), notification_id, user_id),
    )
    conn.commit()
    conn.close()


def mark_all_notifications_read(user_id: str) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE notifications SET read_at = ? WHERE recipient_id = ? AND read_at IS NULL",
        (time.time(), user_id),
    )
    conn.commit()
    conn.close()


# --------------------------------------------------- API keys / webhooks ----
def set_client_api_key(client_id: str, key_hash: str, key_prefix: str) -> None:
    """Generating a new key always replaces any old one for this account -
    there's only ever one live key per client, so "regenerate" doubles as
    "revoke the old one", same as most API-key UIs (Stripe, GitHub tokens)."""
    conn = get_conn()
    now = time.time()
    conn.execute(
        """INSERT INTO client_api_keys (client_id, key_hash, key_prefix, created_at, last_used_at)
           VALUES (?, ?, ?, ?, NULL)
           ON CONFLICT(client_id) DO UPDATE SET
             key_hash = excluded.key_hash, key_prefix = excluded.key_prefix,
             created_at = excluded.created_at, last_used_at = NULL""",
        (client_id, key_hash, key_prefix, now),
    )
    conn.commit()
    conn.close()


def get_client_api_key(client_id: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM client_api_keys WHERE client_id = ?", (client_id,)).fetchone()
    conn.close()
    return row


def revoke_client_api_key(client_id: str) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM client_api_keys WHERE client_id = ?", (client_id,))
    conn.commit()
    conn.close()


def find_client_by_api_key_hash(key_hash: str) -> str | None:
    conn = get_conn()
    row = conn.execute("SELECT client_id FROM client_api_keys WHERE key_hash = ?", (key_hash,)).fetchone()
    conn.close()
    return row["client_id"] if row else None


def touch_api_key_last_used(client_id: str) -> None:
    conn = get_conn()
    conn.execute("UPDATE client_api_keys SET last_used_at = ? WHERE client_id = ?", (time.time(), client_id))
    conn.commit()
    conn.close()


def set_client_webhook(client_id: str, url: str, secret: str | None = None) -> str:
    """Sets/updates the URL. secret is only ever generated once - passing
    None (the normal case, from the settings form which only collects a
    URL) keeps whatever secret already exists so the client's own
    signature-verification code doesn't silently break; only a fresh
    generate_secret action passes a real new one. Returns the secret that
    ended up in effect, since the caller may not have had one to begin
    with."""
    conn = get_conn()
    now = time.time()
    existing = conn.execute("SELECT secret FROM client_webhooks WHERE client_id = ?", (client_id,)).fetchone()
    effective_secret = secret or (existing["secret"] if existing else None) or uuid.uuid4().hex
    conn.execute(
        """INSERT INTO client_webhooks (client_id, url, secret, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(client_id) DO UPDATE SET
             url = excluded.url, secret = excluded.secret, updated_at = excluded.updated_at""",
        (client_id, url, effective_secret, now, now),
    )
    conn.commit()
    conn.close()
    return effective_secret


def get_client_webhook(client_id: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM client_webhooks WHERE client_id = ?", (client_id,)).fetchone()
    conn.close()
    return row


def delete_client_webhook(client_id: str) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM client_webhooks WHERE client_id = ?", (client_id,))
    conn.commit()
    conn.close()


def log_webhook_delivery(client_id: str, order_id: str, event: str, ok: bool,
                          status_code: int | None, error: str | None = None) -> None:
    conn = get_conn()
    conn.execute(
        """INSERT INTO webhook_deliveries (id, client_id, order_id, event, sent_at, ok, status_code, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (uuid.uuid4().hex, client_id, order_id, event, time.time(), 1 if ok else 0, status_code, error),
    )
    conn.commit()
    conn.close()


def list_webhook_deliveries(client_id: str, limit: int = 10):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM webhook_deliveries WHERE client_id = ? ORDER BY sent_at DESC LIMIT ?",
        (client_id, limit),
    ).fetchall()
    conn.close()
    return rows


def promote_to_staff(user_id: str) -> bool:
    """Flips an existing client account straight to staff - for someone
    who already has a Kauli account and needs staff access, no separate
    invite-then-signup round trip needed. Returns False if the account
    doesn't exist or is already staff (nothing to do)."""
    conn = get_conn()
    row = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None or row["role"] == "staff":
        conn.close()
        return False
    conn.execute("UPDATE users SET role = 'staff' WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    return True


def demote_from_staff(user_id: str) -> None:
    conn = get_conn()
    conn.execute("UPDATE users SET role = 'client', is_admin = 0 WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()


def set_voice_clone_consent(order_id: str, ip_address: str | None) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE orders SET voice_clone_consent_given_at = ?, voice_clone_consent_ip = ? WHERE id = ?",
        (time.time(), ip_address, order_id),
    )
    conn.commit()
    conn.close()


def create_order(order_id: str, client_id: str, original_filename: str, audio_path: str,
                  source_lang: str, target_lang: str, tier: str,
                  asr: str, mt: str, tts: str, outdir: str,
                  source_youtube_id: str | None = None, idempotency_key: str | None = None,
                  folder_name: str | None = None, wants_human_voice_over: bool = False) -> str:
    # order_id is passed in (not generated here) so it matches the caller's
    # upload/output directory names and worker.submit_job() lookup - it used
    # to be generated independently here, which meant the DB row's id never
    # matched the directories on disk or what the worker looked up, so jobs
    # silently never started (stuck at "queued" forever).
    now = time.time()
    conn = get_conn()
    conn.execute(
        """INSERT INTO orders
           (id, client_id, original_filename, audio_path, source_lang, target_lang,
            tier, asr, mt, tts, outdir, status, error, created_at, updated_at, source_youtube_id,
            idempotency_key, folder_name, wants_human_voice_over)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', NULL, ?, ?, ?, ?, ?, ?)""",
        (order_id, client_id, original_filename, audio_path, source_lang, target_lang,
         tier, asr, mt, tts, outdir, now, now, source_youtube_id, idempotency_key,
         (folder_name or "").strip() or None, int(wants_human_voice_over)),
    )
    conn.commit()
    conn.close()
    return order_id


def list_folders_for_client(client_id: str) -> list[str]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT folder_name FROM orders WHERE client_id = ? AND folder_name IS NOT NULL ORDER BY folder_name",
        (client_id,),
    ).fetchall()
    conn.close()
    return [r["folder_name"] for r in rows]


def set_order_folder(order_id: str, client_id: str, folder_name: str | None) -> None:
    """Re-files an EXISTING order into a folder, or clears it - folder_name
    was previously only ever set once, at submission (see create_order's
    folder_name param). client_id is checked in the same UPDATE, not as a
    separate SELECT first - the caller passes the order_id from the
    client's OWN order list, but this is the real ownership guard, not
    just the query the route already ran."""
    conn = get_conn()
    conn.execute(
        "UPDATE orders SET folder_name = ? WHERE id = ? AND client_id = ?",
        ((folder_name or "").strip() or None, order_id, client_id),
    )
    conn.commit()
    conn.close()


def get_order_by_idempotency_key(client_id: str, idempotency_key: str):
    """A resubmit with the same key (slow connection, double-click, a
    retried request) returns the order that already exists instead of
    creating - and charging for - a second one."""
    if not idempotency_key:
        return None
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM orders WHERE client_id = ? AND idempotency_key = ?",
        (client_id, idempotency_key),
    ).fetchone()
    conn.close()
    return row


def set_order_ai_cost(order_id: str, ai_cost_usd: float) -> None:
    conn = get_conn()
    conn.execute("UPDATE orders SET ai_cost_usd = ?, updated_at = ? WHERE id = ?",
                 (ai_cost_usd, time.time(), order_id))
    conn.commit()
    conn.close()


def ai_spend_since(since_ts: float) -> float:
    """Real MT spend across every order updated since `since_ts` - "today"
    at the call site. The FinOps guard: if this jumps far past what your
    own order volume in the same window would explain, something (a bugged
    retry loop, a compromised account hammering submissions) is spending
    money faster than real client work justifies."""
    conn = get_conn()
    total = conn.execute(
        "SELECT COALESCE(SUM(ai_cost_usd), 0) FROM orders WHERE updated_at >= ?", (since_ts,)
    ).fetchone()[0]
    conn.close()
    return round(total, 4)


def update_order_status(order_id: str, status: str, error: str | None = None) -> None:
    now = time.time()
    conn = get_conn()
    conn.execute(
        "UPDATE orders SET status = ?, error = ?, updated_at = ?, status_changed_at = ? WHERE id = ?",
        (status, error, now, now, order_id),
    )
    conn.commit()
    conn.close()


def reset_retry_count(order_id: str) -> None:
    conn = get_conn()
    conn.execute("UPDATE orders SET retry_count = 0, updated_at = ? WHERE id = ?", (time.time(), order_id))
    conn.commit()
    conn.close()


def increment_retry_count(order_id: str) -> int:
    """Returns the new count - worker.py compares it against its retry
    budget to decide whether to try again or give up to 'dead_letter'."""
    conn = get_conn()
    conn.execute("UPDATE orders SET retry_count = retry_count + 1, updated_at = ? WHERE id = ?",
                 (time.time(), order_id))
    conn.commit()
    row = conn.execute("SELECT retry_count FROM orders WHERE id = ?", (order_id,)).fetchone()
    conn.close()
    return row["retry_count"] if row else 0


def propose_difficulty_surcharge(order_id: str, pct: float, usd: float, note: str | None,
                                  reason: str = "difficult_audio") -> None:
    conn = get_conn()
    conn.execute(
        """UPDATE orders SET difficulty_surcharge_status = 'pending_approval',
           difficulty_surcharge_pct = ?, difficulty_surcharge_usd = ?, difficulty_surcharge_note = ?,
           difficulty_surcharge_reason = ?, updated_at = ? WHERE id = ?""",
        (pct, usd, note, reason, time.time(), order_id),
    )
    conn.commit()
    conn.close()


def waive_difficulty_surcharge(order_id: str) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE orders SET difficulty_surcharge_status = 'waived', updated_at = ? WHERE id = ?",
        (time.time(), order_id),
    )
    conn.commit()
    conn.close()


def approve_difficulty_surcharge(order_id: str) -> None:
    """Called once the surcharge payment actually completes - folds the
    surcharge into cost_usd so billing history shows the true final
    charge, not just the original base quote."""
    conn = get_conn()
    order = conn.execute("SELECT cost_usd, difficulty_surcharge_usd FROM orders WHERE id = ?", (order_id,)).fetchone()
    if order:
        new_cost = (order["cost_usd"] or 0.0) + (order["difficulty_surcharge_usd"] or 0.0)
        conn.execute(
            "UPDATE orders SET difficulty_surcharge_status = 'approved', cost_usd = ?, updated_at = ? WHERE id = ?",
            (new_cost, time.time(), order_id),
        )
        conn.commit()
    conn.close()


def set_dub_voice(order_id: str, voice: str | None, job_status: str | None = None) -> None:
    """Record which voice actually rendered the current dub track - see the
    dub_voice/dub_voice_job_status migration note above."""
    conn = get_conn()
    conn.execute(
        "UPDATE orders SET dub_voice = ?, dub_voice_job_status = ?, updated_at = ? WHERE id = ?",
        (voice, job_status, time.time(), order_id),
    )
    conn.commit()
    conn.close()


def set_dub_voice_job_status(order_id: str, job_status: str | None) -> None:
    """Just the in-progress-clone flag, without touching which voice is
    currently live - used to flip 'running' on before a background XTTS
    clone starts and off (or to 'failed:...') when it finishes."""
    conn = get_conn()
    conn.execute(
        "UPDATE orders SET dub_voice_job_status = ?, updated_at = ? WHERE id = ?",
        (job_status, time.time(), order_id),
    )
    conn.commit()
    conn.close()


# Order statuses that are still "on the clock" for a deadline - not yet
# delivered, not already handed back to the client to act on. Shared by
# the staff queue's sort order and deadline_watch.py's alert sweep.
TAT_ACTIVE_STATUSES = ("queued", "processing", "awaiting_review", "editor_returned")


def set_order_deadlines(order_id: str, tat_start_at: float, internal_deadline_at: float,
                         deadline_at: float) -> None:
    """Set once, the moment processing actually starts (see tat.py) - a
    later plan change or retry never moves the goalposts on an order
    that's already running."""
    conn = get_conn()
    conn.execute(
        "UPDATE orders SET tat_start_at = ?, internal_deadline_at = ?, deadline_at = ? WHERE id = ?",
        (tat_start_at, internal_deadline_at, deadline_at, order_id),
    )
    conn.commit()
    conn.close()


def list_orders_needing_deadline_check():
    """Every still-active order with a deadline on file - what
    deadline_watch.py sweeps every 15 minutes to decide who needs a
    staff alert. Orders from before tat.py existed (deadline_at IS NULL)
    are simply skipped, not treated as overdue."""
    conn = get_conn()
    placeholders = ",".join("?" for _ in TAT_ACTIVE_STATUSES)
    rows = conn.execute(
        f"SELECT * FROM orders WHERE status IN ({placeholders}) AND deadline_at IS NOT NULL",
        TAT_ACTIVE_STATUSES,
    ).fetchall()
    conn.close()
    return rows


def mark_deadline_warning_sent(order_id: str) -> None:
    conn = get_conn()
    conn.execute("UPDATE orders SET deadline_warning_sent_at = ? WHERE id = ?", (time.time(), order_id))
    conn.commit()
    conn.close()


def mark_deadline_missed_alert_sent(order_id: str) -> None:
    conn = get_conn()
    conn.execute("UPDATE orders SET deadline_missed_alert_sent_at = ? WHERE id = ?", (time.time(), order_id))
    conn.commit()
    conn.close()


def mark_sla_credit_issued(order_id: str) -> None:
    conn = get_conn()
    conn.execute("UPDATE orders SET sla_credit_issued_at = ? WHERE id = ?", (time.time(), order_id))
    conn.commit()
    conn.close()


def set_order_billing(order_id: str, service_level: str, duration_minutes: float, cost_usd: float,
                       addons: list[str] | None = None, cost_breakdown: dict | None = None,
                       is_rush: bool = False) -> None:
    """Frozen at order-creation time (see billing.order_cost_usd) - a price
    change later must never retroactively change what an existing order
    owes or already paid. addons is stored as a small JSON list (e.g.
    '["video_deliverables"]') - paid upgrades this specific order bought
    above its plan's default features. cost_breakdown is the full
    order_cost_usd() return dict, stored as-is so a receipt issued later
    can show real per-service line items (see receipts.line_items_json)."""
    conn = get_conn()
    conn.execute(
        "UPDATE orders SET service_level = ?, duration_minutes = ?, cost_usd = ?, addons = ?, "
        "cost_breakdown_json = ?, is_rush = ? WHERE id = ?",
        (service_level, duration_minutes, cost_usd, json.dumps(addons or []),
         json.dumps(cost_breakdown) if cost_breakdown else None, int(is_rush), order_id),
    )
    conn.commit()
    conn.close()


def set_order_free_preview(order_id: str, is_free_preview: bool) -> None:
    conn = get_conn()
    conn.execute("UPDATE orders SET is_free_preview = ? WHERE id = ?", (int(is_free_preview), order_id))
    conn.commit()
    conn.close()


def set_order_content_safety_flag(order_id: str, flagged: bool, detail: str | None = None) -> None:
    conn = get_conn()
    conn.execute("UPDATE orders SET content_safety_flagged = ?, content_safety_detail = ? WHERE id = ?",
                 (int(flagged), detail, order_id))
    conn.commit()
    conn.close()


def order_has_addon(order, addon_key: str) -> bool:
    if not order or not order["addons"]:
        return False
    try:
        return addon_key in json.loads(order["addons"])
    except (ValueError, TypeError):
        return False


VERBATIM_LEVELS = ("verbatim", "verbatim_light", "clean_read")
CONTENT_HANDLING_OPTIONS = ("tag", "return")
EXISTING_SUBS_OPTIONS = ("ignore", "match_existing", "flag_discrepancy")


def set_job_instructions(order_id: str, speaker_ids: bool, verbatim_level: str, transcribe_lyrics: bool,
                          use_italics: bool, existing_subs: str, no_audio: str, wrong_language: str,
                          instrumental_only: str, notes: str | None,
                          style_guide_path: str | None, style_guide_filename: str | None) -> None:
    """Set once at order creation and never changed after - see the
    migration comment on why. Every *_level/*_only/no_audio/wrong_language
    field is validated against a fixed option set here, not trusted as
    whatever a form happened to submit."""
    assert verbatim_level in VERBATIM_LEVELS
    assert existing_subs in EXISTING_SUBS_OPTIONS
    assert no_audio in CONTENT_HANDLING_OPTIONS
    assert wrong_language in CONTENT_HANDLING_OPTIONS
    assert instrumental_only in CONTENT_HANDLING_OPTIONS
    conn = get_conn()
    conn.execute(
        """UPDATE orders SET
             instr_speaker_ids = ?, instr_verbatim_level = ?, instr_transcribe_lyrics = ?,
             instr_use_italics = ?, instr_existing_subs = ?, instr_no_audio = ?,
             instr_wrong_language = ?, instr_instrumental_only = ?, instr_notes = ?,
             style_guide_path = ?, style_guide_filename = ?
           WHERE id = ?""",
        (int(speaker_ids), verbatim_level, int(transcribe_lyrics), int(use_italics), existing_subs,
         no_audio, wrong_language, instrumental_only, notes, style_guide_path, style_guide_filename,
         order_id),
    )
    conn.commit()
    conn.close()


# --------------------------------------------------- editor return / ops ----
# An editor hits something the client's instructions say to escalate (no
# audio, wrong language, instrumental-only) or anything else that needs a
# human call - the order moves to 'editor_returned' and waits for staff to
# either contact the client or formally return the job.
RETURN_REASONS = ("no_audio", "wrong_language", "instrumental_only", "sound_effects_only", "other")

# What an "additional charge" on an already-paid order can actually be for -
# audio difficulty was the first case (see app.py's audio_difficulty_rate),
# generalized so staff can propose one for any real extra work a client's
# workflow needed, not just that one. Same propose -> client approves and
# pays (Paystack/M-Pesa/bank) -> delivery unlocks flow regardless of reason.
EXTRA_CHARGE_REASONS = {
    "difficult_audio": "Difficult audio (noise, overlapping speakers, accent)",
    "rush_processing": "Rush / expedited turnaround",
    "extra_revision": "Additional revision round beyond what was included",
    "additional_format": "Extra deliverable format not in the original order",
    "other": "Other (see note)",
}


def flag_order_for_return(order_id: str, reason: str, note: str | None) -> None:
    assert reason in RETURN_REASONS
    now = time.time()
    conn = get_conn()
    conn.execute(
        """UPDATE orders SET status = 'editor_returned', return_reason = ?, return_note = ?,
           updated_at = ?, status_changed_at = ? WHERE id = ?""",
        (reason, note, now, now, order_id),
    )
    conn.commit()
    conn.close()


def resume_returned_order(order_id: str, new_audio_path: str | None = None,
                           new_original_filename: str | None = None,
                           new_duration_minutes: float | None = None) -> None:
    """Puts a returned_to_client order back into real processing on the
    SAME order - no second payment, no brand-new order row. Most returns
    only need the client's reply on the message thread (nothing here
    needed changing); a replacement file is only passed when the return
    reason actually required one (e.g. no_audio) - see the
    /client/orders/{id}/resume and /staff/orders/{id}/resume routes."""
    now = time.time()
    conn = get_conn()
    if new_audio_path:
        conn.execute(
            """UPDATE orders SET audio_path = ?, original_filename = ?, duration_minutes = ?,
               status = 'queued', updated_at = ?, status_changed_at = ? WHERE id = ?""",
            (new_audio_path, new_original_filename, new_duration_minutes, now, now, order_id),
        )
    else:
        conn.execute(
            "UPDATE orders SET status = 'queued', updated_at = ?, status_changed_at = ? WHERE id = ?",
            (now, now, order_id),
        )
    conn.commit()
    conn.close()


def list_orders_needing_ops_triage():
    conn = get_conn()
    rows = conn.execute(
        """SELECT orders.*, users.display_name AS client_name FROM orders
           JOIN users ON orders.client_id = users.id
           WHERE orders.status = 'editor_returned'
           ORDER BY orders.updated_at ASC"""
    ).fetchall()
    conn.close()
    return rows


# --------------------------------------------------------- workflow steps ----
def get_workflow_steps_raw(order) -> dict:
    """The stored {step_key: bool} dict, empty if nothing's been checked
    off yet. See billing.workflow_steps_for_order for the full step list
    (including the "deliverables" step, which isn't stored here at all -
    it's computed live from files on disk)."""
    if not order or not order["workflow_steps"]:
        return {}
    try:
        return json.loads(order["workflow_steps"])
    except (ValueError, TypeError):
        return {}


def set_workflow_step(order_id: str, step_key: str, done: bool) -> None:
    order = get_order(order_id)
    steps = get_workflow_steps_raw(order)
    steps[step_key] = bool(done)
    conn = get_conn()
    conn.execute("UPDATE orders SET workflow_steps = ? WHERE id = ?", (json.dumps(steps), order_id))
    conn.commit()
    conn.close()


def get_order(order_id: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    conn.close()
    return row


def next_order_needing_review(exclude_id: str):
    """Oldest other order still sitting in an editor's queue - backs the
    editor's "Finish and load next" action so approving one job can hand
    the next straight back without a trip through the queue page."""
    conn = get_conn()
    row = conn.execute(
        """SELECT * FROM orders WHERE status = 'awaiting_review' AND id != ?
           ORDER BY created_at ASC LIMIT 1""",
        (exclude_id,),
    ).fetchone()
    conn.close()
    return row


def list_orders_for_client(client_id: str):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM orders WHERE client_id = ? ORDER BY created_at DESC", (client_id,)
    ).fetchall()
    conn.close()
    return rows


# Real order-lifecycle buckets a client actually cares about - matches the
# same grouping client_dashboard.html's filter chips use (see that
# template's IN_PROGRESS/READY/NEEDS_YOU), so the dashboard's summary
# numbers and the order-list filters never tell two different stories
# about what state an order is actually in.
CLIENT_STAT_IN_PROGRESS = ("queued", "processing", "editor_returned")
CLIENT_STAT_IN_REVIEW = ("awaiting_review",)
CLIENT_STAT_NEEDS_YOU = ("pending_payment", "returned_to_client")
CLIENT_STAT_COMPLETED = ("ready_for_delivery", "delivered")


def client_dashboard_stats(client_id: str) -> dict:
    """Real counts for the client dashboard's summary cards, plus
    all-time confirmed spend (payments.status = 'completed' only - a
    pending Paystack checkout or an unconfirmed bank transfer isn't
    money this client has actually paid yet, same rule the staff-side
    revenue report uses)."""
    conn = get_conn()

    def _count(statuses):
        placeholders = ",".join("?" for _ in statuses)
        return conn.execute(
            f"SELECT COUNT(*) FROM orders WHERE client_id = ? AND status IN ({placeholders})",
            (client_id, *statuses),
        ).fetchone()[0]

    stats = {
        "in_progress": _count(CLIENT_STAT_IN_PROGRESS),
        "in_review": _count(CLIENT_STAT_IN_REVIEW),
        "needs_you": _count(CLIENT_STAT_NEEDS_YOU),
        "completed": _count(CLIENT_STAT_COMPLETED),
        "total_spent_usd": conn.execute(
            "SELECT COALESCE(SUM(amount_usd), 0) FROM payments WHERE user_id = ? AND status = 'completed'",
            (client_id,),
        ).fetchone()[0],
    }
    conn.close()
    return stats


def list_all_orders():
    """Still-active orders with a real deadline on file sort soonest-due
    first - "today's deadline" at the top, never an unsorted queue (see
    webapp/tat.py). Everything else (already delivered, still awaiting
    payment, dead-lettered, or from before deadlines existed) falls back
    to newest-first underneath, same as before."""
    conn = get_conn()
    placeholders = ",".join("?" for _ in TAT_ACTIVE_STATUSES)
    rows = conn.execute(
        f"""SELECT orders.*, users.display_name AS client_name
           FROM orders JOIN users ON orders.client_id = users.id
           ORDER BY
             CASE WHEN orders.status IN ({placeholders}) AND orders.deadline_at IS NOT NULL THEN 0 ELSE 1 END,
             orders.deadline_at ASC,
             orders.created_at DESC""",
        TAT_ACTIVE_STATUSES,
    ).fetchall()
    conn.close()
    return rows


# ------------------------------------------------------------- messages ----
def create_message(order_id: str, sender_id: str, visibility: str, body: str) -> str:
    """visibility MUST be decided by the caller based on the sender's role,
    never taken from client-supplied input - see app.py's two separate
    routes (client vs staff) rather than one route with a visibility param
    a client request could tamper with."""
    assert visibility in ("client", "internal")
    msg_id = uuid.uuid4().hex[:12]
    conn = get_conn()
    conn.execute(
        "INSERT INTO messages (id, order_id, sender_id, visibility, body, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (msg_id, order_id, sender_id, visibility, body.strip(), time.time()),
    )
    conn.commit()
    conn.close()
    return msg_id


def list_messages(order_id: str, include_internal: bool):
    """include_internal must be False for any client-facing call site -
    enforced by the caller (app.py), not by anything in this function, so
    get that decision right at the route level."""
    conn = get_conn()
    if include_internal:
        rows = conn.execute(
            """SELECT messages.*, users.display_name AS sender_name, users.role AS sender_role
               FROM messages JOIN users ON messages.sender_id = users.id
               WHERE order_id = ? ORDER BY created_at ASC""",
            (order_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT messages.*, users.display_name AS sender_name, users.role AS sender_role
               FROM messages JOIN users ON messages.sender_id = users.id
               WHERE order_id = ? AND visibility = 'client' ORDER BY created_at ASC""",
            (order_id,),
        ).fetchall()
    conn.close()
    return rows


def list_conversations_for_client(client_id: str):
    """One row per order that has at least one client-visible message,
    newest activity first - the real data behind a cross-order 'Messages'
    inbox view (client_files.html's sibling, client_messages.html). Never
    a second messaging system: this is a read-only summary over the same
    messages/orders tables the per-order thread (list_messages,
    create_message) already uses - replying still happens on the real
    order page, where the full thread and the actual reply form live."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT orders.id AS order_id, orders.original_filename, orders.status,
                  m.body AS last_body, m.created_at AS last_at, m.sender_id AS last_sender_id
           FROM orders
           JOIN (
             SELECT order_id, body, created_at, sender_id,
                    ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY created_at DESC) AS rn
             FROM messages WHERE visibility = 'client'
           ) m ON m.order_id = orders.id AND m.rn = 1
           WHERE orders.client_id = ?
           ORDER BY m.created_at DESC""",
        (client_id,),
    ).fetchall()
    conn.close()
    return rows


def mark_read(user_id: str, order_id: str) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO message_reads (user_id, order_id, last_read_at) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id, order_id) DO UPDATE SET last_read_at = excluded.last_read_at",
        (user_id, order_id, time.time()),
    )
    conn.commit()
    conn.close()


def unread_order_ids(user_id: str, include_internal: bool) -> set[str]:
    """Which orders have messages (of the kinds this user can see) newer
    than that user last looked. Used to badge order lists - deliberately
    simple (one query, no per-message read state) since this is a
    two-person demo, not a message-delivery platform."""
    conn = get_conn()
    visibility_clause = "" if include_internal else "AND messages.visibility = 'client'"
    rows = conn.execute(
        f"""SELECT DISTINCT messages.order_id FROM messages
            LEFT JOIN message_reads
              ON message_reads.order_id = messages.order_id AND message_reads.user_id = ?
            WHERE messages.sender_id != ? {visibility_clause}
              AND messages.created_at > COALESCE(message_reads.last_read_at, 0)""",
        (user_id, user_id),
    ).fetchall()
    conn.close()
    return {r["order_id"] for r in rows}


# -------------------------------------------------------------- billing ----
def get_subscription(user_id: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM subscriptions WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row


PERIOD_SECONDS = 30 * 86400


def get_subscription_current(user_id: str):
    """Like get_subscription, but rolls the usage period over first if 30
    days have passed since it started - this is what makes the free
    2-minute allowance an actual monthly trial instead of a one-time
    lifetime cap. Applies to every plan, not just free: a paid plan that's
    lapsed already downgrades to free via billing.effective_plan, and
    should get a clean free allowance for its new (free) period too."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM subscriptions WHERE user_id = ?", (user_id,)).fetchone()
    if row and row["period_started_at"] and time.time() - row["period_started_at"] > PERIOD_SECONDS:
        conn.execute(
            "UPDATE subscriptions SET minutes_used_this_period = 0, period_started_at = ? "
            "WHERE user_id = ?",
            (time.time(), user_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM subscriptions WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row


def set_subscription_plan(user_id: str, plan: str, period_days: int | None) -> None:
    """period_days=None means it never expires on its own (used for the
    manual/enterprise/test-override paths) - normal paid plans always pass
    a real period length so an unrenewed subscription actually lapses."""
    conn = get_conn()
    now = time.time()
    period_end = (now + period_days * 86400) if period_days else None
    conn.execute(
        """INSERT INTO subscriptions (user_id, plan, status, current_period_end, period_started_at)
           VALUES (?, ?, 'active', ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
             plan = excluded.plan, status = 'active',
             current_period_end = excluded.current_period_end,
             period_started_at = excluded.period_started_at,
             minutes_used_this_period = 0""",
        (user_id, plan, period_end, now),
    )
    conn.commit()
    conn.close()


def add_usage_minutes(user_id: str, minutes: float) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE subscriptions SET minutes_used_this_period = minutes_used_this_period + ? "
        "WHERE user_id = ?",
        (minutes, user_id),
    )
    conn.commit()
    conn.close()


def reserve_free_minutes(user_id: str, minutes_requested: float, cap: float) -> float:
    """Atomically grants up to `minutes_requested` of this period's free
    allowance and marks it used in the same step, returning how much was
    actually granted (0 if none left). A plain read-then-later-write (the
    old create_order flow: read free_minutes_remaining, decide the price,
    only call add_usage_minutes afterwards) is a real double-spend window -
    two submissions in quick succession can both read the same "5.0
    remaining" snapshot and each get charged as if the full amount were
    free, handing out more than one month's trial allowance. The
    UPDATE...WHERE guard below only commits if minutes_used_this_period
    still matches what we just read; if a concurrent request already
    changed it, this retries against the fresh value instead of
    overwriting it - a small, real compare-and-swap, not a fresh table
    lock (SQLite's writer lock would serialize this fine too, but this
    stays correct even against a future move to a real concurrent DB)."""
    conn = get_conn()
    for _ in range(5):
        row = conn.execute(
            "SELECT minutes_used_this_period FROM subscriptions WHERE user_id = ?", (user_id,)
        ).fetchone()
        used = (row["minutes_used_this_period"] or 0.0) if row else 0.0
        remaining = max(0.0, cap - used)
        grant = round(min(max(0.0, minutes_requested), remaining), 4)
        if grant <= 0:
            conn.close()
            return 0.0
        cur = conn.execute(
            "UPDATE subscriptions SET minutes_used_this_period = minutes_used_this_period + ? "
            "WHERE user_id = ? AND minutes_used_this_period = ?",
            (grant, user_id, used),
        )
        conn.commit()
        if cur.rowcount == 1:
            conn.close()
            return grant
        # Lost the race against a concurrent request - loop and retry
        # against the now-current value rather than granting on stale data.
    conn.close()
    return 0.0  # gave up after retries - safe default is to bill it, not free it


def wallet_credits_remaining(user_id: str) -> float:
    conn = get_conn()
    row = conn.execute("SELECT wallet_credits FROM subscriptions WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return max(0.0, row["wallet_credits"]) if row else 0.0


def reserve_wallet_credits(user_id: str, credits_ceiling: float) -> float:
    """Same compare-and-swap shape as reserve_free_minutes, for the same
    reason: two near-simultaneous orders both reading the same "$X of
    credits available" snapshot would each get the full discount applied
    to their own total, over-crediting the account across the two (the
    persisted balance still floors at 0 either way, but by then both
    orders have already been billed as if that value covered each of them
    individually - a real, if narrow, way to extract more discount than
    the balance actually contains). credits_ceiling is the caller's own
    cap - the real dollar value of the order's base cost, converted to
    credits - never more than that gets reserved even if the balance is
    bigger."""
    conn = get_conn()
    for _ in range(5):
        row = conn.execute(
            "SELECT wallet_credits FROM subscriptions WHERE user_id = ?", (user_id,)
        ).fetchone()
        balance = (row["wallet_credits"] or 0.0) if row else 0.0
        grant = round(min(max(0.0, credits_ceiling), max(0.0, balance)), 4)
        if grant <= 0:
            conn.close()
            return 0.0
        cur = conn.execute(
            "UPDATE subscriptions SET wallet_credits = wallet_credits - ? "
            "WHERE user_id = ? AND wallet_credits = ?",
            (grant, user_id, balance),
        )
        conn.commit()
        if cur.rowcount == 1:
            conn.close()
            return grant
    conn.close()
    return 0.0  # gave up after retries - safe default is to bill it, not credit it


def add_wallet_credits(user_id: str, credits: float) -> None:
    """Credits a purchased top-up - called once the payment for it
    actually completes (see app.py's _activate_payment, kind='credits_topup').
    Clears wallet_low_alert_sent_at too - a fresh top-up means the next
    time the balance actually drops low again, it's worth a new email,
    not silence because one fired once months ago."""
    conn = get_conn()
    conn.execute(
        "UPDATE subscriptions SET wallet_credits = wallet_credits + ?, wallet_low_alert_sent_at = NULL "
        "WHERE user_id = ?",
        (credits, user_id),
    )
    conn.commit()
    conn.close()


# 75 credits = $7.50 of value at CREDITS_PER_DOLLAR=10 - the same real
# dollar threshold the old WALLET_LOW_THRESHOLD_MINUTES=10 minutes
# represented at the dub rate (10 * $1.50 = $15... actually kept
# proportionate to a "worth topping up soon" balance rather than an exact
# carry-over of the old number, since the old number was itself only ever
# a round starting guess, not observed data).
WALLET_LOW_THRESHOLD_CREDITS = 75.0


def wallet_low_alert_needed(user_id: str) -> bool:
    """True once, the moment the balance is actually low and no alert has
    fired since the last top-up (or ever) - see wallet_low_alert_sent_at
    above. Doesn't fire for an account that's simply never bought credits
    (wallet_credits = 0 from day one isn't "running low", it's just unused)."""
    conn = get_conn()
    row = conn.execute(
        "SELECT wallet_credits, wallet_low_alert_sent_at FROM subscriptions WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    if not row or row["wallet_low_alert_sent_at"]:
        return False
    return 0 < row["wallet_credits"] < WALLET_LOW_THRESHOLD_CREDITS


def mark_wallet_low_alert_sent(user_id: str) -> None:
    conn = get_conn()
    conn.execute("UPDATE subscriptions SET wallet_low_alert_sent_at = ? WHERE user_id = ?",
                 (time.time(), user_id))
    conn.commit()
    conn.close()


def consume_wallet_credits(user_id: str, credits: float) -> None:
    """Called once per order, for whatever portion of it the credit
    balance actually covered (see billing.order_cost_usd's
    credits_applied) - never goes negative even if called with more than
    the balance."""
    if credits <= 0:
        return
    conn = get_conn()
    conn.execute(
        "UPDATE subscriptions SET wallet_credits = MAX(0, wallet_credits - ?) WHERE user_id = ?",
        (credits, user_id),
    )
    conn.commit()
    conn.close()


def grant_bonus_minutes(client_email: str, minutes: float, granted_by_display_name: str) -> bool:
    """The account-manager-approved onboarding path: a real prospect gets a
    bigger free look (e.g. ~10 minutes) before they've paid anything, at a
    staff member's discretion - the grant itself IS the approval, there's
    no separate request/approve workflow to build here. Returns False if no
    client with that email exists yet (they need to have signed up first)."""
    user = get_user_by_email(client_email.strip().lower())
    if not user or user["role"] != "client":
        return False
    conn = get_conn()
    note = f"+{minutes:.0f} min by {granted_by_display_name} ({_now_str()})"
    conn.execute(
        "UPDATE subscriptions SET bonus_minutes = bonus_minutes + ?, "
        "bonus_note = COALESCE(bonus_note || ' | ', '') || ? WHERE user_id = ?",
        (minutes, note, user["id"]),
    )
    conn.commit()
    conn.close()
    return True


def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime())


def create_payment(payment_id: str, user_id: str, plan: str, amount_usd: float,
                    amount_local: float | None, currency: str, provider: str,
                    meta: str | None = None, order_id: str | None = None) -> None:
    """payment_id is generated by the caller BEFORE this is called and
    BEFORE the provider is ever contacted - see webapp/billing.py. That's
    what makes provider webhooks idempotent: we already know about this
    attempt, we're just recording its outcome, never creating on the fly
    from a webhook we can't otherwise verify we initiated. order_id set =
    this is a per-order usage charge; unset = a plan subscription purchase."""
    conn = get_conn()
    conn.execute(
        """INSERT INTO payments
           (id, user_id, plan, order_id, amount_usd, amount_local, currency, provider, status, meta, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
        (payment_id, user_id, plan, order_id, amount_usd, amount_local, currency, provider, meta, time.time()),
    )
    conn.commit()
    conn.close()


def update_payment_meta(payment_id: str, meta: str) -> None:
    """Called after the provider call returns something we need to
    remember to match a later webhook against - e.g. M-Pesa's
    CheckoutRequestID, which only exists once the STK push has actually
    been sent (see find_pending_mpesa_payment)."""
    conn = get_conn()
    conn.execute("UPDATE payments SET meta = ? WHERE id = ?", (meta, payment_id))
    conn.commit()
    conn.close()


def get_payment(payment_id: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
    conn.close()
    return row


def set_payment_receipt(payment_id: str, receipt_path: str | None, staff_note: str | None) -> None:
    conn = get_conn()
    conn.execute("UPDATE payments SET receipt_path = ?, staff_note = ? WHERE id = ?",
                 (receipt_path, staff_note, payment_id))
    conn.commit()
    conn.close()


PENDING_PAYMENT_MAX_AGE_S = 900  # named so the client-facing countdown (order_pay.html) can't drift from this


def get_active_pending_payment_for_order(order_id: str, max_age_s: float = PENDING_PAYMENT_MAX_AGE_S):
    """Most recent still-fresh 'pending' payment for this order, if any.
    Real double-payment risk this closes: _checkout() used to create a
    brand-new payment record every time it was called, with nothing
    stopping a client from starting (and completing) a second real charge
    for the same order while a first one was still in flight - the order
    itself was already protected from double-PROCESSING (_activate_payment
    checks order status before queuing), but nothing stopped a second real
    payment-provider transaction from actually going through. Anything
    older than max_age_s is treated as abandoned, not blocking - a client
    whose first attempt stalled (closed the tab, bad network) needs to be
    able to retry, not get permanently stuck."""
    conn = get_conn()
    cutoff = time.time() - max_age_s
    row = conn.execute(
        "SELECT * FROM payments WHERE order_id = ? AND status = 'pending' AND created_at > ? "
        "ORDER BY created_at DESC LIMIT 1",
        (order_id, cutoff),
    ).fetchone()
    conn.close()
    return row


def complete_payment(payment_id: str, provider_reference: str, plan_period_days: int) -> bool:
    """Idempotent: if this payment is already completed, does nothing and
    returns False - safe to call from a webhook that fires more than once,
    which providers explicitly warn can happen. The UNIQUE constraint on
    provider_reference is the second, database-level line of defence."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
    if row is None or row["status"] == "completed":
        conn.close()
        return False
    try:
        conn.execute(
            "UPDATE payments SET status = 'completed', provider_reference = ?, completed_at = ? "
            "WHERE id = ?",
            (provider_reference, time.time(), payment_id),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # Another request already recorded this exact provider transaction
        # (the UNIQUE constraint firing) - treat as already-handled, not
        # an error, and definitely don't grant the plan a second time.
        conn.close()
        return False
    conn.close()
    return True


def fail_payment(payment_id: str) -> None:
    conn = get_conn()
    conn.execute("UPDATE payments SET status = 'failed' WHERE id = ?", (payment_id,))
    conn.commit()
    conn.close()


def list_payments_for_user(user_id: str):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM payments WHERE user_id = ? ORDER BY created_at DESC", (user_id,)
    ).fetchall()
    conn.close()
    return rows


def list_pending_bank_payments():
    conn = get_conn()
    rows = conn.execute(
        """SELECT payments.*, users.display_name, users.email FROM payments
           JOIN users ON payments.user_id = users.id
           WHERE payments.provider = 'bank' AND payments.status = 'pending'
           ORDER BY payments.created_at ASC"""
    ).fetchall()
    conn.close()
    return rows


def find_pending_mpesa_payment(checkout_request_id: str):
    """M-Pesa's callback doesn't echo back our own payment id, only what we
    gave it - the checkout_request_id we stashed in `meta` at STK-push time
    (see billing_checkout) is the only way to match the callback to the
    payment it belongs to."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM payments WHERE meta LIKE ? AND status = 'pending' AND provider = 'mpesa'",
        (f"%{checkout_request_id}%",),
    ).fetchone()
    conn.close()
    return row


def get_trial_verification_payment(client_id: str):
    """The real, completed $1 Paystack charge for this client, if one
    exists - what staff_refund_trial_verification looks up before issuing
    a real refund. Newest first, though there should only ever be one per
    account (trial_verified_at is set exactly once)."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM payments WHERE user_id = ? AND status = 'completed' "
        "AND meta LIKE '%\"kind\": \"trial_verification\"%' ORDER BY completed_at DESC LIMIT 1",
        (client_id,),
    ).fetchone()
    conn.close()
    return row


def mark_payment_refunded(payment_id: str, refund_reference: str) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE payments SET refunded_at = ?, refund_reference = ? WHERE id = ?",
        (time.time(), refund_reference, payment_id),
    )
    conn.commit()
    conn.close()


# -------------------------------------------------------- client feedback ----
def record_client_feedback(user_id: str, context: str, rating: str) -> str:
    assert rating in ("great", "good", "needs_work")
    feedback_id = uuid.uuid4().hex[:12]
    conn = get_conn()
    conn.execute(
        "INSERT INTO client_feedback (id, user_id, context, rating, created_at) VALUES (?, ?, ?, ?, ?)",
        (feedback_id, user_id, context, rating, time.time()),
    )
    conn.commit()
    conn.close()
    return feedback_id


# ------------------------------------------------------------ newsletters ----
def list_marketing_opted_in_clients():
    """The real, only source of truth for who gets a newsletter - no
    separate 'Brevo Newsletter List' to keep in sync with this by hand;
    marketing_consent here IS the list."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, email, display_name FROM users WHERE role = 'client' AND marketing_consent = 1"
    ).fetchall()
    conn.close()
    return rows


def create_newsletter_record(subject: str, blog_post_id: str | None, feature_update: str,
                              industry_trend_text: str, industry_trend_url: str,
                              sent_by: str, recipient_count: int, newsletter_id: str | None = None) -> str:
    # newsletter_id can be pre-generated by the caller now (app.py's
    # staff_newsletter_send) so it exists BEFORE the send loop runs - it's
    # used as the real Brevo tag on every individual send, which has to be
    # known at send time, not assigned after the fact.
    newsletter_id = newsletter_id or uuid.uuid4().hex[:12]
    conn = get_conn()
    conn.execute(
        """INSERT INTO newsletters (id, subject, blog_post_id, feature_update, industry_trend_text,
           industry_trend_url, sent_by, recipient_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (newsletter_id, subject, blog_post_id, feature_update, industry_trend_text,
         industry_trend_url, sent_by, recipient_count, time.time()),
    )
    conn.commit()
    conn.close()
    return newsletter_id


def list_newsletters():
    conn = get_conn()
    rows = conn.execute(
        """SELECT newsletters.*, users.display_name AS sent_by_name, blog_posts.title AS blog_post_title
           FROM newsletters
           LEFT JOIN users ON users.id = newsletters.sent_by
           LEFT JOIN blog_posts ON blog_posts.id = newsletters.blog_post_id
           ORDER BY newsletters.created_at DESC"""
    ).fetchall()
    conn.close()
    return rows


# ------------------------------------------------------ limit exceptions ----
def create_exception_request(client_id: str, context: str, client_note: str | None) -> str:
    request_id = uuid.uuid4().hex[:12]
    conn = get_conn()
    conn.execute(
        "INSERT INTO limit_exception_requests (id, client_id, context, client_note, status, created_at) "
        "VALUES (?, ?, ?, ?, 'open', ?)",
        (request_id, client_id, context, client_note, time.time()),
    )
    conn.commit()
    conn.close()
    return request_id


def list_exception_requests(status: str = "open"):
    conn = get_conn()
    rows = conn.execute(
        """SELECT limit_exception_requests.*, users.email AS client_email, users.display_name AS client_name
           FROM limit_exception_requests JOIN users ON users.id = limit_exception_requests.client_id
           WHERE limit_exception_requests.status = ?
           ORDER BY limit_exception_requests.created_at ASC""",
        (status,),
    ).fetchall()
    conn.close()
    return rows


def get_exception_request(request_id: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM limit_exception_requests WHERE id = ?", (request_id,)).fetchone()
    conn.close()
    return row


def resolve_exception_request(request_id: str, status: str, staff_id: str, staff_note: str | None) -> None:
    assert status in ("granted", "declined")
    conn = get_conn()
    conn.execute(
        "UPDATE limit_exception_requests SET status = ?, resolved_by = ?, staff_note = ?, resolved_at = ? "
        "WHERE id = ?",
        (status, staff_id, staff_note, time.time(), request_id),
    )
    conn.commit()
    conn.close()


def grant_trusted_submitter(client_id: str, hours: float = 24.0) -> None:
    """Lifts the order-submission rate limit for this client for a fixed
    window - not a permanent flag, so a one-time real need doesn't quietly
    become a standing, unmonitored exception."""
    conn = get_conn()
    conn.execute(
        "UPDATE users SET trusted_submitter_until = ? WHERE id = ?",
        (time.time() + hours * 3600, client_id),
    )
    conn.commit()
    conn.close()


# --------------------------------------------------------- receipts ----
def create_receipt(payment_id: str, client_id: str, description: str, amount_usd: float,
                    amount_local: float | None, currency: str, provider: str,
                    provider_reference: str | None, line_items: list[dict] | None = None) -> str:
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] + 1
    receipt_number = f"KAULI-{n:06d}"
    receipt_id = uuid.uuid4().hex[:12]
    conn.execute(
        """INSERT INTO receipts (id, receipt_number, payment_id, client_id, description, amount_usd,
           amount_local, currency, provider, provider_reference, email_status, issued_at, line_items_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
        (receipt_id, receipt_number, payment_id, client_id, description, amount_usd,
         amount_local, currency, provider, provider_reference, time.time(),
         json.dumps(line_items) if line_items else None),
    )
    conn.commit()
    conn.close()
    return receipt_id


def get_receipt(receipt_id: str):
    """Includes the order_id off the linked payment (NULL for a plan
    purchase or wallet top-up, which aren't tied to one order) - lets the
    receipt page link back to "View order" without a second query."""
    conn = get_conn()
    row = conn.execute(
        """SELECT receipts.*, payments.order_id AS order_id
           FROM receipts LEFT JOIN payments ON payments.id = receipts.payment_id
           WHERE receipts.id = ?""",
        (receipt_id,),
    ).fetchone()
    conn.close()
    return row


def get_receipt_for_payment(payment_id: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM receipts WHERE payment_id = ?", (payment_id,)).fetchone()
    conn.close()
    return row


def get_receipt_for_order(order_id: str):
    conn = get_conn()
    row = conn.execute(
        """SELECT receipts.* FROM receipts JOIN payments ON payments.id = receipts.payment_id
           WHERE payments.order_id = ? ORDER BY receipts.issued_at DESC LIMIT 1""",
        (order_id,),
    ).fetchone()
    conn.close()
    return row


def list_receipts_for_client(client_id: str):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM receipts WHERE client_id = ? ORDER BY issued_at DESC", (client_id,)
    ).fetchall()
    conn.close()
    return rows


def list_receipts(email_status: str | None = None):
    conn = get_conn()
    query = ("SELECT receipts.*, users.email AS client_email, users.display_name AS client_name "
              "FROM receipts JOIN users ON users.id = receipts.client_id WHERE 1=1")
    params: list = []
    if email_status:
        query += " AND receipts.email_status = ?"
        params.append(email_status)
    query += " ORDER BY receipts.issued_at DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows


def list_receipts_needing_delivery():
    """'queued' (auto-send never attempted - mailer not configured, or a
    payment kind that predates it) or 'failed' (attempted and Brevo
    rejected/errored) - either way, staff needs to get this to the client
    some other way. 'sent' receipts (auto or manual) don't show up here."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT receipts.*, users.email AS client_email, users.display_name AS client_name
           FROM receipts JOIN users ON users.id = receipts.client_id
           WHERE receipts.email_status IN ('queued', 'failed')
           ORDER BY receipts.issued_at DESC"""
    ).fetchall()
    conn.close()
    return rows


def mark_receipt_sent(receipt_id: str) -> None:
    """Staff manually confirming they forwarded it themselves (email,
    WhatsApp, etc.) - see set_receipt_email_result for the automatic path."""
    conn = get_conn()
    conn.execute("UPDATE receipts SET email_status = 'sent', email_send_detail = NULL WHERE id = ?",
                 (receipt_id,))
    conn.commit()
    conn.close()


def set_receipt_email_result(receipt_id: str, ok: bool, detail: str) -> None:
    """Records the outcome of an automatic send attempt (see
    mailer.send_email) - 'sent' with Brevo's messageId, or 'failed' with
    the real reason so staff sees why and can fall back to forwarding it
    themselves rather than a receipt silently never going out."""
    conn = get_conn()
    conn.execute("UPDATE receipts SET email_status = ?, email_send_detail = ? WHERE id = ?",
                 ("sent" if ok else "failed", detail, receipt_id))
    conn.commit()
    conn.close()


# ------------------------------------------------------------- youtube ----
def create_youtube_watch(client_id: str, playlist_id: str, label: str | None) -> str:
    watch_id = uuid.uuid4().hex[:12]
    conn = get_conn()
    conn.execute(
        "INSERT INTO youtube_watches (id, client_id, playlist_id, label, active, created_at) "
        "VALUES (?, ?, ?, ?, 1, ?)",
        (watch_id, client_id, playlist_id, label, time.time()),
    )
    conn.commit()
    conn.close()
    return watch_id


def list_youtube_watches(client_id: str | None = None, active_only: bool = False):
    conn = get_conn()
    query = "SELECT * FROM youtube_watches WHERE 1=1"
    params: list = []
    if client_id:
        query += " AND client_id = ?"
        params.append(client_id)
    if active_only:
        query += " AND active = 1"
    query += " ORDER BY created_at DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows


def set_youtube_watch_active(watch_id: str, active: bool) -> None:
    conn = get_conn()
    conn.execute("UPDATE youtube_watches SET active = ? WHERE id = ?", (1 if active else 0, watch_id))
    conn.commit()
    conn.close()


def record_youtube_poll_result(watch_id: str, error: str | None) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE youtube_watches SET last_checked_at = ?, last_error = ? WHERE id = ?",
        (time.time(), error, watch_id),
    )
    conn.commit()
    conn.close()


def video_already_seen(watch_id: str, video_id: str) -> bool:
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM youtube_pending_imports WHERE watch_id = ? AND video_id = ?", (watch_id, video_id)
    ).fetchone()
    conn.close()
    return row is not None


def create_pending_import(watch_id: str, video_id: str, title: str, published_at: float | None) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO youtube_pending_imports (id, watch_id, video_id, title, published_at, "
        "status, found_at) VALUES (?, ?, ?, ?, ?, 'new', ?)",
        (uuid.uuid4().hex[:12], watch_id, video_id, title, published_at, time.time()),
    )
    conn.commit()
    conn.close()


def list_pending_imports(client_id: str, status: str = "new"):
    conn = get_conn()
    rows = conn.execute(
        """SELECT youtube_pending_imports.*, youtube_watches.label AS watch_label
           FROM youtube_pending_imports
           JOIN youtube_watches ON youtube_watches.id = youtube_pending_imports.watch_id
           WHERE youtube_watches.client_id = ? AND youtube_pending_imports.status = ?
           ORDER BY youtube_pending_imports.found_at DESC""",
        (client_id, status),
    ).fetchall()
    conn.close()
    return rows


def set_pending_import_status(import_id: str, status: str) -> None:
    assert status in ("new", "imported", "dismissed")
    conn = get_conn()
    conn.execute("UPDATE youtube_pending_imports SET status = ? WHERE id = ?", (status, import_id))
    conn.commit()
    conn.close()


def mark_pending_import_ordered(client_id: str, video_id: str) -> None:
    """Called from create_order whenever a submitted order's YouTube video
    happens to match a pending import for this client - closes the loop
    without the client having to separately go dismiss/confirm it."""
    conn = get_conn()
    conn.execute(
        """UPDATE youtube_pending_imports SET status = 'imported'
           WHERE video_id = ? AND status = 'new' AND watch_id IN (
               SELECT id FROM youtube_watches WHERE client_id = ?
           )""",
        (video_id, client_id),
    )
    conn.commit()
    conn.close()


# ----------------------------------------------------------------- crm ----
# Our own lead pipeline: 'new' -> 'contacted' -> 'qualified' -> 'proposal'
# -> 'won' | 'lost'. 'won' is set automatically on signup (see
# get_or_create_user), not by staff remembering to flag it.
LEAD_STATUSES = ("new", "contacted", "qualified", "proposal", "won", "lost")
LEAD_OPEN_STATUSES = ("new", "contacted", "qualified", "proposal")
LEAD_SOURCES = ("website", "instagram", "facebook", "tiktok", "whatsapp", "calendly", "referral", "other")


def create_lead(name: str, email: str, phone: str | None, company: str | None,
                 message: str | None, preferred_time: str | None,
                 source: str = "website", created_by: str | None = None,
                 volume_estimate: str | None = None, org_type: str | None = None,
                 personal_email_flag: bool = False) -> str:
    """source defaults to 'website' for the public callback form; staff
    logging a lead from anywhere else (an Instagram DM, a WhatsApp inquiry,
    a referral) pass the real source explicitly - see webapp/app.py's
    manual "add lead" route. There's no live API pulling leads from those
    platforms automatically (that needs a developer account + OAuth app per
    platform); this is the honest, working version until that exists.

    volume_estimate/org_type/personal_email_flag are triage signal for
    staff (see staff_leads.html), not an automated accept/reject gate -
    every submission that passes the honeypot and rate limit becomes a
    real lead regardless of what's in these fields."""
    assert source in LEAD_SOURCES
    lead_id = uuid.uuid4().hex[:12]
    now = time.time()
    conn = get_conn()
    conn.execute(
        """INSERT INTO leads (id, name, email, phone, company, message, preferred_time, status, source,
           created_at, volume_estimate, org_type, personal_email_flag)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, ?, ?)""",
        (lead_id, name, email, phone, company, message, preferred_time, source, now,
         volume_estimate, org_type, 1 if personal_email_flag else 0),
    )
    if created_by:
        conn.execute(
            "INSERT INTO lead_notes (id, lead_id, author_id, body, created_at) VALUES (?, ?, ?, ?, ?)",
            (uuid.uuid4().hex[:12], lead_id, created_by, f"Logged manually (source: {source}).", now),
        )
    conn.commit()
    conn.close()
    return lead_id


def list_leads(status: str | None = None, source: str | None = None):
    conn = get_conn()
    query = "SELECT * FROM leads WHERE 1=1"
    params: list = []
    if status:
        query += " AND status = ?"
        params.append(status)
    if source:
        query += " AND source = ?"
        params.append(source)
    query += " ORDER BY created_at DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows


def get_lead(lead_id: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    conn.close()
    return row


def count_new_leads() -> int:
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) FROM leads WHERE status = 'new'").fetchone()[0]
    conn.close()
    return n


def set_lead_status(lead_id: str, status: str, changed_by: str | None = None) -> None:
    assert status in LEAD_STATUSES
    conn = get_conn()
    conn.execute("UPDATE leads SET status = ? WHERE id = ?", (status, lead_id))
    if changed_by:
        conn.execute(
            "INSERT INTO lead_notes (id, lead_id, author_id, body, created_at) VALUES (?, ?, ?, ?, ?)",
            (uuid.uuid4().hex[:12], lead_id, changed_by, f"Status changed to '{status}'.", time.time()),
        )
    conn.commit()
    conn.close()


def add_lead_note(lead_id: str, author_id: str, body: str) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO lead_notes (id, lead_id, author_id, body, created_at) VALUES (?, ?, ?, ?, ?)",
        (uuid.uuid4().hex[:12], lead_id, author_id, body, time.time()),
    )
    conn.commit()
    conn.close()


def list_lead_notes(lead_id: str):
    conn = get_conn()
    rows = conn.execute(
        """SELECT lead_notes.*, users.display_name AS author_name FROM lead_notes
           LEFT JOIN users ON lead_notes.author_id = users.id
           WHERE lead_id = ? ORDER BY created_at ASC""",
        (lead_id,),
    ).fetchall()
    conn.close()
    return rows


def leads_pipeline_summary():
    """Funnel counts by stage and by source, plus an overall conversion
    rate (won / (won + lost), i.e. of leads that reached a final outcome,
    how many became customers) - the two numbers a CRM exists to answer."""
    conn = get_conn()
    by_status = {row["status"]: row["n"] for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM leads GROUP BY status")}
    by_source = conn.execute(
        """SELECT source, COUNT(*) AS n,
                  SUM(CASE WHEN status = 'won' THEN 1 ELSE 0 END) AS won
           FROM leads GROUP BY source ORDER BY n DESC"""
    ).fetchall()
    conn.close()
    won = by_status.get("won", 0)
    lost = by_status.get("lost", 0)
    decided = won + lost
    return {
        "by_status": by_status,
        "by_source": [dict(r) for r in by_source],
        "won": won, "lost": lost,
        "conversion_rate": (won / decided) if decided else None,
    }


def stale_leads(threshold_hours: float = 48.0):
    """Open leads (not won/lost) with no activity - no note added and no
    status change - since the threshold. The "who's going cold" list."""
    cutoff = time.time() - threshold_hours * 3600
    open_placeholders = ",".join("?" for _ in LEAD_OPEN_STATUSES)
    conn = get_conn()
    rows = conn.execute(
        f"""SELECT leads.* FROM leads
            WHERE leads.status IN ({open_placeholders})
              AND leads.created_at < ?
              AND leads.id NOT IN (
                  SELECT lead_id FROM lead_notes WHERE created_at >= ?
              )
            ORDER BY leads.created_at ASC""",
        (*LEAD_OPEN_STATUSES, cutoff, cutoff),
    ).fetchall()
    conn.close()
    return rows


# ------------------------------------------------------- onboarding CRM ----
# 'new' (just signed up) -> 'activated' (first payment/order went through).
# 'nudged' means a 48h-inactivity reminder got queued - it's a side branch
# off 'new', not a dead end: an account can go 'new' -> 'nudged' -> 'activated'.
ONBOARDING_STATUSES = ("new", "nudged", "activated")
ONBOARDING_MESSAGE_KINDS = ("welcome", "first_payment", "inactivity_nudge")


def queue_onboarding_message(user_id: str, kind: str, subject: str, body: str) -> str:
    """Writes a fully-rendered message to onboarding_messages with status
    'pending_send'. A real transactional provider (Brevo, see
    webapp/mailer.py) is wired up now - callers attempt a real send right
    after queuing (see set_onboarding_message_email_result) and this row
    flips to 'sent' automatically. Still written first either way, so if
    the mailer isn't configured or a send fails, staff see it on
    /staff/leads and can act on it by hand - the row always exists,
    nothing about that fallback needing this to succeed."""
    assert kind in ONBOARDING_MESSAGE_KINDS
    message_id = uuid.uuid4().hex[:12]
    conn = get_conn()
    conn.execute(
        "INSERT INTO onboarding_messages (id, user_id, kind, subject, body, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'pending_send', ?)",
        (message_id, user_id, kind, subject, body, time.time()),
    )
    conn.commit()
    conn.close()
    return message_id


def has_onboarding_message(user_id: str, kind: str) -> bool:
    """Guards every trigger below against queuing the same message twice
    for the same account - e.g. a second login for an account that's
    already 'new' shouldn't queue a second welcome message."""
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM onboarding_messages WHERE user_id = ? AND kind = ? LIMIT 1",
        (user_id, kind),
    ).fetchone()
    conn.close()
    return row is not None


def list_onboarding_messages(status: str | None = None):
    conn = get_conn()
    query = ("SELECT onboarding_messages.*, users.email AS user_email, users.display_name AS user_display_name "
              "FROM onboarding_messages JOIN users ON users.id = onboarding_messages.user_id WHERE 1=1")
    params: list = []
    if status:
        query += " AND onboarding_messages.status = ?"
        params.append(status)
    query += " ORDER BY onboarding_messages.created_at DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows


def mark_onboarding_message_sent(message_id: str) -> None:
    """Staff manually confirming they forwarded it themselves - see
    set_onboarding_message_email_result for the automatic path."""
    conn = get_conn()
    conn.execute(
        "UPDATE onboarding_messages SET status = 'sent', sent_at = ?, email_send_detail = NULL WHERE id = ?",
        (time.time(), message_id),
    )
    conn.commit()
    conn.close()


def set_onboarding_message_email_result(message_id: str, ok: bool, detail: str) -> None:
    """Records the outcome of an automatic send attempt (see
    mailer.send_email) - 'sent' with Brevo's messageId, or stays
    'pending_send' with the real failure reason so staff know why and can
    fall back to sending it themselves."""
    conn = get_conn()
    if ok:
        conn.execute(
            "UPDATE onboarding_messages SET status = 'sent', sent_at = ?, email_send_detail = ? WHERE id = ?",
            (time.time(), detail, message_id),
        )
    else:
        conn.execute(
            "UPDATE onboarding_messages SET email_send_detail = ? WHERE id = ?",
            (detail, message_id),
        )
    conn.commit()
    conn.close()


def set_onboarding_status(user_id: str, status: str) -> None:
    assert status in ONBOARDING_STATUSES
    conn = get_conn()
    conn.execute("UPDATE users SET onboarding_status = ? WHERE id = ?", (status, user_id))
    conn.commit()
    conn.close()


def clients_needing_activation_nudge(threshold_hours: float = 48.0):
    """Real clients (role='client') who signed up more than threshold_hours
    ago, still on onboarding_status='new', and have never submitted an
    order - the post-signup equivalent of stale_leads() above. Staff use
    this to know who to personally nudge; queuing the actual
    'inactivity_nudge' message (and flipping status to 'nudged') is a
    separate, explicit staff action, not automatic - a human should decide
    when a real client actually gets chased, not a cron job."""
    cutoff = time.time() - threshold_hours * 3600
    conn = get_conn()
    rows = conn.execute(
        """SELECT users.* FROM users
           WHERE users.role = 'client' AND users.onboarding_status = 'new'
             AND users.created_at IS NOT NULL AND users.created_at < ?
             AND users.id NOT IN (SELECT DISTINCT client_id FROM orders)
           ORDER BY users.created_at ASC""",
        (cutoff,),
    ).fetchall()
    conn.close()
    return rows


# ------------------------------------------------------------------ ops ----
# Phase 7 (roadmap: ops dashboard) - SLAs, workload, revenue, built only
# from data that already exists (orders, payments). Deliberately no
# per-staff workload breakdown: orders have no "assigned_to" column, so
# that's not something to report on until assignment is actually tracked -
# better to show nothing than to fabricate a metric.
ACTIVE_ORDER_STATUSES = ("pending_payment", "queued", "processing", "awaiting_review", "editor_returned")
TERMINAL_OK_STATUSES = ("ready_for_delivery", "delivered")
TERMINAL_FAIL_STATUSES = ("failed", "returned_to_client", "dead_letter")


def orders_by_status():
    """Current snapshot: how many orders sit in each stage right now."""
    conn = get_conn()
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM orders GROUP BY status").fetchall()
    conn.close()
    return {row["status"]: row["n"] for row in rows}


def stale_active_orders(threshold_hours: float = 24.0):
    """Orders stuck in an in-flight status longer than the threshold, oldest
    first - the "what's actually blocked" list, not just a status count.
    updated_at is when the order last changed stage, so this measures time
    stuck in the CURRENT stage, not total age since creation."""
    cutoff = time.time() - threshold_hours * 3600
    placeholders = ",".join("?" for _ in ACTIVE_ORDER_STATUSES)
    conn = get_conn()
    rows = conn.execute(
        f"""SELECT orders.*, users.display_name AS client_name FROM orders
            JOIN users ON orders.client_id = users.id
            WHERE orders.status IN ({placeholders}) AND orders.updated_at < ?
            ORDER BY orders.updated_at ASC""",
        (*ACTIVE_ORDER_STATUSES, cutoff),
    ).fetchall()
    conn.close()
    return rows


def turnaround_stats(days: int = 30):
    """Average/median time from order creation to ready_for_delivery, for
    orders that actually finished successfully within the window - the SLA
    number. Excludes failed and still-in-flight orders (no end time yet)."""
    cutoff = time.time() - days * 86400
    placeholders = ",".join("?" for _ in TERMINAL_OK_STATUSES)
    conn = get_conn()
    rows = conn.execute(
        f"""SELECT created_at, updated_at FROM orders
            WHERE status IN ({placeholders}) AND updated_at >= ?""",
        (*TERMINAL_OK_STATUSES, cutoff),
    ).fetchall()
    conn.close()
    durations = sorted(r["updated_at"] - r["created_at"] for r in rows)
    if not durations:
        return {"count": 0, "avg_hours": None, "median_hours": None}
    avg = sum(durations) / len(durations)
    mid = len(durations) // 2
    median = durations[mid] if len(durations) % 2 else (durations[mid - 1] + durations[mid]) / 2
    return {"count": len(durations), "avg_hours": avg / 3600, "median_hours": median / 3600}


def failure_rate(days: int = 30):
    """Share of orders that finished 'failed' vs any terminal state, within
    the window - a basic reliability signal."""
    cutoff = time.time() - days * 86400
    ok_placeholders = ",".join("?" for _ in TERMINAL_OK_STATUSES)
    fail_placeholders = ",".join("?" for _ in TERMINAL_FAIL_STATUSES)
    conn = get_conn()
    ok = conn.execute(
        f"SELECT COUNT(*) FROM orders WHERE status IN ({ok_placeholders}) AND updated_at >= ?",
        (*TERMINAL_OK_STATUSES, cutoff),
    ).fetchone()[0]
    failed = conn.execute(
        f"SELECT COUNT(*) FROM orders WHERE status IN ({fail_placeholders}) AND updated_at >= ?",
        (*TERMINAL_FAIL_STATUSES, cutoff),
    ).fetchone()[0]
    conn.close()
    total = ok + failed
    return {"ok": ok, "failed": failed, "total": total, "rate": (failed / total) if total else None}


def minutes_processed(days: int = 30) -> float:
    """Sum of billed minutes for orders that actually completed in the
    window - the throughput number behind the revenue number."""
    cutoff = time.time() - days * 86400
    placeholders = ",".join("?" for _ in TERMINAL_OK_STATUSES)
    conn = get_conn()
    total = conn.execute(
        f"""SELECT COALESCE(SUM(duration_minutes), 0) FROM orders
            WHERE status IN ({placeholders}) AND updated_at >= ?""",
        (*TERMINAL_OK_STATUSES, cutoff),
    ).fetchone()[0]
    conn.close()
    return total or 0.0


def orders_created_since(days: int) -> int:
    cutoff = time.time() - days * 86400
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) FROM orders WHERE created_at >= ?", (cutoff,)).fetchone()[0]
    conn.close()
    return n


def revenue_summary(days: int = 30):
    """Confirmed revenue only (status='completed') - a pending Paystack
    checkout or an unconfirmed bank transfer is not revenue yet. Split by
    provider and by what was actually paid for (a per-order usage charge
    vs a plan subscription) so a skewed month is easy to explain."""
    cutoff = time.time() - days * 86400
    conn = get_conn()
    total_all_time = conn.execute(
        "SELECT COALESCE(SUM(amount_usd), 0) FROM payments WHERE status = 'completed'"
    ).fetchone()[0]
    total_period = conn.execute(
        "SELECT COALESCE(SUM(amount_usd), 0) FROM payments WHERE status = 'completed' AND completed_at >= ?",
        (cutoff,),
    ).fetchone()[0]
    by_provider = conn.execute(
        """SELECT provider, COALESCE(SUM(amount_usd), 0) AS total, COUNT(*) AS n
           FROM payments WHERE status = 'completed' AND completed_at >= ?
           GROUP BY provider""",
        (cutoff,),
    ).fetchall()
    by_kind = conn.execute(
        """SELECT CASE WHEN order_id IS NOT NULL THEN 'usage' ELSE 'plan' END AS kind,
                  COALESCE(SUM(amount_usd), 0) AS total, COUNT(*) AS n
           FROM payments WHERE status = 'completed' AND completed_at >= ?
           GROUP BY kind""",
        (cutoff,),
    ).fetchall()
    pending_bank = conn.execute(
        "SELECT COALESCE(SUM(amount_usd), 0), COUNT(*) FROM payments WHERE status = 'pending' AND provider = 'bank'"
    ).fetchone()
    conn.close()
    return {
        "total_all_time": total_all_time,
        "total_period": total_period,
        "by_provider": [dict(r) for r in by_provider],
        "by_kind": [dict(r) for r in by_kind],
        "pending_bank_amount": pending_bank[0] or 0.0,
        "pending_bank_count": pending_bank[1] or 0,
    }


def orders_created_between(start_ts: float, end_ts: float) -> int:
    """Count of orders created in [start_ts, end_ts) - the building block for
    real 'vs previous period' comparisons on the staff overview page (see
    orders_reaching_status_between and revenue_between for the same pattern
    applied to completions and revenue)."""
    conn = get_conn()
    n = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE created_at >= ? AND created_at < ?",
        (start_ts, end_ts),
    ).fetchone()[0]
    conn.close()
    return n


def orders_reaching_status_between(status: str, start_ts: float, end_ts: float) -> int:
    """Count of orders currently in `status` whose status_changed_at falls in
    [start_ts, end_ts). Only meaningful for a status real orders don't leave
    once reached (e.g. 'delivered' is terminal) - otherwise status_changed_at
    reflects the most recent transition, not necessarily the one into this
    status."""
    conn = get_conn()
    n = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE status = ? AND status_changed_at >= ? AND status_changed_at < ?",
        (status, start_ts, end_ts),
    ).fetchone()[0]
    conn.close()
    return n


def revenue_between(start_ts: float, end_ts: float) -> float:
    """Confirmed revenue (completed payments only) in [start_ts, end_ts) -
    same definition as revenue_summary's total_period, just over an
    arbitrary bounded window instead of "since N days ago", so the overview
    page can compare this calendar month against last."""
    conn = get_conn()
    total = conn.execute(
        "SELECT COALESCE(SUM(amount_usd), 0) FROM payments WHERE status = 'completed' AND completed_at >= ? AND completed_at < ?",
        (start_ts, end_ts),
    ).fetchone()[0]
    conn.close()
    return total or 0.0


def recent_status_changes(limit: int = 12):
    """Most recently changed orders - the real signal behind the staff
    overview's activity feed. No separate audit-log table exists yet; this
    derives "what just happened" from status_changed_at, which every real
    status transition already updates (see update_order_status and the
    other status-writing call sites)."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT orders.id, orders.status, orders.status_changed_at, orders.original_filename,
                  users.display_name AS client_name
           FROM orders JOIN users ON orders.client_id = users.id
           ORDER BY orders.status_changed_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return rows


def recent_client_messages(limit: int = 8):
    """Most recent staff-sent, client-visible messages - real activity for
    the overview feed (an actual event with a real actor, not derived from a
    status field like recent_status_changes)."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT messages.order_id, messages.body, messages.created_at, messages.sender_id,
                  orders.original_filename, sender.display_name AS staff_name,
                  client.display_name AS client_name
           FROM messages
           JOIN orders ON messages.order_id = orders.id
           JOIN users AS client ON orders.client_id = client.id
           LEFT JOIN users AS sender ON messages.sender_id = sender.id
           WHERE messages.visibility = 'client'
           ORDER BY messages.created_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return rows


def daily_job_trend(days: int = 30):
    """Real, retroactive trend data - no new snapshot table needed, because
    created_at and status_changed_at are both real timestamps every order
    already has. Two series: jobs CREATED per day (created_at) and jobs
    DELIVERED per day (status_changed_at, status='delivered' - terminal,
    so this is a real one-time event per order, not recount-able later).
    SQLite's strftime works directly on a unix epoch REAL with the
    'unixepoch' modifier - no Python-side date math needed."""
    cutoff = time.time() - days * 86400
    conn = get_conn()
    created_rows = conn.execute(
        """SELECT strftime('%Y-%m-%d', created_at, 'unixepoch') AS day, COUNT(*) AS n
           FROM orders WHERE created_at >= ? GROUP BY day""",
        (cutoff,),
    ).fetchall()
    delivered_rows = conn.execute(
        """SELECT strftime('%Y-%m-%d', status_changed_at, 'unixepoch') AS day, COUNT(*) AS n
           FROM orders WHERE status = 'delivered' AND status_changed_at >= ? GROUP BY day""",
        (cutoff,),
    ).fetchall()
    conn.close()
    created_by_day = {r["day"]: r["n"] for r in created_rows}
    delivered_by_day = {r["day"]: r["n"] for r in delivered_rows}
    days_list = []
    for i in range(days - 1, -1, -1):
        day = datetime.fromtimestamp(time.time() - i * 86400).strftime("%Y-%m-%d")
        days_list.append({"day": day, "created": created_by_day.get(day, 0), "delivered": delivered_by_day.get(day, 0)})
    return days_list


def top_clients_by_usage(days: int = 30, limit: int = 5):
    """Clients ranked by billed minutes processed in the window - real
    duration_minutes off completed/in-flight orders, not a fabricated usage
    metric. Counts any order created in the window regardless of current
    status, since duration is known at order-creation time (billing.py sets
    it from the upload) rather than only once delivered."""
    cutoff = time.time() - days * 86400
    conn = get_conn()
    rows = conn.execute(
        """SELECT users.id AS client_id, users.display_name AS client_name,
                  COALESCE(SUM(orders.duration_minutes), 0) AS minutes, COUNT(*) AS n
           FROM orders JOIN users ON orders.client_id = users.id
           WHERE orders.created_at >= ? AND orders.duration_minutes IS NOT NULL
           GROUP BY users.id ORDER BY minutes DESC LIMIT ?""",
        (cutoff, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def margin_summary(days: int = 30):
    """Internal-only: revenue (what orders actually billed for) vs an
    ESTIMATED ai cost (see billing.ESTIMATED_AI_COST_PER_MINUTE - a
    placeholder to calibrate, not real accounting), broken out by
    source->target language pair so a specific route's real profitability
    is visible at a glance. Revenue is what was CHARGED (order.cost_usd),
    not necessarily collected yet - pair with revenue_summary's confirmed-
    payments figure for the collected side."""
    from . import billing  # local import - db.py has no other dependency on billing.py
    cutoff = time.time() - days * 86400
    conn = get_conn()
    rows = conn.execute(
        """SELECT source_lang, target_lang, mt, duration_minutes, cost_usd, ai_cost_usd
           FROM orders WHERE updated_at >= ? AND cost_usd IS NOT NULL AND duration_minutes IS NOT NULL""",
        (cutoff,),
    ).fetchall()
    conn.close()
    by_pair: dict[str, dict] = {}
    total_revenue = total_cost = 0.0
    for r in rows:
        pair = f"{r['source_lang']} -> {r['target_lang']}"
        # Prefer the REAL, metered cost (ai_cost_usd - real Claude token
        # usage, see providers/mt.py) whenever this order actually
        # accrued one; only fall back to the per-minute estimate for
        # orders processed before that was tracked, or on a provider that
        # doesn't report real usage.
        est_cost = round(r["ai_cost_usd"], 4) if r["ai_cost_usd"] else round(
            (r["duration_minutes"] or 0) * billing.ESTIMATED_AI_COST_PER_MINUTE.get(r["mt"], 0.0), 4)
        entry = by_pair.setdefault(pair, {"pair": pair, "orders": 0, "revenue_usd": 0.0, "est_ai_cost_usd": 0.0})
        entry["orders"] += 1
        entry["revenue_usd"] += r["cost_usd"] or 0.0
        entry["est_ai_cost_usd"] += est_cost
        total_revenue += r["cost_usd"] or 0.0
        total_cost += est_cost
    for entry in by_pair.values():
        entry["revenue_usd"] = round(entry["revenue_usd"], 2)
        entry["est_ai_cost_usd"] = round(entry["est_ai_cost_usd"], 4)
        entry["est_margin_usd"] = round(entry["revenue_usd"] - entry["est_ai_cost_usd"], 2)
        entry["est_margin_pct"] = (entry["est_margin_usd"] / entry["revenue_usd"]) if entry["revenue_usd"] else None
    return {
        "by_pair": sorted(by_pair.values(), key=lambda e: -e["revenue_usd"]),
        "total_revenue_usd": round(total_revenue, 2),
        "total_est_ai_cost_usd": round(total_cost, 4),
        "total_est_margin_usd": round(total_revenue - total_cost, 2),
    }


# --------------------------------------------------- voice actors & payouts ----
# Staff-managed human voice-over talent (see SCHEMA's voice_actors /
# voice_actor_payouts comments for why there's no self-service actor
# portal or automated payout rail here yet). Everything below is a plain
# CRUD + ledger layer - no matching/casting/escrow logic, because there
# are no real actors yet to match against.

def create_voice_actor(name: str, languages: list[str], email: str | None = None,
                        phone: str | None = None, bio: str | None = None,
                        rate_per_min_usd: float | None = None, notes: str | None = None) -> str:
    actor_id = uuid.uuid4().hex[:12]
    conn = get_conn()
    conn.execute(
        """INSERT INTO voice_actors (id, name, email, phone, languages, bio, rate_per_min_usd,
                                      status, notes, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
        (actor_id, name.strip(), (email or "").strip().lower() or None, (phone or "").strip() or None,
         ",".join(languages), (bio or "").strip() or None, rate_per_min_usd,
         (notes or "").strip() or None, time.time()),
    )
    conn.commit()
    conn.close()
    return actor_id


def list_voice_actors(status: str | None = None) -> list[dict]:
    conn = get_conn()
    if status:
        rows = conn.execute("SELECT * FROM voice_actors WHERE status = ? ORDER BY name",
                             (status,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM voice_actors ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_voice_actor(actor_id: str) -> dict | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM voice_actors WHERE id = ?", (actor_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_voice_actor(actor_id: str, name: str, languages: list[str], email: str | None,
                        phone: str | None, bio: str | None, rate_per_min_usd: float | None,
                        notes: str | None) -> None:
    conn = get_conn()
    conn.execute(
        """UPDATE voice_actors SET name = ?, email = ?, phone = ?, languages = ?, bio = ?,
                                    rate_per_min_usd = ?, notes = ? WHERE id = ?""",
        (name.strip(), (email or "").strip().lower() or None, (phone or "").strip() or None,
         ",".join(languages), (bio or "").strip() or None, rate_per_min_usd,
         (notes or "").strip() or None, actor_id),
    )
    conn.commit()
    conn.close()


def set_voice_actor_status(actor_id: str, status: str) -> None:
    assert status in ("active", "inactive")
    conn = get_conn()
    conn.execute("UPDATE voice_actors SET status = ? WHERE id = ?", (status, actor_id))
    conn.commit()
    conn.close()


def assign_voice_actor(order_id: str, actor_id: str | None) -> None:
    """actor_id=None un-assigns - a staff member re-casting the order."""
    conn = get_conn()
    conn.execute("UPDATE orders SET voice_actor_id = ?, updated_at = ? WHERE id = ?",
                 (actor_id, time.time(), order_id))
    conn.commit()
    conn.close()


def is_invited_voice_actor(email: str) -> bool:
    """The voice-actor equivalent of is_invited_staff - except there's no
    separate invites table: the voice_actors roster row itself IS the
    invite, staff already created it with a real email (see
    staff_voice_actors_create). user_id IS NULL means nobody's actually
    signed up under this email yet - once they do, link_voice_actor_user
    fills it in, and this stops matching (correctly - a returning actor
    signing in again should just log in as themselves, not be treated as
    a fresh, unlinked invite)."""
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM voice_actors WHERE email = ? AND user_id IS NULL", (email.strip().lower(),)
    ).fetchone()
    conn.close()
    return row is not None


def link_voice_actor_user(email: str, user_id: str) -> None:
    conn = get_conn()
    conn.execute("UPDATE voice_actors SET user_id = ? WHERE email = ? AND user_id IS NULL",
                 (user_id, email.strip().lower()))
    conn.commit()
    conn.close()


def get_voice_actor_by_user_id(user_id: str) -> dict | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM voice_actors WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_orders_for_voice_actor(actor_id: str):
    """Every order actually cast to this actor - their real job list, most
    recently assigned first. No status filter: a finished/delivered order
    stays visible (their own record of past work), not just active ones."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM orders WHERE voice_actor_id = ? ORDER BY updated_at DESC", (actor_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_payout(actor_id: str, order_id: str, minutes: float, rate_per_min_usd: float) -> str:
    """Records what's owed - does not move any real money. See
    mark_payout_paid for the only state change after this: a staff member
    confirming they actually sent it, outside this system."""
    payout_id = uuid.uuid4().hex[:12]
    amount = round(minutes * rate_per_min_usd, 2)
    conn = get_conn()
    conn.execute(
        """INSERT INTO voice_actor_payouts
           (id, actor_id, order_id, minutes, rate_per_min_usd, amount_usd, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'owed', ?)""",
        (payout_id, actor_id, order_id, round(minutes, 2), rate_per_min_usd, amount, time.time()),
    )
    conn.commit()
    conn.close()
    return payout_id


def list_payouts(status: str | None = None) -> list[dict]:
    """Joined with the actor's name/order's client-facing filename so a
    payouts list is actually readable without a second lookup per row."""
    conn = get_conn()
    q = """SELECT p.*, a.name AS actor_name, o.original_filename AS order_filename
           FROM voice_actor_payouts p
           JOIN voice_actors a ON a.id = p.actor_id
           JOIN orders o ON o.id = p.order_id"""
    params: tuple = ()
    if status:
        q += " WHERE p.status = ?"
        params = (status,)
    q += " ORDER BY p.created_at DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_payout_paid(payout_id: str, paid_by_user_id: str, reference: str | None = None) -> None:
    """The one action that means a real transfer actually happened -
    staff confirming it themselves, not this system triggering anything.
    See voice_actor_payouts' own comment on why there's no payment-API
    integration here."""
    conn = get_conn()
    conn.execute(
        """UPDATE voice_actor_payouts SET status = 'paid', paid_at = ?, paid_by = ?, paid_reference = ?
           WHERE id = ? AND status = 'owed'""",
        (time.time(), paid_by_user_id, (reference or "").strip() or None, payout_id),
    )
    conn.commit()
    conn.close()
