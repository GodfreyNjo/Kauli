# Kauli SEO and marketing: 12-week plan

Built from the November 4, 2026 analytics review and the Growth & Revenue Implementation Plan. One rule runs through all of it: judge SEO by
sign-ups and first uploads from organic pages, not by raw visits. If a page gets about 100 visits and no sign-ups, the page is the problem.

## What is already built (live once deployed)

| Item | Where |
|---|---|
| Language pages for the reviewed languages with sample, price, AI vs human comparison, promise, FAQ and related links | `/transcribe/spanish`, `/german`, `/french`, `/chinese-mandarin` |
| Language + job pages (20) | `/transcribe/<language>/<job>`: interview-transcription, podcast-transcription, youtube-subtitles, research-transcripts, call-center-transcripts |
| Every public page in 10 languages with hreflang and sitemap entries | `/zh/...`, `/es/...`, `/ar/...` and so on (translated once, remembered) |
| Call to action in the middle and at the end of every blog post, matched to its topic, with UTMs | automatic |
| Search Console and Bing verification tags | set `GOOGLE_SITE_VERIFICATION` and `BING_SITE_VERIFICATION` on the server |
| Real customers only in analytics | mark accounts on the client page; `KAULI_INTERNAL_EMAILS` / `KAULI_INTERNAL_DOMAINS` mark them automatically |
| UTM link builder | Site analytics page |

## Week 1: measurement (you)

1. Google Search Console: add the property `https://kauli-forgemedia.com`, choose the HTML tag method, copy the token, set `GOOGLE_SITE_VERIFICATION` in the server `.env`, redeploy, press Verify.
2. Submit `https://kauli-forgemedia.com/sitemap.xml` in Search Console. Do the same in Bing Webmaster Tools (`BING_SITE_VERIFICATION`).
3. Open Staff > Translations and press "Translate every public page now" so every language page is ready before a crawler asks.
4. Flag your own and test accounts as internal (client page, tick "Internal or test account").
5. Tag every link you share with the UTM builder on Site analytics. Naming: `utm_source` = where (linkedin, whatsapp, newsletter, youtube-desc, partner-name), `utm_medium` = kind (social, email, referral, outreach), `utm_campaign` = what (spanish-interviews, pilot-outreach).
6. Keep a UTM sheet (a shared spreadsheet): date, link, source, medium, campaign, where posted.

## Weeks 2 to 3: choose the next pages from real data

- In Search Console list the queries you already appear for (Performance > Queries). Pair them with Google Keyword Planner volumes.
- Pick 15 to 20 targets with clear buying intent and low competition. Prefer "language + job" over "audio to text".
- Do not create pages for the other 70 languages until Search Console shows impressions for them.

## The weekly rhythm (weeks 1 to 12): one strong page or post a week

| Week | Publish | Goal keyword family |
|---|---|---|
| 1 | Update the three top posts (YouTube tax, AI vs human dubbing, YouTube bot check): CTA is automatic, add a transcription angle to each intro | existing readers |
| 2 | "AI vs human transcription: when to pay for review" (refresh the existing guide with prices and the sample) | decision |
| 3 | "Spanish interview transcription: a practical guide" | Spanish + interviews |
| 4 | "Otter alternative for multilingual work" | comparison |
| 5 | "Verbatim vs clean read: which transcript do you need?" | research |
| 6 | "German podcast transcripts: show notes, SEO and subtitles" | German + podcasts |
| 7 | "Mandarin to English subtitles for YouTube" | Chinese + subtitles |
| 8 | "French interview transcription and the mistakes machines make" | French + interviews |
| 9 | "Transcribing research interviews: consent, privacy and coding" | research |
| 10 | "Call transcripts for quality review: what to check" | call centres |
| 11 | "Rev vs Kauli vs Otter: what each is best at" | comparison |
| 12 | Review: keep the pages that brought sign-ups, rewrite or drop the rest | all |

Each post: a headline with the language and job, a real example, the price, who reviews it, a short FAQ, one button ("Transcribe your first file free"), links to the matching page.
The middle and end calls to action are added automatically and tagged `utm_source=blog&utm_medium=cta&utm_campaign=<post>`.

## In parallel (SEO takes 3 to 6 months)

- Send a free human-reviewed sample to 30 target prospects (creators, researchers, agencies). Talk to everyone who replies. Record what they say.
- Join 3 to 5 communities where people ask for transcription help. Answer with real help, not links.
- Find 2 or 3 partners (video editors, translation agencies, podcast producers) who can refer customers.

## Weekly review (every Monday, 20 minutes)

Impressions, clicks, ranking pages (Search Console). Sign-ups by source, first uploads, payments (Site analytics, real customers only). Double down on pages that bring sign-ups.

## Targets

Visitor to sign-up 2.5% by day 30, 3.5% by day 60, 4 to 5% by day 90. First upload to first payment 15% by day 30. Paying customers 5 / 10 / 20 at days 30 / 60 / 90.

## Not built, on purpose

- Percentage discounts, coupons, "2 months free" annual offers and discounted minute packs (the standing no-discount rule). The plan document suggests them; revisit only if you change the rule.
- Pages for languages with no search impressions yet.
