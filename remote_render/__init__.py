"""Remote rendering: a second PC renders clips for this one.

Optional and off by default (Settings → Advanced settings → Remote
rendering). Only the render step moves: download, transcription, AI scoring,
the queue, the library and publishing all stay on the computer you use.

    Main PC (engine)                                  Render PC
    pipeline -> queue.py (SQLite) <--HTTPS-- worker.py (pulls jobs)
                gateway.py :8766                -> one child process per job
                (worker-only routes)              -> core.pipeline._render_files

- protocol.py  what a render job is, what a worker can do, and matching
- settings.py  the user's choices (stored with the app's other secrets)
- tls.py       the gateway's self-signed certificate and its fingerprint
- queue.py     the main PC's authoritative job and worker state
- gateway.py   the worker-facing API, a separate server from the app's own
- piece.py     cutting one clip's stretch of the source, without re-encoding
- worker.py    the render PC's side: pairing, heartbeat, jobs
- dispatch.py  the pipeline's side: sending clips out, taking results in
- service.py   starting and stopping all of it inside the engine

docs/REMOTE-RENDERING.md explains it for users.
"""
