# Format builder: the plan

How to use this file: each phase is a list of tasks with `[ ]` boxes. When a task is done it is ticked, in the same change that does it.
Each phase ends with "You will see" (what to try in the dashboard) and "Done when" (how we know it works). We stop after every phase so you can
say "yes, next" or "change this". Nothing here is built until you say go.

Status: **Phases 0 and 1 are built and waiting for your OK. Phases 2 to 7 are not started.**

---

## What you decided

| # | Decision |
|---|---|
| 1 | The preview is the **center** of the builder. Add and Layers on the left, the selected element's settings on the right, format-wide settings below. |
| 2 | **Labels and handles are editable like every other element**: font, size, colors, corner radius, padding, outline, shadow, opacity, position, size. |
| 3 | The preview **is a real render** and is **always up to date**: after every change the new render starts at once, so pressing play already shows the edit. Only the part around the playhead is rendered first, then the rest. |
| 4 | A **draggable timeline** under the play button moves to any part of the video. |
| 5 | **Right-click** on an element of the preview opens our own menu (edit, delete, and more) instead of Chrome's. Elements can also be **dragged and resized**. |
| 6 | **No fixed slots.** You add as many elements as you want: headline, captions, point label, handle, free text, logo or image, box or shape, button, progress bar, zoom or shake, music or sound. **No emojis or stickers.** |
| 7 | **Spoken words:** only the **point labels** (and the captions, headline and takeaway) follow what is said. Every other element uses a fixed timing: the whole clip, the first N seconds, the last N seconds, or a set time range. |
| 8 | **Before play (paused or not started)** every element shows its default text as a ghost on the picture. **While playing** the real render shows what is being said or done. |
| 9 | A new format is built on **fixed sample videos** I pick from what is already downloaded, with a way to switch to other samples. |
| 10 | **Edit look** opens the builder with the **actual short** as the preview (its own video, words, headline, labels and takeaway). Saving goes to **that clip only**, with two extra buttons: **Save as a new format** and **Update "XXX" format**. |
| 11 | **A clip already posted to YouTube cannot have its look edited.** No Edit look link, and the server refuses it too. |
| 12 | Existing formats (Podcast, Stream and your saved ones) **convert automatically** and keep looking exactly the same. (Where an old format showed point labels, the converted format has a label element; a new format starts without one.) |
| 13 | **Campaign clips** never get a handle, progress bar, logo or button, whatever the format says. |
| 14 | The old **"Check with real render"** box is removed once the live preview works. |
| 15 | **No undo and redo.** |
| 16 | The **point label is an optional element**: a format only has labels if you add one, and you can add more than one (for example a numbered tag in a second style). Every label is **fully customizable**, exactly like the headline and captions. |
| 17 | **Editing the look of a clip with a waiting post keeps the post's place in the queue** and it sends the new file. |

---

## What exists today (so we do not rebuild it)

- **Page:** `villomillo/web/templates/formats.html` (the builder is a card in the My Templates tab, with inline JavaScript), route `/formats` in `villomillo/web/app.py`, page data in `pages.formats_page`.
- **Look as data:** `villomillo/formats/designs.py`. A look is a fixed set of switches (`captions`, `headline`, `chips`, `takeaway`, `logo`, `cta`, `brand.watermark`, `brand.progress_bar`, `effects`, `music`, `place`). This is what Phase 1 replaces.
- **Templates in the database:** `villomillo/formats/templates.py` (tables `templates` and `template_assignments`).
- **Rendering:** `villomillo/formats/render.py` (`render`, `_overlays`, `framed_still`), with `captions.py`, `effects.py`, `music.py`, `overlays.py`, `placement.py`, `assets.py`, `fonts.py`.
- **Preview:** `villomillo/formats/preview.py`. Today it draws a still frame plus text drawn by the page (`stage`), and `make_preview` makes one full 35 s video on a button.
- **Dragging:** the stage in `formats.html` already moves and resizes the headline, captions, labels, button and logo (`place` values). This is the base for Phase 6.
- **Edit look:** `/formats?clip=<id>`, route `/api/formats/apply-clip`, `villomillo/relook.py`. It only works for clips waiting for review, and the preview is a sample video, not the real clip.

---

## Phase 0: Measure and choose the samples

Goal: know how fast a render is on your PC, and have good samples ready, before the design depends on it.

