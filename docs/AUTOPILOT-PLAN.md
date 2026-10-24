# VilloMillo Autopilot: the plan

Status: **plan, nothing built yet.** Written 2026-10-24. This replaces every earlier autopilot plan (the old file was removed).
Wireframes: https://claude.ai/artifact/SjggvWZAR5gVKLuhLydb1J

Not replaced, because they describe work that is already built: `docs/DASHBOARD-REDESIGN.md` (the dashboard) and
`docs/FORMAT-BLUEPRINTS.md` (the look system; its type detector, F2, is used by this plan).

## 1. What we are building

A system that is always working, finding or being given videos, making the clips most likely to earn, and posting them to
the right channels. **Everything is about the best CPM.** It needs you for almost nothing: no approval step, only scores.

**Three ways to supply videos. One pipeline does everything else.**

| Source | How a video arrives | Where it lives in the dashboard |
|---|---|---|
| **Paste** | You paste a YouTube, Twitch or Kick link | The link bar on Home |
| **Research** | The system searches YouTube (Creative Commons only), Twitch and Kick for videos with a high enough expected CPM | Research |
| **Campaigns** | The system finds Content Rewards campaigns worth clipping, joins them, and queues their source videos | Campaigns |

## 2. Decisions that are locked

| Topic | Decision |
|---|---|
| Rights | No rights check for pasted links. Twitch and Kick: any public streamer, rights assumed. YouTube research: Creative Commons only, with credit given. **Credit is added only for research videos.** The old strict rights gate is removed. |
| Where videos come from | YouTube, Twitch and Kick only. TikTok is posting only. |
| Approval | None. Scores decide. Nothing waits for a tap, except the cases in section 9. |
| Channel | One YouTube channel, one TikTok account and one Instagram account **dedicated to one category** (Podcasts, Stream clips, ...). All owned by you. A clip posts to all three of its category's accounts. |
| No channel fits | The clips are held and Home says "create a channel for this category". Once you add it, they post. |
| Limits to clear | Clip grade 75 or more, video-type certainty 75% or more, channel fit 4 of 5 or more. |
| Campaign floor | $1.00 per 1,000 views and enough budget left to be worth it. Campaigns are joined by the system itself when they clear the floor. |
| Spend | No monthly caps. A **safety breaker** pauses everything at $25 Claude or $5 Jev since your last approval and asks to continue. Out of funds: it pauses and says which account to top up. |
| Daily posting | No limit of our own. Per-account platform limits apply: YouTube 100 a day per channel (your number), TikTok and Instagram as in section 7. |
| Paste screen | Shows the plan and the score of each clip it wants to make. If none score, you can remove the video. If some do, you press Start. |
| Hosting | Always on, on your computer. |
| Build order | Paste, then Campaigns, then Research. |
| Old code | Keep, rewrite or delete as needed (section 10). |

## 3. The pipeline

```
 Paste ----\
 Research ---+--> 1 Analyse --> 2 Score --> 3 Plan --> 4 Clip --> 5 Check --> 6 Route --> 7 Post --> 8 Learn
 Campaigns -/        |            |           |          |          |           |           |
                  download,    viral score   (Paste:   render in  length,     category    each platform's
                  transcribe,  + CPM +       you press the look    sound,      -> channel  limits; campaign:
                  find moments expected $    Start)               campaign    or HOLD     submit the link
                                                                  rules
```

1. **Analyse.** The engine downloads, transcribes and finds candidate moments. *This needs a new engine option: stop after
   analysis and hand back the candidates, then render only the ones chosen* (today the engine does all four stages in one job).
2. **Score.** Each candidate gets a viral score, an estimated CPM and an expected revenue (section 4). Detecting the video type
   (podcast, stream, sports...) happens here too, using the cascade in docs/FORMAT-BLUEPRINTS.md section 5.
3. **Plan.** Paste shows it and waits for Start. Research and Campaigns skip the screen and go straight on if the video clears
   their floor.
4. **Clip.** Render the chosen candidates in the look for their type.
5. **Check.** Length, picture, sound, safe zones, originality, the campaign's own rules, and the grade again on the finished clip.
   A clip below the floors is not posted.
6. **Route.** Permission is no longer a factor. The clip's type gives its category, the category gives its channel. No fitting
   channel: HOLD.
7. **Post.** Title and caption options are written and the best chosen. Each account posts within its platform's limit; queued
   clips go in order of expected revenue. A Research video adds a credit line. A campaign clip's link is submitted.
8. **Learn.** Real views (and campaign payouts) come back and correct the estimates.

## 4. Scoring: one formula for all three sources

- **Viral score (0 to 100)**: how likely the clip is to perform. From the engine's moment score (text, audio, visual, reaction,
  engagement), plus Jev's reading of the clip: stands alone, hook, payoff. Floor: **75**.
- **CPM, in dollars per 1,000 views**:
  - Campaign: the payout the campaign states. Real money.
  - Paste and Research: an *estimate*. Until real results exist it is anchored to the campaign floor:
    `estimated CPM = $1.00 x category factor x (viral score / 75)`. The category factor starts at 1.0 and is learned from real
    results. Estimates are labelled "estimated" everywhere.
