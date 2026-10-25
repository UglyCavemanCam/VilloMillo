# Code audit (Phase 1)

What each part of `villomillo/` is for in the new plan (`docs/AUTOPILOT-PLAN.md`), and what happens to it.
**Keep** = stays as is. **Rewrite** = stays for now, replaced in the phase named. **Delete** = gone.

## Done in Phase 1

| Part | What happened |
|---|---|
| Old research pipeline: `discovery.py`, `filters.py`, `grading.py`, `scoring.py`, `engine.py`, `transcripts.py`, `learning.py`, `selfcheck.py`, the Research page, weekly review, `once` and `check` commands, the `research:` and `grading:` channel settings, and their database tables | **Deleted.** None of it serves the new plan (it graded whole videos by title and view counts; the new plan scores individual clips). Research returns in Phase 8 (`research.py`) and the scoring brain in Phase 4, both built fresh. |
| `rights.py`, `channel_rights`, `video_rights`, `campaign_videos` tables, `villomillo rights`, `villomillo campaign`, `rights_gate` setting | **Deleted.** Nothing checks whether you have permission to clip a link. Old databases keep their unused tables; they are harmless. |
| Monthly and daily dollar caps (`claude_usd_per_month`, `jev_usd_per_day`) | **Deleted.** Replaced by the spend breaker. |
| `spend.py` (new), `spend_log` table, `spend_breaker` setting | **New.** Counts Claude and Jev spend since your last approval. At $25 Claude or $5 Jev, or when a provider says the account has no balance, the worker stops taking jobs, AI calls are refused, and every page shows a banner with "Approve and continue". `villomillo approve-spend` does the same from the terminal. |
| `routing.candidates` | No longer asks whether a channel has permission to use a video. |
| `clipper.submit` | No longer looks up the YouTube licence or holds videos for rights. Takes no YouTube client. |

## Removed later

| Part | What happened |
|---|---|
| TikTok (`tiktok.py`, its channel settings, the Connect TikTok step, its keys and tests) | **Deleted.** Automatic TikTok posting needs a developer approval that is not available. |

## Module by module

| Module | Purpose | Decision |
|---|---|---|
| `config.py`, `db.py`, `store.py`, `tasks.py`, `runner.py`, `services.py`, `timefmt.py`, `secrets.py` | Settings, database, job queue, worker, keys | **Keep** |
| `spend.py` | Spend breaker | **Keep** (new) |
| `claude.py`, `jev.py` | Claude and Jev clients | **Keep** |
| `scoring.py` | Viral score, estimated CPM, expected revenue, and the video's kind | **Keep** (new, Phase 4) |
| `clipper.py`, `links.py` | Talks to the clipping engine (preview, make chosen moments), checks finished clips; recognises YouTube, Twitch and Kick links | **Keep.** Phase 3 added "preview" and "make chosen moments" |
| `validate.py`, `campaign.py` (per-campaign clip rules) | Technical checks and campaign rule checks on a finished clip | **Keep.** Phase 7 feeds `campaign.py` from the rules saved on each campaign |
| `research.py` | The Run Research switch: watches channels, searches Creative Commons YouTube, queues and starts videos, gates them, credits the creator | **Keep** (new, Phase 8) |
| `contentrewards.py`, `campaigns.py` | Reads the public Content Rewards list and campaign pages; vets, queues and tracks campaigns and the links to hand in | **Keep** (new, Phase 7) |
| `formats/` | The on-screen look recipes (blueprints, text, render) | **Keep** |
| `copywriter.py` | Titles and captions for YouTube | **Keep** |
| `channels.py` | Channels stored in the database: add, edit, delete, one per category | **Keep** (new, Phase 2) |
| `posting.py`, `accounts.py`, `routing.py` | Posting, per-account pause and audit state, choosing a channel (a clip goes to its category's channel, or is held) | **Keep** |
| `youtube.py`, `quota.py` | YouTube Data API reads and their daily allowance | **Keep** (used by the Paste intake and, later, Research) |
| `runs.py`, `safety.py` | A pasted link from analysis to posted clips; the emergency stop | **Keep** (new, Phase 5) |
| `studio.py` | Dashboard helper for the live clip-progress view on the Clips page | **Keep.** The run page covers a pasted link's progress |
| `cli.py`, `web/` | Command line and dashboard | **Keep.** The Research page is gone (returns in Phase 8); Home becomes the paste page in Phase 5 |

## Not yet decided

- Solved's rules moved to `campaigns.SOLVED_RULES` in Phase 7; `Profile.campaign` is gone.
- Twelve tests fail on a clean checkout for reasons unrelated to this work: eleven outro-font tests need Windows fonts, and one engine signal-peak test (`test_clip_intent`).
