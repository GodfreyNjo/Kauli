# Working on the code

## Set up

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: use WSL, the tests need ffmpeg and Linux paths
pip install -r requirements.txt -r requirements-dev.txt
pre-commit install                                     # runs the quality gate before every commit
cp .env.example .env                                   # then fill in only what you need; tests never read it
```

`ffmpeg` must be installed (the tests generate small audio files with it).

## The commands you will use

| Command | What it does |
|---|---|
| `bash scripts/check.sh fast` | Quality gate plus the tests of the most-changed areas. Run before every commit. |
| `bash scripts/check.sh` | The same, plus the whole test suite. Run before you ask for review and before any deploy. |
| `python scripts/quality_gate.py` | Lint, function and module size, against the baseline. |
| `python scripts/quality_gate.py --report` | The known debt, biggest first. |
| `python -m pytest tests/test_x.py -k words` | One test while you work on it. |
| `python -m coverage run -m pytest && python -m coverage report` | Which lines the tests never run. |
| `python scripts/generate_module_map.py` | Refresh the module map after you add or rename a module. |
| `uvicorn webapp.app:app --reload --port 8000` | Run the site locally. |

## The flow of a change

1. **Branch** from `main`: `git switch -c short-name-of-the-change`. One topic per branch.
2. **Write the test first** when you are fixing a bug or adding a rule (it should fail), then the code.
3. **Keep functions small** (aim 20 lines) and give new code its own module when it is a new topic, rather than adding to `app.py`.
4. **Run** `bash scripts/check.sh fast` often and `bash scripts/check.sh` before review.
5. **Commit** with a message that says why, not just what (below).
6. **Open a pull request** and ask a second person to review it. The reviewer reads the test first, then the code, and uses the checklist below.
7. **Merge to `main` only after approval and green checks.** `main` always works.
8. **Deploy** only when the owner asks (see [Deploying](/staff/engineering/05-deploying)).

If you are working alone, wait before merging and read your own change on the diff screen as if you had not written it. The checklist still applies.

## Commit messages

```
Short summary in the imperative, under 70 characters

Why this change is needed, and anything a reader would otherwise ask.
What was decided, and what was left for later.
```

## Review checklist

A reviewer asks, and the author has already checked:

- [ ] Does it do what the description says, and nothing else?
- [ ] Is there a test that would have failed before and passes now?
- [ ] Is every function short and about one thing? Are the names clear without a comment?
- [ ] Is anything copied that should live in one place (prices, limits, wording)?
- [ ] What happens when it fails? Is the failure recorded, and can a customer be told what to do?
- [ ] Does it touch money, a migration, or customer data? If so, was it tested against realistic data, and is there a backup and a way back?
- [ ] Are there new settings or secrets? They go in `.env`, never in the repository.
- [ ] Is the guide updated if the change alters how the system works?
- [ ] Does the quality gate pass, and did the debt list stay the same or shrink?

## Definition of done

The code is merged, the tests and the quality gate pass, the guide is current, and it has been deployed and **checked on the live site** (the pages load, the logs are clean, the data is intact).
Until it has been checked live, it is not done.

## Adding a feature: where does the code go?

- A new **topic** (a new report, a new kind of email, a new integration): a new module in `webapp/` with a docstring that says what it is for, and a test file with the same name.
- A new **page**: a route function that checks who is asking, calls the module for the data, and renders a template. Keep route functions short.
- A new **table or column**: add it to `SCHEMA` or the migration section of `init_db()` in `db.py`, additively, and write a test that runs `init_db()` on an old database.
- A new **price or limit**: in `billing.py`, once. Anything that shows it calls the function.
