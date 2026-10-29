"""The /solutions/<slug> pages: what each service is, who it is for, how to choose between its options, and where to read
or hear more.

Written for the current positioning: Instant AI transcription and translation in many languages, AI dubbing in selected
languages, and human review in selected languages for up to 99% accuracy. Page titles never name a language pair; the
languages we work in are listed on /languages and /human-review, and the sentences below take them from the live
configuration so they cannot drift.

Every page has the same shape, so a visitor can find what fits them:
  points      why Kauli for this job
  choose      the options on offer for this job and who each is for
  demo        which interactive demo (/try?use=...) shows it
  guides      blog slugs to read next (only published posts are shown)
  fit_guide   which plan fits
"""
from __future__ import annotations

QUALITY = "Human-reviewed orders reach up to 99% accuracy"


# The demo (/try?use=...) that shows each service best: a scenario with the same kind of people and content.
DEMO_FOR = {
    "ngos": "field", "youtubers": "video", "media-broadcast": "news", "e-learning": "course", "voice-over": "video",
    "captioning": "news", "video-translation": "video", "video-transcription": "meeting", "video-dubbing": "video",
    "video-localization": "video", "audio-to-text": "meeting", "youtube-video-to-text": "video",
    "podcast-transcription": "podcast", "interview-transcription": "interview", "call-centers": "call",
}


