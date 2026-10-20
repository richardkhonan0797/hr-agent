"""Persistent, globally serialized public-demo admission control.

The database clock is read after acquiring the singleton row lock. No model or
network work happens while that lock is held. Rejected requests consume nothing;
admitted attempts remain charged even when subsequent model execution fails.
"""
from datetime import datetime, timedelta, timezone
import math

JAKARTA = timezone(timedelta(hours=7))

SCHEMA = """
CREATE TABLE IF NOT EXISTS demo_chat_quota (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    day DATE,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    recent TIMESTAMPTZ[] NOT NULL DEFAULT '{}'
);
INSERT INTO demo_chat_quota (singleton) VALUES (TRUE) ON CONFLICT DO NOTHING;
"""


def decide(now: datetime, day, attempts: int, recent: list[datetime],
           daily_limit: int, minute_limit: int) -> tuple[int, int, list[datetime]]:
    """Return retry seconds (zero means admitted), new count and rolling window."""
    local = now.astimezone(JAKARTA)
    attempts = attempts if day == local.date() else 0
    recent = [stamp for stamp in recent if stamp > now - timedelta(seconds=60)]
    waits = []
    if attempts >= daily_limit:
        midnight = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                                      microsecond=0)
        waits.append(math.ceil((midnight - local).total_seconds()))
    if len(recent) >= minute_limit:
        # Handles lowering the configured limit while older admissions remain.
        waits.append(math.ceil((sorted(recent)[-minute_limit] + timedelta(seconds=60)
                                - now).total_seconds()))
    if waits:
        return max(1, *waits), attempts, recent
    return 0, attempts + 1, recent + [now]


def reserve(connection, daily_limit: int, minute_limit: int) -> int:
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '5s'")
            cursor.execute("SET LOCAL statement_timeout = '5s'")
            cursor.execute("SELECT day, attempts, recent FROM demo_chat_quota "
                           "WHERE singleton = TRUE FOR UPDATE")
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("Quota migration required")
            cursor.execute("SELECT clock_timestamp() AS now")
            now = cursor.fetchone()["now"]
            retry, count, recent = decide(now, row["day"], row["attempts"],
                                           row["recent"], daily_limit, minute_limit)
            if not retry:
                cursor.execute("UPDATE demo_chat_quota SET day = %s, attempts = %s, "
                               "recent = %s WHERE singleton = TRUE",
                               (now.astimezone(JAKARTA).date(), count, recent))
            return retry