- [x] Add a small command, `python -m villomillo formats-bench`, that renders one 5 s segment of a downloaded video at half size and at full size and prints the time of each. (It also lists the sample videos it would open on and says what the timings mean.)
- [x] Choose the **sample videos**: the downloaded videos with a word-timed transcript and the most speech in a 35 s stretch. The first becomes the default sample. (`preview.sample_videos` now returns them most-speech-first, so the builder's sample dropdown already opens on the best one; `preview.best_samples` gives the top three with the stretch each is shown on.)
- [x] Decide the **segment length** (about 5 s) and the **draft size** (about half) from the measurements.
- [x] Find out why a stage and a real render could differ, and list the measurements the tests will compare (Phase 7).

You will see: a short note with the timings and the three samples chosen.
Done when: the timings are in this file under "Measured".

Measured (on the cloud machine I work on, 4 cores, a 1080p test video; **run `python -m villomillo formats-bench` on your PC and paste your numbers here, they are what counts**):

| Way of rendering 5 seconds | Seconds |
|---|---|
| Draft: shrunk to half size at the very end | 6.1 to 6.7 |
| Preview: full size, fast encoder | 8.1 |
| Final quality: full size, slow encoder (what clips use) | 10.8 to 10.9 |

What that tells us:
- **Almost all of it is ffmpeg** (6.6 of 8.3 s in the preview way). Following the face takes 1.5 s, and the text pictures are cheap.
- Shrinking only at the end saves about a quarter. A real draft has to **shrink the video first and draw the text pictures smaller** (Phase 3), and **follow the face once for the whole sample** and reuse it for every piece (the face does not depend on the look).
- So the plan stays: pieces of **5 seconds** (`preview.SEGMENT_SECONDS`) at **half size** (`preview.DRAFT_SCALE`), with the piece under the playhead first. If your PC is slower than this one, the same command will tell us, and the piece length is one number to change.

Why a stage and a real render can differ (these are what Phase 7 compares): the page draws text with the browser's fonts and its own copy of the headline wrapping and shrinking rules; the real render uses the bundled fonts, shrinks pills to fit 1000 px, fades each picture in and out for a quarter of a second, and burns the captions with a subtitle engine whose line breaks and animation the page can only imitate. Positions (the numbers every element carries) are the same on both sides, so the test compares where each element lands.

---

## Phase 1: Elements instead of fixed switches

Goal: a format is a list of elements. Nothing on screen changes yet.

- [x] Define the element model in a new `villomillo/formats/elements.py`. Every element has:
  - `id`, `type`, `name`, `visible`, `locked`, and its order in the list (front to back);
  - **position and size** (in the 1080 by 1920 picture; the safe-zone check for dragged elements comes with dragging in Phase 6, the existing placement checks still run);
  - **style**: font, size, weight, text color, outline (color and width), shadow, box color, corner radius, padding, opacity, alignment, uppercase;
  - **timing**: `whole_clip`, `first_seconds N`, `last_seconds N`, or `range from to`; labels, captions, headline and takeaway use `spoken` (they follow the speech);
  - **text**: default text (shown before play), and its source (`ai_headline`, `ai_takeaway`, `ai_point`, `spoken_words`, `fixed`, `handle`);
  - `rules`: for example `never_on_campaign`.
- [x] Element types: `headline`, `captions`, `label` (optional; shows one tag per point the speaker makes; add none, one or several; every style option as the headline), `takeaway`, `handle`, `text` (free), `logo`, `shape`, `button`, `progress_bar`, `effect` (zoom, shake, blur and the others already in `effects.py`), `music`. No emoji type. **In Phase 1 effects and music stay format-wide settings** (as today); they appear as fixed entries in the Layers list in Phase 2 and become elements you add in a later phase.
- [x] Write `from_legacy(design)`: converts today's switches into an element list. The Podcast and Stream presets must come out looking identical.
- [x] Teach `designs.normalize` to accept an element list, check each element in plain words (as it does now), and keep accepting the old shape by converting it. (The old sections are worked out from the elements, so the placement checks, AI Generate and the page that exists today keep working untouched.)
- [x] Teach `render.py` to draw from the element list. The existing painters (headline, chips, takeaway, handle, logo, button, bar) become the painters for their element types, each reading its style.
- [x] Keep the campaign rule in one place: elements marked `never_on_campaign` are dropped for campaign clips.
- [x] Migration: old saved templates are converted when loaded and saved back in the new shape the next time they are saved. Nothing is lost. (An element that was switched off but had been set up is kept hidden.)
- [x] Tests (`tests/test_elements.py`, `tests/test_formats_bench.py`): every picture the converted Podcast, Stream and four other formats draw is **pixel for pixel the same as the old code's**, at the same position and the same times (a frozen copy of the old code is in the test); the progress bar filter is the same text as before; each style option changes the output; bad values are refused in plain words; campaign clips are made without the brand elements; a real render shows free text, a box and a second progress bar.

You will see: nothing different in the dashboard. Your clips look the same.
Done when: the old and new versions of Podcast and Stream make identical clips in the tests, and all existing format tests still pass.

---

## Phase 2: The new builder layout

Goal: the preview in the middle, controls around it.

- [ ] Move the builder's JavaScript out of the page into `villomillo/web/static/formats-builder.js` (the page is 900 lines of inline script today). Same behaviour first.
- [ ] New layout in `formats.html` and the CSS:
  - **Left:** an **Add element** menu (every type) and the **Layers** list (rename, hide, lock, reorder by dragging, duplicate, delete).
  - **Center:** the phone-shaped preview, the play button, and the timeline slot (the timeline itself comes in Phase 3).
  - **Right:** the settings for the selected element only, with sections: text, look, position and size, timing.
  - **Below:** the format name, "Use this format for" categories, layout (face fill, fit with blur, stacked, split), music, effects, and the save buttons.
  - On a narrow window the side panels move below the preview.
- [ ] Every element type gets its full settings, including **labels** and the **handle** (point 2).
- [ ] **Add element** creates a new element with sensible defaults and selects it.
- [ ] Tests: the page loads with every element type; adding and deleting elements changes the saved format; labels and the handle show and save every style option.

You will see: the new builder, with the preview in the center and an Add menu.
Done when: you can add, select, style and remove every kind of element from the new layout, and save the format.

---

## Phase 3: A real preview that is always up to date, and the timeline

Goal: pressing play already shows your latest edit, and you can jump anywhere.

- [ ] **Segments.** Render the preview as short segments (length from Phase 0, about 5 s) so each is fast. In `preview.py` add `make_segment(...)` (one segment of one design) and a fingerprint so an unchanged segment is reused from the cache.
- [ ] **Priority.** A small render queue: the segment under the playhead first, then the segments next to it, then the rest. Any edit starts a new round and cancels the segments that are out of date.
- [ ] **Draft size.** Render at about half size so edits show fast (size from Phase 0). A full-size check is still available.
- [ ] **Endpoints:** `POST /api/formats/preview/start` (design + sample + playhead), `GET /api/formats/preview/status` (which segments are ready), `GET /formats/preview/<name>` (the files; this exists today).
- [ ] **Player.** Two video elements, swapped at each segment boundary, so playback looks continuous. The old picture stays visible until the new one is ready, with a small "updating" mark. Press play while a segment is still rendering: it waits for that segment and says so.
- [ ] **Timeline** under the play button: drag to any moment. It shows the nearest ready picture at once and renders that spot first. It also shows a thin bar per element for when it is on screen.
- [ ] **Changes start the render by themselves**, a short moment after you stop typing or dragging (no Render button).
- [ ] **Samples:** the builder opens on the default sample from Phase 0; a dropdown switches to the other samples.
- [ ] Remove the old **Check with real render** box and `make_preview`'s button (point 14), once this works.
- [ ] Tests: a segment renders only the asked stretch; an edit invalidates exactly the affected segments; an unchanged edit reuses the cache; two quick edits only finish the last one; dragging the timeline asks for the right segment first.

You will see: change a color, wait a moment, press play, and the change is already in the video; drag the timeline anywhere.
Done when: after an edit the segment under the playhead is ready in about the time measured in Phase 0, and a full play from start to end shows the edit everywhere without gaps.

---

## Phase 4: Edit look on your real short

Goal: Edit look opens the builder on the actual clip.

- [ ] **Store the clip's real text** when it is made (headline, point labels with their times, takeaway, effects the AI chose) in the clip's record next to `look_design`, so the builder can show them again. Today only the design is saved.
- [ ] `formats_page(..., clip_id)` returns the clip's video, its start and end, its words, its real text and its design. The preview uses these instead of a sample.
- [ ] The preview of a real clip is the segmented preview of that clip's own stretch of the video.
- [ ] **Which clips:** any clip that has not been posted. A clip whose post is uploaded or posted has **no Edit look link**, and `/api/formats/apply-clip` refuses it with a plain reason (point 11). A clip with a waiting (not yet sent) post can be edited; its post keeps its place in the queue and sends the new file.
- [ ] **Three save buttons** while editing a clip:
  - **Apply to this clip only** (the default): re-makes that clip with the edited look; the format is not changed.
  - **Save as a new format**: keeps the edit as a new template.
  - **Update "XXX" format**: changes the format the clip came from. It affects future clips only, never clips already made.
- [ ] Show the Edit look link in the clip panel for every clip that can be edited (today only clips waiting for review).
- [ ] Tests: opening with `?clip=` loads that clip's video, words and text; a posted clip is refused by the page and the server; each of the three save buttons does exactly its own thing; a clip with a waiting post keeps its schedule.

You will see: Clips, open a clip, Edit look: the builder opens with your real short in the center.
Done when: a change in the builder shows up in the real clip's file, the clip's panel shows the new look, and a posted clip offers no Edit look.

---

## Phase 5: The preview follows what is said

Goal: ghosts when paused, the real thing while playing.

- [ ] **Ghost mode** (paused, or not started): the stage shows every element as an outline with its default text, so you can see and select them all.
- [ ] **Live mode** (playing): the ghosts disappear and the real render shows what is happening: captions show the spoken words, a **label** appears with each point the speaker makes while they say it, the headline and the takeaway show the clip's real text.
- [ ] **Label timing** comes from the points found for the clip (their start and end times); for the fixed samples the points come from the sample text.
- [ ] Every other element uses the timing you set: the whole clip, the first N seconds, the last N seconds, or a time range. The timeline's thin bars show it.
- [ ] Switching between ghost and live happens on play and pause, and while dragging the timeline.
- [ ] Tests: at several moments of a test clip, the label that should be on screen is the point being spoken; elements with a fixed timing are on screen exactly in their range.

You will see: pause and see every element; press play and see the real words, labels and headline.
Done when: on test clips each label starts and ends within a fraction of a second of its point.

---

## Phase 6: Right-click, drag and resize

Goal: edit directly on the picture.

- [ ] Extend the stage's dragging (today the headline, captions, labels, button and logo) to **every element**, with resize handles on the ones that can resize.
- [ ] **Right-click** on an element opens our own menu (the browser's menu is blocked there): **Edit, Duplicate, Hide, Lock, Bring forward, Send back, Reset position, Delete**. Right-click on empty space: **Add element**.
- [ ] Clicking an element selects it and opens its settings on the right.
- [ ] Keyboard: Delete removes the selected element; arrow keys nudge it.
- [ ] Positions are kept inside the safe zone; the existing placement checks (`placement.py`) report a clash in plain words.
- [ ] Tests: each menu action changes the format as described; delete removes the element from the next render; drag and resize save the right numbers.

You will see: right-click the headline, choose Delete, and it is gone from the next preview; drag a label to a new spot.
Done when: every menu action works on every element type.

---

## Phase 7: Polish and safety checks

- [ ] A **drift test**: the stage's drawing and the real render put each element in the same place (compares positions on several test frames), so the preview cannot lie.
- [ ] Windows fonts: confirm every bundled font and uploaded font renders in the preview and the final clip.
- [ ] Clear plain-language messages for every refusal (too small to read, hidden behind the buttons, two elements overlapping).
- [ ] Update `docs/FORMAT-BLUEPRINTS.md` and the README for the element model.
- [ ] Full test run, then a run-through with you on your real clips.

Done when: you have used the builder on a real clip and a new format from start to finish.

---

## Risks

- **Render speed** decides how fresh the preview feels. Phase 0 measures it; half size and the segment cache are the answer if it is slow.
- **Segment seams**: switching between video pieces could flicker. The two-player swap and the tests in Phase 3 are for this.
- **Preview drift**: if the stage and the render disagree the preview lies. The drift test (Phase 7) and building both from the same element numbers prevent it.
- **Old formats**: the automatic conversion must be exact. Phase 1 compares frames pixel by pixel before anything else moves on.

## Questions still open

None. Phases 0 and 1 are being built; we stop after Phase 1 for your go on Phase 2.
