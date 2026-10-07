"""The user's clock.

Every "today" and every user-supplied date-time resolves through here, so the
same moment lands on the same day whichever code path asks. The container runs
in UTC, so the server's own `date.today()` / naive `datetime.now()` is 4-5 hours
off the user's evening — never use them for anything the user sees.
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

# Settings.timezone's default: the zone the app assumes when nothing valid is set.
DEFAULT_TZ = "America/New_York"
_warned: set[str] = set()


def user_tz(state) -> ZoneInfo:
    """The user's zone. An unset or invalid name (only reachable through a
    hand-edited config.json — both setters validate) falls back to the default
    instead of raising; it is logged once per name."""
    name = getattr(state, "timezone", None)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        key = repr(name)
        if key not in _warned:
            _warned.add(key)
            log.warning("invalid timezone %s; using %s", key, DEFAULT_TZ)
        return ZoneInfo(DEFAULT_TZ)


def local_now(state) -> datetime:
    """Now, aware, in the user's zone."""
    return datetime.now(user_tz(state))


def local_today(state) -> str:
    """Today's date (YYYY-MM-DD) on the user's clock."""
    return local_now(state).date().isoformat()


def parse_user_datetime(value: str, tz: ZoneInfo) -> datetime:
    """An ISO date-time from the user or the model, as naive local time in `tz`.

    Stored times are naive local. Stripping an explicit offset kept the
    wall-clock digits in the wrong zone ('14:00Z' became 14:00 local), so an
    aware value is converted instead. Raises ValueError."""
    parsed = datetime.fromisoformat(str(value).strip())
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(tz).replace(tzinfo=None)
    return parsed
