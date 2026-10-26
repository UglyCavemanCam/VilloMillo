# Format blueprints: how every clip gets the right look, automatically

Status: **superseded in part (2026-10-25).** The recipes below (Clean frame, Plain frame) were removed; formats are now templates made on the Formats page and stored in the database (see `villomillo/formats/`). The research, layers idea and safe zones below still stand. Earlier status: **F1 built and tested; F2-F4 designed, not built.** Written 2026-10-24 after the Option A / A2 previews (throwaway scripts, since removed;
they are in git history before the clean-up). Chosen base look: **A2 "Clean frame"**.

## 1. The problem

One look cannot suit every kind of video. A podcast clip, a Twitch stream moment, a fishing catch and a cooking
tutorial need different framing, different text and different pacing. The system must (1) work out what a video is,
(2) pick and combine the right building blocks, and (3) never ship a clip that is only "a crop plus captions",
because YouTube (Oct 2026) now says it reaches less.

## 2. What the engine already has (checked in this repo)

| Piece | Where | State |
|---|---|---|
| Gaming / Reaction: 11 layouts held as data (Split, Basecam, Half, Fullscreen, Blurred, small/circle facecam, Game UI, Mosaic, Dual facecam, Duo split), webcam detection, head-clear-of-buttons placement | `gaming/`, `docs/GAMING.md` | Built. A switch someone turns on |
| Sports (soccer, basketball): moment scoring and follow-the-ball framing | `config/sports.yaml`, `docs/SPORTS.md` | Built. A switch |
| Podcast: shot-by-shot framing for multi-camera footage | `video/podcast.py` | Built. A switch |
| Vertical Live (already-vertical streams), Longform | `core/modes.py` | Built. A switch |
| Audio classifier (527 AudioSet classes), voice turns, game text and vision, visual features | `analysis/` | Built |
| Word-synced captions with highlighting, loudness checks | `video/captions.py`, `villomillo/validate.py` | Built |

**The gap:** every mode is chosen by hand, and nothing layers titles, labels, takeaways, a progress bar or a
watermark on top of a layout. Blueprints fill both gaps; they reuse the existing layouts rather than replace them.

## 3. Research summary by video type

Evidence grades: **A** = platform's own wording (read directly or quoted by several outlets), **B** = several
independent practitioner sources agree, **C** = one or two vendor blogs. Almost all numeric claims in this field
are vendor-sourced and unverified, so every format below is a hypothesis to test with our own retention numbers
(Phase 6), not a proven winner.

| Type | Best-supported format | Grade | Gaps |
|---|---|---|---|
| **All** | Strongest moment first; text in the first frame; captions (many watch muted); keep text out of the bottom ~480 px and right ~140 px on Shorts; trim dead air at the end | B | Safe-zone pixels differ by source: preview on a phone |
| **All** | Add original value: "original commentary, unique edits, or storytelling". Crops, captions, voice-over that only describes the screen, and template-only changes do not count. Permission gives no exemption | A (YouTube policy page read directly; Oct 1 2026 post via reports) | No published threshold |
| **Podcast / interview / talking head** | Face-tracked speaker, big readable captions, a hook title, on-screen labels for the points made, a visible change every ~5-15 s (punch-in, trimmed pause) | B/C | Cut-frequency numbers are anecdotal |
| **Twitch / game stream** | Stacked webcam + gameplay (webcam about 38-40% on top, or game on top), webcam kept for reaction moments, gameplay-only moments fill or blur-fit, captions of the streamer, start from a 1080p source, no black bars | B | Sources disagree on webcam on top vs tracking crop; layout alone does not hold attention |
| **Outdoor / fishing / action / nature (little speech)** | Payoff or mid-action first, natural sound kept, very little text (4-8 word hook in frame one, a callout only when the title or speech states it), slow-motion replay only if the source is high frame rate | C (extrapolated from general Shorts hook and silent/ASMR advice) | **No fishing-specific evidence found.** Must be A/B tested |
| **Sports highlights** | 10-20 s from build-up to celebration, follow-the-action crop, scoreboard kept readable, hook in 1-3 s | C | Nothing found on scoreboard or replay design; broadcast footage needs rights clearance |
| **Tutorial / cooking / DIY** | Finished result first, short materials line, 3-step sweet spot with numbered step cards, one action per step, 25-40 s | C | Retention figures are one vendor's |
| **Reaction / commentary / comedy** | The creator's own commentary must be central; a small face in a corner on someone else's video is not enough | B (policy-aligned) | The system cannot invent commentary: this type needs a human contribution |
| **Vlog / IRL / travel** | Open on the strongest moment, trim dead air with jump cuts but not every breath, new visual every 3-5 s, b-roll to cover cuts and orient the viewer | C | Cut intervals vary 2-10 s across sources |

