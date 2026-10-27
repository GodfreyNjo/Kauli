"""Blog posts written for US/global search intent (transcription, subtitles,
translation) - the counterpart to the existing Kenya/Swahili-focused posts.

Kept as content in the repo and inserted by `seed_blog_posts` (idempotent: a
post whose slug already exists is skipped, never overwritten, so an edit made
in the blog editor is never clobbered). Run on the server with:

    docker exec kauli python3 -m webapp.blog_seed            # drafts
    docker exec kauli python3 -m webapp.blog_seed --publish  # live

House style (see blog_ai_assist.py): one sentence per paragraph, exactly
three key takeaways before the first <h2>, no invented statistics, clients or
claims. Competitor prices below were read from their public pricing pages in
October 2026 and are labelled as such with links; Kauli's own figures match
webapp/billing.py's US price card."""
from __future__ import annotations

import sys

POSTS = [
    {
        "slug": "how-to-transcribe-a-youtube-video",
        "title": "How to Transcribe a YouTube Video to Text (4 Methods, Compared)",
        "category": "Transcription Guides",
        "description": "Four ways to turn a YouTube video into text - the built-in transcript, caption downloads, AI transcription and human transcription - and when to use each.",
        "body_html": """<p>You found a YouTube video you need as text - for notes, a blog post, subtitles or research.</p>
<p>There are four realistic ways to get it, and the right one depends on whose video it is and how accurate the result has to be.</p>

<div class="m-key-takeaways"><p class="m-key-takeaways-label">Key takeaways</p><ul>
<li>YouTube's built-in transcript is free and instant, but it is auto-generated and usually needs cleanup.</li>
<li>If the video is yours, the cleanest route is to edit and download the captions in YouTube Studio.</li>
<li>AI transcription of the audio is fast and cheap, and human review is what you add when the text will be published or quoted.</li>
</ul></div>

<h2>Method 1: Use YouTube's built-in transcript</h2>
<p>Open the video, expand the description, and look for "Show transcript" - when a video has captions, YouTube lists them as timestamped text you can copy.</p>
<p>It costs nothing and takes seconds.</p>
<p>The catch is quality: auto-generated captions can miss names, jargon and accents, and they rarely have polished punctuation.</p>
<p>It also isn't available on every video, because the creator may have captions turned off.</p>

<h2>Method 2: Edit and download captions (your own videos)</h2>
<p>If the video is yours, YouTube Studio's Subtitles section lets you edit the automatic captions and download them as a subtitle file.</p>
<p>That is the best option for your own content, because you correct the text once and reuse it everywhere.</p>
<p>Only do this with videos you own or have permission to use - for someone else's video, ask first.</p>

<h2>Method 3: Transcribe the audio with an AI service</h2>
<p>AI transcription services take the video's audio and return a transcript, usually within minutes.</p>
<p>With Kauli you can paste a YouTube link or upload the file, choose the spoken language and download the result as Word, text or SRT.</p>
<p>Instant AI transcription starts at $0.12 per audio-minute in the US, and it is machine-generated, so skim it for names and numbers before you publish.</p>
<p>It covers 77 languages - see the <a href="/languages">full language list</a>.</p>
<p>YouTube sometimes blocks automated downloads of particular videos, and if a link fails you can upload the video file instead.</p>

<h2>Method 4: Pay for human transcription</h2>
<p>For legal material, medical content or quotes you will publish, a person should check the text against the audio.</p>
<p>Kauli's Human-reviewed option adds a real reviewer for Swahili, Kikuyu and English, and other services offer human transcription in more languages.</p>
<p>Human transcription costs far more per minute, so reserve it for text that has to be right.</p>

<h2>Which method should you use?</h2>
<table>
<thead><tr><th>Method</th><th>Cost</th><th>Speed</th><th>Best for</th></tr></thead>
<tbody>
<tr><td>Built-in transcript</td><td>Free</td><td>Instant</td><td>Quick reference and rough notes</td></tr>
<tr><td>Studio captions (your video)</td><td>Free</td><td>Minutes, plus your editing time</td><td>Subtitles for your own channel</td></tr>
<tr><td>AI transcription</td><td>Low, per minute</td><td>Minutes</td><td>Drafts, notes, search and first-pass subtitles</td></tr>
<tr><td>Human transcription</td><td>Highest, per minute</td><td>Hours to days</td><td>Published, legal or quoted text</td></tr>
</tbody></table>

<h2>Tips for a cleaner transcript</h2>
<ul>
<li><strong>Pick the right spoken language.</strong> A wrong language setting is the most common cause of garbage output.</li>
<li><strong>Use the cleanest audio you can.</strong> Background music and crosstalk lower accuracy for every method.</li>
<li><strong>Proofread proper nouns.</strong> Names, brands and places are where machines slip most often.</li>
<li><strong>Keep timestamps if you'll make subtitles.</strong> An SRT file carries them automatically.</li>
</ul>

<p>Want to try it on your own video? Your first 5 minutes of Instant AI transcription are free after a one-time $1 card check that is credited to your account - start with our <a href="/solutions/youtube-video-to-text">YouTube to text</a> page.</p>""",
    },
    {
        "slug": "ai-vs-human-transcription-which-do-you-need",
        "title": "AI vs Human Transcription: Which One Do You Actually Need?",
        "category": "Transcription Guides",
        "description": "A practical way to choose between AI and human transcription: what each does well, where each fails, and how to decide by the cost of an error, not the hype.",
        "body_html": """<p>"AI transcription is nearly perfect now" and "never trust AI transcription" are both too blunt to help you decide anything.</p>
<p>The useful question is what happens if a word is wrong - because that tells you how much checking the text deserves.</p>

<div class="m-key-takeaways"><p class="m-key-takeaways-label">Key takeaways</p><ul>
<li>AI transcription is fast and inexpensive, but nobody has checked it against the audio.</li>
<li>A human reviewer catches names, idioms and context that a machine gets wrong without sounding wrong.</li>
<li>Choose by the cost of an error: drafts and notes can run on AI alone, while anything published, legal or quoted should be reviewed.</li>
</ul></div>

<h2>What AI transcription does well</h2>
<p>With clear audio and one speaker, modern AI transcription is usually accurate enough to read and search.</p>
<p>It is excellent for meeting notes, interview first passes, searchable archives and rough subtitle drafts.</p>
<p>It also returns results in minutes, which changes how often you can afford to transcribe things at all.</p>

<h2>Where AI transcription struggles</h2>
<ul>
<li><strong>Overlapping speakers.</strong> Crosstalk is hard to separate and attribute.</li>
<li><strong>Strong accents and dialects.</strong> Accuracy varies a lot by language and region.</li>
<li><strong>Noise and music.</strong> Background sound competes with speech.</li>
<li><strong>Names and jargon.</strong> Proper nouns and specialist terms are guessed, not known.</li>
<li><strong>Code-switching.</strong> Speakers who mix languages mid-sentence confuse single-language models.</li>
</ul>
<p>The dangerous part is that errors rarely look like errors - the text reads fluently while saying something slightly different.</p>

<h2>What a human reviewer adds</h2>
<p>A reviewer listens to the source audio while reading the draft, so they can fix names, attribute speakers and resolve unclear phrases.</p>
<p>They also give you accountability: a person has looked at the text and stands behind it.</p>
<p>The cost is time and money, which is why most people use the hybrid approach below.</p>

<h2>The hybrid: AI draft, then human review</h2>
<p>The efficient way to get reviewed text is to let AI produce the draft and have a person check it, instead of transcribing from scratch.</p>
<p>That is how Kauli's two options work: Instant AI delivers the machine draft in minutes, and Human-reviewed adds a real reviewer before delivery.</p>

<table>
<thead><tr><th></th><th>Instant AI</th><th>Human-reviewed</th></tr></thead>
<tbody>
<tr><td>Transcription price (US)</td><td>From $0.12 per audio-minute</td><td>From $1.20 per audio-minute</td></tr>
<tr><td>Reviewed by a person</td><td>No - labelled as AI-generated</td><td>Yes, before delivery</td></tr>
<tr><td>Languages</td><td>77</td><td>Swahili, Kikuyu and English</td></tr>
<tr><td>Typical use</td><td>Notes, drafts, search, first-pass subtitles</td><td>Published subtitles, quotes, anything with your name on it</td></tr>
</tbody></table>

<p>If you need human-reviewed transcription in a language outside Swahili, Kikuyu and English, we don't offer it - other agencies do.</p>

<h2>A simple decision rule</h2>
<ul>
<li><strong>Will anyone act on, publish or quote this text?</strong> If yes, add human review.</li>
<li><strong>Is it a legal, medical or compliance record?</strong> Use human review, whatever the cost.</li>
<li><strong>Is it for your own notes or search?</strong> AI alone is usually enough.</li>
<li><strong>Is the audio noisy, multi-speaker or in a low-resource language?</strong> Lean towards review.</li>
</ul>

<p>You can compare both on the <a href="/#pricing">pricing section</a> or start with the <a href="/solutions/audio-to-text">audio to text</a> page.</p>""",
    },
    {
        "slug": "transcription-cost-2026-price-guide",
        "title": "How Much Does Transcription Cost in 2026? A Plain-English Price Guide",
        "category": "Transcription Guides",
        "description": "What AI and human transcription cost per minute in 2026, based on providers' public pricing pages, plus how to avoid paying for more accuracy than you need.",
        "body_html": """<p>Transcription prices look chaotic: per minute, per month, per page, with different rates for AI and human work.</p>
<p>This guide puts them on one scale - cost per audio-minute - using what providers publish on their own pricing pages.</p>
<p>Kauli sells transcription too, so treat this as one provider's honest notes, with links so you can check every number yourself.</p>

<div class="m-key-takeaways"><p class="m-key-takeaways-label">Key takeaways</p><ul>
<li>Human transcription is priced around $2 per audio-minute at the providers we checked, while AI transcription is a small fraction of that.</li>
<li>Subscriptions can be the cheapest per minute but only if you actually use the included minutes every month.</li>
<li>The best way to save money is to match the service to the stakes instead of buying human review for everything.</li>
</ul></div>

<h2>The three pricing models</h2>
<ul>
<li><strong>Pay per minute.</strong> You pay for exactly the audio you submit, with no commitment.</li>
<li><strong>Subscription minutes.</strong> A monthly fee includes a bucket of minutes, and unused minutes are usually wasted.</li>
<li><strong>Per page or per project.</strong> Common for legal transcripts and quotes, and harder to compare.</li>
</ul>

<h2>Human transcription</h2>
<p>Rev lists human transcription at $1.99 per minute on its <a href="https://www.rev.com/pricing" target="_blank" rel="noopener">public pricing page</a>.</p>
<p>Happy Scribe lists human-made transcription starting from $2.00 per minute on its <a href="https://www.happyscribe.com/pricing" target="_blank" rel="noopener">pricing page</a>.</p>
<p>So at the two providers we checked, human transcription is roughly $2 per audio-minute at standard turnaround.</p>

<h2>AI transcription</h2>
<p>Happy Scribe lists additional AI minutes at $0.20 per minute on top of its plans.</p>
<p>Transkriptor's <a href="https://transkriptor.com/pricing/" target="_blank" rel="noopener">public plans</a> include $9.99 per month for 300 minutes and $19.99 per month for 2,400 minutes (disclosure: Transkriptor is one of the vendors behind part of Kauli's own AI transcription).</p>
<p>Kauli's Instant AI transcription is pay-as-you-go from $0.12 per audio-minute in the US, with no subscription.</p>

<h2>Translation and subtitles</h2>
<p>Rev lists human-made global subtitles from $6.49 per minute for Spanish and Hindi, rising to $15.99 per minute for Japanese and Korean.</p>
<p>Kauli's Instant AI translation is $0.30 per audio-minute in the US, but it is machine translation and is not reviewed by a person.</p>
<p>Human review is available for Swahili, Kikuyu and English at $1.40 per audio-minute for transcription plus translation.</p>

<h2>What 10 hours of audio actually costs</h2>
<p>Ten hours is 600 minutes, so the arithmetic is simple.</p>
<table>
<thead><tr><th>Rate per minute</th><th>Total for 600 minutes</th></tr></thead>
<tbody>
<tr><td>$0.12 (Kauli Instant AI)</td><td>$72</td></tr>
<tr><td>$1.20 (Kauli Human-reviewed)</td><td>$720</td></tr>
<tr><td>$1.99 (Rev human)</td><td>$1,194</td></tr>
</tbody></table>
<p>The difference between AI and human is not a rounding error, which is why the choice matters.</p>

<h2>How to avoid overpaying</h2>
<ul>
<li><strong>Review only what needs it.</strong> Use AI for notes and search, and pay for review on what gets published.</li>
<li><strong>Do the subscription math.</strong> A plan only wins if you'll really use the minutes each month.</li>
<li><strong>Check turnaround surcharges.</strong> Rush delivery usually costs more.</li>
<li><strong>Clean your audio first.</strong> Noisy files cost more in review time later.</li>
</ul>

<p>Prices change often, so check the linked pages before you decide - and our own <a href="/#pricing">pricing</a> is always current.</p>""",
    },
    {
        "slug": "add-subtitles-to-video-in-another-language",
        "title": "How to Add Subtitles in Another Language to Your Video (Step by Step)",
        "category": "Transcription Guides",
        "description": "A step-by-step workflow for translating video subtitles: transcribe, translate, review, export SRT or VTT, and publish on YouTube or burn the captions in.",
        "body_html": """<p>Subtitles in a second language let people watch your video even when they don't speak the original one.</p>
<p>The workflow is the same whatever tool you use: get an accurate transcript, translate it, check it, export a subtitle file and publish.</p>

<div class="m-key-takeaways"><p class="m-key-takeaways-label">Key takeaways</p><ul>
<li>Accurate subtitles start with an accurate transcript in the original language.</li>
<li>Machine translation is a fast first draft, but a person who knows the language should check names, idioms and numbers.</li>
<li>SRT and VTT files can be uploaded to platforms like YouTube, while burned-in captions are the option for places that don't accept caption files.</li>
</ul></div>

<h2>Step 1: Get a transcript in the original language</h2>
<p>Every translation is only as good as the text it starts from.</p>
<p>Transcribe the audio in its original language, choose the right spoken language and variant, and fix obvious errors before translating.</p>

<h2>Step 2: Translate the text</h2>
<p>Machine translation is quick and inexpensive, and it is a sensible first draft.</p>
<p>Kauli's Instant AI can translate between 76 languages, and it labels the result as machine translation that no person has reviewed.</p>
<p>For Swahili, Kikuyu and English you can add human review, which is worth it for anything you'll publish.</p>

<h2>Step 3: Check the translation</h2>
<p>Ideally a native speaker reads the subtitles while watching the video.</p>
<ul>
<li><strong>Names and places.</strong> They should be spelled consistently and correctly.</li>
<li><strong>Numbers, dates and times.</strong> Formats and conventions differ between languages.</li>
<li><strong>Idioms and humour.</strong> A literal translation often says something odd.</li>
<li><strong>Length.</strong> Subtitles need to be readable in the time they're on screen, so long lines should be shortened.</li>
</ul>

<h2>Step 4: Export a subtitle file</h2>
<p>SRT is the simplest and most widely supported subtitle format.</p>
<p>VTT is similar and is the standard for web video players.</p>
<p>Both store the text with start and end times, and Kauli provides SRT and VTT downloads.</p>

<h2>Step 5: Publish</h2>
<table>
<thead><tr><th>Option</th><th>How it works</th><th>Best for</th></tr></thead>
<tbody>
<tr><td>Upload an SRT or VTT file</td><td>In YouTube Studio, add a subtitle language and upload the file</td><td>YouTube and players that support caption files</td></tr>
<tr><td>Burned-in captions</td><td>The text is rendered permanently into the video</td><td>Social platforms and places without caption support</td></tr>
</tbody></table>
<p>Burned-in captions can't be turned off or translated again, so keep the original video too.</p>

<h2>A quick workflow that works</h2>
<ul>
<li>Transcribe with <a href="/solutions/audio-to-text">audio to text</a>.</li>
<li>Translate the transcript with Instant AI, or order human review for Swahili, Kikuyu and English.</li>
<li>Download the SRT, skim it against the video, and upload it.</li>
</ul>

<p>If you're starting with a YouTube video, the <a href="/solutions/youtube-video-to-text">YouTube to text</a> page shows how to begin.</p>""",
    },
    {
        "slug": "how-to-transcribe-swahili-audio",
        "title": "How to Transcribe Swahili Audio: What Works, What Doesn't, and What to Check",
        "category": "Transcription Guides",
        "description": "Why Swahili is harder than it looks for automatic transcription - code-switching, Sheng, regional differences - and how to get a transcript you can trust.",
        "body_html": """<p>Swahili is widely spoken across East Africa, and plenty of tools list it as "supported".</p>
<p>In practice the quality varies a lot with how people actually speak, so it helps to know where the risks are.</p>

<div class="m-key-takeaways"><p class="m-key-takeaways-label">Key takeaways</p><ul>
<li>Real Swahili speech mixes in English, slang and regional phrasing that automatic transcription often handles unevenly.</li>
<li>A machine draft is a fast starting point, but names, numbers and time expressions need a human check.</li>
<li>Kikuyu is different again: no speech-recognition model supports it yet, so it needs a person to transcribe it.</li>
</ul></div>

<h2>Why Swahili audio is tricky</h2>
<ul>
<li><strong>Code-switching.</strong> Speakers move between Swahili and English mid-sentence, which confuses single-language models.</li>
<li><strong>Sheng and slang.</strong> Urban Kenyan speech uses street slang that standard models rarely learned. See our post on <a href="/blog/sheng-vs-standard-swahili-machine-translation">Sheng and machine translation</a>.</li>
<li><strong>Regional differences.</strong> Kenyan and Tanzanian Swahili differ in vocabulary and phrasing.</li>
<li><strong>The Swahili clock.</strong> Time expressions count from a different starting hour, a classic source of errors - read <a href="/blog/the-swahili-clock-why-saa-tisa-asubuhi-isn-t-9-am">why saa tisa asubuhi isn't 9 AM</a>.</li>
<li><strong>Names and places.</strong> Proper nouns are guessed unless a person corrects them.</li>
</ul>

<h2>A workflow that works</h2>
<p>Start with the cleanest audio you can get, and choose Swahili as the spoken language.</p>
<p>Run an AI draft to get timestamped text quickly.</p>
<p>Then have someone who speaks the language read the draft while listening to the audio.</p>
<p>Kauli's Human-reviewed option does that step for you, with a real reviewer checking the AI draft before delivery.</p>

<h2>What to check in a Swahili transcript</h2>
<ul>
<li><strong>Time expressions.</strong> Make sure hours were interpreted the way the speaker meant them.</li>
<li><strong>Numbers and amounts.</strong> Money, dates and phone numbers are costly if wrong.</li>
<li><strong>Borrowed English words.</strong> They should appear as spoken, not mangled into similar-sounding Swahili.</li>
<li><strong>Speaker labels.</strong> In interviews, each person's words should be attributed correctly.</li>
</ul>

<h2>What about Kikuyu?</h2>
<p>Kikuyu has far less training data than Swahili, and no speech-recognition model supports it today.</p>
<p>At Kauli, Kikuyu orders are transcribed by a real person - read <a href="/blog/why-ai-cant-transcribe-kikuyu">why most AI tools can't transcribe Kikuyu</a>.</p>

<h2>Next steps</h2>
<p>You can transcribe Swahili audio with <a href="/solutions/video-transcription">Kauli's Swahili and Kikuyu transcription</a>, choosing Instant AI for speed or Human-reviewed for accuracy.</p>
<p>If you also need English subtitles, translation and review are available for Swahili, Kikuyu and English.</p>""",
    },
    {
        "slug": "podcast-transcripts-why-and-how",
        "title": "Podcast Transcripts: Why Bother, and How to Make One Without Losing a Weekend",
        "category": "Transcription Guides",
        "description": "What a podcast transcript is good for - accessibility, show notes, repurposing, search - and a quick, low-cost way to make one for every episode.",
        "body_html": """<p>A podcast is audio, and audio is hard to skim, search or quote.</p>
<p>A transcript fixes that, and with AI it no longer takes a weekend to produce one.</p>

<div class="m-key-takeaways"><p class="m-key-takeaways-label">Key takeaways</p><ul>
<li>A transcript makes an episode accessible, skimmable and quotable.</li>
<li>Published on the episode page, it gives search engines text to read instead of only audio.</li>
<li>AI transcription makes it practical for every episode, and a quick proofread of names and terms is usually all it needs.</li>
</ul></div>

<h2>What a transcript is good for</h2>
<ul>
<li><strong>Accessibility.</strong> People who are deaf or hard of hearing, or listening somewhere they can't use sound, can still follow the episode.</li>
<li><strong>Show notes and articles.</strong> You can lift quotes, summaries and chapter points straight from the text.</li>
<li><strong>Search on your site.</strong> Visitors can find the episode that discussed a topic.</li>
<li><strong>Repurposing.</strong> Clips, newsletters and social posts start from real wording.</li>
<li><strong>Translation.</strong> Text can be translated to reach listeners in other languages.</li>
</ul>

<h2>How to make one</h2>
<p>Export the finished episode audio, ideally the mixed file listeners hear.</p>
<p>Upload it to a transcription service and choose the spoken language.</p>
<p>Download the transcript with speaker labels and timestamps if you want chapters.</p>
<p>Read it once while listening to a few sections, fixing names, brands and jargon.</p>
<p>Publish the text on the episode page rather than hiding it in a PDF, so it is part of the page.</p>

<h2>What it costs</h2>
<p>At $0.12 per audio-minute for Instant AI, a 60-minute episode comes to about $7.20.</p>
<p>That is a cost you can afford on every episode of a weekly show.</p>

<h2>Honest caveats</h2>
<ul>
<li><strong>AI isn't perfect.</strong> Guest names and technical terms are the usual errors.</li>
<li><strong>A transcript doesn't guarantee rankings.</strong> It gives you more useful text on the page, which is all it can promise.</li>
<li><strong>Edit before publishing.</strong> Instant AI is not reviewed by a person, so give it a quick read.</li>
</ul>

<p>Start with our <a href="/solutions/podcast-transcription">podcast transcription</a> page, and your first 5 minutes are free after a one-time $1 card check that is credited to your account.</p>""",
    },
    {
        "slug": "interview-transcription-checklist-research-journalism",
        "title": "Transcribing Interviews for Research and Journalism: A Practical Checklist",
        "category": "Transcription Guides",
        "description": "A practical checklist for transcribing interviews: consent, recording quality, speaker labels, verbatim vs clean read, and checking quotes.",
        "body_html": """<p>An interview is only as useful as the text you can quote from it accurately.</p>
<p>This checklist covers what to do before, during and after transcription so that your quotes stand up.</p>

<div class="m-key-takeaways"><p class="m-key-takeaways-label">Key takeaways</p><ul>
<li>Good transcripts start with consent and a clean recording, long before any software is involved.</li>
<li>Always verify quotes against the audio, because machine transcription can sound fluent while being wrong.</li>
<li>Decide on verbatim or clean read up front, and handle sensitive files with the same care as the interview itself.</li>
</ul></div>

<h2>Before the interview</h2>
<ul>
<li><strong>Get consent to record.</strong> Tell participants you're recording and how the audio will be used.</li>
<li><strong>Tell them about processing.</strong> If a transcription service will process the audio, say so.</li>
<li><strong>Test your audio.</strong> One speaker per microphone and a quiet room beat any software fix.</li>
<li><strong>Record in the best format available.</strong> Avoid heavy compression where you can.</li>
</ul>

<h2>Choosing verbatim or clean read</h2>
<p>A clean read removes filler words and false starts so the text reads smoothly.</p>
<p>Verbatim keeps every false start and filler, which matters for linguistics, legal records and some research methods.</p>
<p>Our <a href="/formatting-standards">formatting standards</a> explain both, and on human-reviewed orders you choose per order.</p>

<h2>During transcription</h2>
<ul>
<li><strong>Use speaker labels.</strong> Quotes must be attributed to the right person.</li>
<li><strong>Keep timestamps.</strong> They let you find the exact moment for verification.</li>
<li><strong>Pick the right language.</strong> A mismatch produces nonsense, and multilingual interviews may need extra care.</li>
</ul>

<h2>After transcription</h2>
<ol>
<li>Read the transcript while listening to the sections you'll quote.</li>
<li>Correct names, numbers, places and technical terms.</li>
<li>Confirm every quote against the audio before publishing.</li>
<li>Anonymise or redact where participants were promised confidentiality.</li>
<li>Store files securely and delete them when you no longer need them.</li>
</ol>

<h2>A note on AI and accuracy</h2>
<p>Instant AI transcription is machine-generated and not reviewed by a person, so treat it as a draft.</p>
<p>For Swahili, Kikuyu and English you can add human review, where a real editor checks the text against the audio.</p>
<p>For sensitive material, read our <a href="/privacy">privacy policy</a> to see how files and our transcription providers are handled.</p>

<p>When you're ready, our <a href="/solutions/interview-transcription">interview transcription</a> page has the details, and you can transcribe in the language the interview was held in.</p>""",
    },
    {
        "slug": "translate-video-chinese-spanish-to-english",
        "title": "How to Translate a Video From Chinese, Spanish or Any Other Language Into English",
        "category": "Transcription Guides",
        "description": "A practical workflow for translating video from Chinese, Spanish and other languages into English - transcribe, translate, check, and export subtitles.",
        "body_html": """<p>You have a video in Chinese, Spanish or another language, and you need English you can use.</p>
<p>The reliable way is a short pipeline: transcribe in the original language, translate, check, then export subtitles.</p>

<div class="m-key-takeaways"><p class="m-key-takeaways-label">Key takeaways</p><ul>
<li>Transcribe first in the original language, because translation can only be as good as that text.</li>
<li>Machine translation is fast and low-cost, but check names, numbers, idioms and formality before you rely on it.</li>
<li>Pick the right variant of the language, such as Mandarin or Cantonese, or Latin American or European Spanish.</li>
</ul></div>

<h2>Step 1: Transcribe in the original language</h2>
<p>Choose the spoken language carefully, because variants differ.</p>
<ul>
<li><strong>Chinese.</strong> Mandarin and Cantonese are separate options - see <a href="/transcribe/chinese-mandarin">Mandarin Chinese</a> and <a href="/transcribe/chinese-cantonese">Cantonese Chinese</a>.</li>
<li><strong>Spanish.</strong> Regional vocabulary varies - see <a href="/transcribe/spanish">Spanish transcription</a>.</li>
<li><strong>Everything else.</strong> Browse the <a href="/languages">full list of 77 languages</a>.</li>
</ul>

<h2>Step 2: Translate into English</h2>
<p>Kauli's Instant AI translates the transcript into English, or into any of the other languages we support, in minutes.</p>
<p>In the US it is $0.30 per audio-minute for transcription plus translation.</p>
<p>It is machine translation and is not reviewed by a person, and it is labelled that way on every order.</p>

<h2>Step 3: Check what machines get wrong</h2>
<ul>
<li><strong>Names.</strong> People, brands and places should keep their original form or an agreed romanisation.</li>
<li><strong>Numbers and dates.</strong> Conventions differ, so verify each one.</li>
<li><strong>Idioms.</strong> Literal translations of sayings can be confusing or wrong.</li>
<li><strong>Formality.</strong> Many languages mark politeness in ways English doesn't, so tone can shift.</li>
</ul>
<p>If the English will represent someone publicly, ask a bilingual person to read it against the video.</p>

<h2>Step 4: Export subtitles</h2>
<p>Download the English subtitles as SRT or VTT and upload them to your video platform.</p>
<p>Our guide to <a href="/blog/add-subtitles-to-video-in-another-language">adding subtitles in another language</a> covers the publishing options.</p>

<h2>What we don't do (yet)</h2>
<p>Dubbing and human review are available for Swahili, Kikuyu and English, not for Chinese, Spanish or the other languages.</p>
<p>For those you get transcription and Instant AI translation, with SRT, Word and text downloads.</p>

<p>Ready to try it? Start from the <a href="/solutions/audio-to-text">audio to text</a> page - your first 5 minutes are free after a one-time $1 card check credited to your account.</p>""",
    },
]


