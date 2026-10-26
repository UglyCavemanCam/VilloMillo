"""A suggested webcam box for a stream: where the streamer's face is, drawn as the box the stacked layout cuts.

It is a guess from the face (a webcam box is about 3.8 face-widths across and 2.8 tall, with the face near its middle), in the
top-left quarter of the picture where streamers' webcams usually sit. The builder fills the four boxes with it; you can change them.
"""

from __future__ import annotations

import pathlib

import cv2
import numpy as np

BOX_WIDTH_PER_FACE = 3.8
BOX_HEIGHT_PER_FACE = 2.8
FACE_CENTRE_FROM_TOP = 0.62   # the face sits this far down its webcam box (a share of the box height)
MIN_SAMPLES = 3


def suggest(source: pathlib.Path, times: list[float] | None = None) -> dict | None:
    """{x, y, w, h} as percentages of the picture, or None when no face is found in the top-left quarter."""
    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        return None
    front = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    sw, sh = cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    duration = (cap.get(cv2.CAP_PROP_FRAME_COUNT) / (cap.get(cv2.CAP_PROP_FPS) or 24.0)) or 60.0
    found = []
    try:
        for t in (times or list(np.linspace(duration * 0.1, duration * 0.9, 10))):
            cap.set(cv2.CAP_PROP_POS_MSEC, float(t) * 1000)
            ok, frame = cap.read()
            if not ok:
                continue
            gray = cv2.cvtColor(cv2.resize(frame, (960, 540)), cv2.COLOR_BGR2GRAY)
            for x, y, w, h in front.detectMultiScale(gray, 1.1, 5, minSize=(30, 30)):
                cx, cy = (x + w / 2) * sw / 960, (y + h / 2) * sh / 540
                if cx < sw / 2 and cy < sh / 2:
                    found.append((cx, cy, w * sw / 960))
    finally:
        cap.release()
    if len(found) < MIN_SAMPLES:
        return None
    cx = float(np.median([f[0] for f in found]))
    cy = float(np.median([f[1] for f in found]))
    face_w = float(np.median([f[2] for f in found]))
    box_w, box_h = BOX_WIDTH_PER_FACE * face_w, BOX_HEIGHT_PER_FACE * face_w
    x0 = min(max(cx - box_w / 2, 0.0), sw - box_w)
    y0 = min(max(cy - FACE_CENTRE_FROM_TOP * box_h, 0.0), sh - box_h)
    return {"x": round(100 * x0 / sw), "y": round(100 * y0 / sh), "w": round(100 * box_w / sw), "h": round(100 * box_h / sh)}