## 4. The blueprint system

A **blueprint** is a small, declarative building block. A **recipe** is an ordered stack of blueprints for one
video type. The renderer reads the recipe; nothing is hard-coded per genre.

### 4.1 Layers (a recipe has at most one blueprint per layer, except Text and Brand)

1. **Framing**: how footage sits in 1080x1920.
   `clean_frame` (A2: tracked window over a blurred copy, title band above), `full_bleed_tracked` (A),
   `stacked_facecam` (existing gaming layouts), `fullscreen_game`, `native_vertical` (pass-through),
   `follow_action` (existing sports), `speaker_cuts` (existing podcast), `blur_fit` (whole frame in the middle: the
   safe default for anything unknown).
2. **Text**: `hook_title` (stays up 1 s + 0.4 s per word, 2.5-7 s), `captions_pop` / `captions_plain` / `captions_off`,
   `skill_chips`, `checklist`, `step_cards`, `callout`, `takeaway`.
3. **Motion**: `punch_ins`, `slow_push`, `tighten_pauses`, `cold_open`, `slow_mo_replay`, `none`.
4. **Brand**: `progress_bar_bottom` (14 px, flush bottom), `watermark_center` (faint, fixed, see 6).
5. **Audio**: `loudness_normalize` (existing), `keep_natural_sound`, `no_music`.

A blueprint file states: `id`, `layer`, what it **needs** (a face, speech, a webcam box, 50+ fps), what it
**conflicts with**, its parameters, and the safe zones it occupies.

### 4.2 Example recipes (illustrative)

```yaml
podcast_talk:        # the A2 look
  framing: clean_frame
  text: [hook_title, captions_pop, skill_chips, takeaway]
  motion: [punch_ins, tighten_pauses]
  brand: [progress_bar_bottom, watermark_center]
game_stream:
  framing: stacked_facecam          # existing gaming layouts, chosen by the webcam detector
  text: [hook_title, captions_plain, takeaway]
  motion: [punch_ins]               # on the facecam at reaction peaks
  brand: [progress_bar_bottom, watermark_center]
outdoor_action:
  framing: full_bleed_tracked       # or blur_fit if the source is wide with no clear subject
  text: [hook_title, callout]       # no captions unless there is speech
  motion: [cold_open]               # payoff first; slow_mo_replay only if the source is high fps
  audio: [keep_natural_sound]
  brand: [progress_bar_bottom, watermark_center]
generic_safe:                       # anything the detector is unsure about
  framing: blur_fit
  text: [hook_title, captions_plain]
  brand: [progress_bar_bottom, watermark_center]
```

### 4.3 The resolver (turns a type into a finished recipe)

1. Start from the detected type's recipe.
2. Apply the channel's filters. Example: the Solved campaign says "ONLY that footage" and "no other brands in frame", so
   anything using outside footage is removed and the watermark is flagged for their support to confirm.
3. Fill parameters from the clip: the title (already written by Claude and checked by Jev), the skills or steps named in
   the transcript, the takeaway. **Text may only name what the speaker names**; the existing accuracy check applies.
4. Check conflicts and safe zones; drop or shrink a blueprint that cannot fit rather than overlap the face or the
   platform buttons.
5. **Originality guard:** refuse a recipe whose only additions are crop and captions. At least one "added value"
   blueprint must be present (title with structure, labels or cards, cold open, tightened edit).
