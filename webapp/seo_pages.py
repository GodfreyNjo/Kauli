"""Pages for the searches where a visitor already needs what Kauli sells: a language plus a job ("Spanish interview transcription").

Why these and not more: the plan for growth is a few strong pages in the languages Kauli can review with a person, not hundreds of thin
ones. A page exists only for a reviewed language (Spanish, German, French, Chinese) and a job people really search for. Each page has its own
sample, price, review checks and questions, so none is a copy of another with a word swapped.

Honesty rules (same as the rest of the site): the sample is an illustration of the kind of corrections an editor makes, and says so; prices are
read from the live price card; human review is only described as available where it can really be ordered (the caller passes `live`).
"""
from __future__ import annotations

REVIEWED = {
    # code -> (transcribe slug, human-review slug)
    "es": ("spanish", "spanish"),
    "de": ("german", "german"),
    "fr": ("french", "french"),
    "zh": ("chinese-mandarin", "chinese"),
}

# What an editor typically fixes in each language, as a short illustration (not a customer's file).
SAMPLES = {
    "es": {"lines": [
        {"ai": "El presupuesto de dos mil veinticinco se aprobó en Madrid el ocho de marzo con ochenta votos.",
         "human": "El presupuesto de 2025 se aprobó en Madrid el 8 de marzo con 80 votos.", "note": "Numbers and dates written the way a reader expects."},
        {"ai": "Dijo que el CEO de Telefónica no iba a asistir.", "human": "Dijo que la CEO de Telefónica no iba a asistir.", "note": "Gender agreement the machine could not hear."},
        {"ai": "Vamos a revisar eso mas tarde, ok.", "human": "Vamos a revisar eso más tarde, ¿ok?", "note": "Accents and punctuation restored."}]},
    "de": {"lines": [
        {"ai": "Die Bundesregierung hat die Haushaltsplanung vor gelegt.", "human": "Die Bundesregierung hat die Haushaltsplanung vorgelegt.", "note": "Separable verbs and compound words kept whole."},
        {"ai": "Herr Müller von der Stadt werke sagte das.", "human": "Herr Müller von den Stadtwerken sagte das.", "note": "Organisation names spelled correctly."},
        {"ai": "Wir treffen uns um halb drei, oder?", "human": "Wir treffen uns um 14:30 Uhr, oder?", "note": "\"Halb drei\" is 2:30, not 3:30: a clock the machine can misread."}]},
    "fr": {"lines": [
        {"ai": "Il y a eu beaucoup de gens qui son venus.", "human": "Il y a eu beaucoup de gens qui sont venus.", "note": "Agreement and verb forms corrected."},
        {"ai": "Le premier ministre a annoncer le plan.", "human": "Le premier ministre a annoncé le plan.", "note": "Past participle instead of infinitive: the two sound the same."},
        {"ai": "On se retrouve a l'assemblée nationale.", "human": "On se retrouve à l'Assemblée nationale.", "note": "Accents and the name of the institution."}]},
    "zh": {"lines": [
        {"ai": "我们公司在二零二五年增长了百分之三十。", "human": "我们公司在2025年增长了30%。", "note": "Numbers and years written the usual way."},
        {"ai": "王总说他会去上海参加会议。", "human": "王总说他会去上海参加会议。", "note": "Checked against the audio: the name and title are right as they are."},
        {"ai": "这个产品很受欢迎，销量非常不错。", "human": "这款产品很受欢迎，销量非常不错。", "note": "Natural measure word for the product."}]},
}

