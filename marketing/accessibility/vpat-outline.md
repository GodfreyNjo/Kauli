# Kauli: VPAT / Accessibility Conformance Report (ACR) outline

**Status: outline only. Nothing in this file is a claim of conformance.** Every "Supports" in the final report must be backed by a test record in the evidence folder. Until that exists, the public wording stays "we are preparing one" (this is what the landing page says).

---

## 1. Decisions needed before testing starts

| Decision | Recommendation | Why |
|---|---|---|
| Which VPAT edition | **VPAT 2.5 INT** (covers WCAG, Section 508 and EN 301 549 in one report) | US public buyers ask for the 508 or WCAG edition, EU buyers for EN 301 549; INT answers all three |
| WCAG version | **2.1 Level A and AA** for the main table, with a short **2.2** appendix | The DOJ Title II rule points to 2.1 AA; newer procurement forms are starting to ask about 2.2 |
| Who tests | Start in-house, then a **third-party audit** before publishing | A self-attested report carries legal risk if wrong, and public buyers trust an independent audit more |
| Who signs | One named owner (product) and one reviewer (engineering) | Someone must answer buyer follow-up questions |
| Where it lives | A public `/accessibility` page with the ACR as a PDF and an accessibility statement | Buyers look for it before they call |

## 2. Scope: what the report covers

**In scope (customer-facing):**
- Sign-in and sign-up pages (`/login`)
- New-order wizard (upload, YouTube link, options, review, submit)
- Orders list, order page (player, transcript table, toolbar, summary, ask, downloads, captioned video, "Do more with this file")
- Billing, plans, vocabulary, notes, workspace, settings, account closure
- Public marketing site, solution pages, blog, demo (`/try`), help
- Emails the service sends (receipts, order ready)

**Out of scope (say so in the report):** staff-only tools (staff pages, the editor "Ereri"), the public API and MCP server (no user interface; document separately), and third-party widgets we embed (YouTube player, Google sign-in, payment pages, chat assistant bubble unless we build it).

**Record per report:** product name and version or release date tested, date of evaluation, browsers and assistive technology used, contact (hello@kauli-forgemedia.com), and a link to the accessibility statement.

## 3. How to test

**Automated (find the easy 30%):** axe DevTools or axe-core in CI, WAVE, Lighthouse accessibility, and a contrast checker on every colour pair. Run on every page type above, signed in and signed out, light and dark theme.

**Manual (the other 70%):**
- Keyboard only: tab through every flow; no traps, visible focus, logical order, dialogs return focus.
- Screen readers: NVDA with Firefox and Chrome (Windows), VoiceOver with Safari (macOS and iOS), TalkBack with Chrome (Android). JAWS if the auditor has it.
- Zoom and reflow: 200% text resize, 400% zoom at 1280 px wide (reflow without two-direction scrolling), text-spacing bookmarklet.
- Display: Windows high contrast, dark theme, `prefers-reduced-motion`.
- Media: captions, transcripts and audio description for every video and audio file we publish ourselves (demo videos, podcast, promo, YouTube).
- Content: plain-language check on errors, instructions and emails.

**Record for each test:** page or flow, tool and version, steps, result, screenshot or recording, date, tester. Keep in `evidence/<criterion>/`.

## 4. Conformance levels to use

Supports, Partially Supports, Does Not Support, Not Applicable, Not Evaluated (the ITI terms). Every "Partially Supports" and "Does Not Support" needs remarks that say **what fails, where, and the workaround**. Never use "Not Applicable" for a criterion we simply have not tested.

## 5. Criteria table (WCAG 2.1 Level A and AA: 50 criteria)

The last column is **where to look first in Kauli and the likely risk**, taken from what the product does. It is a test plan, not a finding.