- **Expected revenue** = predicted views x CPM / 1,000. Predicted views start as a rough guess from the source video's reach
  (views, channel size, age) and are replaced by measured results.
- **The gate**: a clip is made and posted only if viral score is at least 75 **and** CPM is at least the floor for its source:
  - Paste: estimated CPM at least $1.00 (so viral 75 or more, until learning moves it).
  - Campaign: the campaign's CPM at least $1.00, and budget left.
  - Research: at least the **rising bar**.
- **Rising bar (Research)**: starts at $1.00. It rises in small steps only when the real CPM of posted research clips beats what
  was predicted, up to a ceiling you set. It never falls below $1.00. Step size and ceiling are starting values to tune.
- **Queue order** when things compete (processing and posting): highest expected revenue first.

## 5. Channels

A **channel** = a category + its YouTube channel + its TikTok account + its Instagram account + its look and caption tags.
- Stored in the dashboard, not in files. A **Channels** page lists them; **Add channel** is a short wizard: name, category, connect
  YouTube, connect TikTok, connect Instagram, caption tags. Each connection shows its state (connected, audit pending, paused).
- **Categories** are the baseline types: Podcast, Stream clips (Twitch and Kick), Sports, and more as looks are built.
- **Hold and create**: a scored clip whose category has no channel is kept, not posted. Home shows "Create a channel for
  *Outdoors*" with an Add channel button. Adding it releases the held clips.
- One category can have only one channel per platform for now.

## 6. The three sources in detail

**Paste.** Link bar on Home. Accepts YouTube, Twitch and Kick (video or clip links; the engine already reads all three). Flow:
paste, then Analysing with progress (minutes, since it downloads and transcribes), then the **plan screen**: the video, its detected
type, its category and channel, and a table of candidate clips with viral score, estimated CPM and expected revenue, each marked
as passing or not. None pass: "Remove video" (and the analysis is thrown away). Some pass: "Start clipping" makes those, and
they post automatically.

**Campaigns (Content Rewards).**
1. *Find*: read open campaigns with their payout rate, budget left, per-clip cap, minimum views, allowed platforms, rules, how many
   clips are wanted and the source (a channel or videos).
2. *Vet*: floor $1.00 per 1,000 views; budget left worth the work; an allowed platform we have an account for; a channel whose
   category fits; and a source we can reach. Campaigns are ranked by expected earnings.
3. *Join*: automatically if it clears the floor. **If the API cannot join, the fallback is: it scores the campaign and shows you
   everything, and you join by hand.**
4. *Queue*: find the source channel's videos and feed the pipeline, newest first. The campaign's rules (turned into checkable
   rules by Claude, plus judged ones by Jev) apply to every clip. The campaign's clip count is followed; with none stated there
   is no limit.
5. *Submit*: post to the campaign's allowed platforms, then submit the link. **If submission cannot be done by API, the clip is
   shown under "Ready to submit" with the link and a copy button.**
6. *Track*: approved views and payout feed Learn. Budget left is re-checked before each clip, and the campaign is dropped when it runs out.
- **API status is unknown.** Whop's docs describe API keys and a bounty-submissions API; nothing I found confirms campaign endpoints for
  Content Rewards. Step one of this section is a small probe script you run once with your key on your PC; what it prints decides
  how much is automatic.

**Research.** A "Run Research" switch in its own section. It looks for videos with the highest expected CPM and runs the pipeline on
any that clear the rising bar, with no plan screen.
- *YouTube*: Creative Commons videos only, with a credit line in the caption. Search is limited (search results report about
  100 searches a day by default), so discovery leans on cheap calls: channel feeds, most-popular lists and category browsing,
  and uses search sparingly. A quota increase can be requested.
- *Twitch and Kick*: any public streamer's videos and clips, found through the engine's channel feed code (yt-dlp), no YouTube quota.
- It reports what it looked at, what it skipped and why, and the current bar.

## 7. Posting and platform limits

