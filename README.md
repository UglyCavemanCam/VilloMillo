# VilloMillo

A research-first system that finds videos worth clipping, makes vertical clips from the ones worth clipping,
checks and grades them, and plans the posts. It runs on your own PC, and a local dashboard is the only interface.

VilloMillo is built on a fork of the open-source Clips Kitty engine (AGPL-3.0-or-later, by ColinGPT9), which stays in this
repository as the clipping engine. VilloMillo's own code is in [`villomillo/`](villomillo/).

## What it does

1. **Research.** Finds videos on watched channels and topics through the YouTube API, grades each one with AI
   (Jev first, Claude for second opinions on borderline ones), and tracks growth. YouTube's daily allowance is tracked, and a
   spend breaker pauses everything when AI spending since your last approval reaches $25 (Claude) or $5 (Jev).
2. **Clip.** Hands a video to the clipping engine, collects the clips it makes, and checks each one
   (length, picture, loudness, campaign rules).
3. **Look.** Renders clips in a format recipe (framing, captions, hook title, progress bar) from
   [`config/villomillo/blueprints.yaml`](config/villomillo/blueprints.yaml). See [docs/FORMAT-BLUEPRINTS.md](docs/FORMAT-BLUEPRINTS.md).
4. **Review.** You approve or reject each clip in the dashboard. With a channel's default `autopilot: review`, nothing is posted without your approval.
5. **Post.** Plans each approved clip for the right channel and platform, writes title and caption options, and sends it
   through the platform's API or leaves it for you to post by hand. Platforms stay manual until their audit is recorded.
6. **Learn.** Self-checks whether the grades point the right way on past uploads and writes a weekly review.

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

**The clipping engine** is a separate local program on `127.0.0.1:8765`. Start it with `python main.py serve` (its
dependencies are in [`requirements.txt`](requirements.txt)). Without it, research and review still work, and clipping
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

`python -m villomillo --help` lists them all. The common ones:

| Command | What it does |
|---|---|
| `status` | counts by stage, today's YouTube quota and AI spend since your last approval |
| `once` | run one research cycle now |
| `approve-spend` | approve more spending after the breaker paused work, or after topping up an empty account |
| `clip` | clip one video now and wait for the engine |
| `copy` | write title and caption options for planned posts |
| `accounts` / `account` / `audit` | channel routes, audits, pause and resume |
| `youtube-connect` / `post-now` | sign in to YouTube once; send one planned post now |
| `check` | test whether the grades point the right way on past uploads |

## Settings

- [`config/villomillo/app.yaml`](config/villomillo/app.yaml): YouTube allowances, spend breaker, schedule, dashboard port, database path.
- [`config/villomillo/channels/`](config/villomillo/channels/): one file per channel (niche, rules, format, platforms).
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
| `villomillo/` | VilloMillo: research, grading, clipper client, formats, posting, routing, dashboard (`web/`) |
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