TASKS = {
    "interview-transcription": {
        "solution": "interview-transcription", "name": "interview transcription", "kicker": "Interview transcription",
        "headline": "{lang} interview transcription, checked by a person when it has to be right",
        "intro": "Journalists, researchers and HR teams record interviews in {lang} and need every word attributed to the right person. Upload the recording, "
                 "get a speaker-labelled transcript with timestamps in minutes, and add a human editor when a quote will be published.",
        "tips": [("Speakers labelled", "Each speaker turn is marked, so a quote can be traced to who said it. An editor can name the speakers for you on reviewed orders."),
                 ("Verbatim or clean", "Tell us in the order notes whether you want every word (research) or a tidied read (publication). Our guide explains the difference."),
                 ("Timestamps you can cite", "A start time on every line makes it quick to find the exact moment and check the audio.")],
        "faq": [("Can you transcribe a two-person {lang} interview with overlapping speech?", "Yes. Instant AI separates speakers where it can; a human review catches the places where voices overlap or a speaker is hard to tell apart."),
                ("Is a {lang} interview transcript safe to quote?", "Treat the Instant AI result as a draft: check names, numbers and quotes against the audio. With human review an editor does that check before the transcript is marked ready.")],
    },
    "podcast-transcription": {
        "solution": "podcast-transcription", "name": "podcast transcription", "kicker": "Podcast transcripts",
        "headline": "{lang} podcast transcripts for your show notes, site and subtitles",
        "intro": "A transcript makes a {lang} episode searchable, quotable and accessible. Upload the episode, download Word, text, SRT or VTT, and publish "
                 "the transcript beside the audio.",
        "tips": [("Transcript on your site", "A text version gives search engines the words of the episode and gives listeners who cannot play audio a way to read it."),
                 ("Chapters and quotes", "Timestamped lines make it quick to pull quotes for social posts and to mark chapters."),
                 ("Subtitles for video clips", "Download SRT or VTT to caption the video version or short clips of the same episode.")],
        "faq": [("Can I get a {lang} podcast transcript with timestamps?", "Yes. Every line carries a start time, and you can download Word, text, SRT and VTT versions."),
                ("Can you translate my {lang} podcast transcript into English?", "Yes. Choose transcription plus translation. Instant AI translation is machine translation; add human review where a language offers it for a result you will publish.")],
    },
    "youtube-subtitles": {
        "solution": "youtube-video-to-text", "name": "YouTube subtitles", "kicker": "YouTube subtitles",
        "headline": "{lang} YouTube subtitles and captions you can upload in minutes",
        "intro": "Paste a YouTube link or upload the video, choose {lang}, and download SRT or VTT subtitle files, or send them straight to your channel. "
                 "Add translated subtitles in other languages from the same transcript.",
        "tips": [("From the link", "Paste the video address; there is nothing to download or upload by hand."),
                 ("SRT and VTT", "Subtitle files in the formats YouTube and other platforms accept, with line lengths made to read comfortably."),
                 ("More languages from one transcript", "Once the {lang} transcript is done, order a translation without uploading again.")],
        "faq": [("How do I add {lang} subtitles to a YouTube video?", "Paste the video link, choose subtitles, then download the SRT or VTT file and upload it in YouTube Studio, or connect your channel to publish from Kauli."),
                ("Can I add subtitles in other languages too?", "Yes. Use \"Do more with this file\" on the finished order to add a translation; the stored transcript is reused, so there is nothing to upload or transcribe again.")],
    },
    "research-transcripts": {
        "solution": "interview-transcription", "name": "research transcripts", "kicker": "Research and verbatim transcripts",
        "headline": "{lang} research transcripts: verbatim when you need every word",
        "intro": "Interviews, focus groups and field recordings in {lang} need a transcript you can code and cite. Ask for verbatim, keep timestamps, "
                 "and have an editor check names, terms and speaker changes against the audio.",
        "tips": [("Verbatim or clean read", "Verbatim keeps hesitations and repetitions; a clean read removes them. Say which you need when you order."),
                 ("Consent and privacy", "Your files are used only to make your result. See the privacy policy for how long they are kept and how to delete them."),
                 ("Codable output", "Word and text downloads with timestamps and speaker labels import cleanly into analysis tools.")],
        "faq": [("Can you do a verbatim {lang} transcript?", "Yes. Write \"verbatim\" in the order notes. A human editor follows that style; Instant AI gives a standard transcript you can then correct yourself."),
                ("How do you handle sensitive recordings?", "Recordings are processed to make your transcript and are not shared. You can delete a file and its results from your account. The privacy policy has the details.")],
    },
    "call-center-transcripts": {
        "solution": "call-centers", "name": "call centre transcripts", "kicker": "Call centre transcripts",
        "headline": "{lang} call transcripts for quality review and training",
        "intro": "Turn recorded {lang} calls into searchable text for quality assurance, coaching and compliance review, in batches through the app or the API.",
        "tips": [("Batch and API", "Send many recordings at once from the account or with the API, and collect the transcripts as they finish."),
                 ("Two speakers, clearly labelled", "Agent and customer turns are separated where the audio allows, with a time on every line."),
                 ("Review where it matters", "Use human review on the calls that feed a decision, such as a complaint or a compliance check.")],
        "faq": [("Can you transcribe {lang} phone-quality audio?", "Yes. Accuracy depends on audio quality, and phone-quality audio is harder than studio audio, so check key numbers and names. Human review is available where a call matters."),
                ("Can I connect this to our systems?", "Yes. There is an API and webhooks for finished orders, so transcripts can go straight into your own tools.")],
    },
}

