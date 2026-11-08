# Coding standards

These rules exist so that any developer can open any file, understand it, change it safely, and hand it on. They follow the ideas in *Clean Code*
(Robert C. Martin) and are enforced by tools wherever a tool can enforce them. The tools are in [Working on the code](/staff/engineering/03-working-on-the-code).

> **A note on "Betty style".** Betty is the style checker for **C** code (80-column lines, functions of at most 40 lines, and so on). It does not read Python.
> The Python equivalent of its spirit is PEP 8 (checked by `ruff`), plus the function and file size limits below, which the quality gate checks. We use the
> same size limit Betty uses (a function is at most 40 lines), with a stricter aim of 20. Lines are limited to 120 characters rather than 80, because prose
> comments explaining *why* are valuable here.

## 1. Self-explanatory code

| Rule | In practice |
|---|---|
| **One reason to change** (Single Responsibility) | A module, class or function does one thing. If you cannot name it without "and", split it. `diagnostics.py` records and reads failures; it does not render pages. |
| **Short functions** | **Aim for 20 lines, never more than 40.** A function does one thing at one level of detail. A long function is a list of steps: give each step its own function with a name. |
| **Say it once** (DRY) | Every rule or number has one home. Prices are worked out in `billing.order_cost_usd()` only; a screen that needs a price calls it. Copying a rule is how two screens come to disagree. |
| **Keep it simple, do not build ahead** (KISS, YAGNI) | Write what the task needs. No framework for a feature nobody has asked for. Two similar lines are better than a premature abstraction. |
| **Names, not comments, say what** | `minutes_left_after_free_allowance`, not `m`. A comment that restates the code should be deleted and the name improved instead. |
| **Comments say why** | Write the business reason or the trap: *why* free minutes are reserved at order creation (two orders at once), *why* a payment is processed in one place only. The code already says what. |
| **Docstrings** | Every module starts with a docstring whose **first sentence** says what it is for (the [module map](/staff/engineering/08-module-map) is built from it). Public functions say what they return and what they never do. |

### Limits the quality gate enforces

| Limit | Value | Why |
|---|---|---|
| Lines in a function | 40 (aim 20) | Fits in your head and on one screen |
| Lines in a module | 800 | A module longer than that has more than one reason to change |
| Line length | 120 | Readable in a diff and in a side-by-side review |
| Complexity of a function | 12 | More branches than that cannot be tested thoroughly |
| Parameters of a function | 8 | More than that is a missing object |

## 2. Handing on the code to the next developer

- **Formatting is not a discussion.** `.editorconfig` and `ruff` decide. A review comment about spacing means a tool is missing, so add the rule.
- **Write the decision down.** Anything a newcomer would ask "why?" about goes in the [decision log](/staff/engineering/06-decision-log) or in a comment at that line.
- **Every change is reviewed** by a second person before it is merged (see Working on the code). Review spreads knowledge as much as it catches mistakes.
- **A change that alters how the system works updates this guide** in the same commit.
- **Leave the code a little better than you found it.** If you touch a long function, take one step out of it into a named function. The quality gate records the debt, and it may only go down.

## 3. Code that is easy to diagnose

- **Fail loudly, in one place.** Do not hide a failure with `except: pass`. Catch the specific error you expect and can handle; let everything else reach the global error handler,
  which records it with a reference number. The one acceptable broad catch is around something optional (an email, a notification) that must never stop the main job,
  and it must log what it swallowed.
- **Log events as data, not sentences.** `log.info("payment confirmed", extra={"payment_id": ..., "order_id": ...})`. Include the trace id, the order or account involved, and the outcome.
  Never log a password, key, token or full card or phone number.
- **Validate at the edge.** Check input where it enters (a route, a webhook, a file upload) and give a message a person can act on. Inside the code, trust what has been checked.
- **Make failure states visible.** An order stuck in a status, a payment left pending, an email that did not send: each has a column that says why (`error`, `email_send_detail`), so the
  runbook can start from data, not guesses.
- **Every bug fix starts with a test that fails** for that bug, then the fix. The test is the proof, and the guard against it coming back.

## 4. Tests

- Test the behaviour a customer or staff member sees, not the private steps. A good test reads like the sentence in the bug report.
- Tests must **not depend on the machine they run on**: no real `.env`, no network, no wall-clock luck. `tests/conftest.py` removes the developer's real settings from every test.
- Name a test as a sentence: `test_a_new_customers_free_first_order_is_not_asked_for_money`.
- Run the one test you are working on while you work (`pytest tests/test_prepay.py -k free_first`), the whole suite before you push.

## 5. Python specifics

- Type hints on every new function signature. `from __future__ import annotations` at the top of every module.
- `pathlib.Path`, not string paths. `with` for every file and connection.
- Money is in dollars as `float` in the existing code and rounded to cents at the edge (`round(x, 2)`); never compare money with `==` outside tests. Credits are `float` too.
- No mutable default arguments. No wildcard imports. No `eval` or `exec`.
- Database access goes through `db.py` functions with `?` placeholders. Never build SQL with string formatting from user input.
- Templates: put logic in Python, not in Jinja. A template shows data it was handed.

## 6. How the standards are enforced

`python scripts/quality_gate.py` runs lint (`ruff`), the function and module size limits, and compares with `quality-baseline.json`, the list of shortcomings that existed when the gate was
introduced. **New code must be clean. Old shortcomings may stay but may not get worse.** When you fix one, run `python scripts/quality_gate.py --update` and the debt list shrinks.
The same check runs before every commit (pre-commit) and in CI. See [Code health](/staff/engineering/07-code-health) for the current debt.
