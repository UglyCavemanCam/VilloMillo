"""How much of the play a clip keeps around a moment.

Build-up, moment, reaction. A goal clip starts with the attack and ends on the
celebration; it never starts as the ball goes in. The seconds before and after
come from the sport's event list, and a moment found by its crowd roar is
dated earlier first, because the roar arrives after the ball goes in.
"""


def window(pre: float, post: float, t: float, *, min_len: float, max_len: float,
           video_end: float, lag: float = 0.0, post_extra: float = 0.0) -> tuple[float, float]:
    """(start, end) around a moment at `t` seconds.

    `lag`: how late the signal that found it arrives (the crowd), so the
    moment itself is `t - lag`. The window stays within [0, video_end] and the
    job's clip lengths: too short grows forward (the reaction) then back; too
    long gives up the start of the build-up first, never the moment."""
    moment = max(0.0, t - max(0.0, lag))
    end_of_video = max(video_end, moment)
    start = max(0.0, moment - max(0.0, pre))
    end = min(end_of_video, moment + max(0.0, post) + max(0.0, post_extra))
    if end - start > max_len:
        # Keep the moment and everything after it that fits; lose build-up first.
        after = min(end - moment, max_len * 0.6)
        end = moment + after
        start = max(0.0, end - max_len)
    if end - start < min_len:
        end = min(end_of_video, start + min_len)
        start = max(0.0, end - min_len)
    return round(start, 2), round(end, 2)