| Platform | Cap | Needs |
|---|---|---|
| YouTube | 100 posts per channel per day (your number; Google's own upload bucket is reported to be about 100 a day per project, unconfirmed) | Audit passed for public posts; until then uploads are private |
| TikTok | About 15 per account per day, shared across API apps, varies (TikTok developer guidelines) | Content Posting API approval; unaudited apps post privately |
| Instagram | 100 API posts per 24 hours is the figure I recall; not confirmed | Business or Creator account linked to a Facebook Page; a public link to the file |

Each account has a daily counter; when it is full, clips wait for the next day in expected-revenue order. You will do the approvals
and audits; until each passes, that platform's posts are ready to copy by hand and Home says so.

## 8. Safety and always-on

- **Safety breaker**: pause at $25 Claude or $5 Jev spent since your last approval; approve to allow another $25 or $5. A provider
  error that means no funds also pauses, naming the account.
- **Emergency stop**: pauses every automatic step on every channel.
- **Undo**: a posted clip can be deleted from its platforms from the Calendar where the API allows.
- **Always on**: `run-app.bat` starts the engine, the worker and the dashboard together; a startup entry runs it at login; a watchdog
  restarts the engine if it dies; Home shows a health line (engine, worker, last post, funds). Windows sleep must be off.

## 9. What still needs you

Only four things, all on Home under **Needs you**:
1. A clip's category has no channel: **Add channel**.
2. The safety breaker tripped, or funds ran out: **Approve** or top up.
3. A campaign that has to be joined or submitted by hand (if the API cannot do it).
4. A platform that is paused, disconnected or waiting for an audit.

## 10. Existing code: keep, rewrite, delete

Preliminary, from reading the modules. Each decision is re-checked file by file in step A0 before anything is deleted, and the
tests are kept green after each removal.

| Module | Decision | Why |
|---|---|---|
| `youtube.py`, `quota.py`, `secrets.py`, `store.py`, `db.py`, `tasks.py`, `validate.py`, `formats/`, `copywriter.py`, `studio.py`, `posting.py`, `accounts.py`, `web/` | **Keep, extend** | Work and fit the plan (posting gains TikTok and Instagram; accounts become per channel) |
| `clipper.py` | **Keep, change** | Drop the rights check; add analysis-only and render-chosen; accept YouTube, Twitch, Kick |
| `routing.py` | **Rewrite** | Category-based channel choice; no rights |
| `campaign.py` | **Rewrite** | Generic campaign rules from a parsed brief, not one hard-coded campaign |
| `discovery.py`, `filters.py` | **Rewrite** | New sources (Creative Commons search, Twitch, Kick), source policy, the CPM floor |
| `scoring.py`, `grading.py`, `engine.py` | **Rewrite** | Replaced by the viral score, CPM and expected-revenue model and the shared pipeline |
| `learning.py`, `selfcheck.py` | **Rewrite** | Learn from real views and payouts, not from old uploads only |
| `runner.py`, `cli.py`, `config.py` | **Rewrite** | Three sources, channels in the database, no budgets file |
| `claude.py`, `jev.py` | **Keep, extend** | Add funds-out detection and the breaker |
| `rights.py` and its tables | **Delete** | The gate is gone; campaign sources get their own table |
| `config/villomillo/channels/solved.yaml`, the budgets in `app.yaml` | **Delete after migrating** | The Solved channel becomes a database channel; its campaign becomes a Campaigns record; budgets become the breaker |

## 11. Build order

| Step | Work | Done when |
|---|---|---|
| A0 | File-by-file audit, remove the rights gate and budget caps, migrate the Solved channel | Tests green; no code path refuses a pasted link for rights |
| A1 | Channels: database model, Channels page, Add channel wizard (YouTube first), hold-and-create | Add a channel; held clips release (test) |
| A2 | Engine: analysis-only and render-chosen; Twitch, Kick and YouTube intake | A video returns scored candidates without rendering; render makes only the chosen |
| A3 | Scoring: viral score, estimated CPM, expected revenue, gate; type detector v1 and category | Plan data for a test video is stable and explained |
| A4 | **Paste complete**: link bar, plan screen, Start, run page, auto-post on the gate, Needs you, safety breaker, emergency stop | Paste to posted clip with a fake engine; breaker pauses at the amounts |
| A5 | TikTok and Instagram posting, per-account caps, stats collection | A post goes to the three accounts; caps hold |
| A6 | **Campaigns**: probe, vet, join or manual fallback, queue, rules, submit, track | A real campaign is vetted and queued; manual steps shown if no API |
| A7 | **Research**: Run Research, YouTube Creative Commons, Twitch, Kick, rising bar, credit line | Research finds and clips a video unattended |
| A8 | Learning loop: real stats correct the category factors and the rising bar | Predicted versus real views shown on Insights |
| A9 | Always-on: startup entry, watchdog, health line | Survives a reboot and an engine crash |

## 12. Limits to be honest about

- **No human approval means mistakes post publicly.** The protection is the scores, the checks, the breaker, the emergency stop and
  undo. Early on, the CPM numbers for Paste and Research are guesses; they improve only as real results arrive.
- **Platform approvals take time and are outside our control.** Until YouTube's audit, TikTok's audit and Instagram's review pass,
  those posts are private or ready to copy.
- **Campaign automation depends on an API that may not exist.** The fallback is manual join and submit.
- **Analysing a video costs time and money** (download, transcribe, score) even when the answer is "remove it".
- **Using any public streamer's content, with rights assumed, can lead to takedowns or strikes on your accounts.** That is your call;
  the plan records it so it is not a surprise.
- **Campaign payouts** have per-clip caps, a review delay (third-party guides say about 72 hours), and budgets that run out; the plan re-checks budget before each clip.
- **YouTube search is rationed** (reported about 100 a day), which limits Research until a quota increase.

## 13. Still to confirm

1. The probe result from your Whop key (what campaigns and submissions it can see).
2. Is the Content Rewards campaign you joined the Solved campaign already set up here?
3. The first channel's name and handle, and its category.
4. The Instagram limit (100 per 24 hours) from Meta's current docs.