def build(billing, hr_names: str, dub_names: str) -> dict:
    free = billing.FREE_MINUTES_PER_MONTH
    pro, prem = billing.PLANS["pro"], billing.PLANS["premium"]
    video_rate = billing.ADDONS["video_deliverables"]["rate_per_min"]

    def plans(for_free, for_pro, for_prem, for_ent):
        return [
            {"plan": "Free", "for": for_free,
             "detail": f"{free:.0f} Instant AI transcription minutes free every month, then pay per minute."},
            {"plan": "Pro", "for": for_pro,
             "detail": f"${pro['price_usd']}/month: {pro['included_ai_minutes']:.0f} Instant AI minutes every month for "
                       f"transcription and translation."},
            {"plan": "Premium", "for": for_prem,
             "detail": f"${prem['price_usd']}/month: {prem['included_ai_minutes']:,.0f} Instant AI minutes{billing.hours_suffix(prem['included_ai_minutes'])} every month for "
                       f"transcription and translation, burned-in captions "
                       "and dubbed video included."},
            {"plan": "Enterprise", "for": for_ent,
             "detail": "Negotiated volume rate, invoicing, a dedicated account manager and faster turnaround."},
        ]

    pricing = {"title": "Transparent per-minute pricing",
               "body": f"Pay for what you process, or commit to a plan with minutes included. Prices are per audio-minute and "
                       f"shown before you order, and your first {free:.0f} minutes every month are free."}

    instant = {"name": "Instant AI", "best": "Drafts, internal use, anything you will review yourself.",
               "get": "Ready in minutes and machine-generated, and labelled that way. Transcription in 70+ languages, translation between them."}
    human = {"name": "Human-reviewed", "best": "Anything you will publish, air, submit or quote.",
             "get": f"A dedicated editor checks the work against your audio, and {QUALITY[0].lower()}{QUALITY[1:]}. Available in {hr_names}."}

    pages = {
        # ------------------------------------------------------------------ translation
        "video-translation": {
            "title": "AI Video Translation and Subtitles, Human-Reviewed on Request",
            "meta_description": "Translate videos and audio into 70+ languages in minutes with Instant AI, or add a dedicated editor for up to 99% accuracy. Transcript, subtitles or a dub. Per-minute pricing.",
            "kicker": "Video and audio translation",
            "h1": "Translate your videos for every audience",
            "intro": "Upload a file or paste a link and get a translated transcript, subtitles or a dubbed track back. Instant AI "
                     "translates between more than 70 languages in minutes, and a human review pass takes the result up to 99% accuracy "
                     "when it has to be right.",
            "why_heading": "Built for translation you can actually publish",
            "points": [
                {"title": "Translate between 70+ languages",
                 "body": "Transcribe the speech and translate it in one order. Instant AI is machine translation and is labelled "
                         "as not reviewed by a person."},
                {"title": "Human review for up to 99% accuracy",
                 "body": f"On a Human-reviewed order a dedicated editor checks every line against the source audio. Available in {hr_names}, with more languages coming soon."},
                {"title": "Transcript, subtitles or a full dub",
                 "body": f"Pick the deliverable your video needs: a translated transcript to read, SRT or VTT subtitles to upload, or a dubbed track (dubbing is available in {dub_names})."},
                {"title": "Minutes included on every paid plan",
                 "body": f"Pro and Premium include {pro['included_ai_minutes']:.0f} and {prem['included_ai_minutes']:,.0f}{billing.hours_suffix(prem['included_ai_minutes'])} Instant AI minutes a month, and they cover translation as well as transcription."},
            ],
            "choose": [
                {**instant, "name": "Instant AI translation", "best": "Understanding a video, internal sharing, first drafts of subtitles."},
                {**human, "name": "Human-reviewed translation", "best": "Subtitles and transcripts that will be published."},
                {"name": "Translation plus a dub", "best": "Viewers who would rather listen than read.",
                 "get": f"The translated script voiced in a new language. Dubbing is available in {dub_names}; pick a premium AI voice for the most natural result."},
            ],
            "demo": "video",
            "guides": ["add-subtitles-to-video-in-another-language", "translate-video-chinese-spanish-to-english",
                       "ai-vs-human-transcription-which-do-you-need"],
            "cta_heading": "Translate your next video",
            "cta_body": "Upload a real clip or paste a YouTube link. Your first minutes are free.",
            "related": ["captioning", "video-dubbing", "video-localization"],
            "tour": {"mode": "upload", "filename": "interview_clip.mp4"},
            "faq": [
                {"q": "How many languages can you translate between?",
                 "a": "Instant AI translates between more than 70 languages. The full list is on the languages page, and each language has a page of its own."},
                {"q": "How accurate is the translation?",
                 "a": "Instant AI is machine translation, so it is fast and good for understanding, and it is labelled as not reviewed by a person. "
                      f"{QUALITY} because a dedicated editor checks the work against the audio."},
                {"q": "Which languages have human review?",
                 "a": f"Human review is available in {hr_names}, and more languages are coming soon. The human review page shows what is available now."},
                {"q": "Can I get subtitles instead of a dub?",
                 "a": "Yes. Choose transcription plus translation and download SRT or VTT files, or connect your YouTube channel to publish captions."},
                {"q": "What does it cost?",
                 "a": f"Instant AI translation is priced per audio-minute and the price is shown before you confirm. The first {free:.0f} minutes every month are free, and Pro and Premium include monthly minutes that cover translation."},
            ],
            "fit_guide": plans("Trying a translation on one clip.", "Regular translation work, such as a weekly video.",
                               "Subtitled or dubbed video files ready to publish.", "Teams translating at volume, needing invoicing."),
        },
        # ------------------------------------------------------------------ captions
        "captioning": {
            "title": "Subtitles and Closed Captions for Any Video, in 70+ Languages",
            "meta_description": "Generate subtitles and closed captions for YouTube or any video in minutes. SRT and VTT files, burned-in captions, 70+ languages, and human review for up to 99% accuracy.",
            "kicker": "Subtitles and closed captions",
            "h1": "Subtitles and captions for every video you publish",
            "intro": "Get timestamped subtitles in minutes, as a separate SRT or VTT file or burned into the video. Add human review "
                     "when accuracy matters, for up to 99% accurate captions.",
            "why_heading": "Built for captions people rely on",
            "points": [
                {"title": "SRT, VTT or burned-in",
                 "body": f"Download a subtitle file for your player, or have captions burned into the video (included on Premium, or ${video_rate:.2f}/minute on any plan)."},
                {"title": "Up to 99% accurate with human review",
                 "body": f"An editor checks every line against the audio before the order is marked ready. Available in {hr_names}."},
                {"title": "70+ languages and translated captions",
                 "body": "Transcribe in the language spoken and translate the captions into other languages in the same order."},
                {"title": "Publish straight to YouTube",
                 "body": "Connect your channel and push finished captions to the video, or paste a link instead of uploading a file."},
            ],
            "choose": [
                {**instant, "name": "Instant AI captions", "best": "Social clips, internal training, fast turnaround on volume."},
                {**human, "name": "Human-reviewed captions", "best": "Accessibility requirements, broadcast, courses and anything with names and numbers."},
                {"name": "Burned-in captions", "best": "Platforms that do not support caption files.",
                 "get": "The captions rendered into the picture, delivered as a video file."},
            ],
            "demo": "video",
            "guides": ["srt-vs-vtt-which-subtitle-format-to-use", "captions-vs-subtitles-vs-transcripts", "add-subtitles-to-video-in-another-language"],
            "cta_heading": "Caption your next video",
            "cta_body": "Upload a clip or paste a link and see the captions for yourself.",
            "related": ["video-transcription", "video-translation", "voice-over"],
            "tour": {"mode": "upload", "filename": "webinar_recording.mp4"},
            "faq": [
                {"q": "Which subtitle formats do you deliver?",
                 "a": "SRT and VTT subtitle files, a Word or text transcript, and burned-in captions as a video file."},
                {"q": "Are the captions accurate enough for accessibility?",
                 "a": f"Instant AI captions are a fast first draft and are labelled as machine-generated. {QUALITY}, so choose human review for captions that must be right."},
                {"q": "Can I caption a YouTube video without downloading it?",
                 "a": "Yes. Paste the link and we fetch the audio ourselves, then you can push the captions back to YouTube once your channel is connected."},
                {"q": "Can I get captions in another language?",
                 "a": "Yes. Choose transcription plus translation and the captions come back in the language you pick."},
            ],
            "fit_guide": plans("Captioning an occasional video.", "A steady stream of videos each month.",
                               "Burned-in captions and dubbed video files included.", "A team or network captioning at volume."),
        },
        # ------------------------------------------------------------------ dubbing
        "video-dubbing": {
            "title": "AI Video Dubbing with Natural Voices, Reviewed Before It Ships",
            "meta_description": "Dub a video into a new language with AI voices in minutes. Choose a premium voice, add human review for up to 99% accurate scripts, and get a dubbed video file.",
            "kicker": "Video dubbing",
            "h1": "Dub your videos into new languages",
            "intro": "Kauli transcribes your video, translates the script and voices it in a new language, then hands back a dubbed audio "
                     "track or a finished video. Add human review to check the script before it is voiced.",
            "why_heading": "Built for dubs people will actually watch",
            "points": [
                {"title": "A script you can trust",
                 "body": f"Add human review and a dedicated editor checks the transcript and translation against the audio first, for up to 99% accuracy. Available in {hr_names}."},
                {"title": "Natural voices",
                 "body": f"Dubbing is available in {dub_names}, with more languages coming soon. Choose a premium AI voice for the most natural result, or a human voice actor as an add-on."},
                {"title": "A dubbed video file, not just audio",
                 "body": f"Download the dubbed audio, or a finished video with burned-in captions (included on Premium, or ${video_rate:.2f}/minute on any plan)."},
                {"title": "Consent before any voice is cloned",
                 "body": "If a dub must sound like the original speaker we require your confirmation of rights and consent first. It is enforced on the server, never assumed."},
            ],
            "choose": [
                {**instant, "name": "Instant AI dub", "best": "Internal videos, explainers and fast tests.",
                 "get": "Transcription, translation and a synthesized voice in one order, ready in minutes."},
                {"name": "Premium AI voice", "best": "Public videos where voice quality is noticed.",
                 "get": "A more natural AI voice as an add-on on a dub."},
                {**human, "name": "Human-reviewed dub", "best": "Anything customers, students or viewers will watch.",
                 "get": f"The script is checked by an editor before it is voiced. {QUALITY}."},
            ],
            "demo": "video",
            "guides": ["translate-video-chinese-spanish-to-english", "ai-vs-human-transcription-which-do-you-need", "translate-youtube-titles-descriptions-and-captions"],
            "cta_heading": "Hear a dub of your own video",
            "cta_body": "Upload a short clip and listen to the result. Your first minutes are free.",
            "related": ["voice-over", "captioning", "video-localization"],
            "tour": {"mode": "upload", "filename": "training_video.mp4"},
            "faq": [
                {"q": "Which languages can you dub into?",
                 "a": f"Dubbing is available in {dub_names}, and more languages are coming soon. Translation and transcription cover 70+ languages."},
                {"q": "Is a person involved?",
                 "a": f"Not on Instant AI, which is machine-generated and labelled that way. On a Human-reviewed order an editor checks the script first. {QUALITY}."},
                {"q": "Can the dub sound like the original speaker?",
                 "a": "Only with the speaker's consent. We ask you to confirm you have the rights before voice cloning runs."},
                {"q": "Do I get a video file or only audio?",
                 "a": f"Both are possible. Audio and subtitles are included on every plan, and a dubbed video with burned-in captions is included on Premium or ${video_rate:.2f}/minute on any plan."},
            ],
            "fit_guide": plans("Hearing what a dub sounds like.", "Dubbing a few videos a month.",
                               "Finished dubbed video files every month.", "A catalogue to dub, with managed billing and invoicing and a dedicated account manager."),
        },
        # ------------------------------------------------------------------ voice over
        "voice-over": {
            "title": "AI Voice Over Services, or a Real Human Voice Actor",
            "meta_description": "Add voice over to any video: natural AI voices in minutes or a human voice actor as an add-on, with consent required before any voice is cloned and human review for up to 99% accurate scripts.",
            "kicker": "Voice over services",
            "h1": "Voice over for your videos, AI-fast or human",
            "intro": "Every dub starts as an AI voice over. Choose a premium voice for a more natural sound, or add a real voice actor when "
                     "you need a specific tone. A human review pass checks the script before it is voiced.",
            "why_heading": "Built for voice over, not just text to speech",
            "points": [
                {"title": "AI voice over in minutes",
                 "body": f"Pick a language and get a voiced track back. Voice over is available in {dub_names}, with more languages coming soon."},
                {"title": "A real voice actor as an add-on",
                 "body": "When a project needs a human performance, add a voice actor to the order. It is an add-on, not a different product."},
                {"title": "A checked script",
                 "body": f"Human review checks the script against the audio first, for up to 99% accuracy. Available in {hr_names}."},
                {"title": "Consent before any voice is cloned",
                 "body": "Voice cloning needs your confirmation of rights and consent. It is enforced on the server."},
            ],
            "choose": [
                {"name": "Standard AI voice", "best": "Internal videos, drafts and quick explainers.", "get": "Included in any dub."},
                {"name": "Premium AI voice", "best": "Public videos and ads.", "get": "A more natural voice, as an add-on."},
                {"name": "Human voice actor", "best": "Brand work and anything needing a specific tone.", "get": "A real performance, as an add-on on a human-reviewed dub."},
            ],
            "demo": "video",
            "guides": ["translate-video-chinese-spanish-to-english", "ai-vs-human-transcription-which-do-you-need"],
            "cta_heading": "Hear the voices on your own script",
            "cta_body": "Upload a clip and listen. Your first minutes are free.",
            "related": ["video-dubbing", "captioning", "video-localization"],
            "tour": {"mode": "upload", "filename": "product_explainer_voiceover.mp4"},
            "faq": [
                {"q": "Is the voice AI or a person?",
                 "a": "AI by default, and clearly labelled. A human voice actor is available as an add-on."},
                {"q": "Can you clone my voice?",
                 "a": "Only with explicit consent and confirmation of rights, checked before any cloning runs."},
                {"q": "Which languages are available?",
                 "a": f"Voice over is available in {dub_names}, and more languages are coming soon."},
            ],
            "fit_guide": plans("Hearing a voice on one clip.", "A few voiced videos a month.",
                               "Voiced video files delivered ready to publish.", "A studio or agency voicing at volume."),
        },
        # ------------------------------------------------------------------ localization
        "video-localization": {
            "title": "Video Localization: Transcribe, Translate, Caption and Dub",
            "meta_description": "Localize a video in one order: transcript, translation, subtitles or a dub. 70+ languages with Instant AI, human review for up to 99% accuracy, one team and one invoice.",
            "kicker": "Video localization",
            "h1": "One order from source video to every audience",
            "intro": "Localizing a video normally means a transcription vendor, a translation vendor and a dubbing studio. Kauli does each "
                     "step in one place, with Instant AI for speed and a human review pass where accuracy matters.",
            "why_heading": "Every step, one team",
            "points": [
                {"title": "Transcribe, translate, caption, dub",
                 "body": "Choose what the video needs or combine the steps: a translated transcript, subtitles, or a fully dubbed track with captions."},
                {"title": "Up to 99% accurate with human review",
                 "body": f"A dedicated editor checks each step against the source audio. Available in {hr_names}, with more languages coming soon."},
                {"title": "70+ languages",
                 "body": f"Instant AI transcribes and translates 70+ languages. Dubbing is available in {dub_names}."},
                pricing,
            ],
            "choose": [
                {"name": "Transcript only", "best": "Searchable archives, notes and research.", "get": "Timestamped text with speakers, in Word, text or SRT."},
                {"name": "Subtitles in new languages", "best": "Reaching viewers who prefer to read.", "get": "Translated SRT or VTT files, or captions burned into the video."},
                {"name": "Full dub", "best": "Reaching viewers who prefer to listen.", "get": f"Translated, voiced and delivered as audio or video. Available in {dub_names}."},
            ],
            "demo": "video",
            "guides": ["add-subtitles-to-video-in-another-language", "translate-youtube-titles-descriptions-and-captions",
                       "course-creators-accurate-transcripts-and-fast-localization"],
            "cta_heading": "Localize one video and compare",
            "cta_body": "Start with a short clip. Your first minutes are free.",
            "related": ["video-translation", "video-dubbing", "captioning"],
            "tour": {"mode": "upload", "filename": "documentary_segment.mp4"},
            "faq": [
                {"q": "Can I order transcription, translation and a dub together?",
                 "a": "Yes. Choose the full service level and the whole pipeline runs from one order."},
                {"q": "How do I keep terminology consistent?",
                 "a": "Add notes to the order, or upload a style guide, and the editor follows them on human-reviewed orders."},
                {"q": "Can my team share an account?",
                 "a": "Yes. Add team members to your account and they work on the same orders and billing."},
            ],
            "fit_guide": plans("Localizing one video to compare.", "A regular localization workload.",
                               "Subtitled and dubbed video files as standard.", "A localization programme with invoicing."),
        },
        # ------------------------------------------------------------------ transcription
        "video-transcription": {
            "title": "Video and Audio Transcription in 70+ Languages, Up to 99% Accurate",
            "meta_description": "Transcribe video and audio to timestamped text with speaker labels in minutes. Instant AI in 70+ languages, or human review for up to 99% accuracy. Word, text and SRT downloads.",
            "kicker": "Transcription services",
            "h1": "Accurate transcripts of every video and recording",
            "intro": "Get a timestamped transcript in minutes, or add a human review pass for up to 99% accuracy. Download Word, text, SRT "
                     "or VTT, with speaker labels where you want them.",
            "why_heading": "Built for transcripts you can use",
            "points": [
                {"title": "Minutes, not days",
                 "body": "Instant AI returns a draft as soon as processing finishes, in 70+ languages."},
                {"title": "Up to 99% accurate with human review",
                 "body": f"A dedicated editor checks the draft against the audio. Available in {hr_names}."},
                {"title": "Clean read or verbatim",
                 "body": "Pick the style: a tidy read for publishing, or a verbatim record that keeps every false start."},
                {"title": "Speakers, timestamps, exports",
                 "body": "Jump to any moment, label speakers, and download Word, plain text, SRT or VTT."},
            ],
            "choose": [
                {**instant, "name": "Instant AI transcript", "best": "Notes, search, first drafts and volume."},
                {**human, "name": "Human-reviewed transcript", "best": "Legal, research, publishing and anything quoted."},
                {"name": "Add-ons", "best": "Noisy audio or many speakers.",
                 "get": "Audio clean-up before transcription and speaker identification."},
            ],
            "demo": "meeting",
            "guides": ["ai-vs-human-transcription-which-do-you-need", "verbatim-vs-clean-read-transcription", "how-does-ai-transcription-work"],
            "cta_heading": "Transcribe a real recording",
            "cta_body": "Upload a file or paste a link. Your first minutes are free.",
            "related": ["captioning", "video-translation", "interview-transcription"],
            "tour": {"mode": "upload", "filename": "deposition_recording.mp4"},
            "faq": [
                {"q": "How accurate is it?",
                 "a": f"Instant AI depends on audio quality, accents and background noise, and is labelled as machine-generated. {QUALITY}."},
                {"q": "Which languages are supported?",
                 "a": "Instant AI transcribes 70+ languages. Human review is available in selected languages, and the list is growing."},
                {"q": "Can I get the text in Word?",
                 "a": "Yes: Word, plain text, SRT and VTT downloads on every plan."},
                {"q": "Is my recording private?",
                 "a": "Files are processed to produce your transcript and are only visible to your account and our staff. See the privacy policy for how files and providers are handled."},
            ],
            "fit_guide": plans("An occasional recording.", "Regular transcription each month.",
                               "Transcription and translation with video files included.", "Teams transcribing at volume."),
        },
        # ------------------------------------------------------------------ YouTube creators
        "youtubers": {
            "title": "Translate and Dub Your YouTube Videos for a Global Audience",
            "meta_description": "Paste a YouTube link and get a transcript, translated captions or a dubbed track. Reach viewers in new languages, push captions to your channel, and add human review for up to 99% accuracy.",
            "kicker": "For YouTubers and creators",
            "h1": "Translate your YouTube videos",
            "intro": "Paste a link and Kauli fetches the audio itself. Get a transcript, translated subtitles or a dubbed track back, and "
                     "publish captions straight to your channel.",
            "why_heading": "Built for how creators work",
            "points": [
                {"title": "Paste a link, not a file",
                 "body": "Give us a YouTube URL and we fetch the audio, so there is no download and re-upload."},
                {"title": "Captions pushed to your channel",
                 "body": "Connect your channel and publish finished captions to the video, in the languages you ordered."},
                {"title": "Auto-import for a whole channel",
                 "body": "New public uploads appear as one-click pending orders. Nothing runs or bills until you choose."},
                {"title": "Up to 99% accurate when it matters",
                 "body": f"Add human review for your best videos. Available in {hr_names}."},
            ],
            "choose": [
                {"name": "Captions only", "best": "More watch time from viewers who read along.", "get": "Subtitles in the spoken language and translated subtitles in others."},
                {"name": "Translated titles and descriptions", "best": "Being found in other languages.", "get": "A transcript you can translate and reuse for metadata."},
                {"name": "A dubbed track", "best": "Viewers who prefer to listen.", "get": f"Your video voiced in a new language. Available in {dub_names}."},
            ],
            "demo": "video",
            "guides": ["translate-youtube-titles-descriptions-and-captions", "how-to-transcribe-a-youtube-video", "add-subtitles-to-video-in-another-language"],
            "cta_heading": "Try it on your last upload",
            "cta_body": "Paste the link, or connect your channel for ongoing import.",
            "related": ["video-dubbing", "captioning", "voice-over"],
            "tour": {"mode": "link"},
            "faq": [
                {"q": "Do I have to download my video first?",
                 "a": "No. Paste the YouTube URL and we fetch the audio directly."},
                {"q": "Can I connect my whole channel?",
                 "a": "Yes. New public uploads show up as one-click pending orders, and nothing is processed or billed automatically."},
                {"q": "Is this better than YouTube's built-in auto-dubbing?",
                 "a": "YouTube's auto-dub is a free starting point for simple, single-speaker videos. Kauli adds a choice of voices, transcripts and subtitle files you own, and human review when you want accuracy."},
                {"q": "Do I get a dubbed video file?",
                 "a": f"Burned-in captions or a dubbed video file are included on Premium, or ${video_rate:.2f}/minute on any plan. Audio, transcript and subtitles are included on every plan."},
            ],
            "fit_guide": plans("Testing one upload.", "A weekly upload schedule.", "Burned captions or dubbed video files ready to re-upload.",
                               "A network or agency across many channels."),
        },
        # ------------------------------------------------------------------ media
        "media-broadcast": {
            "title": "Transcription, Subtitles and Dubbing for Media and Broadcast",
            "meta_description": "Fast, accurate transcripts, subtitles and dubs for newsrooms and broadcasters. Instant AI in minutes, human review for up to 99% accuracy, and an Enterprise tier for the fastest turnarounds.",
            "kicker": "For media and broadcast",
            "h1": "Broadcast-ready subtitles and dubs, on your deadline",
            "intro": "Newsrooms and broadcasters need text quickly and need it right. Instant AI turns footage into transcripts and subtitles "
                     "in minutes, human review takes it up to 99% accuracy, and an Enterprise partnership adds the fastest turnarounds.",
            "why_heading": "Built for real deadlines",
            "points": [
                {"title": "Minutes to a first draft",
                 "body": "Instant AI returns a transcript or subtitles when processing finishes, so editors can start while the story is live."},
                {"title": "A tracked delivery estimate",
                 "body": "Every human-reviewed order shows an estimated delivery time on your dashboard from the moment it is confirmed."},
                {"title": "Broadcast-ready files",
                 "body": f"SRT and VTT subtitles, burned-in captions and dubbed video files (included on Premium, or ${video_rate:.2f}/minute on any plan)."},
                {"title": "Enterprise turnaround and invoicing",
                 "body": "A dedicated faster tier, a dedicated account manager and managed billing and invoicing for organizations with an ongoing relationship."},
            ],
            "choose": [
                {**instant, "name": "Instant AI for live news", "best": "Monitoring, logging and first-pass subtitles under a tight clock."},
                {**human, "name": "Human review before air", "best": "Aired captions, interviews and archive that will be quoted."},
                {"name": "Enterprise partnership", "best": "Daily volume and the fastest turnarounds.", "get": "Negotiated rates, a dedicated account manager and faster turnaround. Talk to us."},
            ],
            "demo": "video",
            "guides": ["media-and-broadcast-partner-with-kauli-fast-turnarounds", "srt-vs-vtt-which-subtitle-format-to-use", "captions-vs-subtitles-vs-transcripts"],
            "cta_heading": "Talk to us about your next deadline",
            "cta_body": "Book a call, or start with a real clip to see the quality and turnaround.",
            "related": ["video-dubbing", "captioning", "voice-over"],
            "tour": {"mode": "upload", "filename": "evening_bulletin_segment.mp4"},
            "faq": [
                {"q": "Can you meet a same-day deadline?",
                 "a": "Instant AI delivers in minutes. Human-reviewed orders show a delivery estimate when confirmed, and Enterprise adds a faster dedicated tier. Tell us about your deadline."},
                {"q": "Do you provide burned-in captions?",
                 "a": f"Yes, as a video file. Included on Premium and Enterprise, or ${video_rate:.2f}/minute on any plan."},
                {"q": "Can we be invoiced?",
                 "a": "Yes. Invoicing is the standard on Enterprise, alongside a negotiated volume rate and a dedicated account manager."},
                {"q": "How accurate is human review?",
                 "a": f"{QUALITY}, because an editor checks the draft against the audio before delivery."},
            ],
            "fit_guide": plans("Piloting a segment.", "Regular weekly programming.", "Burned captions and dubbed video as standard.",
                               "Daily volume with fast turnaround and invoicing."),
        },
        # ------------------------------------------------------------------ e-learning
        "e-learning": {
            "title": "Course Transcripts and Localization for Creators and Training Teams",
            "meta_description": "Create accurate transcripts and subtitles for your courses and localize them into 70+ languages fast. Human review for up to 99% accuracy, minutes included on every plan.",
            "kicker": "For course creators and training teams",
            "h1": "Accurate course transcripts, localized fast",
            "intro": "Upload a lesson and get a transcript and subtitles in minutes, then translate them for every cohort. Human review keeps "
                     "terminology accurate, up to 99%, across every lesson.",
            "why_heading": "Built for courses and training",
            "points": [
                {"title": "A transcript for every lesson",
                 "body": "Searchable transcripts and downloadable subtitles make lessons accessible and easier to follow."},
                {"title": "Localize in minutes",
                 "body": f"Translate transcripts and subtitles into 70+ languages, and dub lessons in {dub_names}."},
                {"title": "Consistent terminology",
                 "body": f"Add notes or a style guide and a human editor follows it across lessons. Available in {hr_names}."},
                {"title": "Minutes included",
                 "body": f"Pro and Premium include {pro['included_ai_minutes']:.0f} and {prem['included_ai_minutes']:,.0f}{billing.hours_suffix(prem['included_ai_minutes'])} Instant AI minutes a month for transcription and translation."},
            ],
            "choose": [
                {"name": "Transcripts and subtitles", "best": "Accessibility and searchable lessons.", "get": "Word, text, SRT and VTT for every module."},
                {"name": "Translated subtitles", "best": "A new audience, fast and affordable.", "get": "Instant AI translation, with human review for the lessons that matter most."},
                {"name": "Dubbed lessons", "best": "Learners who prefer to listen.", "get": f"Voiced in a new language. Available in {dub_names}."},
            ],
            "demo": "course",
            "guides": ["course-creators-accurate-transcripts-and-fast-localization", "add-subtitles-to-video-in-another-language", "how-does-ai-transcription-work"],
            "cta_heading": "Run a lesson through Kauli",
            "cta_body": "Upload one module and see the transcript, subtitles and translation. Your first minutes are free.",
            "related": ["video-translation", "video-transcription", "video-localization"],
            "tour": {"mode": "upload", "filename": "module_3_lesson.mp4"},
            "faq": [
                {"q": "Can I localize an entire course?",
                 "a": "Yes. Upload each lesson, or connect a YouTube playlist, and choose the languages. Pro and Premium minutes cover transcription and translation."},
                {"q": "How do I keep terminology consistent?",
                 "a": "Add notes to the order or upload a style guide. On human-reviewed orders the editor follows it."},
                {"q": "Will the subtitles work on my course platform?",
                 "a": "SRT and VTT files work on most platforms. Burned-in captions are available as a video file."},
                {"q": "How accurate is it?",
                 "a": f"Instant AI is a fast draft and is labelled as machine-generated. {QUALITY}."},
            ],
            "fit_guide": plans("One lesson to see the quality.", "A course launching in a few languages.",
                               "A full course with subtitled or dubbed video files.", "A training department or platform with invoicing."),
        },
        # ------------------------------------------------------------------ NGOs
        "ngos": {
            "title": "Transcription and Translation for NGOs and Civil Society Groups",
            "meta_description": "Transcribe field recordings and translate reports, training and campaign videos into 70+ languages. Human review for up to 99% accuracy, per-minute pricing and no agency rates.",
            "kicker": "For NGOs and civil society",
            "h1": "Reach every community in its own language",
            "intro": "Field interviews, training videos and campaigns need to reach the people they are about. Kauli transcribes and translates "
                     "across 70+ languages, with human review where accuracy and trust matter, at a per-minute price you can budget.",
            "why_heading": "Built for field and campaign work",
            "points": [
                {"title": "Transcribe in the language spoken",
                 "body": "Instant AI transcribes 70+ languages, with a manual transcription option for languages no speech model covers yet."},
                {"title": "Up to 99% accurate with human review",
                 "body": f"A dedicated editor checks the work against the audio. Available in {hr_names}, with more languages coming soon."},
                {"title": "Consent before any voice is cloned",
                 "body": "Dubbing in the speaker's own voice needs your confirmation of rights and consent. It is enforced on the server."},
                {"title": "Budget-friendly and predictable",
                 "body": f"Per-minute pricing with no bundled credits, minutes included on every paid plan, and invoicing on Enterprise. Your first {free:.0f} minutes every month are free."},
            ],
            "choose": [
                {"name": "Field recordings", "best": "Interviews, focus groups and community meetings.", "get": "Transcripts with speakers and timestamps, ready for analysis or reports."},
                {"name": "Training and campaign video", "best": "Reaching communities that speak other languages.", "get": "Translated subtitles, and dubbed tracks in selected languages."},
                {**human, "name": "Human review", "best": "Donor reports, evidence and anything published under your name."},
            ],
            "demo": "meeting",
            "guides": ["ngos-and-civil-society-transcription-and-translation", "interview-transcription-checklist-research-journalism", "ai-vs-human-transcription-which-do-you-need"],
            "cta_heading": "See the quality on your own material",
            "cta_body": "Upload a real clip or paste a YouTube link. Your first minutes are free.",
            "related": ["video-translation", "video-transcription", "captioning"],
            "tour": {"mode": "upload", "filename": "field_report.mp4"},
            "faq": [
                {"q": "Will our recordings stay confidential?",
                 "a": "We do not share your data with anyone else and do not run ad tracking on this site. Files are currently kept until you delete them: ask us, or use the Your data section in Settings, to delete a file or your account data at any time."},
                {"q": "Can we be invoiced?",
                 "a": "Yes. Invoicing is part of the Enterprise plan for organizations with an ongoing relationship."},
                {"q": "What if our language is not supported by speech recognition?",
                 "a": "Instant AI transcribes 70+ languages. For a language no model covers yet, such as Kikuyu, a human transcriber types the source by ear at a slightly higher per-minute price."},
                {"q": "How is pricing for volume?",
                 "a": f"Pro is ${pro['price_usd']}/month and includes {pro['included_ai_minutes']:.0f} Instant AI minutes for transcription and translation, Premium is ${prem['price_usd']}/month with {prem['included_ai_minutes']:,.0f}{billing.hours_suffix(prem['included_ai_minutes'])}. For a custom volume rate, talk to us."},
            ],
            "fit_guide": plans("A pilot with a few recordings.", "Monthly programme reporting.", "Videos with burned captions or dubbed files for screenings.",
                               "A multi-country programme needing invoicing."),
        },
        # ------------------------------------------------------------------ call centers
        "call-centers": {
            "title": "Call Center Transcription and Call Quality Review",
            "meta_description": "Turn recorded calls into searchable transcripts with agent and customer separated. Review more calls, translate them, and send results to your tools through API and webhooks. Up to 99% accuracy with human review.",
            "kicker": "For call centers and support teams",
            "h1": "Every call transcribed, searchable and ready to review",
            "intro": "Recorded calls hold what your customers actually say, and almost nobody has time to listen. Kauli turns recordings into transcripts "
                     "with the agent and the customer separated, so quality, compliance and coaching teams can read, search and count calls "
                     "instead of listening to them.",
            "why_heading": "Built for quality, coaching and compliance work",
            "points": [
                {"title": "Agent and customer, separated",
                 "body": "Speaker labels and timestamps on every line, so a reviewer can jump straight to the moment a call turned."},
                {"title": "Searchable across every call",
                 "body": "Find every call that mentions a product, a refund or a competitor. Download transcripts as CSV to sort and count them in a spreadsheet."},
                {"title": "Notes in seconds",
                 "body": "Make an overview, key points and action items from any call with AI notes, then check and edit them."},
                {"title": "Up to 99% accurate where it matters",
                 "body": f"Use Instant AI for coaching and trends, and add a dedicated editor for complaints, disputes and evidence. Human review is available in {hr_names}."},
                {"title": "Calls in other languages",
                 "body": "Transcribe and translate calls so a supervisor who does not speak the language can still review them."},
                {"title": "Connects to the tools you use",
                 "body": "Send recordings in through the API and get finished transcripts out by signed webhook, or use Zapier, Make or n8n to reach your helpdesk, CRM or spreadsheet."},
            ],
            "choose": [
                {"name": "Quality assurance and coaching", "best": "Reviewing more calls with the same team.",
                 "get": f"Instant AI transcripts with speakers and timestamps in minutes, and {pro['included_ai_minutes']:,.0f} to {prem['included_ai_minutes']:,.0f} minutes a month{billing.hours_suffix(prem['included_ai_minutes'])} included on Pro and Premium."},
                {"name": "Multilingual support lines", "best": "Teams that take calls in several languages.",
                 "get": "Transcription in 70+ languages with translation, so reviewers can read every call in one language."},
                {**human, "name": "Human-reviewed evidence", "best": "Complaints, disputes, compliance checks and anything you may need to show."},
                {"name": "Integrated pipeline", "best": "Teams that want it automatic.",
                 "get": "Send each recording through the API as it is made and receive the finished transcript by webhook, with an account manager on Enterprise."},
            ],
            "demo": "call",
            "guides": ["call-center-transcription-quality-assurance-and-integrations", "record-meetings-and-interviews-with-ai-post-processing", "ai-vs-human-transcription-which-do-you-need"],
            "cta_heading": "Try it on one of your own calls",
            "cta_body": "Upload a recording, or ask us for a pilot on a batch of calls. Your first minutes are free.",
            "related": ["video-transcription", "interview-transcription", "audio-to-text"],
            "tour": {"mode": "upload", "filename": "support_call.mp3"},
            "faq": [
                {"q": "Does it separate the agent from the customer?",
                 "a": "Yes. Transcripts label who is speaking and when, so you can read the agent and the customer separately. Speaker labels are most reliable on clear recordings with distinct voices."},
                {"q": "Can it connect to our phone system?",
                 "a": "Any phone system that can export its recordings can send them to Kauli, by upload or through our API. Finished transcripts come back by signed webhook. We do not yet have one-click connectors to specific telephony platforms, and an integration can be built on the API; talk to us about yours."},
                {"q": "Can it transcribe live calls as they happen?",
                 "a": "Not yet for phone calls. Kauli transcribes recordings once a call ends, and the meeting recorder can join online Zoom, Google Meet and Microsoft Teams calls."},
                {"q": "How accurate is it?",
                 "a": "Instant AI is machine-generated and very good on clear audio, and it is labelled as not reviewed by a person. Human review has an editor check the text against the audio and can reach up to 99% accuracy."},
                {"q": "What about confidentiality?",
                 "a": "We do not share your data with anyone else. Files are kept until you delete them, and you can ask us to delete files or account data at any time. Tell callers that calls are recorded, as the rules where you operate require."},
                {"q": "How is it priced?",
                 "a": f"Per audio-minute, shown before you order. Pro is ${pro['price_usd']}/month with {pro['included_ai_minutes']:,.0f} Instant AI minutes, Premium is ${prem['price_usd']}/month with {prem['included_ai_minutes']:,.0f}{billing.hours_suffix(prem['included_ai_minutes'])}, and Enterprise has negotiated volume rates and invoicing."},
            ],
            "fit_guide": plans("Trying a handful of calls.", "A small team reviewing calls every month.", "A larger team that also needs translation and video files.",
                               "A call center that needs a negotiated rate, invoicing and an integration."),
        },
    }
    for slug, demo in DEMO_FOR.items():
        if slug in pages:
            pages[slug]["demo"] = demo
    return pages


