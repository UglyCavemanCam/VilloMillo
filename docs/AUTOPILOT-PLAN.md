# VilloMillo Autopilot: the plan, in plain words

Written 2026-10-24. Nothing here is built yet. Pictures of every screen (example data):
https://claude.ai/artifact/SjggvWZAR5gVKLuhLydb1J

This replaces all earlier autopilot plans. Two older documents stay because they describe things already built:
`docs/DASHBOARD-REDESIGN.md` (the dashboard) and `docs/FORMAT-BLUEPRINTS.md` (how clips are made to look).

---

## 1. The goal in one paragraph

VilloMillo should always be working. It gets videos, picks the moments most likely to earn money, turns them into clips,
and posts them to the right YouTube channel, with no one approving anything. **Everything is judged
by one thing: the best CPM** (what a video earns per 1,000 views). You step in only when something truly needs you.

## 2. Decided since the first version

- **Instagram is out of the plan and the system** (it allows only 3 reels a day, which does not meet the requirements).
- **TikTok is out of the plan and the system** (posting to it automatically needs a TikTok developer app approval that is not available). Posting is
  YouTube only. (The engine's own code, inherited from Clips Kitty, still has Instagram and TikTok parts; VilloMillo does not use them.)
- **First channel: Podcasts.** Its YouTube channel is the one already connected.
- **The Solved campaign** is already joined by hand on Content Rewards. It becomes the first record on the Campaigns page.

## 3. Words used in this plan

| Word | What it means here |
|---|---|
| **CPM** | Dollars earned per 1,000 views. |
| **Viral score** | A number from 0 to 100: how likely a clip is to do well. Below 75, we don't make or post it. |
| **Category** | The kind of video: Podcast, Stream clips, Sports, and so on. |
| **Channel** | One category with its own YouTube channel. A clip posts there. |
| **Held** | A good clip that has nowhere to go yet (no channel for its category). It waits; nothing is lost. |
| **Audit** | A platform's approval for posting automatically and publicly. Until it passes, posts stay private or wait for you. |

## 4. Three ways videos come in. One system does the rest.

| Way in | How it works | Where it lives |
|---|---|---|
| **Paste** | You paste a YouTube, Twitch or Kick link. You see the clips it would make and their scores, then press Start. | The link bar on Home |
| **Research** | You switch on "Run Research". It looks for videos with the best expected CPM and clips them on its own. | The Research page |
| **Campaigns** | It finds Content Rewards campaigns paying $1.00 or more per 1,000 views, clips their videos, and shows you what to join and submit (see section 12: Whop's key permissions do not offer joining or submitting). | The Campaigns page |

Only the way a video arrives is different. The scoring, clipping and posting are identical for all three.

## 5. What happens to every video

1. **Look at it.** The video is downloaded and read, and possible clips are found. Nothing is made yet.
2. **Score it.** Every possible clip gets a viral score, an estimated CPM and an expected revenue. The system also works out
   what kind of video it is (podcast, stream, and so on).
3. **Decide.** Paste: you see the scores and press Start. Research and Campaigns: it goes ahead on its own if the video is good enough.
4. **Make the clips.** Only the clips that scored well are made, in the right look for that kind of video.
5. **Check them.** Length, sound, picture, the campaign's rules, and the score again on the finished clip. A clip that falls
   short is not posted.
6. **Pick the channel.** The kind of video decides the category, and the category decides the channel. No channel for it? The clip is held. (Until every category has its own YouTube channel, the switch at the top of Channels, on by default, sends every clip to one chosen channel instead, so nothing is held. Turn it off to go back to one channel per category.)
7. **Post it.** A title and caption are written and the clip is posted to the channel's YouTube, inside its
   daily limit. A campaign clip's link is then submitted to the campaign.
8. **Learn.** Real views and payouts come back and make the next scores more accurate.

## 6. How it decides what is worth posting

- **Viral score** needs to be **75 or more**.
- **CPM** needs to clear a floor, and the floor depends on how the video arrived:
  - **Campaign:** the campaign pays at least **$1.00** per 1,000 views, and has money left.
  - **Paste:** estimated CPM at least **$1.00**.
  - **Research:** estimated CPM at least the **rising bar**. It starts at $1.00 and moves up in small steps, but only when real
    results beat what was predicted. It never drops below $1.00. You set the highest it can go.
- **Before real results exist,** CPM for Paste and Research is an estimate: `$1.00 × category factor × (viral score ÷ 75)`.
  It is always labelled "estimated" and is replaced by real data as YouTube stats come in.
- **Expected revenue** = predicted views × CPM ÷ 1,000. When several clips are waiting, the highest expected revenue goes first.

Example: a clip with a viral score of 91 has an estimated CPM of $1.21. With a rough guess of 8,000 views, that is about
$9.68. A clip scoring 68 is estimated at $0.91 and is not made.

Two more checks: the **video type** must be at least 75% certain (otherwise a safe, plain look is used), and the **channel fit**
must be 4 out of 5 or better (otherwise the clip is held).

## 7. Channels

- A channel is a category plus its YouTube channel. The account is yours.
- A **Channels** page lists them. **Add channel** is a short step-by-step: name and category, connect YouTube,
  then caption tags and the look.
- If a good clip has no matching channel, it is held and Home says **"Create a channel for [category]"** with an Add channel
  button. When you add it, the held clips post.

## 8. The only things that need you

All shown on Home under **Needs you**:
1. A clip's category has no channel yet. Press **Add channel**.
2. Spending paused (see below), or an account ran out of money. **Approve** or add funds.
3. A campaign that has to be joined or submitted by hand (only if Content Rewards has no way to do it automatically).
4. A platform that is paused, disconnected, or still waiting for its audit.

## 9. Safety

- **Spending guard:** there are no monthly caps, but everything pauses when Claude has spent **$25** or Jev **$5** since your
  last approval. You approve to let it spend that much more.
- **Out of money:** if Claude or Jev runs out of funds, the system pauses and tells you which one to top up.
- **Emergency stop:** one button pauses everything on every channel.
- **Platform limit:** YouTube 100 posts per channel per day (your number). When a channel reaches its limit, its clips wait for the next day.
- **Always on:** it runs on your computer. One start file launches the engine, the worker and the dashboard, starts them when you
  log in, and restarts the engine if it stops. Your computer must not go to sleep.

---

## 10. The phases, in simple terms

There are ten phases. You get something usable after Phase 5. Effort is my rough guess: small, medium or large.

### Phase 1: Clear the decks (small to medium)
**In plain words:** Before building, remove the parts of the old system that don't fit. That means the rule that refuses to
clip a video without proof of rights, the monthly spending caps, and the old research code that was written in a rush. Each file is
checked first and either kept, rewritten or deleted. The tests are run after every removal so nothing quietly breaks.
**After this:** nothing new to see, but pasting a link can no longer be blocked over rights, and AI spending is guarded by the
pause-and-approve breaker instead of monthly caps.
**Done when:** all tests pass and no part of the code refuses a pasted link.
**Status: done.** The old research code was deleted outright (the app is not live, so nothing needed keeping); Research and the scoring brain are rebuilt in Phases 4 and 8. The per-file decisions are in `docs/CODE-AUDIT.md`. The spend breaker was built here rather than in Phase 5, so removing the
caps never left spending unguarded. Moving the Solved channel into the new channel system moves to Phase 2, where channels are built.

### Phase 2: Channels (medium)
**In plain words:** Build the Channels page and the Add channel steps. Channels are stored in the dashboard instead of in files.
Add the "held, then create a channel" behaviour. YouTube connects on each channel's page.
**After this:** you can add and edit channels and see which accounts are connected.
**Done when:** adding a channel releases clips that were held for it.
**Status: done.** Channels live in the database. The Channels page lists them with their YouTube status; Add channel creates one (name, category,
what it posts) and then walks you through YouTube, tags and look on the channel's page. A clip with a category posts only on that
category's channel; with none it is held and Home says "Create a channel for ...". Adding the channel plans the held clips, best score first.
Podcasts is the starting channel (its YouTube sign-in is the one you already made). Clips get their category from their channel until the
type detector (Phase 4) assigns it. The Solved campaign rules now live in `campaigns.py` (Phase 7).

### Phase 3: Teach the engine to preview (large)
**In plain words:** Today the engine does everything in one go: download, read, find moments, then make the clips. Your plan
screen needs the scores before any clips are made, so the engine must be able to stop after finding the moments, show them,
and then make only the ones chosen. It must also accept Twitch and Kick links as well as YouTube. This is the biggest technical
change in the plan.
**After this:** you can ask the engine for scored moments without making any clips.
**Done when:** a video returns its scored moments, and a later request makes only the chosen ones.
**Status: done, tested with stand-ins for the slow parts (no real video has been run through it yet).** A job can now be a **preview**: the engine
downloads, reads and scores the video, keeps every moment it found (numbered, best first, with its score and the reason), and makes no
clips. A second job can name the moment numbers to make and makes only those, without finding them again or re-measuring the video.
Twitch and Kick past broadcasts already worked in the engine; VilloMillo now recognises YouTube, Twitch and Kick links (`villomillo/links.py`),
and refuses live channels, Twitch clips and other sites with a plain reason. Try it: `python -m villomillo preview <link>`, then
`python -m villomillo make <link> --moments 1,3`. The scores are the engine's own for now; Phase 4 adds the viral score, category and CPM on top.

### Phase 4: The scoring brain (medium to large)
**In plain words:** Build the part that gives every possible clip its viral score, estimated CPM and expected revenue, and that
works out what kind of video it is (so it knows the category and the look). This is the formula in section 5.
**After this:** any test video produces a clear, explainable list of scores.
**Done when:** the same video always gives the same scores, and each score can be explained in a sentence.
**Status: done, tested with a stand-in for Jev (not yet with the real service).** `villomillo/scoring.py` scores every previewed moment: **viral** =
60% the engine's rating + 40% Jev's reading of the clip's words (hook, payoff, stands alone); **estimated CPM** = $1.00 x category factor x viral / 75;
**expected revenue** = predicted views (a flat 8,000 guess for now) x CPM / 1,000. Each moment carries one plain sentence per number and the verdict
("Will not be made: the viral score 60 is under 75 ..."). The video's **kind** (podcast or interview, livestream, talk, or something else) comes from Jev
weighing each kind; the certainty is the winner's share, and under 75% the plain look is used. The kind sets the category (Podcasts, Stream clips,
Talks, Other) and the look. Jev's answers are stored by the text they were about, so the same video always gives the same scores and nothing is paid for
twice; a tripped spend breaker stops new readings. The floors differ by source (Paste $1.00, Research the rising bar, Campaign the campaign's own pay). Every
number lives in `scoring:` in `app.yaml`. Try it: `python -m villomillo score <link>`. Not built yet: channel fit (4 of 5), which Phase 5 adds when it
picks the channel; category factors are all 1.0 until real results exist (Phase 9).

### Phase 5: Paste works from start to finish (large) — the first thing you can use
**In plain words:** Put it together for the Paste way in. The link bar on Home; a progress screen while it looks at the video; the
plan screen with the score of each clip and a Start button (or "Remove video" if none qualify); then the clips are made, checked
and posted automatically. This phase also adds the spending guard, the emergency stop and the Needs you list.
**After this:** you paste a link and clips appear on your YouTube channel with no further work.
**Done when:** a test link goes all the way to a posted clip, and the spending guard pauses at the right amounts.
**Status: built and tested with stand-ins for the engine, Jev and Claude; not yet run on a real video.** Home has the link bar (YouTube, Twitch or Kick) and a
Videos list. A pasted link becomes a *run*: the worker has the engine look at it, scores every moment, works out the video's kind, category, channel and fit
(4 of 5 or better), and the **plan screen** shows each clip's viral score, estimated CPM and expected revenue with a "Why these numbers" explanation, a
**Start clipping N clips** button, or "No clip here is likely to earn enough" with **Remove video**. Start makes only the clips that cleared the floors, in
the look the kind decides. Each finished clip that passes its checks is approved without waiting for you and planned on its category's channel (held, with
"Create a channel for ...", if there is none; sent to your review queue if the video fits the channel under 4 of 5). **Needs you** lists a plan waiting for
Start and any failed video. The **Emergency stop** button in the top bar pauses every automatic step until you resume; the spending guard banner from Phase 1
is on every page. The Channels page now has "How YouTube posts go out" and "Send posts for real". Posts are only written to a folder until you turn that on
(and, for automatic YouTube posting, record the YouTube audit as passed). Not done: scoring the finished clip a second time (the existing checks and the
stand-alone reading still apply), The Podcasts channel now has a general podcast
description instead of the Solved one; if your database already holds the channel, edit those on its page. `run-app.bat` now starts the worker too, which pasted links need.

### Phase 6: Real results and the daily limit (small)
**In plain words:** A daily limit counter for the channel's YouTube account, and collection of real view counts for the learning step. (This phase was
TikTok posting. TikTok was removed from the plan because its automatic posting needs a developer approval that is not available.)
**After this:** the channel page shows how many posts went out today against the limit, and Insights shows real views.
**Status: done.** The channel page shows "Posted today N of cap" (YouTube: 100 by default, editable). A post that would go over waits for tomorrow. Every 6
hours the app reads the real views, likes and comments of recent posts through your YouTube key (a post you made by hand counts too, because its video is
found from the link you recorded) and keeps them; Insights shows them. If the last five posts of a channel get under a third of its usual views, its YouTube
account is paused with that reason. Also changed here: the Podcasts channel no longer carries the Solved campaign's required tags or its 2-a-day / 3-hour
spacing, and the YouTube fallback description no longer says "Solved". A channel saved while TikTok was part of the app still loads: its TikTok settings are dropped.
**Done when:** the daily limit holds and real views are collected.

