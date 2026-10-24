"""Soccer (football): the first sport. config/sports.yaml's `soccer` entry is
its data; this package holds the rules the data can't say, the scoreboard
reader and the framing that follows the ball (ball.py; not framing.py, which
would shadow the framing() hook below). See docs/SPORTS.md."""

from sports.soccer.profile import SoccerProfile


def profile(config: dict, option: dict, video=None) -> SoccerProfile:
    from sports.core.profile import weights_for

    return SoccerProfile(name="soccer", option=dict(option), weights=weights_for(config))


def framing(clip_path, config: dict) -> dict:
    """The ball and the play, for a 9:16 crop (sports/soccer/ball.py). The
    detector and its input size come from config/sports.yaml's `framing`."""
    import sports
    from sports.soccer import ball

    settings = sports.spec("soccer").get("framing") or {}
    tracking = ball.compute(clip_path, model_name=str(settings.get("model") or "yolov8n.pt"),
                            imgsz=int(settings.get("imgsz") or 1280))
    led = tracking.pop("led", {})
    print(f"      Soccer framing: ball {led.get('ball', 0)}, players {led.get('players', 0)}, "
          f"close-up {led.get('close-up', 0)} sample(s)")
    return tracking


def prepass(video_path, duration: float) -> dict:
    """The scoreboard, read while Whisper runs. {} when the OCR isn't installed."""
    from analysis import game_text

    if not game_text.available():
        print("      (scoreboard: the OCR isn't installed, scoring the match without it)")
        return {}
    import time

    from core import cancel
    from sports.soccer import scoreboard

    t0 = time.monotonic()
    board = scoreboard.read_video(video_path, duration, cancel=cancel.check_active)
    if board.box is None:
        print("      Scoreboard: none found on screen (sideline footage, or no score shown)")
    else:
        print(f"      Scoreboard: read {len(board.readings)} time(s) in {time.monotonic() - t0:.0f}s")
    return {"board": board}
