"""Five guides written for the audiences the solution pages serve: podcasters with a website, teams that record meetings and
interviews, media and broadcast partners, course creators, and NGOs and civil society groups.

Inserted by `seed_guides` at startup, published, once per slug (a small marker table remembers a guide was seeded, so one a
staff member edits or deletes is never put back). House style as in blog_seed.py: one sentence per paragraph, exactly three
key takeaways before the first h2, no invented statistics, clients or claims. Every product statement matches the live
product: if a feature changes, change the guide here (an already-seeded post is then edited in the staff blog editor).
"""
from __future__ import annotations

GUIDES = [
    {
        "slug": "podcast-transcripts-to-your-website-with-webhooks",
        "title": "How to Publish Podcast Transcripts to Your Website Automatically with Webhooks",
        "category": "Transcription Guides",
        "description": "Send every finished podcast transcript to your website without lifting a finger: how Kauli webhooks work, a no-code setup with Zapier, Make or n8n, and a short script if you prefer your own endpoint.",
        "body_html": """<p>Publishing a transcript for every episode helps listeners, search engines and the people who would rather read than listen.</p>
<p>Doing it by hand for every episode is the part most shows give up on.</p>

<div class="m-key-takeaways"><p class="m-key-takeaways-label">Key takeaways</p><ul>
<li>A Kauli webhook sends your website a signed message the moment a transcript is ready, with links to the finished files.</li>
<li>You can connect it without code through Zapier, Make or n8n, or receive it on your own endpoint.</li>
<li>Choose human review for the transcripts you publish under your name, which reaches up to 99% accuracy.</li>
</ul></div>

<h2>What a webhook does for a podcast</h2>
<p>A webhook is a message one service sends to another when something happens.</p>
<p>Here, the event is that an order is delivered, and the message is a signed POST to a web address you choose.</p>
<p>The message carries the order and secure links to the transcript in text, Word, SRT and VTT, so your website can fetch whichever it needs.</p>
<p>The full format is on the <a href="/developers#webhooks">developers page</a>.</p>

<h2>Step 1: Add your webhook address</h2>
<p>Sign in, open Settings and add the web address that should receive the message.</p>
<p>Kauli gives you a secret, which you use to check that a message really came from Kauli.</p>
<p>Nothing downloads from an account until a plan has been chosen, so choose one first, and the free plan counts.</p>

<h2>Step 2, the no-code route: Zapier, Make or n8n</h2>
<p>All three tools can start a workflow from a webhook, which they call a catch hook or webhook trigger.</p>
<p>Create the trigger, copy its address into Kauli's Settings, and send a test.</p>
<p>Add a step that fetches the plain-text transcript from the link in the message.</p>
<p>Add a final step that creates a post or page in your website, such as a WordPress, Ghost or Webflow item, and fills in the episode title and the transcript.</p>
<p>Because the trigger fires when the order is ready, the transcript appears on your site without anyone touching it.</p>

<h2>Step 2, the code route: your own endpoint</h2>
<p>If you run your own site, receive the message directly and check the signature before trusting it.</p>
<pre><code>import hmac, hashlib

def valid(secret, timestamp, body, header):
    expected = "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)</code></pre>
<p>The signature is in the X-Kauli-Signature header and the timestamp is in X-Kauli-Timestamp.</p>
<p>Once it checks out, read the transcript link from the order, download the text and save it with the episode.</p>

<h2>Starting the order automatically too</h2>
<p>The same automation can send new episodes to Kauli.</p>
<p>Your tool downloads the new audio from your feed, and calls the API to create an order with the file attached.</p>
<p>The webhook then brings the finished transcript back, so the whole path from new episode to published transcript runs on its own.</p>
<p>API keys and the order fields are explained on the <a href="/developers">developers page</a>.</p>

<h2>Choosing accuracy for published transcripts</h2>
<p>Instant AI is fast and is labelled as machine-generated.</p>
<p>Human review adds an editor who checks every line against the audio, and it reaches up to 99% accuracy.</p>
<p>Many shows publish Instant AI for every episode and choose human review for the ones they promote or quote.</p>
<p>The <a href="/blog/ai-vs-human-transcription-which-do-you-need">guide to choosing between them</a> goes through the trade-off.</p>

<h2>Where to go next</h2>
<p>See what the output looks like on the <a href="/solutions/podcast-transcription">podcast transcription page</a>, or hear it in the <a href="/try?use=podcast">demo</a>.</p>
<p>If you want help wiring it up to your site, <a href="/demo">book a demo</a> and we will walk through it with you.</p>""",
    },
    {
        "slug": "record-meetings-and-interviews-with-ai-post-processing",
        "title": "Record Meetings and Interviews, Then Let AI Do the Post-Processing",
        "category": "Process & Quality",
        "description": "How to capture live meetings and interviews with Kauli, either by sending the notetaker to your call or uploading a recording, and what you get back: speaker-labelled transcripts, summaries and exports.",
        "body_html": """<p>The recording is the easy part of a meeting or an interview.</p>
<p>The hours that follow, turning it into notes, quotes and next steps, are where the time goes.</p>

<div class="m-key-takeaways"><p class="m-key-takeaways-label">Key takeaways</p><ul>
<li>You can bring a conversation into Kauli by sending the meeting notetaker to the call or by uploading the recording.</li>
<li>AI post-processing gives you a speaker-labelled, timestamped transcript within minutes, plus exports and summaries.</li>
<li>For quotes and records that matter, add human review, which reaches up to 99% accuracy.</li>
</ul></div>

<h2>Two ways to capture the conversation</h2>
<p>The first is the meeting notetaker, which joins your Zoom, Google Meet or Microsoft Teams call from the Meetings tab of your dashboard.</p>
<p>You paste the meeting link, and the notetaker joins, records and hands the recording back as an order when the call ends.</p>
<p>The notetaker is part of the Pro and Premium plans, and it needs enough included minutes left to cover the meeting.</p>
<p>The second is simply an upload, for interviews you recorded on a phone, a recorder or another tool, in any common audio or video format.</p>
<p>You can also paste a YouTube link if the conversation is already online.</p>

<h2>What AI post-processing gives you</h2>
<p>Once the recording is in, Instant AI transcribes it in minutes, in more than 70 languages.</p>
<p>The transcript is timestamped, so you can jump straight to any moment.</p>
<p>Speakers can be labelled, so a quote is attached to the person who said it.</p>
<p>From the order page you can ask for a summary and ask questions of the transcript.</p>
<p>You can download the text as Word, plain text, SRT or VTT, and translate it into another language in the same order.</p>

<h2>Sending the result where your team works</h2>
<p>When the transcript is ready, Kauli can post to Slack or Microsoft Teams, and send the finished order to Notion.</p>
<p>Webhooks, Zapier, Make and n8n let you route it anywhere else, such as a project tool or a research repository.</p>
<p>The <a href="/developers">developer documentation</a> covers the API and webhooks if you want to build it yourself.</p>

<h2>Choosing the right level of accuracy</h2>
<p>For internal notes and action items, Instant AI is usually enough, and it is clearly labelled as machine-generated.</p>
<p>For research, journalism, legal records and quotes that will be published, add human review.</p>
<p>An editor checks the transcript against the audio, and the result reaches up to 99% accuracy.</p>
<p>Clear audio helps either way, so place the microphone close and ask people not to talk over each other.</p>

<h2>A checklist before you press record</h2>
<ul>
<li>Tell everyone the conversation is being recorded and why.</li>
<li>Check the audio before the conversation starts.</li>
<li>Ask people to say their names at the start so speaker labels are easy.</li>
<li>Write down names and technical terms in the order notes.</li>
</ul>
<p>The <a href="/blog/interview-transcription-checklist-research-journalism">interview checklist</a> goes into more detail.</p>

<h2>Try it</h2>
<p>Hear a sample in the <a href="/try?use=meeting">interactive demo</a>, or read about the <a href="/solutions/interview-transcription">interview transcription service</a>.</p>""",
    },
    {
        "slug": "media-and-broadcast-partner-with-kauli-fast-turnarounds",
        "title": "Media and Broadcast: How to Partner with Kauli for Fast, Accurate Turnarounds",
        "category": "Process & Quality",
        "description": "A practical guide for newsrooms, broadcasters and production companies: how a Kauli partnership works, from a pilot to an Enterprise agreement with faster turnarounds, and what you can expect at each step.",
        "body_html": """<p>Broadcast work runs on deadlines, and transcripts, subtitles and translations sit on the critical path.</p>
<p>This guide explains how media teams work with Kauli, from a first pilot to a standing partnership built around fast turnarounds.</p>

<div class="m-key-takeaways"><p class="m-key-takeaways-label">Key takeaways</p><ul>
<li>Instant AI returns transcripts and subtitles in minutes, and human review brings them up to 99% accuracy before air.</li>
<li>An Enterprise partnership adds negotiated rates, invoicing, a dedicated account manager and a faster dedicated turnaround tier.</li>
<li>You can start with a single segment, and connect your systems through the API and webhooks when you are ready.</li>
</ul></div>

<h2>Where Kauli fits in a media workflow</h2>
<p>Kauli turns footage into timestamped transcripts, subtitle files, translations and dubbed tracks.</p>
<p>Journalists and producers use the transcript to find the quote, editors use the subtitles, and distribution teams use the translations to reach new audiences.</p>
<p>Deliverables include SRT and VTT subtitle files, Word and text transcripts, burned-in captions and dubbed video files.</p>

<h2>Two speeds, chosen per job</h2>
<p>Instant AI is for speed.</p>
<p>It returns a draft when processing finishes, and it is labelled as machine-generated, so use it for logging, monitoring and first-pass subtitles.</p>
<p>Human review is for what goes to air.</p>
<p>A real editor checks the draft against the audio, and the result reaches up to 99% accuracy, with an estimated delivery time shown on your dashboard from the moment the order is confirmed.</p>

<h2>How a partnership works</h2>
<ol>
<li><strong>Start with a pilot.</strong> Send a representative segment through both Instant AI and human review and compare the output with your house style.</li>
<li><strong>Talk through your deadlines.</strong> Tell us your typical volumes, languages and the times of day that matter, so the turnaround can be agreed against real needs.</li>
<li><strong>Agree an Enterprise plan.</strong> This is where the volume rate, invoicing, a named account manager and the faster dedicated turnaround tier come in.</li>
<li><strong>Connect your systems.</strong> Use the API to submit footage and webhooks to be told the moment a transcript is ready, so nobody has to refresh a page.</li>
</ol>

<h2>Built for the details broadcasters care about</h2>
<ul>
<li>Names, places and numbers are what editors check, and human review pays particular attention to them.</li>
<li>Style notes and a style guide travel with the order, so captions follow your conventions.</li>
<li>Voice cloning for a dub is never assumed: it needs your confirmation of rights and consent.</li>
<li>Your files and generated deliverables stay private to your account, and you can ask us to delete them at any time.</li>
</ul>

<h2>Reaching new audiences</h2>
<p>Subtitles and translations extend a story beyond its first audience.</p>
<p>Instant AI translates between more than 70 languages, and dubbing is available in selected languages, with more coming.</p>
<p>Human review is available in selected languages too, and the <a href="/human-review">human review page</a> lists what is available now.</p>

<h2>Next step</h2>
<p>Read about the <a href="/solutions/media-broadcast">media and broadcast service</a>, then <a href="/demo">book a call</a> to talk through your deadlines.</p>
<p>Or start with a clip today, since your first minutes are free.</p>""",
    },
    {
        "slug": "course-creators-accurate-transcripts-and-fast-localization",
        "title": "Course Creators: Accurate Transcripts and Fast Localization for Every Lesson",
        "category": "Localization",
        "description": "How online course creators and training teams can turn lessons into accurate transcripts and subtitles, then localize them quickly and affordably for learners in other languages.",
        "body_html": """<p>A course is a library of spoken lessons, and a transcript is what makes each one searchable, accessible and ready to translate.</p>
<p>This guide shows a workflow that keeps accuracy high and the cost of localizing low.</p>

<div class="m-key-takeaways"><p class="m-key-takeaways-label">Key takeaways</p><ul>
<li>Transcribe every lesson once, check it, and use that transcript as the source for subtitles and translations.</li>
<li>Instant AI makes localization fast and affordable, and human review lifts the lessons that matter most to up to 99% accuracy.</li>
<li>Pro and Premium include monthly minutes that cover both transcription and translation.</li>
</ul></div>

<h2>Step 1: Transcribe each lesson</h2>
<p>Upload the lesson video or audio, or paste a link if it is hosted on YouTube.</p>
<p>Instant AI returns a timestamped transcript in minutes, ready to download as Word, text, SRT or VTT.</p>
<p>A transcript lets learners search for a topic, follow along, and learn in noisy places or without sound.</p>

<h2>Step 2: Check the terminology</h2>
<p>Courses are full of terms that an automatic transcript can get wrong, such as product names, acronyms and the names of frameworks.</p>
<p>Add notes to the order or upload a style guide, and a human editor follows it across every lesson.</p>
<p>Human review is available in selected languages and reaches up to 99% accuracy, so keep it for the foundations of the course and the lessons that learners rewatch.</p>

<h2>Step 3: Localize</h2>
<p>With a checked transcript, translation is the next step.</p>
<p>Instant AI translates between more than 70 languages, so one lesson can be ready for several audiences the same day.</p>
<p>Download translated subtitle files for your course platform, since SRT and VTT work on most of them.</p>
<p>If learners prefer to listen, dubbing is available in selected languages, with natural AI voices and an option for a human voice actor.</p>

<h2>Keeping the cost under control</h2>
<ul>
<li>Your first few minutes every month are free, so you can test a lesson end to end.</li>
<li>Pro includes 2,500 Instant AI minutes a month and Premium includes 6,500 (about 108 hours), and they cover transcription and translation.</li>
<li>Yearly billing costs less than paying monthly.</li>
<li>Premium also includes burned-in captions and dubbed video files.</li>
</ul>

<h2>A simple course workflow</h2>
<ol>
<li>Record the lesson and upload it as soon as it is final.</li>
<li>Use Instant AI for the transcript and subtitles.</li>
<li>Choose human review for key lessons and anything learners will quote.</li>
<li>Translate the transcript and subtitles into your target languages.</li>
<li>Upload the subtitle files to your platform and publish.</li>
</ol>

<h2>See it on a lesson</h2>
<p>Play the <a href="/try?use=course">course demo</a> to hear and read a sample, and see the <a href="/solutions/e-learning">course creators page</a> for options and pricing.</p>
<p>Your first minutes are free, so run one lesson through and compare.</p>""",
    },
    {
        "slug": "call-center-transcription-quality-assurance-and-integrations",
        "title": "Call Center Transcription: Quality Assurance, Speaker Labels and How to Connect Your Recordings",
        "category": "Transcription Guides",
        "description": "How a call center can turn recorded calls into searchable text with agent and customer separated, review more calls with less effort, and send transcripts to the tools it already uses.",
        "body_html": """<p>Most call centers record every call and listen to almost none of them.</p>
<p>Text changes that, because a transcript can be searched, skimmed and counted in a way audio cannot.</p>

<div class="m-key-takeaways"><p class="m-key-takeaways-label">Key takeaways</p><ul>
<li>A transcript with the agent and the customer separated lets a reviewer read a call in a minute instead of listening for ten.</li>
<li>Upload recordings, send them through the API as they are made, or have finished transcripts pushed to your tools with webhooks.</li>
<li>Use human review for the calls that matter most, such as complaints, disputes and compliance checks, which reaches up to 99% accuracy.</li>
</ul></div>

<h2>What a call transcript gives your team</h2>
<p>Each line of the transcript is tied to a time in the recording and to a speaker, so you can jump to the moment that matters.</p>
<p>Searching across transcripts finds every call that mentions a product, a competitor or a phrase you are watching for.</p>
<p>The AI notes feature turns a call into an overview, key points and action items that an agent or a supervisor can check in seconds.</p>

<h2>Review more calls without more reviewers</h2>
<p>Quality teams usually score a small sample because listening is slow.</p>
<p>Reading is faster, so the same people can look at more calls, and they can start from the calls with the most complaints or the longest handling time.</p>
<p>Download a set of transcripts as CSV to sort and count them in a spreadsheet.</p>

<h2>Instant AI or human review</h2>
<p>Instant AI is ready in minutes and is machine-generated, so treat it as a strong first draft.</p>
<p>Human review has a real editor check the text against the audio, and it can reach up to 99% accuracy.</p>
<p>Many teams use Instant AI for coaching and trend work and human review for evidence they may need to show a customer or a regulator.</p>

<h2>Getting recordings in</h2>
<ol>
<li>Upload audio or video files from your account, or paste a link.</li>
<li>Send recordings from your own systems with the <a href="/developers">API</a>, so every call is transcribed as it is made.</li>
<li>Join online calls with the meeting recorder for Zoom, Google Meet and Microsoft Teams.</li>
</ol>
<p>Any phone system that can export its recordings can use the first two routes.</p>

<h2>Getting transcripts out</h2>
<p>When an order finishes, a signed webhook tells your system, with links to the transcript in text, Word, SRT and VTT.</p>
<p>Tools such as Zapier, Make and n8n can pass it on to a helpdesk, a CRM or a spreadsheet without code.</p>
<p>The <a href="/blog/podcast-transcripts-to-your-website-with-webhooks">webhook guide</a> shows the same steps in detail.</p>

<h2>Customers in other languages</h2>
<p>A call in one language can be transcribed and translated, so a supervisor who does not speak it can still review the call.</p>
<p>Machine translation is labelled as such, and human review is available in selected languages.</p>

<h2>Privacy and consent</h2>
<p>Tell callers that calls are recorded, as the rules where you operate require.</p>
<p>Your recordings are not shared with anyone else, and you can ask us at any time to delete files or account data.</p>
<p>If you have specific compliance needs, <a href="/demo">talk to us</a> before you start.</p>

<h2>Next step</h2>
<p>Hear a sample on the <a href="/try?use=call">call center demo</a>, and read the <a href="/solutions/call-centers">call centers page</a> for options and plans.</p>
<p>Your first minutes are free, so you can test it on your own recordings.</p>""",
    },
    {
        "slug": "ngos-and-civil-society-transcription-and-translation",
        "title": "Transcription and Translation for NGOs and Civil Society Groups: A Practical Guide",
        "category": "Process & Quality",
        "description": "How NGOs and civil society organizations can turn interviews, field recordings and campaign videos into accurate transcripts and translations on a budget, with privacy and consent in mind.",
        "body_html": """<p>Civil society organizations collect a great deal of spoken evidence, from interviews and focus groups to community meetings and training sessions.</p>
<p>Turning it into text, and into the languages your audiences speak, is often slow and expensive.</p>

<div class="m-key-takeaways"><p class="m-key-takeaways-label">Key takeaways</p><ul>
<li>Instant AI transcribes and translates in more than 70 languages in minutes, at a per-minute price you can budget.</li>
<li>Human review, which reaches up to 99% accuracy, is worth it for donor reports, evidence and anything published under your name.</li>
<li>Consent and confidentiality come first: tell people how recordings are used, and delete files when you no longer need them.</li>
</ul></div>

<h2>What you can do with it</h2>
<ul>
<li>Transcribe interviews and focus groups, with speakers and timestamps, for analysis and reporting.</li>
<li>Translate reports, training videos and campaign material for communities that speak other languages.</li>
<li>Add subtitles to videos so they can be watched without sound.</li>
<li>Produce dubbed versions in selected languages for community screenings.</li>
</ul>

<h2>Planning around the budget</h2>
<p>Use Instant AI for everything first, since it is the lowest cost and ready in minutes.</p>
<p>Your first minutes each month are free, and paid plans include monthly minutes for transcription and translation, so predictable work is cheaper on a plan.</p>
<p>Add human review where the cost of an error is high, such as quotes in a report to a donor.</p>
<p>Organizations with an ongoing relationship can arrange invoicing and a negotiated rate on an Enterprise plan.</p>

<h2>Languages that models do not cover</h2>
<p>Speech recognition is strong in more than 70 languages, but not every community language is covered.</p>
<p>For a language no model supports yet, such as Kikuyu, a human transcriber types the source by ear, at a somewhat higher per-minute price.</p>
<p>Check the <a href="/languages">languages page</a> for what Instant AI covers, and the <a href="/human-review">human review page</a> for where a person checks the work.</p>

<h2>Privacy, consent and sensitive material</h2>
<p>Recordings from the field can include people who are at risk if they are identified.</p>
<p>Get informed consent that covers transcription, translation and storage, and record only what you need.</p>
<p>Files are visible only to your account and our staff, and we do not share your data or run ad tracking on this site.</p>
<p>Files are kept until you delete them, so use the Your data section in Settings, or ask us, to delete a file or your account data whenever you wish.</p>
<p>If you want a dub in the speaker's own voice, we require your confirmation of rights and consent before cloning runs.</p>

<h2>A workflow that works for small teams</h2>
<ol>
<li>Record with consent and clear audio.</li>
<li>Upload the files and choose Instant AI for transcription.</li>
<li>Read the transcript, and correct names and terms.</li>
<li>Choose human review for the material you will publish or send to funders.</li>
<li>Translate the final text into the languages your audiences use.</li>
</ol>

<h2>Next step</h2>
<p>See the <a href="/solutions/ngos">NGO and civil society page</a> for options and plans, or <a href="/demo">talk to us</a> about invoicing and volume.</p>
<p>You can also run a sample today, because the first minutes are free.</p>""",
    },
]