def seed_blog_posts(author_id: str, publish: bool = False) -> list[tuple[str, str]]:
    """Insert every post whose slug doesn't exist yet. Returns [(slug, outcome)]."""
    from . import db

    results = []
    existing = {p["slug"] for p in db.list_blog_posts(published_only=False)}
    for post in POSTS:
        if post["slug"] in existing:
            results.append((post["slug"], "skipped (already exists)"))
            continue
        db.create_blog_post(
            slug=post["slug"], title=post["title"], description=post["description"],
            body_html=post["body_html"], author_id=author_id,
            status="published" if publish else "draft", category=post["category"],
        )
        results.append((post["slug"], "published" if publish else "draft"))
    return results


def _default_author_id() -> str | None:
    from . import db
    conn = db.get_conn()
    row = conn.execute("SELECT id FROM users WHERE role = 'staff' ORDER BY is_admin DESC, created_at ASC LIMIT 1").fetchone()
    conn.close()
    return row["id"] if row else None


if __name__ == "__main__":
    publish = "--publish" in sys.argv
    author = None
    if "--author-id" in sys.argv:
        author = sys.argv[sys.argv.index("--author-id") + 1]
    author = author or _default_author_id()
    if not author:
        sys.exit("No staff user found to author the posts - pass --author-id <user id>.")
    for slug, outcome in seed_blog_posts(author, publish=publish):
        print(f"{outcome:28s} {slug}")
