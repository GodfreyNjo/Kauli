"""Content for the human-review pages: /human-review and one page per language.

Availability is NOT stored here: it is read from the live configuration (see app._human_review_live), so a language
only ever says "Available now" when an order for it can really be placed. Everything below is written to be true
whether or not a language is live yet.
"""
from __future__ import annotations

# slug -> language. `code` matches the codes the order form uses (Kikuyu is "ke").
LANGUAGES = {
    "english": {
        "code": "en", "name": "English", "native": "English", "flag": "flag-en",
        "countries": ["US", "GB", "CA", "AU", "KE", "NG", "IN"],
        "regions": "the United States, the United Kingdom, Canada, Australia and everywhere English is a working language",
        "title": "Human-Reviewed English Transcription, Subtitles and Dubbing",
        "description": "English transcripts and subtitles checked by a real editor against your audio. Accents, names, numbers and technical terms handled before you publish.",
        "headline": "English you can publish without a second read",
        "intro": "Instant AI is very good at clear English. Human review is for the moments it is not: strong accents, overlapping speakers, product names, numbers and specialist terms.",
        "context": "English audio is rarely one English. A Nairobi panel, a Lagos podcast and a Chicago webinar all sound different, and automatic transcripts stumble on all three in different places.",
        "checks": ["Names of people, places and brands", "Numbers, dates and currencies", "Industry terms and acronyms", "Speaker changes and who said what"],
        "faq": [("Is English human review the same price as other languages?", "Human review is priced per audio-minute and shown before you order. See the pricing section for the current rate."),
                ("Can you review a transcript I already have?", "Send us the audio with your transcript requirements in the order notes and an editor works from the audio, not from the draft alone.")],
        "keywords": ["human", "review", "english", "transcri", "accuracy", "subtitle"],
    },
    "swahili": {
        "code": "sw", "name": "Swahili", "native": "Kiswahili", "flag": "flag-ke",
        "countries": ["KE", "TZ", "UG", "RW", "CD"],
        "regions": "Kenya, Tanzania, Uganda, Rwanda and the eastern Democratic Republic of the Congo",
        "title": "Swahili Dubbing, Translation, Subtitles and Human Review",
        "description": "Convert a Swahili video to English, or back: dubbing, translation, subtitles and captions for YouTube or any video. AI-drafted in minutes, checked by a human editor. Transparent per-minute pricing.",
        "headline": "Swahili and English dubbing, without the six-week turnaround",
        "intro": "Kauli began with Swahili and Kikuyu, and it is still where our editors are strongest. Upload a video, choose the languages, and get a draft in minutes that a real editor checks line by line.",
        "context": "Swahili changes between Nairobi, Dar es Salaam and the coast, and everyday speech mixes in English and Sheng. A tool that only knows textbook Swahili misses what your audience actually says.",
        "checks": ["Kenyan, Tanzanian and coastal usage", "Code-switching with English and Sheng", "Names, places and local institutions", "Time expressions, such as saa moja meaning seven o'clock"],
        "faq": [("Can you dub a Swahili video into English?", "Yes. Choose full dubbing, Swahili as the spoken language and English as the target. The translated English is spoken in a synthetic voice; your original Swahili is never re-voiced unless you ask for the reverse."),
                ("Is a person involved?", "On human-reviewed orders an editor checks the transcript and translation against the audio before the order is marked ready. Instant AI orders are machine-generated and say so."),
                ("Can you add Swahili subtitles to a YouTube video?", "Yes. Paste the link, pick subtitles, and download SRT or VTT files, or connect your channel to publish captions.")],
        "keywords": ["swahili", "kiswahili", "dubbing", "kenya", "subtitle", "translate"],
        "legacy": [("/solutions/video-dubbing", "Video dubbing between Swahili, Kikuyu and English"),
                   ("/solutions/video-translation", "Swahili, Kikuyu and English video translation"),
                   ("/solutions/captioning", "Subtitles and captions for YouTube and any video"),
                   ("/solutions/ngos", "Programs and NGOs working in Swahili and Kikuyu"),
                   ("/solutions/youtubers", "Swahili and Kikuyu for YouTube channels")],
    },
    "kikuyu": {
        "code": "ke", "name": "Kikuyu", "native": "Gĩkũyũ", "flag": "flag-ke",
        "countries": ["KE"],
        "regions": "Central Kenya, Nairobi and the Kikuyu diaspora around the world",
        "title": "Kikuyu (Gĩkũyũ) Dubbing, Translation and Human-Reviewed Transcription",
        "description": "Kikuyu transcription, translation, subtitles and dubbing between Kikuyu, Swahili and English, written and checked by people because no automatic tool handles Gĩkũyũ well.",
        "headline": "Gĩkũyũ is written by people, so it is reviewed by people",
        "intro": "Almost no speech tool understands Kikuyu. Kauli does not pretend otherwise: a person transcribes it, a person checks it, and then it can be translated, subtitled or dubbed.",
        "context": "Kikuyu is tonal and uses letters that machines drop, such as ĩ and ũ. Content for Central Kenya audiences, from church recordings to radio and community video, deserves spelling that is right.",
        "checks": ["Correct diacritics and spelling", "Proverbs and idiom translated for meaning, not word for word", "Names, clans and places", "Switches between Kikuyu, Swahili and English"],
        "faq": [("Why is Kikuyu always human-reviewed?", "There is no reliable speech recogniser for Kikuyu, so a person transcribes it. That is why Kikuyu orders cost a little more per minute."),
                ("Can you dub Kikuyu into English or Swahili?", "Yes. After the transcript is written and checked, translation and dubbing follow the same pipeline as every other language pair."),
                ("Can I get Kikuyu subtitles for a video?", "Yes. Choose subtitles with Kikuyu as the spoken language and download SRT or VTT files.")],
        "keywords": ["kikuyu", "gikuyu", "kenya", "dubbing", "human", "swahili"],
        "legacy": [("/solutions/video-dubbing", "Video dubbing between Swahili, Kikuyu and English"),
                   ("/solutions/video-translation", "Swahili, Kikuyu and English video translation"),
                   ("/solutions/ngos", "Programs and NGOs working in Swahili and Kikuyu"),
                   ("/solutions/e-learning", "Training and e-learning in Swahili and Kikuyu")],
    },
    "chinese": {
        "code": "zh", "name": "Chinese", "native": "中文", "flag": "",
        "countries": ["CN", "TW", "HK", "SG"],
        "regions": "mainland China, Taiwan, Hong Kong and Singapore",
        "title": "Human-Reviewed Chinese Transcription, Translation and Subtitles",
        "description": "Mandarin transcription, translation and subtitles checked by a person: names, measure words and register handled before you publish for audiences in China, Taiwan and Singapore.",
        "headline": "Chinese that reads naturally to the people it is for",
        "intro": "Machine translation gets the words. A reviewer gets the register: how formal to be, which characters your audience expects, and how a name should be written.",
        "context": "Mainland audiences read Simplified characters and Taiwan and Hong Kong audiences read Traditional. Names, brands and numbers need care in either.",
        "checks": ["Simplified or Traditional characters for your audience", "Names and brand renderings kept consistent", "Measure words and natural phrasing", "Formal or conversational register"],
        "faq": [("Which Chinese do you work in?", "Mandarin. Tell us in the order notes whether your audience reads Simplified or Traditional characters."),
                ("Is Chinese human review available now?", "The page above shows the live status. Where it says coming soon, you can still order Instant AI transcription and translation today.")],
        "keywords": ["chinese", "mandarin", "translate", "subtitle", "human"],
    },
    "german": {
        "code": "de", "name": "German", "native": "Deutsch", "flag": "flag-de",
        "countries": ["DE", "AT", "CH"],
        "regions": "Germany, Austria and Switzerland",
        "title": "Human-Reviewed German Transcription, Translation and Subtitles",
        "description": "German transcripts, translations and subtitles checked by a person: compound words, formal and informal address and regional usage handled for Germany, Austria and Switzerland.",
        "headline": "German with the right level of formal",
        "intro": "German punishes small mistakes: a wrong compound, a du where Sie belongs, a Swiss word in a German video. A reviewer catches what a model passes over.",
        "context": "Standard German differs across Germany, Austria and Switzerland, and spoken Swiss German is a different matter again. Say which audience you write for and the review follows it.",
        "checks": ["Du or Sie used consistently", "Compound words and capitalisation", "Austrian and Swiss usage when it applies", "Numbers, units and date formats"],
        "faq": [("Can you review Swiss German audio?", "We work in standard German. Tell us if the speakers use a regional dialect so we can flag what needs extra care."),
                ("Is German human review available now?", "The status badge on this page is live. Where it says coming soon, Instant AI transcription and translation are available today.")],
        "keywords": ["german", "deutsch", "translate", "subtitle", "human"],
    },
    "spanish": {
        "code": "es", "name": "Spanish", "native": "Español", "flag": "flag-es",
        "countries": ["ES", "MX", "AR", "CO", "US"],
        "regions": "Spain, Latin America and Spanish-speaking communities in the United States",
        "title": "Human-Reviewed Spanish Transcription, Translation and Subtitles",
        "description": "Spanish transcripts and subtitles checked by a person: regional vocabulary, tú, usted and vos, and names handled for Spain and Latin America.",
        "headline": "Spanish for Madrid, Mexico City and Buenos Aires",
        "intro": "One Spanish does not fit every audience. A reviewer checks that vocabulary and address suit the people who will watch, not a neutral average.",
        "context": "Words for everyday things change between Spain and Latin America, and between Latin American countries. The right choice depends on your audience, and you can tell us in the order notes.",
        "checks": ["Regional vocabulary for your audience", "Tú, usted or vos used consistently", "Accents and punctuation", "Names, places and brands"],
        "faq": [("Which Spanish do you review?", "Tell us your audience in the order notes: Spain, Mexico, Argentina, Colombia or the United States, and the review follows it."),
                ("Is Spanish human review available now?", "The status badge on this page is live. Where it says coming soon, Instant AI transcription and translation are available today.")],
        "keywords": ["spanish", "espanol", "translate", "subtitle", "human"],
    },
    "french": {
        "code": "fr", "name": "French", "native": "Français", "flag": "flag-fr",
        "countries": ["FR", "CA", "BE", "CH", "SN", "CI"],
        "regions": "France, Canada, Belgium, Switzerland and French-speaking Africa",
        "title": "Human-Reviewed French Transcription, Translation and Subtitles",
        "description": "French transcripts and subtitles checked by a person: tu and vous, Québec and African French, accents and names handled for your audience.",
        "headline": "French from Paris to Montréal to Dakar",
        "intro": "French is spoken on four continents and written one way. A reviewer makes sure your subtitles and transcripts suit the audience in front of you.",
        "context": "Quebec French, Belgian French and the French of West and Central Africa each have their own words and rhythm, and tu or vous changes the whole tone of a video.",
        "checks": ["Tu or vous used consistently", "Accents and typography", "Québec, Belgian or African usage when it applies", "Names, places and brands"],
        "faq": [("Is French human review available now?", "French human review is on our way. Instant AI transcription and translation in French are available today, and you can ask us to prioritise human review."),
                ("Can I ask for French human review?", "Yes. Use the button on this page to tell us what you need and the volume, and we will say when it can start.")],
        "keywords": ["french", "francais", "translate", "subtitle", "human"],
    },
}

