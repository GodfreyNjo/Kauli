# Decision log

Decisions that a newcomer would otherwise reverse by accident. Each says what was decided, why, and what it costs. Add a new entry when you make a decision that is hard to see from the code.
Newest first.

## D10. The quality gate is a ratchet, not a clean-slate rule (2026-11)

**Decision.** `scripts/quality_gate.py` holds new code to the full standard and lets old shortcomings stay but never grow. `quality-baseline.json` is the record.
**Why.** The code had thousands of long lines and over a hundred long functions when the standard was introduced. Demanding a clean slate would have blocked every change; ignoring
the standard would have let it spread. A ratchet makes every change at least as good as the last.
**Cost.** The baseline file must be committed and shrinks only through `--update`.

## D9. Tests never read a developer's real `.env` (2026-11)

**Decision.** `tests/conftest.py` removes every name found in `.env` and `.env.example` from the environment for each test.
**Why.** Five tests passed on one machine and failed in the full run because they quietly read an integration key, the public URL, or the Transkriptor key from the real `.env`.
**Cost.** A test that needs a setting must set it itself.

## D8. Anyone who has paid can download and edit everything (2026-11)

**Decision.** A real payment of any kind opens downloads and editing on every order of that account, including orders made with free minutes. Before the first payment, an order
made with free minutes can be read but not downloaded.
**Why.** A customer who has paid is a customer; keeping their free-minute orders locked read as a trap and added a second payment step.
**Cost.** A customer could pay a small amount and then open their other free-minute orders. The free minutes are 30 a month and cost little to serve.

## D7. Large Instant AI orders are paid before the file is uploaded, using prepaid credits (2026-11)

**Decision.** When the browser reports a recording longer than the free minutes cover, the customer pays on `/client/pay-first` first. The payment is a wallet top-up for exactly the amount due;
the file then uploads and the order uses those credits.
**Why.** Hundreds of megabytes were being uploaded and stored for orders that were never paid for, and a customer could wait many minutes for an upload before seeing a price.
**Alternatives rejected.** A new "awaiting upload" order status would have touched every page and job that lists orders. Credits already exist, are applied automatically, and never expire,
so a customer who pays and never uploads loses nothing.
**Cost.** Orders with add-ons, human review, dubbing or rush are not covered (their price has parts credits do not pay for) and keep the older flow. A recording whose length the browser cannot read also keeps it.

## D6. Free-minute notices are sent only after the work has started (2026-11)

**Decision.** "You have used half of your free minutes" and "You have 0 free minutes left" are sent when an order actually starts, not when it is created.
**Why.** An order waiting for payment has processed nothing, so telling a customer their free minutes are gone was wrong and confusing.

## D5. Kenyan customers are charged in shillings at checkout (earlier)

**Decision.** For customers in Kenya on the shillings price card, Paystack charges in KES at the day's rate; everyone else pays in dollars. Our pages now say the shilling amount before checkout.
**Why.** Mobile money works only in shillings, and the card price must match what the customer expects to see.

## D4. Emails to customers are signed "Kauli Team"

**Decision.** Customer emails are signed by the team, never by one person's name. **Why.** The business must not depend on one person.

## D3. Instant AI is always labelled as AI-generated and not reviewed by a person

**Decision.** Everywhere Instant AI output or its price is shown, it says so. **Why.** Honesty about what the customer is getting; human review is a separate, paid service.

## D2. No discounts and no percentage coupons

**Decision.** Prices are the same for everyone. The free minutes are the only free offer. Plan `discount` fields exist in `billing.py` and are all zero.
**Why.** A single, honest price list that can be explained in one sentence.

## D1. One SQLite file, backed up nightly

**Decision.** The site uses a single SQLite database on a data volume, backed up every night to object storage and checked every morning.
**Why.** At the current size it is simple, fast and easy to back up and restore. **Cost.** One writer at a time; the day this is the bottleneck is the day to move to a server database.