6. After rendering, run the automated checks again (length, safe zones, text over the face, contrast, campaign rules).
   Any failure falls back to the next simpler recipe, never to publishing the failed clip.

## 5. Automatic video-type detection

Detect per **clip window**, not per video: a stream VOD mixes gameplay and talking; a podcast cuts to graphics.

Cascade, cheapest first (this combination is our design; the pieces are standard techniques, not a benchmarked pipeline):

1. **Free metadata** (already fetched): YouTube category (20 Gaming, 17 Sports, 15 Pets & Animals, 26 Howto & Style,
   22 People & Blogs, 24 Entertainment, 23 Comedy, 27 Education, 28 Science & Technology, 25 News, 19 Travel,
   10 Music), title and tag keywords, live-stream flag, duration, aspect ratio. Weak alone: a fishing video may be
   filed under Sports, Pets & Animals or People & Blogs.
2. **Local signals** from the clip window: speech share and words per minute, speaker count (`voice_turns`), AudioSet
   tags (`panns`: speech, music, crowd, water, wind, game sounds), faces (count, size, stability), the face that
   speaks in time with the audio and stays in the same spot across the video (how the gaming detector already finds a webcam), cut rate, motion, on-screen HUD or text
   (`game_vision`, `game_text`).
3. **Local vision model** (gemma3:12b through Ollama, already installed, free): 4-6 sampled frames and a fixed menu of
   types.
4. **Jev Choice question** (cheap, typed): metadata + transcript excerpt + signal summary + the vision label, answering
   with a type and confidence.
5. **Gate:** agreement and confidence at or above 0.75 gives that type's recipe. Between 0.5 and 0.75 gives the safe
   variant of the top family and flags the clip for review. Below 0.5 gives `generic_safe` and requires review.
6. **Learning:** your corrections on the review page are stored and become test cases. Before automatic selection is
   switched on for a type, it must hit **90% accuracy on a labelled sample of at least 40 clips per type** from our own
   downloads.

## 6. Watermark spec

- Small text of the channel handle in the exact middle of the frame, about 19% opaque (alpha 48/255) with a hairline
  dark edge so it shows on light and dark shots. Drawn under the captions, never over text.
- Same size, position and opacity on every clip (consistency is the recommended practice).
- **What it does and does not do:** it deters casual re-uploads and keeps credit with the channel. Anyone can still
  crop, blur or inpaint it, and no source I found measured how well watermarks stop reposting. A slow drift or an
  intermittent mark (the DRM-vendor approach) is harder to cover and can be added later.
- **Check first:** the Solved brief bans "other brands in frame". Our own handle is probably fine, but ask their support.

## 7. Build plan (inserts between Phase 4 and Phase 5)

| Step | Work | Exit test |
|---|---|---|
| F1 (built) | Blueprint core: file format, resolver, the A2 look as blueprints (clean frame, hook title, pop captions, chips, takeaway, bottom bar, watermark), wired into the render for podcast-type clips | A2 preview reproduced from a recipe, with a matching layout on sample frames; resolver tests for conflicts, safe zones and the originality guard |
| F2 | Type detector v1 (cascade above), labelled sample, accuracy report, confidence gate, review override | 90% type accuracy per type on 40+ labelled clips, or the type stays manual |
| F3 | Wrap the existing gaming and sports layouts as framing blueprints and add the text and brand layers over them | A Twitch clip and a match clip rendered through recipes, passing safe-zone checks |
| F4 | New families: outdoor / action, tutorial, vlog. Each ships behind a flag and is A/B tested through Phase 6 | Real retention compared with the generic recipe over at least 20 posts per family |

## 8. Decisions needed

1. ~~Channel name~~ Settled: **CavemanCam** (handle @cavemancam).
2. ~~Watermark~~ Settled: stays fixed in the middle, about 19% opaque. Drift can be added later if clips get stolen anyway.
3. Which families to build first (podcast is already designed; the Solved campaign needs it).
4. Whether to ask the Solved Discord about the watermark, the takeaway text and the cold open before posting.