### Phase 7: Campaigns (large)
**In plain words:** Whop's API cannot list or join Content Rewards campaigns (the test showed this), so the system reads the public campaign list on
contentrewards.com first, checks the pay and the money left, reads each campaign's rules, finds its videos and clips them. If that list cannot be
read, you paste a campaign link and the system reads that one page. It then shows you exactly what to join and which links to submit (a few taps).
**After this:** a Campaigns page listing every campaign it looked at, why it recommends or skips each one, and a queue of videos.
**Done when:** one real campaign is checked and queued, and any manual steps are clearly shown.
**Status: built.** The worker reads the public list hourly. A campaign is "worth joining" only if it pays by views (not a pool), allows YouTube, pays at least
the CPM floor ($1.00 per 1,000 views) and has at least $250 of budget left; every other campaign is listed as skipped with the reason. Joining and handing in
each clip's link stay manual: press "I've joined" on the campaign's page, then enter its source videos, required caption tags and rules (Content Rewards shows
those only to members). Solved is seeded as already joined with its rules and tags. The system clips one video at a time, five ahead, uses the campaign's pay as
the clip's CPM, and skips the Start step. Finished campaign clips carry the campaign's tags instead of the channel's, and the Campaigns page lists the links
to hand in. Tested against real pages parsed offline and stand-in sites; no full live run yet.