def refresh(pages: dict, hr_names: str) -> None:
    """Fill the four older, simpler pages with the same choose / demo / guides sections as the rest."""
    extras = {
        "audio-to-text": {
            "choose": [
                {"name": "Instant AI", "best": "Notes, search and first drafts.", "get": "A transcript in minutes in 70+ languages, labelled as machine-generated."},
                {"name": "Human-reviewed", "best": "Anything you will publish or quote.", "get": f"An editor checks the work against the audio, for up to 99% accuracy. Available in {hr_names}."},
            ],
            "demo": "meeting",
            "guides": ["mp3-to-text-how-to-convert-audio-files", "how-does-ai-transcription-work", "ai-vs-human-transcription-which-do-you-need"],
        },
        "youtube-video-to-text": {
            "choose": [
                {"name": "Transcript", "best": "Notes, research and repurposing a video.", "get": "Paste a link and get timestamped text."},
                {"name": "Subtitles", "best": "Your own videos.", "get": "SRT and VTT files, or captions pushed to your channel."},
                {"name": "Translated", "best": "Reaching a new audience.", "get": "Transcript and subtitles in other languages."},
            ],
            "demo": "video",
            "guides": ["how-to-transcribe-a-youtube-video", "translate-youtube-titles-descriptions-and-captions", "add-subtitles-to-video-in-another-language"],
        },
        "podcast-transcription": {
            "choose": [
                {"name": "Instant AI transcript", "best": "Show notes and search, every episode.", "get": "Speaker-labelled text in minutes."},
                {"name": "Human-reviewed", "best": "Transcripts you publish under your name.", "get": f"Up to 99% accuracy. Available in {hr_names}."},
                {"name": "Automatic publishing", "best": "Shows with a website.", "get": "A webhook sends each finished transcript to your site, or to Zapier, Make or n8n."},
            ],
            "demo": "meeting",
            "guides": ["podcast-transcripts-to-your-website-with-webhooks", "podcast-transcripts-why-and-how", "verbatim-vs-clean-read-transcription"],
        },
        "interview-transcription": {
            "choose": [
                {"name": "Upload a recording", "best": "Interviews you recorded yourself.", "get": "Transcript with speakers and timestamps in minutes."},
                {"name": "Send Kauli to the call", "best": "Live meetings and remote interviews.", "get": "On Pro and Premium, Kauli joins Zoom, Google Meet or Teams, records and returns a transcript."},
                {"name": "Human-reviewed", "best": "Quotes, research and publication.", "get": f"Up to 99% accuracy. Available in {hr_names}."},
            ],
            "demo": "meeting",
            "guides": ["record-meetings-and-interviews-with-ai-post-processing", "interview-transcription-checklist-research-journalism", "how-to-transcribe-a-zoom-recording"],
        },
    }
    for slug, extra in extras.items():
        if slug in pages:
            pages[slug].update(extra)
    for slug, demo in DEMO_FOR.items():
        if slug in pages:
            pages[slug]["demo"] = demo
