# The account area: names, addresses and safety

The signed-in part of the site is the customer's **account**. This page says how it is named, how it is built, and how its safety measures work, so that the next developer extends it the same way.

## Say "account" and "customer", not "client"

- In anything a customer sees (addresses, page titles, buttons, emails) use **account**: `/account/billing`, "Your account".
- In anything staff see use **customers**: `/staff/customers`, "Back to Customers".
- "client" stays only where it is a technical word: the database role value `client`, the Python variable names, `client_scope_id`. Do not rename those; it would touch every table and gain nothing.
- Why: it is what Stripe, GitHub, Notion and others call it, it reads naturally, and "client" sounds like a firm with clients (a law firm) rather than someone using a product.

## Addresses

- Keep the hierarchy flat and predictable: `/account`, `/account/billing`, `/account/orders/{id}`, `/account/notes`. One level of section under `/account`; no deep nesting.
- Account **settings** (profile, security, notifications) live under `/settings` with a tab per topic: `/settings?tab=security`.
- **Old addresses never break.** `webapp/legacy_paths.py` sends `/client/...` to `/account/...` and `/staff/clients/...` to `/staff/customers/...` with a 308 redirect that keeps the method, the body and the query. Old emails, notifications, bookmarks and API clients keep working. Add a pair to `RENAMED_PREFIXES` when you rename a section; never just rename.
- **The OAuth callbacks are the exception** (`/client/youtube/callback`, `/client/integrations/{provider}/callback`). Google, Slack and the others have those exact addresses on file, so they must not move. A test (`tests/test_legacy_paths.py`) fails if someone moves them.
- Private prefixes (`/account`, `/client`, `/staff`, `/settings`) are kept out of search results and `robots.txt`. A new private prefix must be added to the three lists (`_NOINDEX_PREFIXES` in `app.py`, the robots body, and `web_security.PRIVATE_PREFIXES`).

## Safety measures, and where each one lives

| Measure | What it does | Code |
|---|---|---|
| **Sign-in list** | Every sign-in is a row in `user_sessions`. `/settings/sessions` lists them (device, country, a shortened address, last active) and offers "Sign out" for each and "Sign out of all other devices". | `webapp/sessions.py`, `webapp/account_security.py` |
| **Real remote sign-out** | The cookie is signed but carries no state, so alone it cannot be cancelled. `current_user` asks `sessions.is_active` on every request; a revoked row signs that device out on its next click. Sign-ins from before the feature are adopted, so nobody was logged out by its arrival. | `current_user` in `app.py` |
| **Confirm it is you ("sudo mode")** | Before an action that is hard to undo or gives access to others, the customer re-enters their password, or a 6-digit code emailed to them if they have no password (Google or emailed-link sign-ins). Good for 10 minutes. | `webapp/sudo_mode.py` |
| **Masked data** | An email is shown back as `a•••@example.com` on the confirmation page. Never print a full key, token or card number again after it was created. | `sudo_mode.mask_email` |
| **Confirm a destructive action** | A modal dialog, with a red confirm button, instead of the browser's plain prompt. | `webapp/static/app_ux.js` |
| **Inline validation** | A field is checked when the person leaves it and again as they type; the message appears under the field. | `app_ux.js` (`data-validate`) |
| **Submit feedback** | On submit the button is disabled at once (no double charge) and says what it is doing. | `app_ux.js` (`data-loading`) |

### Which actions need "confirm it is you"

Creating or revoking an API key, turning off two-factor sign-in or regenerating its backup codes, inviting or removing a team member, cancelling a subscription, closing the account or requesting deletion. The list is `GUARDED` in the test `tests/test_account_security.py`; the guard is a route dependency:

```python
@app.post("/settings/team/remove", dependencies=[Depends(sudo_mode.required)])
```

When you add a route that changes who can get in, or that spends or removes something that cannot be brought back, add the dependency and add the route to that test.

Changing the password already asks for the current password, so it does not need the extra step.

## Writing pages in the account area

- **Destructive** actions (remove, revoke, delete, disconnect, close): put `data-confirm="Question?"` and `data-danger` on the button or the form. Never `onclick="return confirm(...)"`; a test fails if the customer pages use it. From a script call `kauliConfirm(message, isDanger, whenSure)`.
- **Explicit save for important data** (profile, billing, security, anything that costs money or changes who has access): a form with a Save button. **Auto-save only for toggles** (a notification switch), where the effect is visible at once and trivially undone.
- Add `data-validate` to a form and use the browser's own rules (`required`, `type`, `minlength`, `pattern`). The server still checks everything; the browser check is a courtesy. A rule written in HTML must match `supabase_auth.password_policy_errors`, never be stricter or looser.
- Do not add a submit-button loading state by hand; `app_ux.js` does it for every `POST` form. Opt out with `data-no-loading` (for example a form that streams a download).
- Group settings into tabs in the sidebar or the tab bar, one topic per tab. Do not build one long page.
