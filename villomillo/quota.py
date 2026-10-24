"""YouTube's daily allowances, counted here so we stop before Google makes us.

The day rolls over at midnight Pacific time. `publish.quota.pacific_day` (from the engine)
does that without needing a timezone database, which Windows lacks.
"""

from __future__ import annotations

from datetime import datetime

from publish.quota import pacific_day

from .db import Database

GENERAL = "general"  # most calls: 1 unit each
SEARCH = "search"  # search.list has its own, much smaller, allowance


class QuotaExceeded(RuntimeError):
    def __init__(self, bucket: str, used: int, cap: int):
        super().__init__(f"YouTube {bucket} allowance used up for today ({used}/{cap})")
        self.bucket, self.used, self.cap = bucket, used, cap


class Quota:
    def __init__(self, db: Database, *, general_cap: int, search_cap: int):
        self.db = db
        self.caps = {GENERAL: general_cap, SEARCH: search_cap}

    def charge(self, bucket: str, units: int = 1, *, now: datetime | None = None) -> int:
        """Count `units` against today's allowance, or raise without counting if it would go over."""
        day, cap = pacific_day(now), self.caps[bucket]
        with self.db.conn(immediate=True) as c:
            row = c.execute("SELECT units FROM quota_usage WHERE day=? AND bucket=?", (day, bucket)).fetchone()
            used = row["units"] if row else 0
            if used + units > cap:
                raise QuotaExceeded(bucket, used, cap)
            c.execute(
                "INSERT INTO quota_usage (day, bucket, units) VALUES (?, ?, ?) "
                "ON CONFLICT(day, bucket) DO UPDATE SET units = units + excluded.units",
                (day, bucket, units),
            )
            return used + units

    def exhaust(self, bucket: str, *, now: datetime | None = None) -> None:
        """Google said the allowance is gone: stop for the rest of the day whatever our count says."""
        day, cap = pacific_day(now), self.caps[bucket]
        with self.db.conn(immediate=True) as c:
            c.execute(
                "INSERT INTO quota_usage (day, bucket, units) VALUES (?, ?, ?) "
                "ON CONFLICT(day, bucket) DO UPDATE SET units = MAX(units, excluded.units)",
                (day, bucket, cap),
            )

    def used(self, bucket: str, *, now: datetime | None = None) -> int:
        with self.db.conn() as c:
            row = c.execute(
                "SELECT units FROM quota_usage WHERE day=? AND bucket=?", (pacific_day(now), bucket)
            ).fetchone()
        return row["units"] if row else 0
