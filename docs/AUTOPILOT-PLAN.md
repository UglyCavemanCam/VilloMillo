# VilloMillo Autopilot: the plan, in plain words

Written 2026-10-24. Nothing here is built yet. Pictures of every screen (example data):
https://claude.ai/artifact/SjggvWZAR5gVKLuhLydb1J

This replaces all earlier autopilot plans. Two older documents stay because they describe things already built:
`docs/DASHBOARD-REDESIGN.md` (the dashboard) and `docs/FORMAT-BLUEPRINTS.md` (how clips are made to look).

---

## 1. The goal in one paragraph

VilloMillo should always be working. It gets videos, picks the moments most likely to earn money, turns them into clips,
and posts them to the right YouTube and TikTok accounts, with no one approving anything. **Everything is judged
by one thing: the best CPM** (what a video earns per 1,000 views). You step in only when something truly needs you.

## 2. Decided since the first version

- **Instagram is out of the plan and the system** (it allows only 3 reels a day, which does not meet the requirements). Posting is
  YouTube and TikTok only. (The engine's own code, inherited from Clips Kitty, still has Instagram parts; VilloMillo does not use them.)
- **First channel: Podcasts.** Its YouTube channel is the one already connected. Its TikTok account still has to be created.
- **The Solved campaign** is already joined by hand on Content Rewards. It becomes the first record on the Campaigns page.

## 3. Words used in this plan

| Word | What it means here |
|---|---|
| **CPM** | Dollars earned per 1,000 views. |
| **Viral score** | A number from 0 to 100: how likely a clip is to do well. Below 75, we don't make or post it. |
| **Category** | The kind of video: Podcast, Stream clips, Sports, and so on. |
| **Channel** | One category with its own YouTube channel and TikTok account. A clip posts to both. |
| **Held** | A good clip that has nowhere to go yet (no channel for its category). It waits; nothing is lost. |
| **Audit** | A platform's approval for posting automatically and publicly. Until it passes, posts stay private or wait for you. |

## 4. Three ways videos come in. One system does the rest.

| Way in | How it works | Where it lives |
|---|---|---|
| **Paste** | You paste a YouTube, Twitch or Kick link. You see the clips it would make and their scores, then press Start. | The link bar on Home |
| **Research** | You switch on "Run Research". It looks for videos with the best expected CPM and clips them on its own. | The Research page |
| **Campaigns** | It finds Content Rewards campaigns paying $1.00 or more per 1,000 views, joins them, and clips their videos. | The Campaigns page |

Only the way a video arrives is different. The scoring, clipping and posting are identical for all three.

## 5. What happens to every video

1. **Look at it.** The video is downloaded and read, and possible clips are found. Nothing is made yet.
2. **Score it.** Every possible clip gets a viral score, an estimated CPM and an expected revenue. The system also works out
   what kind of video it is (podcast, stream, and so on).
3. **Decide.** Paste: you see the scores and press Start. Research and Campaigns: it goes ahead on its own if the video is good enough.
4. **Make the clips.** Only the clips that scored well are made, in the right look for that kind of video.
5. **Check them.** Length, sound, picture, the campaign's rules, and the score again on the finished clip. A clip that falls
   short is not posted.
6. **Pick the channel.** The kind of video decides the category, and the category decides the channel. No channel for it? The clip is held.
7. **Post it.** A title and caption are written and the clip is posted to the channel's YouTube and TikTok, inside each
   platform's daily limit. A campaign clip's link is then submitted to the campaign.
8. **Learn.** Real views and payouts come back and make the next scores more accurate.

## 6. How it decides what is worth posting

- **Viral score** needs to be **75 or more**.
- **CPM** needs to clear a floor, and the floor depends on how the video arrived:
  - **Campaign:** the campaign pays at least **$1.00** per 1,000 views, and has money left.
  - **Paste:** estimated CPM at least **$1.00**.
  - **Research:** estimated CPM at least the **rising bar**. It starts at $1.00 and moves up in small steps, but only when real
    results beat what was predicted. It never drops below $1.00. You set the highest it can go.
- **Before real results exist,** CPM for Paste and Research is an estimate: `$1.00 × category factor × (viral score ÷ 75)`.
  It is always labelled "estimated" and is replaced by real data as TikTok and YouTube stats come in.
- **Expected revenue** = predicted views × CPM ÷ 1,000. When several clips are waiting, the highest expected revenue goes first.

Example: a clip with a viral score of 91 has an estimated CPM of $1.21. With a rough guess of 8,000 views, that is about
$9.68. A clip scoring 68 is estimated at $0.91 and is not made.

Two more checks: the **video type** must be at least 75% certain (otherwise a safe, plain look is used), and the **channel fit**
must be 4 out of 5 or better (otherwise the clip is held).

## 7. Channels

- A channel is a category plus its two accounts (YouTube and TikTok). All accounts are yours.
- A **Channels** page lists them. **Add channel** is a short step-by-step: name and category, connect YouTube, connect TikTok,
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
- **Platform limits:** YouTube 100 posts per channel per day (your number). TikTok is about 15 per account per day (from
  TikTok's own developer guidelines; it can vary). When an account reaches its limit, its clips wait for the next day.
- **Always on:** it runs on your computer. One start file launches the engine, the worker and the dashboard, starts them when you
  log in, and restarts the engine if it stops. Your computer must not go to sleep.

---

## 10. The phases, in simple terms

There are ten phases. You get something usable after Phase 5. Effort is my rough guess: small, medium or large.

### Phase 1: Clear the decks (small to medium)
**In plain words:** Before building, remove the parts of the old system that don't fit. That means the rule that refuses to
clip a video without proof of rights, the monthly spending caps, and the old research code that was written in a rush. Each file is
checked first and either kept, rewritten or deleted. The tests are run after every removal so nothing quietly breaks.
**After this:** nothing new to see, but pasting a link can no longer be blocked over rights, and the old Solved channel is moved
into the new channel system.
**Done when:** all tests pass and no part of the code refuses a pasted link.

### Phase 2: Channels (medium)
**In plain words:** Build the Channels page and the Add channel steps. Channels are stored in the dashboard instead of in files.
Add the "held, then create a channel" behaviour. YouTube connects first; TikTok plugs in later.
**After this:** you can add and edit channels and see which accounts are connected.
**Done when:** adding a channel releases clips that were held for it.

### Phase 3: Teach the engine to preview (large)
**In plain words:** Today the engine does everything in one go: download, read, find moments, then make the clips. Your plan
screen needs the scores before any clips are made, so the engine must be able to stop after finding the moments, show them,
and then make only the ones chosen. It must also accept Twitch and Kick links as well as YouTube. This is the biggest technical
change in the plan.
**After this:** you can ask the engine for scored moments without making any clips.
**Done when:** a video returns its scored moments, and a later request makes only the chosen ones.

### Phase 4: The scoring brain (medium to large)
**In plain words:** Build the part that gives every possible clip its viral score, estimated CPM and expected revenue, and that
works out what kind of video it is (so it knows the category and the look). This is the formula in section 5.
**After this:** any test video produces a clear, explainable list of scores.
**Done when:** the same video always gives the same scores, and each score can be explained in a sentence.

### Phase 5: Paste works from start to finish (large) — the first thing you can use
**In plain words:** Put it together for the Paste way in. The link bar on Home; a progress screen while it looks at the video; the
plan screen with the score of each clip and a Start button (or "Remove video" if none qualify); then the clips are made, checked
and posted automatically. This phase also adds the spending guard, the emergency stop and the Needs you list.
**After this:** you paste a link and clips appear on your YouTube channel with no further work.
**Done when:** a test link goes all the way to a posted clip, and the spending guard pauses at the right amounts.

### Phase 6: TikTok posting (medium)
**In plain words:** Add posting to TikTok, a daily limit counter for every account, and collection of real view counts for the
learning step. You will need TikTok's approval; until it passes, posts there are private or wait for you.
**After this:** one clip posts to a channel's YouTube and TikTok together.
**Done when:** a clip reaches both accounts and the daily limits hold.

### Phase 7: Campaigns (large)
**In plain words:** First, a small test script you run once on your computer, using your Whop key (steps in section 12), to see what Content Rewards
lets the system do. If it allows it, the system finds campaigns, checks the pay and the money left, joins the good ones, reads
the rules, finds their videos and clips them, then submits the links. If it does not allow it, the system does everything except the joining and
submitting, and shows you exactly what to do (a few taps).
**After this:** a Campaigns page listing every campaign it looked at, why it joined or skipped each one, and a queue of videos.
**Done when:** one real campaign is checked and queued, and any manual steps are clearly shown.

### Phase 8: Research (large)
**In plain words:** The "Run Research" switch. It looks for videos with the best expected CPM on YouTube (Creative Commons
only, with credit given), Twitch and Kick, and runs any that clear the rising bar. YouTube limits how often it can be searched,
so it uses cheaper ways of finding videos first. The old research code is replaced here.
**After this:** it finds, clips and posts videos by itself, and the Research page shows what it looked at and why it said yes or no.
**Done when:** it finds and posts a video with nobody touching anything.

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

---

## 11. Things you should know

- **No approval step means mistakes can post publicly.** The protection is the scores, the checks, the spending guard, the
  emergency stop, and the ability to delete a post. Early on the CPM numbers for Paste and Research are guesses.
- **Platform approvals take time and are outside our control.** Until YouTube and TikTok approve automatic posting,
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
2. **Make a read-only key** (what to tick is explained under "Which permissions" below). In Whop, open the Developer area, then
   API keys, then Create. Set an **expiry** (a week is enough) if the page offers one.
3. **Open PowerShell in the VilloMillo folder** (the same place you ran `.\.venv\Scripts\python.exe -m villomillo serve`).
4. **Run:** `.\.venv\Scripts\python.exe scripts\whop_probe.py`
5. **Paste the key** when it asks (nothing shows as you type) and press Enter.
6. **Copy what it prints** and paste it to me. It is also saved in `data\villomillo\whop-probe.json`. Emails, tokens and addresses are
   already replaced with "[hidden]", but skim it before sending.
7. **Afterwards** you can delete the key in Whop; the script did not keep it.

A line saying `"status": 200` means that part works. `403` means the key lacks permission. `404` means that address does not exist
(expected for several, because the script tries a few likely names). What it finds decides how much of Campaigns can be automatic.

### Which permissions the key needs

I could not read Whop's permission list from here, so these are the kinds to look for, not exact names. The test script prints Whop's
real list (the `permissions` part of its output) so we can be exact next time.

**For the test key, tick only reading:**
- Reading **campaigns / content rewards / bounties** and their **submissions** (one example I have seen named: `bounty:basic:read`).
- Reading basic **company or account** information, so we can see which account the key belongs to.

**Never tick** (for the test key or later): anything about **payouts, transfers, withdrawals, billing, payments, members or
refunds** (for example `payout:transfer_funds`), and do not use the **Admin** role, which allows everything.

**Later, only if the test shows it is possible:** if joining a campaign or submitting a link can be done through the API, the key
will also need the matching **create or edit** permission for submissions or joining, and nothing else. We decide that from the test
result, not before.

**A catch to know about:** Whop's docs say an account key lists the submissions on that account's *own* bounties. You are on the
clipper side (you joined someone else's campaign), so a key may not be able to see your campaigns at all. If the test shows empty
lists or `403`, the next step is a different kind of sign-in (a user token), not a bigger key.

## 13. Still to confirm

1. The result of the Whop test script.
2. The Solved campaign's allowed platforms. The old brief saved in `solved.yaml` did **not** allow TikTok. Does the live Content Rewards page say the same? If so, Solved clips go to YouTube only, and your TikTok is used for
   everything else.
3. The TikTok account's name and handle once it exists.
