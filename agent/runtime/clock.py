"""UTC clock used by queue and worker components."""
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc)
