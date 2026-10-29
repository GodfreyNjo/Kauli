# Kauli promo video: "Type it? Never again." (75 seconds)

**Where it goes:** YouTube (16:9, 1920x1080). Also cut a 30-second and a 15-second version for Shorts, Reels and ads (see the bottom).
**Length:** 75 seconds, the usual size for a product promo (60 to 90 seconds holds attention and still has room for a proof point and a call to action).
**Voice:** one warm, confident narrator, human and a little playful. ElevenLabs **eleven_v3** model, Stability on "Creative" or "Natural" so the tags are obeyed. Voices already in our demos that suit it: Liam (`TX3LPaxmHKxFdv7VOQHJ`) or Alice (`Xb7hH8MSUJpSbSDYk0k2`).
**Every claim below matches the live product.** No invented numbers, customers or testimonials.

How to read the script: text in `[square brackets]` inside a VO line is an **ElevenLabs v3 voice tag** and goes into the text you send. Lines marked **SFX** and **MUSIC** are *not* spoken: generate them separately (prompts are in the last section) and place them in your editor.

---

## The script

| Time | On screen | VO (paste this into ElevenLabs) | SFX / MUSIC |
|---|---|---|---|
| **0:00-0:06** HOOK | A tired creator at a desk, a two-hour waveform on the laptop, a clock spinning. Big text: **"2 hours of recording."** | `[sighs] Two hours of recording. [short pause] And somebody has to type every single word.` | SFX: clock ticking, then fast keyboard clatter that trails off. MUSIC: none, only a low room tone. |
| **0:06-0:13** TURN | The clock freezes. The creator looks up and smiles. Text: **"What if you just... uploaded it?"** | `[short pause] [curious] What if you just... uploaded it?` | SFX: a soft record-stop, then a bright UI click and a short upward whoosh. MUSIC: **drops in here**, light and upbeat. |
| **0:13-0:28** MEET KAULI | Kauli logo, then a screen capture: drag a file in (and paste a YouTube link). The transcript fills in with speaker names and timestamps (use the call-center demo). Text: **"Transcripts in minutes."** | `[excited] Meet Kauli. [short pause] Drop in any audio or video, and get an accurate transcript in minutes. [warmly] Every speaker labeled. Every word timestamped.` | SFX: a gentle "ding" as the transcript lands, soft typing as lines appear. MUSIC: builds. |
| **0:28-0:42** TRANSLATE | The same transcript flips into Spanish, Swahili, Chinese. Subtitles appear on the demo video. A dubbed version plays. Text: **"70+ languages. Subtitles. Dubbing."** | `Then translate it into more than seventy languages. [short pause] Subtitles. [short pause] Even a dubbed voice, in selected languages. [excited] Same video, brand new audience.` | SFX: three quick page-flip whooshes on each language change, a small "pop" on the dub. MUSIC: peak energy. |
| **0:42-0:54** TRUST | A person-editor checks text against a waveform; a badge slides in: **"Up to 99% accurate with human review."** | `[calmly] And when it has to be perfect, add a real human editor. [short pause] You get up to ninety-nine percent accuracy.` | SFX: a soft "confirm" chime on the badge. MUSIC: drops back a little so the line lands. |
| **0:54-1:05** TOOLS | Quick cuts: editing a line in the browser, "Create AI notes" button, a Share link, a webhook sending to a website. Text: **"Edit. Notes. Share. Automate."** | `[upbeat] Edit right in your browser. Make AI notes in one click. Share a link. Or send every finished transcript straight to your website or your tools.` | SFX: four crisp UI clicks, one per cut. MUSIC: steady. |
| **1:05-1:15** CALL TO ACTION | End card on the Kauli logo. Big text: **"Your first 30 minutes every month are free."** Under it: **kauli-forgemedia.com** and a button graphic **"Start free"**. | `[confident] Your first thirty minutes, every month, are free. [short pause] [warmly] Start at kauli dash forge media dot com. [short pause] Kauli. Type it? Never again.` | SFX: a bright "success" sting on the last line. MUSIC: resolves on a clean final chord, 2-second tail. |

**Word count:** about 165 spoken words, which is 70 to 75 seconds at a natural pace with the pauses.
**ElevenLabs characters:** roughly 1,050 including tags, so a single render fits well within our remaining quota. Render it in one go, or one row at a time if you want to re-take a section.

### Why this copy converts
- **Opens on the pain** (typing a recording out), not on the product.
- **One clear promise per scene**: speed, languages, accuracy, convenience, price.
- **Proof where doubt appears**: the human-review line comes right after the AI claims.
- **A concrete, low-risk offer** in the last ten seconds (30 free minutes) and a single action ("Start free").
- The tagline "Type it? Never again." echoes the hook, so the video feels finished.

