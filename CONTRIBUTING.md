# Contributing to VilloMillo

VilloMillo is a local-first system that researches, clips and plans posts for videos. It is Python: the VilloMillo code
in [`villomillo/`](villomillo/) (with a FastAPI dashboard) on top of a clipping engine forked from Clips Kitty. See the
[README](README.md) for what is where.

## Dev setup

```
pip install -r requirements-villomillo.txt     # the research engine, dashboard and renderer
pip install -r requirements.txt                # the clipping engine (large: PyTorch, OpenCV)
pip install pytest pytest-timeout ruff         # the checks CI runs
python -m ruff check .
python -m pytest tests -q
```

The suite needs no GPU, no network and no real footage. The engine's dependencies are only needed to run the engine
itself, not to work on the dashboard or the research code.

To see the dashboard: `python -m villomillo serve`, then open http://127.0.0.1:8780 (on Windows, `run-app.bat` does both).

## Something to run against

You do not need real footage to start. [tests/assets/](tests/assets/) has a written-out transcript and a generator for
synthetic footage:

```
python tests/assets/make_sample_video.py     # 2 min, 1080p, known audio peaks
```

[examples/](examples/) has small runnable programs for scoring a transcript and driving the engine's API.

## Building on it rather than in it

The engine is a local HTTP API on `127.0.0.1:8765`, and VilloMillo is one client of it. Anything else can be another.

- **[docs/API.md](docs/API.md)**: the endpoints meant to be built on, and the traps worth knowing first
- **[docs/EXTENDING.md](docs/EXTENDING.md)**: adding a language, a platform, an AI model or an export format
- **[docs/developers/](docs/developers/README.md)**: writing a plugin with the SDK in [`sdk/python/`](sdk/python/) (MIT)

### Write a plugin

A plugin finds, understands or rates the moments in a video, for one game, sport or kind of show, without changing
VilloMillo. Three commands start one and run it on a test video (in PowerShell, write `py -m` for `python -m`):

```bash
python -m pip install "clipskitty-sdk[yaml,test] @ git+https://github.com/UglyCavemanCam/VilloMillo#subdirectory=sdk/python"
python -m clipskitty_sdk new my-plugin --template game-events --publisher your-github-name
python -m clipskitty_sdk run my-plugin --sample
```

VilloMillo 2.1.0 is the first release that runs plugins, and it comes with SDK 1.3.0. The walk-through is
[docs/developers/first-game-pipeline.md](docs/developers/first-game-pipeline.md), and everything else for plugin
developers is in [docs/developers/](docs/developers/README.md). The SDK is MIT, so your plugin can use any licence you
choose.

## Rules that tests enforce

- **FFmpeg is never called by bare name.** Use `core.binaries.ffmpeg()` / `ffprobe()`, so a bundled copy is found.
- **OpenCV captures go through `video.capture.video_capture()`**, so the file handle is always released.
- **`third_party/` is vendored verbatim.** Do not edit it. Change the code that wraps it instead.
- **A new top-level package must be added to `ENGINE_PACKAGES`** in `_clipskitty_script_host.py` and in
  `sdk/python/clipskitty_sdk/lint.py`, or a plugin could import it.
- **Keys never go in a file in this folder.** They live in the OS keychain (`villomillo/secrets.py`).
- **The dashboard only answers this computer**, and every change refuses a request from another site.

## Pull requests

- Keep each one focused on one change.
- `python -m ruff check .` and `python -m pytest tests -q` pass, and you tried the affected page or command.
- Match the style around you: comments explain *why*, not *what*.

CI runs the same checks on every pull request. It is fast and narrow: a runner has no GPU, no Ollama and no footage, so
**a green tick does not mean clips still come out well.** A change to grading, framing, captions or rendering needs
trying on a real video, and the pull request should say which one.

## Licence

VilloMillo is **AGPL-3.0-or-later**, and contributions are accepted under the same terms: opening a pull request means you
are licensing your change that way. The plugin SDK and the example plugins are MIT.

Adding a dependency? Check its licence first: it has to be compatible with the AGPL, and a new one belongs in the
third-party list in [NOTICE](NOTICE).
