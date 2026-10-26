"""The small checks every part of a design uses, each refusing with a message that says what to change."""

from __future__ import annotations

import re


class DesignError(ValueError):
    """The message says what to change, in plain words."""


def hex_colour(value, what: str, *, blank_ok: bool = False, none_ok: bool = False) -> str | None:
    if value is None and none_ok:
        return None
    if value == "" and blank_ok:
        return ""
    if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value.strip()):
        return value.strip().upper()
    raise DesignError(f"{what} must be a colour like #FFB000.")


def whole(value, lo: int, hi: int, what: str, *, none_ok: bool = False) -> int | None:
    if value is None and none_ok:
        return None
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        raise DesignError(f"{what} must be a whole number from {lo} to {hi}.") from None
    if not lo <= n <= hi:
        raise DesignError(f"{what} must be a whole number from {lo} to {hi}.")
    return n


def _luminance(hex_rgb: str) -> float:
    channels = []
    for i in (1, 3, 5):
        c = int(hex_rgb[i:i + 2], 16) / 255
        channels.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)