def seed_guides(author_id: str, publish: bool = True) -> list[tuple[str, str]]:
    """Insert each guide whose slug is neither in the blog nor in the seeded-guides marker. Returns [(slug, outcome)]."""
    from . import db

    conn = db.get_conn()
    conn.execute("CREATE TABLE IF NOT EXISTS seeded_guides (slug TEXT PRIMARY KEY, seeded_at REAL NOT NULL)")
    done = {r["slug"] for r in conn.execute("SELECT slug FROM seeded_guides")}
    conn.commit()
    conn.close()
    existing = {p["slug"] for p in db.list_blog_posts(published_only=False)}
    out = []
    for post in GUIDES:
        if post["slug"] in done:
            out.append((post["slug"], "already seeded"))
            continue
        if post["slug"] not in existing:
            db.create_blog_post(slug=post["slug"], title=post["title"], description=post["description"], body_html=post["body_html"],
                                author_id=author_id, status="published" if publish else "draft", category=post["category"])
            out.append((post["slug"], "published" if publish else "draft"))
        else:
            out.append((post["slug"], "kept the existing post"))
        conn = db.get_conn()
        conn.execute("INSERT OR IGNORE INTO seeded_guides (slug, seeded_at) VALUES (?, strftime('%s','now'))", (post["slug"],))
        conn.commit()
        conn.close()
    return out


def seed_guides_at_startup() -> None:
    """Called once when the app starts. Does nothing until a staff account exists to be the author."""
    try:
        from . import blog_seed
        author = blog_seed._default_author_id()
        if author:
            seed_guides(author)
    except Exception:
        pass     # a guide failing to seed must never stop the site starting
