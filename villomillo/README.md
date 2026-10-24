# VilloMillo research engine

Finds videos on YouTube, grades them with Jev, and ranks them by how well they fit a channel profile.
Nothing is downloaded, clipped or posted yet (that is Phases 3 and 4 of `docs/TECHNICAL-PLAN.md`).

Run these from the repository root with the project's virtual environment:

| Command | What it does |
|---|---|
| `.venv\Scripts\python.exe -m villomillo once` | One full research cycle now (add `--no-topics` to skip keyword searches) |
| `.venv\Scripts\python.exe -m villomillo run` | Work in the background on a schedule and serve the dashboard |
| `.venv\Scripts\python.exe -m villomillo serve` | Dashboard only: http://127.0.0.1:8780 |
| `.venv\Scripts\python.exe -m villomillo status` | Counts and today's YouTube and Jev usage |

Settings live in `config/villomillo/` (`app.yaml`, and one file per channel in `channels/`).
API keys are read from Windows Credential Manager under the name `villomillo`
(`youtube_api_key`, `typesafe_api_key`, `anthropic_api_key`). They are never written to a file.