| # | Criterion | Level | Look first in Kauli |
|---|---|---|---|
| 1.1.1 | Non-text Content | A | Icon-only buttons (toolbar, bell, menu), logo alt text, progress rings, waveform, decorative SVGs marked `aria-hidden` |
| 1.2.1 | Audio-only and Video-only (Prerecorded) | A | Blog narrations and podcast need a transcript on the page; demo audio |
| 1.2.2 | Captions (Prerecorded) | A | Our own demo videos, promo video, YouTube episode; the `/try` demo players |
| 1.2.3 | Audio Description or Media Alternative | A | Same media: provide a transcript or description. **We do not offer audio description as a service** |
| 1.2.4 | Captions (Live) | AA | Not applicable unless we stream; confirm |
| 1.2.5 | Audio Description (Prerecorded) | AA | Our own videos with important visuals (UI demos) |
| 1.3.1 | Info and Relationships | A | Transcript and cost tables (header cells, scope), form labels, grouped radios, headings, `details`/`summary` widgets |
| 1.3.2 | Meaningful Sequence | A | Order page uses CSS ordering (bottom bar rows use `order`): confirm DOM order still reads sensibly |
| 1.3.3 | Sensory Characteristics | A | Instructions that say "the button on the right" or colour only |
| 1.3.4 | Orientation | AA | Mobile layouts in portrait and landscape |
| 1.3.5 | Identify Input Purpose | AA | Login, sign-up, billing: `autocomplete` values |
| 1.4.1 | Use of Color | A | Status pills (ready, failed), transcript highlights, the green/red hook in the thumbnail-style cards |
| 1.4.2 | Audio Control | A | Demo audio and music: no autoplay over 3 seconds, or a control to stop it |
| 1.4.3 | Contrast (Minimum) | AA | Muted grey text, placeholders, light-blue text on navy, white text on the login photo, orange on blue, disabled states, both themes |
| 1.4.4 | Resize Text | AA | **Login uses a scale-to-fit rule on short windows, and the page does not scroll on desktop widths: test 200% zoom** |
| 1.4.5 | Images of Text | AA | Marketing images that contain words; thumbnails and banners are decorative |
| 1.4.10 | Reflow | AA | 400% zoom on every page type; wide tables (transcript, billing) need to scroll inside their own box |
| 1.4.11 | Non-text Contrast | AA | Input borders on the translucent login card, focus rings, icon buttons, progress rings and bars |
| 1.4.12 | Text Spacing | AA | Fixed-height cards, pills and buttons that may clip text |
| 1.4.13 | Content on Hover or Focus | AA | "More" menu, "More download options", language switcher, tooltips: dismissible, hoverable, persistent |
| 2.1.1 | Keyboard | A | Drop zone, wizard steps, transcript editing, the custom media controls, dialogs, the notification bell |
| 2.1.2 | No Keyboard Trap | A | Dialogs, the editor, the assistant bubble |
| 2.1.4 | Character Key Shortcuts | A | Transcript "Keyboard shortcuts": single-key shortcuts must be remappable, turn-off-able or only active on focus |
| 2.2.1 | Timing Adjustable | A | **Signed out after inactivity, the 15-minute payment window, flash messages that disappear after a few seconds**: warn, extend, or keep messages until dismissed |
| 2.2.2 | Pause, Stop, Hide | A | Auto-updating progress, animated waveform and rings, the pulsing status dot: provide a way to stop, and honour reduced motion |
| 2.3.1 | Three Flashes | A | No flashing content; confirm in demos |
| 2.4.1 | Bypass Blocks | A | Skip link to the main content on every page; landmarks |
| 2.4.2 | Page Titled | A | Unique, descriptive titles; titles update after in-place page swaps |
| 2.4.3 | Focus Order | A | **After an in-place page update or navigation, where does focus go?** Move it to the main heading or announce the change |
| 2.4.4 | Link Purpose (In Context) | A | "Open", "See it", "Show it" style links need context |
| 2.4.5 | Multiple Ways | AA | Navigation, search, sitemap, help |
| 2.4.6 | Headings and Labels | AA | Clear headings on the order page cards; button labels |
| 2.4.7 | Focus Visible | AA | Custom buttons, icon toolbar, summary elements, both themes, over photos |
| 2.5.1 | Pointer Gestures | A | Drag and drop upload has a click alternative; the editor's draggable panel divider needs a keyboard or single-pointer alternative |
| 2.5.2 | Pointer Cancellation | A | Buttons act on release; no down-event actions |
| 2.5.3 | Label in Name | A | Buttons whose visible text differs from their accessible name ("Ask" with an icon, "More") |
| 2.5.4 | Motion Actuation | A | Not applicable unless we use motion input; confirm |
| 3.1.1 | Language of Page | A | `lang` on every page including the translated site pages |
| 3.1.2 | Language of Parts | AA | Transcript cells already carry `lang`; check the language names list and mixed-language copy |
| 3.2.1 | On Focus | A | No context change on focus |
| 3.2.2 | On Input | A | Selects and radios in the wizard must not submit or navigate on change |
| 3.2.3 | Consistent Navigation | AA | Sidebar order across pages |
| 3.2.4 | Consistent Identification | AA | Same icon or label for the same function |
| 3.3.1 | Error Identification | A | Login, wizard, billing errors: text, not just colour, associated with fields |
| 3.3.2 | Labels or Instructions | A | Every field labelled (the email and password fields inside icon groups) |
| 3.3.3 | Error Suggestion | AA | Password rules checklist, upload errors, language and service combinations |
| 3.3.4 | Error Prevention (Legal, Financial, Data) | AA | Purchases show a review step; account closure and delete confirm |
| 4.1.1 | Parsing | A | Valid HTML (retired in 2.2, still in 2.1) |
| 4.1.2 | Name, Role, Value | A | Custom components: step indicators with `role="button"`, menus with `aria-expanded`, `details` used as buttons, the transcript editor cells |
| 4.1.3 | Status Messages | AA | Flash messages, live progress, the upload tray, in-place page refresh: announce through `role="status"` or `aria-live` |

