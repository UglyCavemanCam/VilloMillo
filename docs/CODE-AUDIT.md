# Code audit (Phase 1)

What each part of `villomillo/` is for in the new plan (`docs/AUTOPILOT-PLAN.md`), and what happens to it.
**Keep** = stays as is. **Rewrite** = stays for now, replaced in the phase named. **Delete** = gone.

## Done in Phase 1

| Part | What happened |
|---|---|
| `rights.py`, `channel_rights`, `video_rights`, `campaign_videos` tables, `villomillo rights`, `villomillo campaign`, `rights_gate` setting | **Deleted.** Nothing checks whether you have permission to clip a link. Old databases keep their unused tables; they are harmless. |
| Monthly and daily dollar caps (`claude_usd_per_month`, `jev_usd_per_day`) | **Deleted.** Replaced by the spend breaker. |
| `spend.py` (new), `spend_log` table, `spend_breaker` setting | **New.** Counts Claude and Jev spend since your last approval. At $25 Claude or $5 Jev, or when a provider says the account has no balance, the worker stops taking jobs, AI calls are refused, and every page shows a banner with "Approve and continue". `villomillo approve-spend` does the same from the terminal. |
| `routing.candidates` | No longer asks whether a channel has permission to use a video. |
| `clipper.submit` | No longer looks up the YouTube licence or holds videos for rights. Takes no YouTube client. |

## Module by module

| Module | Purpose | Decision |
|---|---|---|
| `config.py`, `db.py`, `store.py`, `tasks.py`, `runner.py`, `services.py`, `timefmt.py`, `secrets.py` | Settings, database, job queue, worker, keys | **Keep** |
| `spend.py` | Spend breaker | **Keep** (new) |
| `claude.py`, `jev.py` | Claude and Jev clients | **Keep** |
| `clipper.py` | Talks to the clipping engine, checks finished clips | **Keep.** Phase 3 adds "analyse only" and "render chosen clips" |
| `validate.py`, `campaign.py` (per-campaign clip rules) | Technical checks and campaign rule checks on a finished clip | **Keep.** Phase 7 feeds `campaign.py` from the real campaign rules |
| `formats/` | The on-screen look recipes (blueprints, text, render) | **Keep** |
| `copywriter.py` | Titles and captions per platform (YouTube now, TikTok in Phase 6) | **Keep** |
| `posting.py`, `accounts.py`, `routing.py` | Posting, per-account pause and audit state, choosing a channel | **Keep.** Phase 2 turns "profiles" into category channels; Phase 6 adds TikTok |
| `youtube.py`, `quota.py` | YouTube Data API reads and their daily allowance | **Keep** (used by Research and the Paste intake) |
| `discovery.py`, `filters.py`, `grading.py`, `scoring.py`, `engine.py`, `transcripts.py`, `learning.py`, `selfcheck.py` | The old research pipeline: watch channels, search topics, grade by title and transcript, shortlist | **Rewrite in Phase 4 (scoring brain) and Phase 8 (Research).** Kept running until then, because deleting them now would switch Research off with nothing to replace it. None is dead code (checked with vulture). |
| `studio.py` | Dashboard helper for the live studio view | **Rewrite in Phase 5** (run page) |
| `cli.py`, `web/` | Command line and dashboard | **Keep.** Pages change in Phases 2, 5, 7, 8 |

## Not yet decided

- The Solved channel profile (`config/villomillo/channels/solved.yaml`) becomes the first Podcasts channel in Phase 2.
- Twelve tests fail on a clean checkout for reasons unrelated to this work: eleven outro-font tests need Windows fonts, and one engine signal-peak test (`test_clip_intent`).
