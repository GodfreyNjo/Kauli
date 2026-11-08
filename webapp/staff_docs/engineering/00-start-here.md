# Start here

This is the engineering guide for Kauli. If you are new to the code, read this page first, then the pages in order.

## What Kauli is

Kauli turns audio and video into transcripts, subtitles, translations and dubbed voices. Customers upload a recording (or paste a YouTube link),
pick a service, and pay per audio-minute. The fast option, **Instant AI**, is processed by machines and is always labelled as AI-generated and not
reviewed by a person. **Human review** adds an editor who checks the work against the audio, in selected languages.

## The five-minute tour of the code

| Where | What lives there |
|---|---|
| `webapp/app.py` | The website: every page and API route, wired to the modules below. Large; being split by topic. |
| `webapp/db.py` | The database (SQLite): the schema, migrations, and every read and write. |
| `webapp/billing.py` | Prices, the free minutes, credits, and the payment providers (Paystack, M-Pesa, bank transfer). |
| `webapp/worker.py` | Background jobs: takes a paid or free order and runs it through the pipeline. |
| `kauli/` | The processing pipeline: speech recognition, translation, text-to-speech, and the providers behind each. |
| `webapp/templates/` and `webapp/static/` | The pages (Jinja templates), styles and browser scripts. |
| `tests/` | The automated tests (about one hundred files). |
| `scripts/` | Command-line tools: the quality gate, the module map generator, demo and podcast builders. |
| `ops/` | Server scripts: deploy, hardening, backups. |

The [module map](/staff/engineering/08-module-map) lists every module with a one-line description, generated from the code itself.

## Your first day

1. Install the tools: `pip install -r requirements.txt -r requirements-dev.txt`, then `pre-commit install`.
2. Run everything once: `bash scripts/check.sh`. If it passes, your setup is right.
3. Read [Coding standards](/staff/engineering/02-coding-standards) and [Working on the code](/staff/engineering/03-working-on-the-code).
4. Open the staff **Diagnostics** page (admins) and read [Diagnostics runbook](/staff/engineering/04-diagnostics-runbook), so the first time something breaks is not the first time you read it.
5. Pick a small change, make a branch, and take it through review (see Working on the code).

## The rules that matter most

- **Nothing is deployed without being asked.** Deploys happen when the owner says so, after the checks pass.
- **No discounts or percentage coupons.** Prices are the same for everyone; free minutes are the only free thing.
- **Instant AI is always labelled** as AI-generated and not reviewed by a person.
- **Emails to customers are signed "Kauli Team".**
- **Secrets never go in code, docs, chat or commits.** They live in the server's `.env`.
- **A change to behaviour ships with a test and, if it changes how the system works, a change to this guide.**

## Where to look when something is wrong

Start from [Diagnostics runbook](/staff/engineering/04-diagnostics-runbook). In short: every failure shows the customer a reference number; paste it into the
Diagnostics page to see where in the code it happened.
