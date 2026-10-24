# Autopilot: paste a link, and VilloMillo does the rest

Status: **plan, nothing built yet.** Written 2026-10-24. Wireframes (interactive, private to your account):
https://claude.ai/artifact/SjggvWZAR5gVKLuhLydb1J

## 1. The idea in one paragraph

A link bar at the top of Home. You paste a YouTube, Twitch or Kick link. VilloMillo checks you may use the video,
works out what kind of video it is, shows you a one-screen plan, makes the clips in the right look, approves the ones that
pass every rule, writes the titles, picks the channel and the slot, and posts (or leaves a ready-to-copy post until a
platform's audit passes). You only step in when something is doubtful, and every doubtful item says why.

## 2. What exists today, and what is missing

| Step | Today | Missing |
|---|---|---|
| Take a link | `python -m villomillo clip <video id>` only. The dashboard has no link field | The link bar, URL parsing, a "run" record |
| Rights | `rights.py`: owned, licensed, campaign, Creative Commons, else refused. Strict by default | Asking the question inside the flow; remembering "this channel is mine" from the dialog |
| Detect the type | Nothing. A channel has one fixed `format:` | The type detector (F2 in docs/FORMAT-BLUEPRINTS.md), its accuracy test, its confidence gate |
| Look | `formats/`: one recipe, `podcast_talk`, built (F1). Gaming, sports and podcast layouts exist in the engine as switches | Recipes for stream, sports, outdoors (F3, F4) chosen by type |
| Clip | Engine job, polled by the worker (`do_clip`, `do_collect`) | Tying a job to a run and to the plan |
| Check and grade | `validate.py`, `campaign.py`, `grading.py`, originality guard in the blueprint resolver | Nothing: reuse |
| Approve | You, one clip at a time. The channel setting `autopilot: off\|review\|auto` is **defined but nothing reads it** | The rules engine that makes `auto` real |
| Route | `routing.py`: permission first, then a fit score per channel | A preview of routing before clips exist |
| Write | `copywriter.py` | Nothing: reuse |
| Post | `posting.py`: API once the platform's audit passed and dry run is off, else manual-ready | Reminders for manual posts; audits are outside our control |
| Learn | `learning.py`, weekly review, self-check | Feeding results back into the thresholds (advice only) |

## 3. The workflow, step by step

```
 1 Paste -> 2 Rights -> 3 Understand -> 4 Plan -> 5 Clip
                                                     |
 10 Learn <- 9 Post <- 8 Write+route <- 7 Approve <- 6 Check+grade
```

**1. Paste.** One field on Home. Accepts YouTube video links first; Twitch and Kick VOD or clip links in a later phase (the
engine already reads them). A duplicate link returns the existing run. A bad link gets a plain error.

**2. Rights check (seconds, never skipped).** Uses `rights.decide`. Three outcomes:
- *Cleared*: owned, licensed, listed by a campaign you joined, or marked Creative Commons by YouTube. Continue.
- *Unknown*: a dialog asks one question: mine / I have written permission (a note is required) / a campaign lists it (link and
  its wording) / none of these. The answer is stored with the video. "None" keeps it as research only.
- *Creative Commons* also needs credit to the original creator; autopilot adds a credit line to the caption (new work).

**3. Understand.** The type is detected per scene, not per video, by the cascade in docs/FORMAT-BLUEPRINTS.md section 5:
free metadata, then local signals (speech share, speakers, faces, sound classes), then a local vision model, then one Jev
question. Certainty of 0.75 or more picks that type's look. From 0.5 to 0.75 it uses the safe variant and asks. Below 0.5
it uses `generic_safe` and asks.

**4. The plan screen.** One screen, defaults already chosen: rights result, detected type and why, the look, where it will
go and why, how many clips and how often, the autopilot level, and the spend cap. "Start autopilot" begins. In Hands-off
mode it can start by itself after a short countdown.

**5. Make the clips.** The engine downloads, transcribes, finds moments and renders each one in the chosen look. A live
timeline shows the stage. You can close the page; the run continues and the results wait on Home.

**6. Check and grade.** Technical checks, campaign rules, originality, then the grade (stands alone, hook, payoff).

**7. Approve or ask.** The ten rules in section 5. All pass: approved, with the reasons shown and an undo until it posts.
Any fail: it waits for you, or is held, with the exact rule named.

**8. Write and route.** Claude writes title and caption options, code checks them, Jev screens them, the best is chosen.
The channel is picked by permission first, then fit. A clip that fits nowhere is held, never posted.

**9. Schedule and post.** Next free slot respecting the gap and daily cap. API route once that platform's audit passed;
otherwise a ready-to-copy post with a reminder.

**10. Watch and learn.** Views return, grades are tested against them, the weekly review suggests changes. Suggestions are
advice; nothing is applied automatically.

## 4. Autonomy levels and how hands-off is earned

| Level | What it does |
|---|---|
| Off | Analyses the link only. |
| Review (today's default) | Makes the clips and writes the posts. You approve each clip with one tap. |
| Hands-off | Approves clips that pass all ten rules, schedules and posts them. Only doubtful clips ask you. |

Hands-off has to be earned, twice:
1. **Per video type.** A type may go hands-off only after it reads correctly in at least 90% of 40 or more clips you
   labelled. Until then that type stays on Review whatever the level says.
2. **Shadow mode.** For the first 20 clips of a channel, autopilot decides but does not act, and compares its decision with
   yours. Hands-off unlocks when they agree on at least 90%. The numbers are placeholders to tune.

An **emergency stop** pauses every automatic step on every channel at once.

## 5. The ten rules a clip must pass to be approved without you

| # | Rule | If it fails | Built |
|---|---|---|---|
| 1 | Rights allow it | Not clipped; asks once | yes |
| 2 | Technical checks pass | Held with the reason | yes |
| 3 | Campaign rules met | Held with the reason | yes |
| 4 | Original value added (not just crop and captions) | Simpler look, else held | yes |
| 5 | Grade at or above your limit | Waits for you | yes |
| 6 | Video type certain and trusted | Safe look, waits for you | new |
| 7 | A channel fits it well | Held: fits no channel | yes |
| 8 | Not a repeat of a posted or scheduled clip | Waits for you | new |
| 9 | A slot is free and the account is active | Waits for the slot; paused means copy by hand | yes |
| 10 | The budget has room | Autopilot stops and says why | yes |

## 6. Wireframes (ASCII copies; the interactive set is in the link above)

Home, link bar:
```
+----------------------------------------------------------------------+
|        Paste a link. VilloMillo does the rest.                       |
|  [ Paste a YouTube, Twitch or Kick link ..............] [Start autopilot]
|  Autopilot is Review: clips wait for your tap. Change                |
+----------------------------------------------------------------------+
| Autopilot runs                         | Needs you                   |
|  [img] How to Create ... Making 62%    |  Review 2 clips             |
|  [img] Why Smart People ... 2 need you |  One link needs a rights    |
|  [img] Ranked ladder ... 3 posted      |  3 posts ready to copy      |
+----------------------------------------------------------------------+
```

Plan screen after pasting:
```
[thumb] How to Create Your Own Luck  - YouTube - 52 min          [Remove]
+----------------------------------------------+ +--------------------+
| 1 Rights: cleared (campaign video)           | | How hands-off?     |
| 2 What it is: podcast, 92% sure   ######..   | |  ( ) Off           |
|   Look: Clean frame, hook title, captions    | |  (o) Review        |
| 3 Goes to: CavemanCam, picked clip by clip   | |  ( ) Hands-off     |
| 4 Makes: up to 6 clips, 2 a day, Sat 3:26 PM | |  Cap $25/month     |
| > Show what it saw                           | | [Start autopilot]  |
+----------------------------------------------+ +--------------------+
```

## 7. What gets added (technical)

- **`runs` table**: id, profile, source and video id, url, status (analysing, needs_rights, planned, running, done,
  stopped), level, plan as JSON, timestamps. One row per pasted link.
- **`villomillo/autopilot.py`**: `intake(url)`, `build_plan(run)`, `decide(clip)` returning approve, ask or hold plus the list of
  rules passed and failed, stored on the clip so every decision can be explained.
- **Detector** `villomillo/formats/detect.py` (F2), with the labelled sample and accuracy report.
- **Endpoints**: `POST /api/intake`, `POST /api/rights-answer`, `POST /api/run-start`, `POST /api/run-stop`, `GET /run/{id}`.
- **Web**: the link bar on Home; the plan, rights dialog, run and results pages; the autopilot section in Settings. They reuse
  the existing dashboard styles (dark, amber, Geist).
- **Config**: thresholds in `config/villomillo/app.yaml` (grade, certainty, fit, daily cap); `autopilot:` per channel finally read.
- **Sources**: a small source abstraction so a run can start from a YouTube, Twitch or Kick link; the engine already parses all
  three (`sources/urlmatch.py`).

## 8. Build order

| Step | Work | Done when |
|---|---|---|
| A1 | Link bar, `runs` table, intake for YouTube links, duplicate handling | Pasting a link creates a run and shows a plan in about 10 s; costs 1 YouTube unit |
| A2 | Rights dialog and answers stored | A link with unknown rights never reaches the engine without an answer (test) |
| A3 | Plan builder: rights, look, routing preview, slots | The plan's channel matches what routing picks later on test clips |
| A4 | Run orchestration and the live run page | A fake-engine end-to-end test goes from paste to planned posts |
| A5 | Type detector (F2) and its accuracy test | 90% on 40+ labelled clips per type, or that type stays on Review |
| A6 | Rules engine, `autopilot: auto`, shadow mode, undo | Unit test per rule; shadow mode reports agreement with your decisions |
| A7 | Results page, Needs you, emergency stop | Every held or waiting clip names its rule |
| A8 | Twitch and Kick links; add-a-channel wizard; stream and sports looks (F3) | A Twitch VOD runs end to end into a gaming channel |
| A9 | Hands-off posting once audits pass | A scheduled post goes out through the API with no tap |
| A10 | Results feed the weekly review | Review shows which types and looks performed |

## 9. Limits to be honest about

- **"Any video" is limited by rights, not by software.** Autopilot clips only what you own, are licensed for, a campaign lists, or
  YouTube marks Creative Commons. For anything else it asks once, and "none" means research only. YouTube's reused-content
  policy also expects original value, which rule 4 enforces. This is a safety design, not legal advice.
- **Fully hands-off posting depends on platform audits.** Until YouTube's audit passes, API uploads are not public, so those posts stay
  ready-to-copy. Instagram needs a public file link. These are outside our control and not quick.
- **The detector is not built, and its accuracy is unknown.** That is why hands-off is earned per type with a test.
- **Your PC has to be on**, with the engine (`python main.py serve`) and `python -m villomillo run` running.
- **Run time and cost are unmeasured.** The first real run measures both; the caps stop it regardless.

## 10. Decisions needed

1. Which channels besides CavemanCam should exist (for example a gaming channel), and what does each accept?
2. Do you paste only videos you have rights to, or should "none of these" be allowed at all (research only)?
3. First link sources: YouTube only, or Twitch and Kick from the start?
4. Starting limits: grade 75, certainty 75%, fit 4 of 5, 2 auto-posts a day. Keep or change?
5. How should it tell you about things that need you: the dashboard only, or email or phone too?
6. Should the link bar also accept a channel link, to watch it?
