# Module map

Every Python module, with the first sentence of its own description. This page is **generated** by
`scripts/generate_module_map.py` from the modules' docstrings. Do not edit it by hand: improve the module's
docstring instead, then run the script.

## The website and its background work (`webapp/`)

| Module | Lines | What it is for |
|---|---:|---|
| `account_closure.py` | 144 | Closing an account, done respectfully: one self-serve page (no call or email needed), a one-question survey that is optional, an honest alternative for whatever the reason is, a plain "what happens ne... |
| `account_hub.py` | 1389 | The customer-account tools beyond orders: AI Notes, Analytics, the team Workspace, Custom vocabulary, Plans, Rewards (referrals), Integrations, the help Assistant with hand-off to the team, and the st... |
| `account_security.py` | 113 | The account-security pages: where you are signed in, and confirming it is you before something sensitive |
| `app.py` | 12455 | The Kauli website and portals: every page and API route, and the wiring of all the other modules |
| `assistant_core.py` | 225 | The Kauli assistant's brain: what it knows, what it will not talk about, when a person has to step in, and the one inbox it feeds |
| `billing.py` | 1046 | Pricing, currency conversion, and payment provider integrations |
| `blog_ai_assist.py` | 107 | AI-assisted blog drafting for staff - the honest, human-reviewed version of the "Research Specialist / Content Writer / SEO Optimizer / Social Media Manager" pipeline a marketing doc asked for |
| `blog_audio.py` | 471 | Listenable blog posts: a narrated audio version of each post, a podcast feed for Apple Podcasts, Spotify and the rest, and the staff page that makes them |
| `blog_guides.py` | 443 | Five guides written for the audiences the solution pages serve: podcasters with a website, teams that record meetings and interviews, media and broadcast partners, course creators, and NGOs and civil ... |
| `blog_seed.py` | 870 | Blog posts written for US/global search intent (transcription, subtitles, translation) - the counterpart to the existing Kenya/Swahili-focused posts |
| `brand.py` | 48 | The Kauli name and logo as each reader sees them |
| `calendar_auto.py` | 160 | Kauli joins your meetings on its own: connect Google Calendar or Outlook with one click, and the notetaker is sent to every upcoming Zoom, Google Meet or Microsoft Teams call on that calendar, the way... |
| `calendar_feed.py` | 258 | Connect a Google or Outlook calendar with its private calendar link, see the next meetings that have a video link, and send Kauli to one |
| `charts.py` | 122 | Real inline SVG bar charts for staff pages - CRM (staff_leads.html) and Reports (staff_ops.html) - following the dataviz skill loaded for this work: horizontal bars (<=24px thick, 4px rounded ends), d... |
| `check_youtube.py` | 45 | Checks whether this server can fetch a YouTube link right now, and says which mitigations are configured |
| `cloud_import.py` | 193 | Import a recording straight from Google Drive, OneDrive or Dropbox |
| `connections.py` | 633 | One-click integrations: Connect, sign in on the provider's own page, "Connection successful", then a few switches |
| `db.py` | 5755 | The database: every table, its migrations, and every read and write the site makes (SQLite) |
| `demo_content.py` | 300 | Content for the interactive "Hear Kauli" demo (/try) |
| `devto_publish.py` | 67 | Cross-post a Kauli blog post to DEV.to (dev.to, the Forem platform), with canonical_url pointing back to the real Kauli URL - same backlink mechanism as medium_publish.py, on a platform that's actuall... |
| `diagnostics.py` | 174 | Diagnostics: find out quickly what went wrong, and where |
| `diagnostics_routes.py` | 105 | The routes behind diagnostics and the engineering guide |
| `dunning.py` | 189 | When a subscription payment fails (low funds, expired card, a bank hiccup) the customer has not chosen to leave, so we do not treat them as if they had |
| `engagement.py` | 248 | Helping every customer, new or long-standing, get more out of Kauli: the account monitoring, pointed at ALL clients |
| `engineering_docs.py` | 42 | The engineering guide that staff read at /staff/engineering |
| `funnel_audit.py` | 413 | Sign-up funnel audit: who signs up, how far they get, where they stop, and what staff can do to help |
| `ga_events.py` | 97 | Server-side GA4 event tracking via Measurement Protocol |
| `ga_reports.py` | 219 | Reads this site's Google Analytics 4 numbers through the GA4 Data API so staff can see how the website is doing inside Kauli (Staff > Site analytics), next to what Kauli itself knows (orders, payments... |
| `human_review_content.py` | 140 | Content for the human-review pages: /human-review and one page per language |
| `ip_intel.py` | 99 | Real, free datacenter/hosting-IP detection - a legitimate, honest signal that a signup or login is coming from a cloud server rather than a residential connection, without a paid IP-reputation API/Red... |
| `js_strings.py` | 67 | The words the page's scripts show, in the reader's language |
| `late_routes.py` | 19 | Route modules that must be registered last, in this order, so that app.py does not need a line for each |
| `lead_playbook.py` | 527 | Real, ready-to-use outreach content for the CRM's bulk-researched leads (NGOs, media houses, and future categories) - staff_lead_detail.html shows whichever stage matches the lead's own real pipeline ... |
| `legacy_paths.py` | 44 | Old addresses that still work: they send the visitor to the new one |
| `lifecycle.py` | 421 | The messages that follow a sale, each sent from the person a customer would expect to hear from: |
| `locales.py` | 553 | Translated marketing-landing copy for the localized pages (/es, /zh, /ar ...) |
| `locations.py` | 57 | The two offices, each with a page of its own (/locations/nairobi, /locations/dallas) |
| `logging_setup.py` | 53 | Structured (JSON-lines) logging - the doc this was built against asks for a centralized aggregator (Datadog/New Relic/CloudWatch), which needs a real account/subscription this local prototype doesn't ... |
| `mailer.py` | 248 | Real outbound transactional email via Brevo's HTTP API - no SDK needed, just httpx (already a dependency, same one billing.py uses for Paystack) |
| `mcp_server.py` | 146 | Kauli's remote MCP server (Streamable HTTP, JSON responses only) |
| `medium_publish.py` | 81 | Cross-post a Kauli blog post to Medium, with a canonical link back to the real Kauli URL - that backlink is the actual SEO/referral point of doing this at all (a reader on Medium who clicks through, a... |
| `meeting_bot.py` | 173 | Kauli in your meetings: a bot joins a Zoom, Google Meet or Microsoft Teams call, records it, and the recording becomes an ordinary Instant AI order (transcript, preview player, downloads) when the mee... |
| `meeting_notes.py` | 86 | When a meeting Kauli attended has become a transcript, write its notes (summary, decisions, action items) without anyone asking, and, if the customer switched it on for their calendar, email those not... |
| `model_stats.py` | 229 | How the AI providers (including our own models) and the human reviewers are actually doing, for staff |
| `nav_icons.py` | 92 | Real inline SVG glyphs for the sidebar nav, replacing the old single- letter icon boxes ("O", "Q", "R"...) that read as unfinished/placeholder rather than a deliberate choice - see the Sept 2026 dashb... |
| `notifications.py` | 32 | Shared staff-notification helper - used by both app.py (order approvals, exception requests, new leads) and worker.py (background job outcomes: ready for review, dead-lettered), which can't import fro... |
| `notion_export.py` | 141 | Send a finished transcript to a customer's own Notion workspace |
| `onboarding.py` | 195 | Getting a new customer's first order ready with as little friction as possible, while keeping the platform safe |
| `order_ai_assist.py` | 164 | AI summary and Q&A for a client's own delivered order - a real Claude call grounded in that order's own final, human-reviewed transcript text, not a feature "pulled from Transkriptor": Transkriptor's ... |
| `page_translate.py` | 401 | Every page in the visitor's own language, not just the screens we translated by hand |
| `paystack_setup.py` | 132 | Create (or find) Kauli's monthly and yearly plans on Paystack and print the environment lines that switch them on |
| `podcast.py` | 503 | The Podcast page (Resources > Podcast): play our shows and read their transcripts, and the staff tools that feed it |
| `promote.py` | 42 | Change a user's role after they've already signed up |
| `provider_health.py` | 537 | Which outside provider an order failed on, what else could do the job, and what each provider has left |
| `r2_uploads.py` | 120 | Direct-to-storage client uploads via Cloudflare R2, presigned URLs |
| `rate_limit.py` | 60 | Rate limiting - the doc calls for a Redis-backed token bucket so limits survive across multiple app instances; this is a single-process app, so an in-memory bucket gives the exact same protection (bou... |
| `repurpose.py` | 161 | Do more with a file the customer already sent: translate it, dub it or have a person check it, without uploading it again |
| `savings.py` | 38 | Tell a pay-as-you-go customer plainly when a monthly plan would have cost them less |
| `seo_languages.py` | 113 | Per-language facts for the English 'X audio to text' pages and the languages hub - the multilingual-SEO side of the site |
| `seo_pages.py` | 233 | Pages for the searches where a visitor already needs what Kauli sells: a language plus a job ("Spanish interview transcription") |
| `sessions.py` | 175 | Where you are signed in: a list of devices, and the power to sign any of them out |
| `site_analytics.py` | 742 | Kauli's own analytics, alongside Google Analytics: a cookie-free visit counter, and the engines that turn visits, orders and payments into the numbers on the staff Site analytics tab (traffic, audienc... |
| `solution_pages.py` | 794 | The /solutions/<slug> pages: what each service is, who it is for, how to choose between its options, and where to read or hear more |
| `staff_integrations.py` | 198 | Integrations for staff accounts: each staff member can have their alerts sent to their own Slack channel, Microsoft Teams channel or webhook |
| `sudo_mode.py` | 87 | Confirm it is you before something sensitive: re-enter your password, or use a code we email you |
| `summary_templates.py` | 222 | The summary formats a customer can choose from for a finished transcript |
| `supabase_auth.py` | 414 | Thin wrapper around supabase-py for email/password auth |
| `tat.py` | 138 | Turnaround-time (deadline) calculation - a real, deterministic formula from known variables (plan tier, service level, audio duration), not a guess and not an AI estimate |
| `transcript_export.py` | 174 | Plain-text and Word (.docx) transcript exports, built on demand from a finished job's segments - the formats people expect from a transcription service, next to the existing SRT |
| `transcript_reuse.py` | 123 | Nothing is transcribed twice |
| `ui_text.py` | 1836 | The words of the screens a client lives in, in the ten languages Kauli's translated site speaks |
| `upload_security.py` | 493 | File upload security: everything a raw client upload has to pass before Kauli treats it as legitimate media |
| `usage_prompts.py` | 83 | A kind word when someone has used half, and then most, of this month's free minutes |
| `web_security.py` | 95 | Request-level protections that sit in front of every route |
| `weekly_kpi.py` | 124 | The Monday email: the numbers that say whether the business is working, for real customers only, next to the day-30 targets from the growth plan |
| `winback.py` | 281 | Winning back customers who paid and then went quiet: a unique coupon code for one free month of their subscription, offered once |
| `worker.py` | 314 | Runs the real kauli pipeline in a background thread and syncs order status |
| `youtube_captions.py` | 147 | Push a real, generated caption file straight onto a client's own YouTube video - webapp/app.py's /account/youtube/connect/callback/disconnect and /account/orders/{id}/push-captions-youtube routes |
| `youtube_poll.py` | 110 | Read-only YouTube channel/playlist polling - the "detect new uploads for you" half of automation, not the "process and bill automatically" half |

## The processing pipeline: transcription, translation, voices (`kauli/`)

| Module | Lines | What it is for |
|---|---:|---|
| `caption_formats.py` | 130 | More ways to deliver the same captions (kauli/subtitles.py decides what the captions are; this only writes them out): |
| `cli.py` | 93 | Run the processing pipeline from the command line, without the website: `python -m kauli.cli run ...` |
| `mixer.py` | 150 | Lay rendered segments onto a single timeline |
| `models.py` | 348 | Core data model |
| `pipeline.py` | 914 | End-to-end orchestration: audio in, dubbed audio + subtitles + manifest out |
| `providers/asr.py` | 571 | ASR providers |
| `providers/asr_cloud.py` | 224 | Low-cost cloud transcription: Groq (hosted Whisper) and AssemblyAI, plus the chain that tries providers in order |
| `providers/elevenlabs_audio.py` | 165 | ElevenLabs audio services beyond voices, over their REST API (no SDK): |
| `providers/mt.py` | 633 | Translation providers |
| `providers/own_quality.py` | 58 | Where our own models are good enough to be used first, from measurement rather than hope |
| `providers/tts.py` | 465 | TTS providers |
| `screen_text.py` | 181 | Keeping captions clear of text that is already on the screen (lower-thirds, speaker names, titles, graphics) |
| `speaker_gender.py` | 143 | A real, if coarse, default for "who should get which dub voice" - autocorrelation pitch (F0) estimation over a speaker's own audio, classified male/female by a standard crossover threshold, then mappe... |
| `subtitles.py` | 217 | SRT / WebVTT writers |
| `timing.py` | 106 | Duration fitting |

## Command-line tools (`scripts/`)

| Module | Lines | What it is for |
|---|---:|---|
| `check_whitespace.py` | 33 | Fails when a file has trailing whitespace or no final newline |
| `eval_asr.py` | 72 | Measure transcription providers on the same recordings, so we know where our own model is good enough and where an outside provider is still needed |
| `eval_languages.py` | 203 | Measure our own models, language by language, against real recordings and human translations (Google's FLEURS: the same sentences read aloud and translated by people in about 100 languages) |
| `export_model_quality.py` | 35 | Turn the language evaluation (scripts/eval_languages.py -> results.jsonl) into kauli/providers/own_model_quality.json, the file that decides where our own models go first |
| `generate_module_map.py` | 73 | Writes the module map page of the engineering guide from the modules' own docstrings |
| `make_demo_audio.py` | 119 | Render the audio for the /try demo with ElevenLabs and save it under webapp/static/demo/ |
| `make_demo_music.py` | 95 | Background music for the demo videos, composed by ElevenLabs Music (instrumental, no vocals), so it sounds like a real track rather than a synthesizer loop |
| `make_demo_videos.py` | 440 | Render the demo videos shown on /try: people on screen who speak with the pre-rendered ElevenLabs voices, with the captions burned into the picture |
| `make_podcast_episode.py` | 345 | Turn a podcast script (marketing/podcast/episode-NN-script.md, pick it with --episode NN) into a finished episode, all on ElevenLabs: |
| `make_podcast_youtube.py` | 393 | The YouTube version of a podcast episode: a 1080p video, a thumbnail, channel art and captions |
| `make_promo_music.py` | 173 | Original music for the Kauli promo video, synthesized here so there is nothing to license |
| `make_promo_video.py` | 509 | Builds the Kauli promo video ("Type it? Never again.", about 75 seconds, 1920x1080) from the script in marketing/kauli-promo-script.md |
| `make_youtube_demos.py` | 206 | Makes the YouTube versions of the interactive demos: for each use case, the English original upscaled to 1080p with a short title card and an end card, plus a 1280x720 thumbnail and the title and desc... |
| `prepare_madlad.py` | 29 | Fetch Google's MADLAD-400 3B translation model (Apache-2.0) and convert it to a compact CTranslate2 int8 folder that Kauli can run on the server's CPU (kauli.providers.mt.MadladMT) |
| `quality_gate.py` | 190 | The quality gate: new code must meet the standard, and old code may not get worse |
| `reset_test_account.py` | 114 | Make one customer account brand new again, for testing sign-up, the free trial and payments |
