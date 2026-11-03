"""The two offices, each with a page of its own (/locations/nairobi, /locations/dallas).

Everything here is a plain fact about the business: where the office is, what Kauli does and the languages it works in.
No opening hours, phone numbers, client names or ratings appear because none are published; add them here only when they
are real, and the page and its structured data pick them up.
"""
from __future__ import annotations

STREET = {
    "nairobi": "9th floor, Nation Centre, Kimathi Street",
    "dallas": "3710 Rawlins St, Suite 1420",
}

OFFICES = {
    "nairobi": {
        "city": "Nairobi", "region": "", "postal": "", "country": "Kenya", "country_code": "KE",
        "street": STREET["nairobi"],
        "address_line": "9th floor, Nation Centre, Kimathi Street, Nairobi, Kenya",
        "title": "Transcription, Translation and Dubbing in Nairobi, Kenya",
        "description": "Kauli's Nairobi office: transcription, translation, subtitles and dubbing in Swahili, Kikuyu, English and 70+ more languages, with human review.",
        "h1": "Transcription, translation and dubbing in Nairobi",
        "lede": "Our Nairobi office is the home of Kauli's Swahili and Kikuyu work: Kauli began with those two languages. Send audio or video from anywhere and get transcripts, subtitles, translations and dubbed audio back.",
        "serves": [
            ("Swahili and Kikuyu, checked by people", "Human review for Swahili and Kikuyu covers everyday Kenyan speech, code-switching with English and Sheng, and the names and institutions your audience knows.", "/human-review/swahili"),
            ("Kenyan English", "Instant AI is fast on clear English. Human review is for accents, overlapping speakers, names and numbers.", "/human-review/english"),
            ("Subtitles and captions", "SRT and VTT files for YouTube and broadcast, or connect your channel to publish captions.", "/solutions/captioning"),
            ("Dubbing between Swahili, Kikuyu and English", "A draft in minutes, then a person checks the translation before it is voiced.", "/solutions/video-dubbing"),
        ],
        "who": "Newsrooms and broadcasters, NGOs and development teams, universities and e-learning producers, community media, podcasters and YouTube channels.",
        "faq": [
            ("Where is Kauli's Nairobi office?", "9th floor, Nation Centre, Kimathi Street, Nairobi, Kenya."),
            ("Do I have to be in Nairobi to use Kauli?", "No. Kauli is an online service. Upload a file or paste a link from anywhere, and download the result from your account."),
            ("Which languages can your Nairobi team check?", "Swahili, Kikuyu and English are checked by human editors today. Instant AI covers many more languages, and the human-review list is growing."),
        ],
    },
    "dallas": {
        "city": "Dallas", "region": "TX", "postal": "75219", "country": "United States", "country_code": "US",
        "street": STREET["dallas"],
        "address_line": "3710 Rawlins St, Suite 1420, Dallas, TX 75219, USA",
        "title": "Transcription, Translation and Dubbing in Dallas, Texas",
        "description": "Kauli's Dallas office: transcription, subtitles, translation and dubbing in 76+ languages, with human review in selected languages. Pay as you go.",
        "h1": "Transcription, translation and dubbing from Dallas",
        "lede": "Kauli serves clients across the United States from Dallas, Texas. Upload audio or video, choose Instant AI for speed or add a human reviewer when it has to be right, and download transcripts, subtitles and dubbed audio.",
        "serves": [
            ("Fast transcripts for meetings and interviews", "Instant AI turns recordings into text in minutes, with speakers and timestamps.", "/solutions/interview-transcription"),
            ("Subtitles and translation", "Captions for training videos, webinars and social video, and translation between 76+ languages.", "/solutions/captioning"),
            ("Dubbing", "The same video spoken in a new language. You get a draft in minutes, and human review is available in selected languages.", "/solutions/video-dubbing"),
            ("Human review", "A dedicated editor checks the work against your audio in selected languages. See which are available now.", "/human-review"),
        ],
        "who": "Teams that record a lot: training and HR, marketing and communications, research and universities, podcasters, producers and agencies.",
        "faq": [
            ("Where is Kauli's Dallas office?", "3710 Rawlins St, Suite 1420, Dallas, TX 75219, USA."),
            ("Can I pay in US dollars?", "Yes. Prices are shown in US dollars and the exact price of an order is shown before you confirm it."),
            ("Is there a free way to try it?", "Yes. Your first minutes of Instant AI transcription are free each month, and the interactive demo needs no sign-up."),
        ],
    },
}
