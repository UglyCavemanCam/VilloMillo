"""Framing a match for 9:16: follow the ball, not a face.

Face tracking frames whoever is biggest, which on a pitch is the nearest
player or the crowd, while the goal happens off the side of the crop. The
camera systems (Veo, Pixellot, Hudl, Trace) all do the same thing instead:
follow the ball, and when it's lost, where the players are. Here:

- the ball, from the detector the app already ships (models/yolov8n.pt, COCO
  "sports ball"), followed from sample to sample with jumps it can't make
  thrown out;
- when the ball is lost for more than BALL_MEMORY, the players: those near
  where the ball was last seen, else all of them;
- in a close-up (a player filling the frame: a celebration, a reaction), that
  player;
- moved by the shared HoldMove controller (video/framing.py), faster than for
  talk because play moves faster, and snapped at cuts, never panned across.

The result is the same crop path video/cropper.py renders for everything
else, so nothing downstream changes.
"""

import threading

BALL, PERSON = 32, 0
SAMPLE_FPS = 5.0
BALL_CONF = 0.15        # the ball is small and blurred: take weak detections, then vet them
BALL_NEW_CONF = 0.3     # ...but a ball not already being followed needs a surer one (a
                        # steward's vest or a boot scores 0.15-0.3 at 1280 px)
PERSON_CONF = 0.35
BALL_MEMORY = 1.0       # seconds a ball position stays usable after it's lost
MAX_JUMP = 0.3          # share of the frame width the ball can move between samples (not at a cut)
NEAR_BALL = 0.25        # players this close to the last ball are the play
CLOSE_UP = 0.45         # a person this tall (share of the frame height) makes a close-up

_models: dict = {}
_models_lock = threading.Lock()


def plan(samples: list[dict], crop_frac: float) -> tuple[list[tuple[float, float]], dict]:
    """The crop path from what each sample saw. Each sample:
    {"t", "cut", "balls": [(x, y, conf)], "people": [(x, y, w, h)]} in frame
    fractions. Returns ([(t, crop centre x)], how often each source led)."""
    from video.framing import HoldMove, stable_target

    lo, hi = crop_frac / 2, 1 - crop_frac / 2
    hold = HoldMove(move_trigger=0.05, settle=0.012, smoothing=0.35, max_pan_speed=0.8)
    path: list[tuple[float, float]] = []
    recent: list[float] = []
    ball: tuple[float, float] | None = None       # (x, when seen)
    led = {"ball": 0, "players": 0, "close-up": 0, "held": 0}
    prev_t = None
    for s in samples:
        t = float(s["t"])
        if s.get("cut"):
            ball = None
            recent.clear()
        # The ball: the detection nearest where it was, if it could have got there.
        found = None
        candidates = sorted(s.get("balls") or [], key=lambda b: -b[2])
        if ball is not None and t - ball[1] <= BALL_MEMORY:
            near = [b for b in candidates if abs(b[0] - ball[0]) <= MAX_JUMP * max(1.0, (t - ball[1]) * SAMPLE_FPS)]
            found = min(near, key=lambda b: abs(b[0] - ball[0])) if near else None
        elif candidates and candidates[0][2] >= BALL_NEW_CONF:
            found = candidates[0]
        if found is not None:
            ball = (found[0], t)
        people = s.get("people") or []
        close = [p for p in people if p[3] >= CLOSE_UP]
        if close:
            target, source = max(close, key=lambda p: p[2] * p[3])[0], "close-up"
        elif ball is not None and t - ball[1] <= BALL_MEMORY:
            target, source = ball[0], "ball"
        elif people:
            anchor = ball[0] if ball is not None else None
            near = [p[0] for p in people if anchor is not None and abs(p[0] - anchor) <= NEAR_BALL]
            xs = sorted(near or [p[0] for p in people])
            target, source = xs[len(xs) // 2], "players"
        elif recent:
            target, source = recent[-1], "held"
        else:
            target, source = 0.5, "held"
        led[source] += 1
        recent.append(min(max(target, lo), hi))
        steady = stable_target(recent, window=3)
        dt = (t - prev_t) if prev_t is not None else 1.0 / SAMPLE_FPS
        x = hold.snap(steady) if s.get("cut") or prev_t is None else hold.update(steady, dt)
        path.append((round(t, 3), round(min(max(x, lo), hi), 4)))
        prev_t = t
    return path, led


def _model(name: str):
    """The detector for the ball and the players: its own cache, since the
    face tracker's holds whichever model it was first asked for."""
    with _models_lock:
        if name not in _models:
            from ultralytics import YOLO

            from core.binaries import yolo_weights
            from core.gpu import cuda_usable

            model = YOLO(yolo_weights(name))
            usable, _reason = cuda_usable()
            if usable:
                model.to("cuda")
            _models[name] = model
        return _models[name]


def detect(model, frame, imgsz: int) -> tuple[list, list]:
    """(balls [(x, y, conf)], people [(x, y, w, h)]) in frame fractions."""
    from core.gpu import torch_device
    from video.tracker import _infer_lock

    h, w = frame.shape[:2]
    with _infer_lock:
        results = model.predict(frame, classes=[PERSON, BALL], conf=BALL_CONF, imgsz=imgsz,
                                device=torch_device(), verbose=False)
    balls, people = [], []
    for r in results:
        for b in r.boxes:
            x0, y0, x1, y1 = (float(v) for v in b.xyxy[0])
            cls, conf = int(b.cls[0]), float(b.conf[0])
            if cls == BALL:
                balls.append(((x0 + x1) / 2 / w, (y0 + y1) / 2 / h, conf))
            elif cls == PERSON and conf >= PERSON_CONF:
                people.append(((x0 + x1) / 2 / w, (y0 + y1) / 2 / h, (x1 - x0) / w, (y1 - y0) / h))
    return balls, people


def compute(clip_path, model_name: str = "yolov8n.pt", imgsz: int = 1280,
            sample_fps: float = SAMPLE_FPS) -> dict:
    """The crop path for one clip, in the form video/cropper.render_vertical takes."""
    import cv2

    from video.capture import video_capture
    from video.framing import is_cut, small_gray

    model = _model(model_name)
    samples = []
    with video_capture(clip_path) as cap:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        width = cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 16
        height = cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 9
        step = max(1, round(fps / sample_fps))
        prev_small = None
        index = 0
        while True:
            ok = cap.grab()
            if not ok:
                break
            if index % step == 0:
                ok, frame = cap.retrieve()
                if not ok:
                    break
                small = small_gray(frame)
                balls, people = detect(model, frame, imgsz)
                samples.append({"t": index / fps, "cut": is_cut(prev_small, small),
                                "balls": balls, "people": people})
                prev_small = small
            index += 1
    # The share of the source's width a 9:16 crop of its full height takes.
    crop_frac = min(1.0, (height * 9 / 16) / max(width, 1))
    path, led = plan(samples, crop_frac)
    return {"mode": "track", "path": path or [(0.0, 0.5)], "led": led}
