# VilloMillo

The app around the clipping engine: it hands videos to the engine, checks the clips, gives them a look, plans and sends the posts, and guards spending.
See `docs/AUTOPILOT-PLAN.md` for where it is going and `docs/CODE-AUDIT.md` for what each module is for.

Run these from the repository root with the project's virtual environment:

| Command | What it does |
|---|---|
| `.venv\Scripts\python.exe -m villomillo run` | Work in the background on a schedule and serve the dashboard |
| `.venv\Scripts\python.exe -m villomillo serve` | Dashboard only: http://127.0.0.1:8780 |
| `.venv\Scripts\python.exe -m villomillo status` | Counts, today's YouTube usage and AI spend since your last approval |

Settings live in `config/villomillo/` (`app.yaml`, and one file per channel in `channels/`).
API keys are kept in Windows Credential Manager under the name `villomillo` (store them with `python -m villomillo key <name>`). They are never written to a file.