LANG_BLOCKS = {
    "es": {"label": "Spanish", "hard": "Spanish varies between Spain, Mexico, the Caribbean and the Southern Cone, and speakers in the United States often switch between Spanish and English in one sentence.",
           "points": [("Regional Spanish", "Vocabulary, pronunciation and rhythm differ from Madrid to Mexico City to Buenos Aires. Say where the speakers are from in the order notes."),
                      ("Spanish and English together", "Many recordings mix both languages. Instant AI follows the main language; an editor can keep the other language's words correct.")]},
    "de": {"label": "German", "hard": "German packs meaning into long compound words and separable verbs, and speakers move between formal and informal address, with Austrian and Swiss usage on top.",
           "points": [("Compound words kept whole", "Long compounds are written as one word, the way a German reader expects."),
                      ("Formal and informal address", "Sie and du are kept as spoken, which matters for interviews and customer calls.")]},
    "fr": {"label": "French", "hard": "French sounds the same in many written forms (the infinitive and the past participle, for example), so errors hide in plain sight, and Canadian, Belgian and African French differ from metropolitan French.",
           "points": [("Agreement and verb forms", "The endings that sound identical are the ones a careful review checks."),
                      ("Accents and institutions", "Accents and the names of institutions are restored, so a transcript can be published as it is.")]},
    "zh": {"label": "Chinese (Mandarin)", "hard": "Mandarin transcripts depend on the audience: Simplified characters for mainland readers, Traditional for Taiwan and Hong Kong, and names that can be written several ways.",
           "points": [("Simplified or Traditional", "Tell us which your readers use, and the transcript is written that way."),
                      ("Names and measure words", "Names, brands and measure words are kept consistent and natural.")]},
}


def known(slug: str) -> str | None:
    """The language code for a /transcribe/ slug that has job pages, or None."""
    for code, (slug_t, _hr) in REVIEWED.items():
        if slug == slug_t:
            return code
    return None


def combo_links(code: str, task: str) -> dict:
    """Other jobs in this language, and this job in the other languages, for the links at the foot of a page."""
    slug_t = REVIEWED[code][0]
    same_language = [{"href": f"/transcribe/{slug_t}/{t}", "label": f"{LANG_BLOCKS[code]['label']} {TASKS[t]['name']}"} for t in TASKS if t != task]
    same_task = [{"href": f"/transcribe/{REVIEWED[c][0]}/{task}", "label": f"{LANG_BLOCKS[c]['label']} {TASKS[task]['name']}"} for c in REVIEWED if c != code]
    return {"same_language": same_language, "same_task": same_task}


def combo_page(code: str, task: str, price_ai: float, price_human: float, live: bool, hr_slug: str) -> dict:
    """The solution_page.html content for one language + job page."""
    lang, t, block = LANG_BLOCKS[code], TASKS[task], LANG_BLOCKS[code]
    label = lang["label"]
    review = (f"On human-reviewed orders an editor checks the {label} transcript against your audio before it is marked ready, at ${price_human:.2f} per audio-minute."
              if live else
              f"Human review in {label} is being opened up. Instant AI works now, and the human review page shows when review is available for {label}.")
    points = [{"title": title, "body": body.format(lang=label)} for title, body in t["tips"]]
    points += [{"title": title, "body": body} for title, body in block["points"]]
    points.append({"title": f"Instant AI now, a person when it matters",
                   "body": f"Instant AI transcribes {label} from ${price_ai:.2f} per audio-minute and is ready in minutes; it is machine output, so check names and numbers. {review}"})
    faq = [{"q": q.format(lang=label), "a": a.format(lang=label)} for q, a in t["faq"]]
    faq.append({"q": f"What does {label} {t['name']} cost?",
                "a": f"Instant AI is ${price_ai:.2f} per audio-minute and human review is ${price_human:.2f} per audio-minute where available. The price for your file is shown before you order, and your first minutes each month are free to try."})
    return {
        "title": f"{label} {t['name'][0].upper() + t['name'][1:]} and Human Review",
        "meta_description": f"{label} {t['name']}: Instant AI from ${price_ai:.2f} per minute, with a human editor for the work that has to be right. "
                             f"Download Word, TXT, SRT and VTT. Your first minutes are free.",
        "kicker": t["kicker"],
        "h1": t["headline"].format(lang=label),
        "intro": t["intro"].format(lang=label) + " " + block["hard"],
        "why_heading": f"{label} {t['name']} with Kauli",
        "points": points,
        "cta_heading": f"Transcribe your first {label} file free",
        "cta_body": "Upload a recording or paste a link. Your first minutes are free, and the price is shown before anything is charged.",
        "cta_label": "Transcribe your first file free",
        "related": [t["solution"], "audio-to-text"],
        "faq": faq,
        "sample": sample_block(code),
        "guarantee": guarantee_block(label, live, hr_slug),
        "more": combo_links(code, task),
    }


