# ElevenLabs prompts for the Kauli Guides YouTube material

Use these in ElevenLabs' **Image & Video** tab (and Music / Sound Effects for audio). Model names change often, so each prompt is written to work with any current image or video model. Paste the **style line** at the end of every prompt so everything looks like one brand.

> **Style line (paste at the end of every prompt):**
> Brand look: deep navy to bright azure blue gradient (#08365F to #0A6FB8), warm orange accents (#F47A0A), crisp white shapes, soft glow, clean modern flat-illustration style with subtle depth, high contrast, uncluttered, professional tech-education feel.

> **Negative prompt (if the tool has one):** text, words, letters, numbers, captions, watermark, logo, signature, distorted hands, extra fingers, blurry, low quality, clutter.

**About text:** image and video models still spell badly. The prompts below ask for text-free art. Add the words afterwards in an editor (Canva, CapCut, or ElevenLabs' own editor), or just use the finished thumbnail and frames already made in `marketing/podcast/youtube/`. Never ask a model to draw the Kauli logo: upload the real logo file instead (`webapp/static/kauli-logo-nav-light.png`).

**About people:** the hosts are fictional and the voices are AI. Do not generate faces that look like a real, named person. Keep "Voiced with AI voices" visible.

---

## 1. Thumbnail (16:9, 1280 x 720)

**Text-free background art (use with the real words added afterwards):**
> A wide 16:9 image for a YouTube thumbnail. On the left, a large empty area of deep navy-blue gradient for big headline text. On the right, a glowing white document card tilted a few degrees, showing blurred lines of transcript text with one word highlighted in red and a green tick beside the corrected word. Soft orange glow in the bottom left corner, subtle sound-wave lines fading across the background. Dramatic but friendly, high contrast, no readable text. [style line]

**Words to add on top:** `YOUR TRANSCRIPT IS WRONG` (white, with WRONG in orange), small tag `KAULI GUIDES · EP 1`, card text `Nye Robbie` struck through in red and `Nairobi ✓` in green.

**If your model draws text well, try this instead:**
> YouTube thumbnail, 16:9. Huge bold white sans-serif headline "YOUR TRANSCRIPT IS WRONG" with the word WRONG in orange, on a navy-to-blue gradient. On the right a white card showing the word "Nye Robbie" crossed out in red above the word "Nairobi" with a green check mark. Small orange label "KAULI GUIDES · EP 1". [style line]

## 2. Channel banner (16:9, 2560 x 1440; keep everything important inside the centre 1546 x 423)

> A wide, calm banner background: navy-to-azure gradient with a soft orange glow low on the left, gentle white sound-wave lines and tiny floating subtitle bars drifting across the whole width, a large empty centre band left clear for a logo and a title. No text. [style line]

**Add on top:** the Kauli logo, `KAULI GUIDES`, and `Transcription, translation and dubbing, explained`.

## 3. Channel avatar (1:1, 800 x 800)

Use the existing podcast cover (`webapp/static/podcast-cover.jpg`). To make a variation:
> A square emblem background: navy-to-azure gradient, one large soft white circle in the centre as a clean space for a logo, a ring of thin orange sound-wave bars around it. No text. [style line]

## 4. The two hosts (consistent characters, 1:1 portraits, then 16:9 together)

Generate Amara first, then reuse her image as a reference for later shots so she stays the same.

**Amara:**
> Friendly illustrated portrait of a woman in her early thirties, warm brown skin, short natural curly hair, bright curious smile, wearing a navy sweater and large over-ear headphones around her neck, speaking into a studio microphone. Shoulders up, soft studio lighting, simple blue gradient background with a faint orange rim light. Stylised semi-realistic illustration, not a photo, not a real person. [style line]

**Dan:**
> Friendly illustrated portrait of a man in his late thirties, light brown skin, short dark hair and neat beard, relaxed knowing half-smile, wearing a grey-blue shirt with headphones on, in front of a studio microphone. Shoulders up, soft studio lighting, simple blue gradient background with a faint orange rim light. Stylised semi-realistic illustration, not a photo, not a real person. [style line]

**Both together (16:9):**
> Using the two reference portraits, show the same woman and man seated across a small round table in a cosy modern podcast studio, each with a microphone on a boom arm, laptops open, mugs on the table, warm lamp light, soft blue wall with acoustic panels and a few plants. Wide shot, both clearly visible, friendly and relaxed. Keep both characters identical to the references. [style line]

## 5. Chapter scene illustrations (16:9, one per part; use as B-roll stills or video starting frames)

| Part | Prompt (add the style line) |
|---|---|
| Why transcripts fail | A large friendly speech bubble breaking into jumbled blocks of text on a navy background, a magnifying glass revealing one wrong word glowing red. |
| Fix it before you record | Close-up of a studio microphone held a hand's width from a speaking mouth in a quiet, soft-furnished room with curtains and a rug; calm, warm light. |
| Names, numbers, speakers | A transcript page with two colour-coded speaker lines, a short word list floating beside it with a checklist tick, and a number highlighted by a glowing circle. |
| Subtitles people can read | A video frame with two neat lines of subtitle bars at the bottom and a small box of on-screen graphics at the bottom right, with the subtitle bars shifting up and out of the way. |
| Translation and dubbing | A glowing globe with speech bubbles in different colours arcing between continents, and a sound wave turning into a different sound wave. |
| AI or human, and cost | A balanced scale: on one side a small friendly robot-like chip, on the other a person's hand with a pen, with a coin and a padlock floating above. |

## 6. Video clips (ElevenLabs video models; 6 to 10 seconds each, 16:9)

Ask for **no on-screen text** and add words in your editor. If the tool accepts a starting image, upload the matching still from section 5 (or the real logo) and use **image-to-video**.

1. **Channel intro (7 s).** Upload the Kauli logo. *"The logo sits centred on a navy-to-azure gradient. A ring of orange and white sound-wave bars pulses outward once, the logo glows softly and settles. Smooth, confident, premium. Ends on a clean, still logo."*
2. **Cold open: the mistake and the fix (8 s).** *"A clean white transcript card on a blue background. One word in the sentence flickers and changes from a wrong word, highlighted in red, to the correct word, highlighted in green, with a small green check appearing. Slow push-in, soft light."*
3. **Hosts idle loop (10 s, loopable).** Use the two-hosts image. *"Subtle life only: the two people breathe, glance at each other, nod and smile slightly, lamp light flickers softly, steam from a mug. Camera still. No big movement. Seamless loop."*
4. **Recording tips (8 s).** *"Slow slide-in on a microphone placed a hand's width from a speaker in a cosy room; a sound wave on a monitor behind goes from messy to clean."*
5. **Subtitles (8 s).** *"A video frame; two tidy subtitle lines appear at the bottom, then a graphic pops up at the bottom right and the subtitle lines glide to the top of the frame."*
6. **Translation and dubbing (8 s).** *"A glowing globe slowly rotates; coloured speech bubbles pulse along arcs between countries; a sound wave on the left morphs into a different-coloured sound wave on the right."*
7. **End screen background (20 s).** *"A calm, slowly moving navy-to-azure gradient with soft floating light particles and faint sound-wave lines. Leave two large empty rounded-rectangle areas on the right and one empty circle on the left (for YouTube end-screen elements). No text."*

**Talking hosts on camera?** ElevenLabs' lip-sync can make a portrait speak a recording, but it works on one voice at a time and costs credits per second, so a full 23-minute dialogue would be very expensive. A better plan: use the idle loop (clip 3) and the waveform video already built here, and keep lip-sync for short teasers (15 to 30 seconds).

## 7. Short teaser for Shorts and social (9:16, 20 to 30 s)

> Vertical video. A transcript card with the wrong word "Nye Robbie" flashes red, switches to "Nairobi" in green. Fast, punchy cuts to a microphone, subtitle bars, a globe. Bold white and orange accents on navy-blue. [style line]

Voiceover (paste into Text to Speech with the Amara voice): *"Your transcript says Nye Robbie. It should say Nairobi. Here are the seven reasons transcripts go wrong, and how to fix every one of them. Full episode on Kauli Guides."*

## 8. Audio prompts used (to regenerate or make more episodes)

**Music Composer (ElevenLabs Music), instrumental only:**
- **Theme (30 s):** *Warm, upbeat, modern podcast theme tune. Light percussion, plucked strings and soft marimba, positive and curious, about 100 BPM, a clear melody with a clean ending chord. No vocals.*
- **Bed (60 s, loop):** *A quiet, steady instrumental bed to sit under two people talking: soft felt piano, airy pads and a very gentle pulse, about 80 BPM, no melody that competes with speech, no drops, no vocals, even from start to finish.*
- **Outro (20 s):** *A short, warm outro for a podcast: the same plucked strings and soft marimba, building gently for a few bars and resolving to a final chord. About 100 BPM. No vocals.*

**Sound Effects:** room tone (4 s), a single soft laptop key click, a soft page turn, a short light notification ding, a soft gentle whoosh, a soft pleasant chime.

**Voices (Text to Dialogue, Eleven v3):** AMARA = Jessica (warm, bright), DAN = Chris (down-to-earth). Paste `episode-01-dialogue-v3.txt` one part at a time.

## 9. Files to export

| Asset | Size | Format |
|---|---|---|
| Thumbnail | 1280 x 720 | JPG or PNG, under 2 MB |
| Channel banner | 2560 x 1440 | JPG or PNG, under 6 MB |
| Channel avatar | 800 x 800 | JPG or PNG |
| Video clips | 1920 x 1080 | MP4 |
| End screen background | 1920 x 1080, 20 s | MP4 |
| Shorts teaser | 1080 x 1920 | MP4 |