**WCAG 2.2 appendix (optional, but buyers are starting to ask):** 2.4.11 Focus Not Obscured (cookie banner and chat bubble covering focused items), 2.5.7 Dragging Movements, 2.5.8 Target Size Minimum (24 px; small icon buttons), 3.2.6 Consistent Help, 3.3.7 Redundant Entry, 3.3.8 Accessible Authentication (password managers and paste must work; Google sign-in is fine).

## 6. First ten things to check (highest chance of a real finding)

1. Flash messages that vanish after a few seconds (2.2.1, 3.3.1): keep errors until dismissed.
2. In-place page updates and the upload tray: focus management and announcements (2.4.3, 4.1.3).
3. Login at 200 percent and 400 percent zoom, because the card is scaled to fit and the desktop page does not scroll (1.4.4, 1.4.10).
4. Contrast: muted greys, placeholders, white on the photo, orange accents (1.4.3, 1.4.11).
5. Our own media: captions and transcripts for every demo, promo, podcast and YouTube video (1.2.x).
6. The custom media controls and any single-key shortcuts (2.1.1, 2.1.4).
7. Icon-only buttons and their names (1.1.1, 4.1.2, 2.5.3).
8. The transcript table and its editing mode with a screen reader (1.3.1, 4.1.2).
9. Drag-only interactions: the editor panel divider and the file drop zone (2.5.1, 2.1.1).
10. The cookie banner and chat bubble covering focus (2.4.11, 2.4.7).

## 7. Timeline (about six weeks)

| Week | Work | Output |
|---|---|---|
| 1 | Decide edition and scope; set up axe in CI; make the evidence folder | Scope sheet, baseline scan |
| 2 | Automated scans of every page type; keyboard pass | Issue list with severities |
| 3 | Screen reader pass (NVDA, VoiceOver, TalkBack); zoom and reflow | Evidence per criterion |
| 4 | Fix the blockers from section 6; caption and describe our own media | Fixes shipped; re-test |
| 5 | Third-party audit (or an internal reviewer who did not build it) | Audit findings |
| 6 | Write remarks, fill the table, legal read-through, publish `/accessibility` and the PDF | ACR v1.0 and the statement |

Then: re-test every release that changes the interface, and refresh the report at least once a year.

## 8. The accessibility statement (public page, short)

- Our commitment and the standard we aim for (WCAG 2.1 Level AA).
- The date of the last evaluation and how it was done (own testing, third-party audit).
- Known limitations, in plain language, with workarounds.
- How to report a problem (hello@kauli-forgemedia.com), and how fast we reply.
- A link to the full report (ACR PDF) and the date it was published.

## 9. What to say in public until the report exists

Use only statements that are true today:

- "We are preparing an accessibility conformance report for the Kauli web app."
- "Kauli produces captions and transcripts. Whether your whole site or course meets a standard is for you and your counsel to decide."
- "We do not offer audio description or live captioning."

Do not say: "WCAG compliant", "ADA compliant", "Section 508 certified", "fully accessible", or "VPAT available" until the report is published and the evidence is in the folder.

## 10. Ready-made answers for procurement questionnaires

| Question | Answer (edit when true) |
|---|---|
| Do you have a VPAT? | "In preparation for the Kauli web app. Expected: (date). We can share the current test summary under NDA." |
| What standard do you target? | "WCAG 2.1 Level AA, with 2.2 reviewed." |
| Do you support screen readers? | "We test with NVDA, VoiceOver and TalkBack. Results will be in the ACR." |
| Do you provide captioning for our recordings? | "Yes: captions and transcripts, with optional human review." |
| Do you provide audio description or live captions? | "No." |
| Who do we contact about accessibility? | "hello@kauli-forgemedia.com." |
