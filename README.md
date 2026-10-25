# VilloMillo

A system that turns long videos into vertical clips, checks them, and plans the posts. It runs on your own PC, and a local dashboard is the only interface.

VilloMillo is built on a fork of the open-source Clips Kitty engine (AGPL-3.0-or-later, by ColinGPT9), which stays in this
repository as the clipping engine. VilloMillo's own code is in [`villomillo/`](villomillo/).

## What it does

1. **Clip.** Hands a video to the clipping engine, collects the clips it makes, and checks each one
   (length, picture, loudness, campaign rules). A spend breaker pauses everything when AI spending since your last approval
   reaches $25 (Claude) or $5 (Jev).
2. **Look.** Renders clips in a format recipe (framing, captions, hook title, progress bar) from
   [`config/villomillo/blueprints.yaml`](config/villomillo/blueprints.yaml). See [docs/FORMAT-BLUEPRINTS.md](docs/FORMAT-BLUEPRINTS.md).
3. **Review.** You approve or reject each clip in the dashboard. With a channel's default `autopilot: review`, nothing is posted without your approval.
4. **Post.** Plans each approved clip for the right channel and platform, writes title and caption options, and sends it
   through the platform's API or leaves it for you to post by hand. Platforms stay manual until their audit is recorded.

VilloMillo does not check rights: any link you paste is clipped. See [docs/AUTOPILOT-PLAN.md](docs/AUTOPILOT-PLAN.md) for where the
system is going and [docs/CODE-AUDIT.md](docs/CODE-AUDIT.md) for what each module is for.

## Run it

You need Python 3.11 or newer and FFmpeg. On Windows, double-click **`run-app.bat`**: it uses the project's `.venv` when there
is one, installs what is missing, starts the dashboard and opens it in your browser. By hand:

```
pip install -r requirements-villomillo.txt
python -m villomillo serve        # the dashboard only, at http://127.0.0.1:8780
python -m villomillo run          # the dashboard plus the background worker
```

The dashboard answers only requests addressed to this computer. Press `?` in it for keyboard shortcuts.

### From source

Run VilloMillo and its engine straight from a checkout, which is how a change that is not in a release yet gets tried:

```
git clone https://github.com/UglyCavemanCam/VilloMillo
cd VilloMillo
python -m venv .venv                      # keep the environment next to the checkout, not inside a plugin's folder
.venv\Scripts\activate                   # on macOS and Linux: source .venv/bin/activate
pip install -r requirements-villomillo.txt
python -m villomillo serve
```

The engine's own dependencies are in [`requirements.txt`](requirements.txt); start it with `python main.py serve`.

**Keys.** The YouTube, Jev and Claude keys are kept in your operating system's keychain, never in a file in this folder.
Settings in the dashboard shows which are stored, never their values; store one with `python -m villomillo key whop` (or `youtube`, `jev`, `claude`); it asks for the key with hidden typing and never prints it.

**The clipping engine** is a separate local program on `127.0.0.1:8765`. `run-app.bat` starts it for you (in a window titled "Clip engine"), together with the worker and the dashboard, and stops it when you close the app. To run it by hand: `python main.py serve` (its
dependencies are in [`requirements.txt`](requirements.txt)). Without it, review and posting still work, and clipping
says plainly that the engine is not running.

## Plugins