### Phase 8: Research (large)
**In plain words:** The "Run Research" switch. It looks for videos with the best expected CPM on YouTube (Creative Commons
only, with credit given), Twitch and Kick, and runs any that clear the rising bar. YouTube limits how often it can be searched,
so it uses cheaper ways of finding videos first. The old research code is replaced here.
**After this:** it finds, clips and posts videos by itself, and the Research page shows what it looked at and why it said yes or no.
**Done when:** it finds and posts a video with nobody touching anything.
**Status: built and tested with stand-ins for YouTube, the channel feeds, the engine and Jev; not yet run live.** The Research page has the "Run Research" switch
(off until you turn it on), a daily limit (6 videos in 24 hours), the highest the bar may rise to ($3.00), and the topics searched. Every 30 minutes the worker reads the
newest videos of the channels it watches (yours, plus YouTube channels it learned from a Creative Commons hit; this uses public feeds and a cheap licence lookup) and then
spends up to 2 rationed YouTube searches for Creative Commons videos, the topic searched longest ago first. A YouTube video must be Creative Commons, not live, 10 minutes
to 4 hours long, under 14 days old, and (when searched) have 1,000 views. Twitch and Kick videos come only from channels you add; they are checked for being finished
before the engine downloads anything. The best waiting video starts when nothing else is being clipped (campaigns go first) and becomes a run like a pasted link with no Start
press: it is made only if a clip clears the viral floor (75) and the rising CPM bar (it starts at $1.00 and never leaves $1.00 to your ceiling; Phase 9 moves it) and the
video's kind has a channel that fits it 4 of 5. A video with no channel for its kind is not made, and Home says "Create a channel for ...". Every clip's caption credits the
creator and, for YouTube, the Creative Commons licence. The page lists what it looked at and why it said yes or no. **Twitch and Kick channels are now found as well as listed:** each pass looks up one topic on Twitch (the most watched live channels in its category, with your free Twitch Client ID and Secret: `python -m villomillo key twitch`) and on Kick (its website search, which is unofficial and can stop working) and adds what it finds to the watch list marked "added by Research" (up to 30; remove any). Try it: `python -m villomillo research on`, then `python -m villomillo research`.

