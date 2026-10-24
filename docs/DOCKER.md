# Docker (for contributors)

You need [Docker Desktop](https://docs.docker.com/get-started/get-docker/), and
that is the whole list. Compose comes with it: `docker compose` (a space, not
a hyphen) is part of Docker, not a separate program to install.

```
docker compose pull      # prebuilt engine image, ~2 min
docker compose up
```

`pull` is the easy route and the one to take. Skip it and `up` builds the
engine from the Dockerfile instead, which takes around ten minutes the first
time.

**On an Apple Silicon Mac, pull rather than build.** The published image is
amd64 and runs under emulation, which is fine for editing code and running
tests. A local build produces an arm64 image, and one dependency has no arm64
wheel and has to compile, which fails because the base image ships no C
compiler. [#62](https://github.com/UglyCavemanCam/VilloMillo/issues/62) tracks
that. Do not reach for `--platform=linux/amd64` on a local build either. That
emulates the whole ten-minute build, not just the run.

**Give the Docker Desktop VM 12 GB of memory, and 4 CPUs.** Docker Desktop,
Settings, Resources. This is the setting that decides whether renders work at
all, and the default is not enough: 8 GB analyses a video and then produces
zero clips, and the first contributor to get a full run through had to raise it
to 12. The app is developed against a 16 GB machine. Memory is also what caps
video length. An hour of source is around the limit at 12 GB.

Two services, and you need nothing installed but Docker:

| | |
|---|---|
| **engine** | <http://localhost:8765>. The Python pipeline and its API |
| **ollama** | the model the scoring runs against |

Run the checks without installing Python:

```
docker compose run --rm engine pytest
docker compose run --rm engine ruff check .
```

Pull a model the first time:

```
docker compose exec ollama ollama pull gemma3:4b
```

Generate a test video to actually run something against. See
[`../tests/assets/`](../tests/assets/):

```
docker compose run --rm engine python tests/assets/make_sample_video.py
```

---

## What this is for

Working on VilloMillo without installing Python, FFmpeg, PyTorch, OpenCV,
Node and the rest on your own machine, and letting someone on Linux or
macOS contribute to a project whose app only ships for Windows.

**It is for working on the engine.** VilloMillo's dashboard (`python -m villomillo serve`) runs on your own
computer, not in this container.

## How it is wired

| Piece | Why |
|---|---|
| Source bind-mounted to `/app` | Edits are live; no rebuild to run a test |
| `clips-data` volume at `/data` | Downloads and clips survive a rebuild, and never bloat the image |
| `ollama-models` volume | Models are gigabytes; losing them on every rebuild would be miserable |
| `CLIPS_STUDIO_OLLAMA_HOST` | Inside compose, Ollama is a service name, not localhost |
| Ports bound to `127.0.0.1` | The API has no authentication and can read and write video files. It must not be reachable from the network. |

The engine binds `0.0.0.0` **inside** the container, because there
"localhost" means the container itself and nothing on the host could reach
it. The published port is still host-only, and `main.py serve` warns when
asked to bind anything other than `127.0.0.1`.

## First build is slow

Ten minutes or so, mostly PyTorch and OpenCV. Afterwards Docker caches the
dependency layer, and only a change to `requirements.txt` re-triggers it.

`.dockerignore` keeps `data/` out of the build context, without it Docker
would upload every downloaded video and rendered clip before running a single
step.