The engine runs plugins that find, understand, rate or edit the moments of a video, written with the MIT-licensed SDK in
[`sdk/python/`](sdk/python/); see [docs/developers/](docs/developers/README.md). Posting isn't a plugin step: VilloMillo posts clips itself, and through WoopSocial it can post to many sites at once on the creator's own account ([Publish to every platform at once](#publish-to-every-platform-at-once)). `kind: publisher` is refused: plugins don't post.

## Posting publicly

A channel posts to a platform through its API only after that platform's audit is recorded for the channel
(`python -m villomillo audit`). Until then its posts are planned for you to post by hand from the Calendar, with the title
and caption ready to copy. YouTube uploads need a one-time `python -m villomillo youtube-connect` and your own Google API key.
Any account can be paused from Accounts, and failures pause it by themselves.

## Publish to every platform at once

The engine can send a clip through [WoopSocial](https://woopsocial.com/) or [Upload-Post](https://www.upload-post.com/) to many platforms in one
upload (see [docs/UPLOAD-POST.md](docs/UPLOAD-POST.md)). This is **not local**: the clip and its details go to the publishing
service, which holds the connections to your social accounts, so VilloMillo never asks for a social password. You bring your
own key and your own allowance; VilloMillo charges nothing and holds no shared key.

<sub>Affiliate link - VilloMillo may earn a commission if you sign up through it, at no extra cost to you.</sub>

## Commands

`python -m villomillo doctor` checks the engine, worker, keys, each channel's YouTube sign-in and posting mode and says what to do about anything missing.

`python -m villomillo key twitch` stores your Twitch Client ID and Secret (from dev.twitch.tv, kept in the keychain) so Research can find Twitch channels.

`python -m villomillo research on` (or `off`) switches Research, and `python -m villomillo research` runs one pass and lists what it looked at.

`python -m villomillo campaigns` reads the Content Rewards list and prints each verdict; `recheck --campaign ID` re-checks one campaign's clips.

`python -m villomillo --help` lists them all. The common ones:

| Command | What it does |
|---|---|
| `status` | counts by stage, today's YouTube quota and AI spend since your last approval |
| `approve-spend` | approve more spending after the breaker paused work, or after topping up an empty account |
| `preview` / `make` | find and score the moments of a YouTube, Twitch or Kick video without making clips; then make only the ones you pick |
| `score` | score a video's moments (viral score, estimated CPM, expected revenue) with an explanation of each number |
| `clip` | clip one video now and wait for the engine |
| `copy` | write title and caption options for planned posts |
| `accounts` / `account` / `audit` | channel routes, audits, pause and resume |
| `youtube-connect` / `post-now` | sign in to YouTube once; send one planned post now |

## Paste a link

On Home, paste a YouTube, Twitch or Kick link. VilloMillo looks at the video, scores its clips, and shows a plan: press **Start** and
only the clips that clear the floors are made, checked and planned for posting on their category's channel. Needs the clipping engine
and `run-app.bat` (which starts the worker). The **Emergency stop** button pauses everything.

## Settings

- [`config/villomillo/app.yaml`](config/villomillo/app.yaml): YouTube allowances, spend breaker, schedule, dashboard port, database path.
- Clips made with a look are exported at high detail (CRF 14, tagged BT.709) because YouTube compresses every upload again. A clip's side panel shows its **Picture detail** in Mbps and flags anything under 6. Clips made before this change keep their old detail and need to be made again.
- **Time between posts** is a random wait between a shortest and a longest number of minutes (10 and 20 to start), set on the channel's page. Each clip gets its own random wait after the last post, and posts already waiting are re-spaced when the numbers change. **Score a clip needs to post by itself** (70 to start) is on the same page: a clip scoring under it waits in "Waiting for you" instead of posting. When a campaign video ends, the next queued one starts at once instead of waiting for the hourly pass.
- A campaign's **Source videos** box takes YouTube channels (the newest 150 uploads are searched for full episodes), playlist or show links (read in the playlist's own order, up to 200) and single video links. Saving the box looks for videos at once, and a source that gives nothing says why in the Campaigns activity.
- **Post again** (a clip's side panel, for a clip whose post already went out) puts the same post back in the queue and sends it once more, for when its video was deleted from YouTube. It keeps the title and caption, clears the old link and numbers, and refuses with a reason if the channel cannot send for real. If the old video is still on YouTube the new one is a second copy.
- **Upload size** (channel page, Sending posts): Normal sends the clip as made (1080x1920). Larger enlarges a copy to 1440x2560 for the upload only, then deletes the copy, because YouTube may compress bigger uploads more gently. The clip in the dashboard is never changed. It is an experiment: compare one clip on each setting.
- **Who can watch the videos** (public, unlisted or private) is set on each channel's page, under Sending posts. Channels start public, and channels saved before this setting existed were switched from private to public once. YouTube still forces every upload to private while the app's API project has not passed its audit, whatever this says.
- The **Clips** page lists the highest-scoring clip first and can be sorted (score, newest, oldest, longest, shortest) and filtered (minimum score, video, post planned or posted). A clip's side panel has **Post clip**, which writes its titles if they are missing and sends it to YouTube straight away once the channel can post for real.
- A campaign's face rule (the host on screen in the first N seconds) is a tick box on the campaign's page and is checked only when ticked, because only some campaigns require it. Campaign rules saved before the box existed keep the face check. Every clip, from any way in, still gets the file checks, the stands-alone check and the ad-read check where a campaign asks for it.
- Posts planned before a channel could post for real (set to by hand, or dry run) are not sent by themselves. Once the channel is set to Automatically, dry run is off and the audit is recorded as passed, **Send N waiting clips** on the channel's page queues them (best clip first, spaced by the gap and daily limit), and **Send now** in a post's panel sends that one straight away.
- The switch at the top of **Channels** (on by default) sends every clip to one chosen channel, whatever its category, for the time before each category has its own YouTube channel. Turn it off and each clip goes to its category's channel; clips for a category with no channel are held. Turning it on again plans any held clips on the chosen channel.
- Channels are added and edited on the dashboard's **Channels** page and stored in the database. [`config/villomillo/channels/`](config/villomillo/channels/) only holds the starting channel (Podcasts), read once into an empty database.
- [`config/villomillo/blueprints.yaml`](config/villomillo/blueprints.yaml): the format recipes.

## Tests

```
pip install pytest pytest-timeout ruff
python -m ruff check .
python -m pytest tests -q
```

The suite needs no GPU, no network and no real footage. Some tests skip when optional tools are missing.

## Where things are

| Path | What is there |
|---|---|
| `villomillo/` | VilloMillo: clipper client, formats, posting, routing, dashboard (`web/`) |
| `config/villomillo/` | VilloMillo's settings, channels and format recipes |
| `main.py`, `server/`, `core/`, `analysis/`, `llm/`, `video/`, `video_editor/`, `transcription/`, `sources/`, `publish/`, `creator/` | the clipping engine (Clips Kitty fork) |
| `plugins/`, `sdk/`, `examples/` | the engine's plugin platform, its SDK (MIT) and example plugins |
| `gaming/`, `sports/`, `longform/`, `multilingual/`, `remote_render/` | engine modules for other kinds of video; the engine imports them |
| `docs/` | guides: the dashboard design, format blueprints, the engine's API, AI backends, Docker, plugins |
| `tests/` | the test suite |

## Documents inherited from the engine

[ARCHITECTURE.md](ARCHITECTURE.md), [KNOWN-ISSUES.md](KNOWN-ISSUES.md), [CHANGELOG.md](CHANGELOG.md) and
[ROADMAP.md](ROADMAP.md) describe the Clips Kitty engine before it was forked. Parts about its desktop app, installer,
Microsoft Store listing and website no longer apply: those parts were removed from this repository (they are in git history).

## License

**GNU AGPL-3.0-or-later.** See [LICENSE](LICENSE) and [NOTICE](NOTICE) for what that means in practice and for third-party
components. The plugin SDK in [`sdk/python/`](sdk/python/) and the example plugins are MIT.