### Phase 9: Learning from real results (medium)
**In plain words:** Real views and campaign payouts come back and are compared with the predictions. The estimates for each
category are corrected, and the Research bar is raised when results justify it.
**After this:** an Insights page shows predicted versus real views and how the bar has moved.
**Done when:** the numbers shown there are real, and estimates visibly improve.

### Phase 10: Always on (small to medium)
**In plain words:** Make it survive real life: start when you log in, restart the engine if it stops, and show a small health line
on Home (engine, worker, funds, last post).
**After this:** you can leave it running for days.
**Done when:** it keeps working after a reboot and after the engine is killed on purpose.

### Added after Phase 8 (your requests)
- **Engine:** `villomillo run` (what `run-app.bat` launches) starts the clip engine if nothing answers, saves its output to `data/villomillo/engine.log`, starts it again if it exits and stops retrying after 3 exits in 10 minutes. The sidebar light shows On, Starting, Not responding, Stopped or Off and needs two failed 3-second checks to change. Settings has a Clip engine panel (Start, Restart, Open log), a Health panel, and `python -m villomillo doctor` runs the same checks.
- **Remove clips:** a Remove button on each clip, a Select mode on the grid, "Remove all clips" on a video, and a Removed tab with Restore (files are deleted after 30 days). Posts that have not gone out are cancelled; posts already on YouTube are left alone, and the confirm step says so.
- **Seeing what it does:** every step writes to an activity log. A video's page shows its road (look, score, plan, make, check, post) with times and a Now line, and warns when there is no news or the engine is down. Research and Campaigns have a Latest activity feed and a next-pass time; Home has a Now panel and a worker heartbeat. A video now waits for an engine that is not answering instead of failing at once (it fails only after 30 minutes with no progress).
- **Claude finds the moments:** the clip engine used to read every video with a local model (`gemma3:12b`, minutes per 5-minute piece, so hours for a long video). Now, when a Claude key is stored, `villomillo run` starts the engine with Claude Haiku 5.5 reading the video in 20-minute pieces, several at once, and Claude Sonnet 5.5 judging the finalists (both at low or medium thinking effort). Switch it in Settings, Finding moments (it restarts the engine). Your Claude key is copied into the engine's own key store, and the engine records each answer's tokens so the app counts them toward the Claude spending limit at Anthropic's list prices. Estimated about 2 cents of Haiku plus a few cents of Sonnet for a 4-hour video (not measured). Not yet measured: how its picks compare with the local model's.
- **One video, one owner:** `villomillo/claims.py` and `runs.create` make sure no way in takes a video another has. A video is owned while a run is working on it and after it was clipped (the automatic ways also leave a video that was looked at and found worth nothing). Research and Campaigns skip an owned video with the reason shown (a campaign's queue marks it Skipped and goes on to its next). Pasting a video that is being worked on opens that run; pasting one already clipped asks "Clip it again?". A failed or removed run frees the video. The check and the claim are one database step, so two ways in at the same moment cannot both win.
- **Screens matched to the wireframes:** Home's three ways-in cards, source labels and live lines on videos; the plan header (how it arrived, how long it took, the look); Campaigns (Connection card, Goes to channel, plain status, How it will clip); Research (bar card, Today, search allowance, Where it looks, Est. CPM and Result columns); Channels (posted today, look, tags, Pause; the 3-step stepper); a Results page (under Learn) and a Rules page (linked from Settings). The Campaigns and Research boards were updated where we chose a different behaviour (join by hand; Twitch and Kick found and listed).

---

## 11. Things you should know

- **No approval step means mistakes can post publicly.** The protection is the scores, the checks, the spending guard, the
  emergency stop, and the ability to delete a post. Early on the CPM numbers for Paste and Research are guesses.
- **Platform approvals take time and are outside our control.** Until YouTube approves automatic posting,
  those posts are private or wait for you.
- **Campaign automation depends on something that may not exist.** Whop's docs mention API keys and a bounty-submissions API, but
  I found nothing confirming that Content Rewards campaigns can be joined or submitted to automatically. The fallback is manual.
- **Looking at a video costs time and money** (download, read, score) even if the answer is "don't bother".
- **Using any public streamer's content with rights assumed can lead to takedowns or strikes on your accounts.** That is your
  decision; it is noted here so it is not a surprise.
- **Campaign payouts** have limits per clip, a review delay (third-party guides say about 72 hours), and budgets that run out. The system checks the budget before each clip.
- **YouTube search is rationed** (reported at about 100 a day), which slows Research until a larger allowance is requested.

## 12. How to run the Whop test script

This asks Whop what your API key is allowed to see. It only reads, never changes anything, never saves your key and never prints it.

1. **Get the script.** Double-click `get-from-github.bat`.
2. **Make the one key** with the permissions listed under "Which permissions the key needs" below (everything the app will ever
   need, so it is made once). In Whop, open the Developer area, then API keys, then Create. The test script itself only reads,
   whatever the key is allowed to do.
3. **Store the key safely** (do this once; the app reads it from there later). Open PowerShell in the VilloMillo folder, run
   `.\.venv\Scripts\python.exe -m villomillo key whop`, paste the key when asked (nothing shows as you type) and press Enter. It goes
   into Windows Credential Manager, **not into any file in the project**, and is never printed. `...\python.exe -m villomillo key` on its own
   shows which keys are stored. To replace the key, run the same command again.
4. **Open PowerShell in the VilloMillo folder** (the same place you ran `.\.venv\Scripts\python.exe -m villomillo serve`).
5. **Run:** `.\.venv\Scripts\python.exe scripts\whop_probe.py` (it uses the stored key, so it does not ask).
6. **Copy what it prints** and paste it to me. It is also saved in `data\villomillo\whop-probe.json`. Emails, tokens and addresses are
   already replaced with "[hidden]", but skim it before sending.
7. **Keep the key.** It stays in Credential Manager for the app. If you ever need to remove it, delete the "villomillo" entry named `whop_api_key` there, and revoke the key in Whop.

A line saying `"status": 200` means that part works. `403` means the key lacks permission. `404` means that address does not exist
(expected for several, because the script tries a few likely names). What it finds decides how much of Campaigns can be automatic.

### Which permissions the key needs (checked against Whop's real list)

Whop's permission list was pasted in, so these are the exact boxes. Whop also lets you edit a key's permissions later ("Edit permissions"
on the key), so nothing here is permanent.

**Tick these 6:**
| Group | Box | Why |
|---|---|---|
| Content rewards | **Read content rewards** | See campaigns: pay rate, budget left, rules, source |
| Bounty | **bounty:basic:read** | Read submissions (this is the one Whop's submissions list asks for) |
| Company (the group with plain labels, the second "Company") | **Read business information** | Know which account the key belongs to |
| Company (same group) | **Read company balance** | See earnings and balance for the Learn step |
| Payouts | **Read transfers** | See money that arrived |
| Payouts | **Read withdrawals** | See money that went out |

**Optional:** Payments, **Read payments**, if the transfers show nothing useful. It is read only.

**Leave every other box off.** In particular:
- **Content rewards: Create, Update, Delete, Export, and "Moderate content reward submissions".** These are for the company that *runs* a
  campaign. Moderating means approving or rejecting other people's submissions, which can release money. Clippers never need it.
- **Bounty: bounty:create and bounty:update** (they post bounties that spend money).
- **Payouts: Transfer funds, Withdraw funds, and every create, update or delete payout destination.** You withdraw by hand.
- **Payments: Charge payments and Manage payments**, **Members**, **Membership**, **Developer: Manage webhooks**, **Webhook Receive**,
  **Company: transfer ownership, suspend, delete**, and everything about ads, chats, courses, products, plans, forums, livestreams,
  email, domains and AI.

**What this list tells us about automation:** every Content rewards and Bounty box is for the side that *creates and runs* campaigns.
**Nothing in the list lets a key join a campaign or submit a clip's link as a clipper.** So expect Campaigns to work like this:
- Reading campaigns, rates, budgets and results: possible with the key above (the test confirms it).
- Joining a campaign and submitting a link: **a quick tap by you**, with everything prepared. This is the fallback you already chose.
  If the test script shows otherwise, we change this.

Another catch: an account key may only see campaigns that account *owns*, not ones you joined as a clipper. If the test shows empty
lists or `403`, the next step is a different kind of sign-in (a user token), not more boxes.

Keep the key only in the app's keychain. Never paste it into chat, a file or the repository.

## 13. Settled and still open

**Settled by the Whop test (run on your computer):**
- Your key is valid and works. It can list apps; `/bounties` and `/bounty_submissions` answer but are empty for your account.
- Whop's documented API has **no Content Rewards campaign endpoints** that a clipper can use. Every address we tried (`/content_rewards`, `/campaigns` and similar) answered "not found". Whop's own documentation for Content Rewards only describes the brand side (creating campaigns and approving submissions); joining and submitting as a clipper is done on the website.
- So in Phase 7 campaigns are not read through the Whop API. **Decided:** the system reads the public campaign list on contentrewards.com first, and you can paste a campaign link as a fallback (the system then reads that page). Joining and submitting links stay manual either way.

**Settled by you:**
- The Solved campaign allows **YouTube only**. Every campaign needs its own "allowed platforms" setting, checked before a clip is posted.

**Still open:**