---

## Voice tags used (ElevenLabs v3)

`[sighs]`, `[curious]`, `[excited]`, `[warmly]`, `[calmly]`, `[upbeat]`, `[confident]`, `[short pause]`.
Keep tags short and one or two per line: more than that can make the delivery sound acted. If a tag is ignored by the chosen voice, raise the take's expressiveness (lower Stability) or swap the tag for a short punctuation pause ("...").
Say the web address the way it is spoken in the script ("kauli dash forge media dot com"), and show it written on screen.

---

## Music and sound effects: prompts for ElevenLabs

**Music (generate one 80-second track, then trim):**
> Modern, upbeat corporate-pop instrumental, 108 BPM, major key. Warm plucked synth, light finger-snaps and soft claps, gentle bass, no vocals. Starts almost silent with just room tone for the first 6 seconds, enters on a bright upward riser at 0:06, builds steadily to a confident peak around 0:28 to 0:42, eases down slightly at 0:42 to 0:54 for a spoken line, then lifts again and finishes on one clean resolving chord with a 2-second tail.

(We already have a licensed-by-us bed in `webapp/static/demo/bed-bright.mp3` that fits if you want a quick version.)

**Sound effects (ElevenLabs Sound Effects, 1 to 3 seconds each):**
- `Wall clock ticking in a quiet room, 3 seconds`
- `Fast keyboard typing that slows and stops, 3 seconds`
- `Soft vinyl record-stop then silence, 1 second`
- `Bright modern UI click, clean`
- `Short upward whoosh, airy, 1 second`
- `Gentle notification ding, warm, 1 second`
- `Quick page-flip whoosh, 0.5 seconds` (use three times)
- `Soft confirmation chime, positive, 1 second`
- `Cheerful success sting, 1.5 seconds`

**Mix:** voice at -3 dB peak, music at about -20 dB under the voice and rising to -14 dB between lines, effects at -12 dB. Duck the music 4 dB for every spoken line.

---

## YouTube package

**Title (under 60 characters):**
`Kauli: Turn Any Audio or Video into Text, Subtitles, Dubs`

**Description:**
> Stop typing out recordings. Kauli turns audio and video into accurate transcripts in minutes, then translates them into 70+ languages as subtitles or dubbed audio. Need it perfect? Add a human editor for up to 99% accuracy.
>
> What you can do:
> - Transcripts with speaker labels and timestamps
> - Translation into 70+ languages, subtitles, and dubbing in selected languages
> - Edit in your browser, share a read-only link, make AI notes
> - Send finished transcripts to your website or tools with webhooks and the API
>
> Your first 30 minutes every month are free: https://kauli-forgemedia.com
>
> 0:00 The problem
> 0:13 Meet Kauli
> 0:28 Translate and dub
> 0:42 Human review
> 0:54 Tools
> 1:05 Start free

**Tags:** transcription, audio to text, video transcription, subtitles, video translation, AI dubbing, call center transcription, podcast transcription, interview transcription, speech to text

**Thumbnail:** a face mid-sigh on the left, a clean transcript on the right. Text: **"STOP TYPING."** (max 3 words, high contrast).
**Pinned comment:** `Try Kauli free, 30 minutes every month: https://kauli-forgemedia.com. Tell us what you want to transcribe and we will point you to the right option.`
**End screen (last 10 seconds):** subscribe button plus a card to the call-center demo or to the "How to publish podcast transcripts automatically" guide.

---

## Cutdowns

**30 seconds (Shorts / ads):** Hook (0:00-0:06) + Meet Kauli (0:13-0:28, cut the second sentence) + Call to action (1:05-1:15, first line only).
> `[sighs] Two hours of recording. And somebody has to type every word. [excited] Meet Kauli. Drop in any audio or video and get an accurate transcript in minutes, in more than seventy languages. [confident] Your first thirty minutes every month are free. Start at kauli-forgemedia.com.`

**15 seconds (bumper):**
> `[sighs] Typing out recordings? [short pause] [excited] Kauli does it in minutes, in seventy-plus languages. [confident] Thirty minutes free, every month.`

Vertical 9:16 versions: keep the captions large and lower-centre but off the very bottom edge, with two lines at most (the same rule we use for burned-in captions).

---

## Before you publish
- Record the screen captures from the live site, and use the **call-center demo** for the transcript scene so everything shown is real.
- Do not show a customer's real data.
- Add captions (SRT) to the YouTube video. Our own burned-in-caption rules apply.
