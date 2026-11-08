# Code health

The standards in [Coding standards](/staff/engineering/02-coding-standards) were introduced on **2026-11-08**, when the codebase already had a lot of code that did not meet them.
Nothing was rewritten to hide that. Instead, the quality gate recorded it in `quality-baseline.json`, and the gate now makes sure it can only get smaller.

## Where it stood on the day it began

| Measure | Then |
|---|---|
| Functions longer than 40 lines | 108 |
| Modules longer than 800 lines | 7 (`app.py` 12,500, `db.py` 5,800, `ui_text.py`, `account_hub.py`, `billing.py`, `kauli/pipeline.py`, `blog_seed.py`) |
| Lint findings | about 4,800, almost all lines over 120 characters |
| Tests | about 100 files; 5 failed depending on the developer's `.env` (fixed) |

Run `python scripts/quality_gate.py --report` for today's numbers: the list above is a snapshot, the command is the truth.

## Where to pay the debt down first

In this order, because each step makes the next safer:

1. **`webapp/app.py`.** Move whole topics into their own modules, one at a time (the checkout and payment functions; the staff pages; the client pages; the SEO pages). Each move is a small, reviewable change
   and the tests already cover the routes.
2. **The longest functions**, each of which is a list of steps that can be named: `create_order` (about 600 lines), `init_db` (about 600; split by table group), `_activate_payment` and `_checkout`.
3. **`db.py`** by topic: orders, payments, accounts, content. The functions already group naturally.
4. **Data written as code.** `solution_pages.build` is 670 lines of page text: it belongs in data files, not a function.

## How to help

When you change a long function, take **one** step out of it into a small named function with a test, and run `python scripts/quality_gate.py --update`. You have made the next change easier,
and the number in the gate went down. That is the whole method.

## Good examples to copy

New code written to the standard, small and single-purpose, with a docstring that says why:
`webapp/diagnostics.py`, `webapp/engineering_docs.py`, `scripts/quality_gate.py`, `scripts/generate_module_map.py`.