def sample_block(code: str) -> dict:
    s = SAMPLES[code]
    return {"title": f"What a {LANG_BLOCKS[code]['label']} editor fixes", "lines": s["lines"],
            "note": "An illustration of the kinds of corrections an editor makes, written for this page. It is not a customer's file."}


def guarantee_block(label: str, live: bool, hr_slug: str) -> dict:
    return {"title": "Who reviews it and what we promise",
            "body": (f"Every human-reviewed {label} transcript is checked against the audio by an editor before it is marked ready. If you find an error in a reviewed transcript, "
                     "tell us and we correct it free of charge." if live else
                     f"Human review for {label} is opening up language by language. Until it is live for {label}, Instant AI is machine output that you should check; "
                     "the human review page shows where review is available now."),
            "href": f"/human-review/{hr_slug}", "link": f"How {label} human review works"}


def language_page_extras(code: str, price_ai: float, price_human: float, live: bool) -> dict:
    """What a /transcribe/<language> page gets when the language is one Kauli reviews: a sample, the promise and links to the job pages."""
    slug_t, hr_slug = REVIEWED[code]
    label = LANG_BLOCKS[code]["label"]
    return {
        "sample": sample_block(code), "guarantee": guarantee_block(label, live, hr_slug),
        "cta_label": "Transcribe your first file free",
        "more": {"same_language": [{"href": f"/transcribe/{slug_t}/{t}", "label": f"{label} {TASKS[t]['name']}"} for t in TASKS],
                 "same_task": [{"href": f"/transcribe/{REVIEWED[c][0]}", "label": f"{LANG_BLOCKS[c]['label']} transcription"} for c in REVIEWED if c != code]},
        "comparison": [{"name": "Instant AI", "price": f"${price_ai:.2f}", "ready": "In minutes", "best": "Drafts, notes, search and anything you will read through yourself",
                        "checked": "Machine output; check names and numbers"},
                       {"name": "Human-reviewed", "price": f"${price_human:.2f}", "ready": "Slower, with a delivery time shown when you order",
                        "best": "Quotes, publication, research and anything with your name on it", "checked": "An editor checks it against the audio before it is ready"}],
    }


# ------------------------------------------------------------------------------------------------------------ blog posts feed the product
def cta_target(text: str) -> tuple[str, str]:
    """(address, label) of the page a blog post should lead to, from what the post is about."""
    t = text.lower()
    for code, (slug_t, _hr) in REVIEWED.items():
        names = {"es": ("spanish", "español"), "de": ("german", "deutsch"), "fr": ("french", "français"), "zh": ("chinese", "mandarin")}[code]
        if any(n in t for n in names):
            for task, spec in TASKS.items():
                key = task.split("-")[0]
                if key in t or (task == "youtube-subtitles" and ("youtube" in t or "subtitle" in t)):
                    return f"/transcribe/{slug_t}/{task}", f"{LANG_BLOCKS[code]['label']} {spec['name']}"
            return f"/transcribe/{slug_t}", f"{LANG_BLOCKS[code]['label']} transcription"
    for key, (href, label) in {"interview": ("/solutions/interview-transcription", "Interview transcription"),
                               "podcast": ("/solutions/podcast-transcription", "Podcast transcripts"),
                               "youtube": ("/solutions/youtube-video-to-text", "YouTube subtitles and transcripts"),
                               "subtitle": ("/solutions/captioning", "Captions and subtitles"),
                               "dubbing": ("/solutions/video-dubbing", "Video dubbing"),
                               "call": ("/solutions/call-centers", "Call centre transcripts")}.items():
        if key in t:
            return href, label
    return "/solutions/audio-to-text", "Audio to text"


def add_blog_ctas(body_html: str, title: str, slug: str) -> str:
    """A call to action after the third paragraph and another at the end, pointing at the page that matches the post."""
    href, label = cta_target(f"{title} {body_html[:1500]}")
    link = f"{href}?utm_source=blog&utm_medium=cta&utm_campaign={slug}"
    signup = f"/login?mode=signup&utm_source=blog&utm_medium=cta&utm_campaign={slug}"

    def box(kind: str) -> str:
        return (f'<aside class="blog-cta blog-cta-{kind}" aria-label="Try Kauli"><div><strong>Need this transcribed?</strong>'
                f'<span>Your first minutes are free. See how it works for <a href="{link}">{label}</a>.</span></div>'
                f'<a class="btn cta-olive" href="{signup}" data-cta="blog-{kind}">Transcribe your first file free</a></aside>')
    parts = body_html.split("</p>")
    if len(parts) > 5:
        out = "</p>".join(parts[:3]) + "</p>" + box("mid") + "</p>".join(parts[3:])
    else:
        out = body_html
    return out + box("end")