ORDER = ["english", "swahili", "kikuyu", "chinese", "german", "spanish", "french"]

BENEFITS = [
    ("Accuracy where it counts", "Names, numbers, dates and technical terms are exactly where machines slip. A reviewer checks them against your audio.", "check"),
    ("The right tone for your audience", "Formal or friendly, regional or neutral. A person decides, and follows what you ask for.", "voice"),
    ("Someone is accountable", "A real editor signs the order off before it is marked ready. If something is wrong, you can ask for a revision.", "shield"),
    ("Publish with confidence", "Subtitles, transcripts and dubs you do not have to proofread again, for work that carries your name.", "play"),
]

TIPS = [
    ("Start with the clearest audio you have", "Close microphones, less background noise and one speaker at a time help every transcript, human or machine."),
    ("Tell us the names and terms", "Use the order notes for the spelling of people, places, brands and any specialist words. The editor uses your list."),
    ("Pick the transcript style that fits", "Clean read removes the ums and false starts. Full verbatim keeps them. Choose what the transcript is for."),
    ("Turn on speaker labels for more than one voice", "Interviews and panels read far better when each line says who is speaking."),
    ("Choose the right language pair", "Say which language is spoken and which you want out. If your audience is regional, say where."),
    ("Check it in the preview, then ask for changes", "Press play on the finished order, click any line to jump to it, and request a revision for anything you want changed."),
]

GENERAL_KEYWORDS = ["human", "review", "accuracy", "transcri", "subtitle", "dubbing"]
