"""Every time the dashboard shows is Eastern Time (America/New_York), 12-hour clock, with daylight saving handled.

The database keeps UTC. Nothing here changes what is stored: pages convert on the way out.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = timezone.utc


def parse(value: str | datetime | None) -> datetime | None:
    """A stored UTC time (ISO text or datetime) as an aware datetime, or None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def to_et(value: str | datetime | None) -> datetime | None:
    moment = parse(value)
    return moment.astimezone(ET) if moment else None


def now_et() -> datetime:
    return datetime.now(UTC).astimezone(ET)


def today_et() -> date:
    return now_et().date()


def zone_label(moment: datetime | None = None) -> str:
    """EDT or EST, whichever applies at that moment."""
    return (moment or now_et()).astimezone(ET).tzname() or "ET"


def clock(value) -> str:
    """12:35 AM"""
    m = to_et(value)
    return f"{m.hour % 12 or 12}:{m:%M} {'AM' if m.hour < 12 else 'PM'}" if m else ""


def day(value) -> str:
    """Sat, Oct 24"""
    m = to_et(value)
    return f"{m:%a}, {m:%b} {m.day}" if m else ""


def short(value) -> str:
    """Oct 24, 12:35 AM"""
    m = to_et(value)
    return f"{m:%b} {m.day}, {clock(m)}" if m else ""


def full(value) -> str:
    """Sat, Oct 24, 12:35 AM EDT"""
    m = to_et(value)
    return f"{day(m)}, {clock(m)} {m.tzname()}" if m else ""


def ago(value) -> str:
    """5 min ago, 3 h ago, 2 d ago"""
    m = parse(value)
    if not m:
        return ""
    seconds = max(0, (datetime.now(UTC) - m).total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 172800:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} d ago"


def local_date(value) -> date | None:
    m = to_et(value)
    return m.date() if m else None


def start_of_day_utc(d: date) -> datetime:
    """Midnight at the start of this Eastern date, as a UTC time (for cut-offs and daily caps)."""
    return datetime(d.year, d.month, d.day, tzinfo=ET).astimezone(UTC)


def month_grid(year: int, month: int) -> list[date]:
    """The days shown for a month: whole Sunday-to-Saturday weeks that cover it."""
    first = date(year, month, 1)
    start = first - timedelta(days=(first.weekday() + 1) % 7)
    last = (date(year + (month == 12), month % 12 + 1, 1)) - timedelta(days=1)
    end = last + timedelta(days=(5 - last.weekday()) % 7)
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def week_of(d: date) -> list[date]:
    start = d - timedelta(days=(d.weekday() + 1) % 7)
    return [start + timedelta(days=i) for i in range(7)]
