# Format playbooks: what the AI generator is allowed to choose, and why

The AI generator (New AI Generate) must choose from a playbook for the kind of clip you pick. The playbook lives in
`config/villomillo/format_playbooks.yaml`. The code enforces it, so a reply outside the playbook is refused with a reason.

## What the research says (2026, practitioner sources)

These are guides written by caption and clipping-tool companies, not peer-reviewed studies. Treat the numbers as rules of thumb.

- **Contrast:** text should reach a 4.5:1 contrast ratio against its background (WCAG AA). This is the one firm standard.
  Sources: W3C WCAG 2.x, as summarised in the OpusClip and Recapo guides below.
- **Font and outline:** bold sans-serif, white or brand colour, with a dark outline or shadow. Regular and medium weights
  get lost over video. Sources: [Blitzcut, best caption fonts for YouTube Shorts](https://blitzcutai.com/blog/best-caption-fonts-youtube-shorts-2026),
  [OpusClip, YouTube Shorts caption best practices](https://www.opus.pro/blog/youtube-shorts-caption-subtitle-best-practices).
- **Placement:** captions in the lower-middle, roughly 55–78% down the frame, clear of faces and platform text.
  Keep the top fifth (title and channel name) and the bottom quarter (like and share buttons) clear.
  Sources: [Recapo, Shorts safe zone guide](https://recapo.ai/blog/youtube-shorts-safe-zone-guide/),
  [Blitzcut, best caption placement](https://blitzcutai.com/blog/best-caption-placement-short-form-video),
  [CapCut, vertical subtitle placement](https://www.capcut.com/create/vertical-video-subtitles-placement-tips).
- **Size:** sources disagree (about 4% to 10% of frame height). We do not hard-code a figure; the app keeps captions within
  the frame and reads them on a real phone-sized preview.
  Sources: [CutScore, caption font size](https://cutscore.io/blog/best-caption-font-size),
  [Blitzcut, caption sizes](https://blitzcutai.com/blog/best-caption-style-youtube-shorts-2026).
- **Word highlight:** restrained. Optional for calm content; skip animation that moves the essential words around.
  Source: [Recapo, best caption styles for Shorts](https://recapo.ai/blog/best-caption-styles-for-shorts/).
- **Stream clips:** stack the webcam above the game (or the game above the webcam), keep the webcam to about 15–20% of the
  frame, and never let the webcam cover the game's HUD.
  Sources: [Eklipse, stream highlight practices](https://eklipse.gg/help/what-are-the-best-practices-for-editing-stream-highlights/),
  [Eklipse, Twitch clip templates](https://blog.eklipse.gg/beginner-guide-2/twitch-clip-templates.html),
  [Streamgen, Twitch layouts](https://streamgen.cc/blog/twitch-streaming-layouts/).
- **Hook:** the first second carries the payoff or a text hook. The headline does this job.

## Caveats

- Platform safe zones change. A guide notes that TikTok increased its bottom dead zone in late 2025. Check any fixed pixel
  figure on a real phone before relying on it.
- Sources do not agree on exact sizes or safe-zone numbers, so the playbook sets ranges and orderings, not pixel values.

## How the playbooks are used

- The kind you pick (podcast, talk, stream, other) selects its playbook. The AI is given only the caption styles, layouts,
  effects and colours that playbook allows, and the code refuses anything outside it.
- Your feel (calm, balanced, high energy) narrows the caption styles further.
- Calm feels never get words that jump or flash; high energy feels get the bolder styles.
