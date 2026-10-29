# Promo photos: prompts for ElevenLabs Image & Video

The video needs **7 realistic photos**, one per scene. Make them in the ElevenLabs app (**Image & Video**, which your Starter plan includes), then save them into this folder with these exact names and run the build again:

```
marketing/promo/images/01-hook.png
marketing/promo/images/02-turn.png
marketing/promo/images/03-meet.png
marketing/promo/images/04-translate.png
marketing/promo/images/05-trust.png
marketing/promo/images/06-tools.png
marketing/promo/images/07-cta.png
```

(.jpg and .webp also work. Until a photo exists, that scene shows a marked placeholder card.)

## Settings
- **Aspect ratio:** 16:9. **Resolution:** the highest the app offers (2K is plenty; the video is 1080p and the camera move needs the extra pixels).
- **Model:** a photorealistic one (for example Gemini 3 Pro Image, or a GPT Image model). Faces and hands are usually best on those.
- **Keep the same woman in scenes 1, 2, 3 and 7.** Generate scene 1 first, then use it as the **reference image** for scenes 2, 3 and 7 and add "same woman as the reference image".
- The ElevenLabs app does not put words on the picture reliably, so every prompt says **no text**. Kauli's own text, logo and captions are added by the video build.

## Look shared by every prompt (paste this at the end of each one)
> Photorealistic candid editorial photograph, natural light, shot on a 35mm lens, shallow depth of field, real skin texture, warm and modern. No text, no logos, no watermark, no captions, nothing written on screens.

## Where to leave room (important)
The video puts a text banner at the **top left**, a logo at the **top right**, captions along the **bottom**, and in scenes 3 and 4 a **screen recording on the right half**. So:
- Scenes 1, 2, 5, 6, 7: keep the **top-left and the bottom fifth fairly calm** (soft background, no busy detail).
- Scenes 3 and 4: put the people on the **left third** and keep the **right half calm and slightly out of focus**.

---

## 01-hook.png: the problem
> A tired woman in her early 30s of East African heritage, natural curly hair tied back, wearing a green cardigan, sitting at a cluttered home-office desk late at night, rubbing her eyes with one hand. A laptop in front of her shows a long audio waveform. Warm desk-lamp light, headphones around her neck, an empty coffee mug, a wall clock in the soft background. Wide shot, her on the right of frame, plain dark wall at top left. Late-night mood, exhausted but relatable.

## 02-turn.png: the idea
> The same woman as the reference image, now looking up from the laptop with a surprised, delighted smile, eyes wide, one hand raised slightly as if an idea just landed. Brighter, warmer light than the previous shot, as if the room itself lifted. Medium close-up, her face and shoulders centre-right, soft blurred office background, calm top-left corner.

## 03-meet.png: Kauli at work
> The same woman as the reference image, now fresh and energised in a bright daytime home office, sitting at her laptop and smiling confidently while pointing at the screen. Seen from slightly behind her shoulder so the screen glows but shows only a soft blur. She is on the **left third** of the frame, the **right half is a calm, softly blurred bright window and plants**. Optimistic, modern mood.

## 04-translate.png: every audience
> Four friendly people of different backgrounds (a Kenyan man in his 30s, a Latina woman in her 20s, a Chinese woman in her 40s, a German man in his 50s) standing close together on the **left third** of the frame, smiling and looking at a laptop screen together in a bright modern office. The **right half of the frame is calm, softly blurred glass and daylight**. Warm, welcoming, global feeling.

## 05-trust.png: the human editor
> A focused professional man in his 40s with a short greying beard and round glasses, wearing over-ear headphones, carefully reviewing text on a large monitor next to an audio waveform, a pen in his hand and a notebook beside him. Calm, precise, trustworthy mood. Soft daylight from the left, shallow depth of field. Him centre-right, top-left corner calm and darker.

## 06-tools.png: the team workflow
> A bright modern team office, two colleagues (a woman in her 30s and a man in his 20s) collaborating at one laptop, laughing and pointing at the screen, a second monitor and sticky notes softly blurred behind them. Energetic and friendly. Subjects centre-right, top-left corner calm, bottom fifth uncluttered.

## 07-cta.png: the relief
> The same woman as the reference image, now relaxed and happy at her desk in the morning, leaning back in her chair with a coffee mug in both hands and a big genuine smile, laptop closed beside her, sunlight through a window. Everything is tidy, the clock and clutter from the first scene are gone. Her centre-right, top-left calm, bottom fifth uncluttered. Feeling of "work done, finally free".

---

## Thumbnail
The build also makes `kauli-promo-thumbnail.jpg` (1280x720, "STOP TYPING.") from photo 01. If you want a stronger thumbnail, generate one more image of the same woman with a frustrated, wide-eyed expression, **face large on the right half**, name it `thumbnail-face.png`, and tell me: I will put it behind the words.

## After you add the photos
Run (or ask me to run) `python scripts/make_promo_video.py video`. The file becomes `marketing/promo/kauli-promo.mp4` (no "animatic" in the name once all seven photos are in).
