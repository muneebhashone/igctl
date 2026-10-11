"""Parse human-friendly schedule times (``now``, ``+2h``, ``tomorrow 18:00``, ISO) to UTC."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from igctl.backends.base import IgctlError

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_RELATIVE = re.compile(r"^(?:\+|in\s+)\s*((?:\d+\s*[smhdw]\s*)+)$", re.IGNORECASE)
_REL_PART = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)
_CLOCK = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", re.IGNORECASE)
_OFFSET = re.compile(r"^(?:utc|gmt)?\s*([+-])(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)
_WEEKDAYS = {
    name: i
    for i, names in enumerate(
        [
            ("mon", "monday"),
            ("tue", "tues", "tuesday"),
            ("wed", "wednesday"),
            ("thu", "thur", "thurs", "thursday"),
            ("fri", "friday"),
            ("sat", "saturday"),
            ("sun", "sunday"),
        ]
    )  # fmt: skip
    for name in names
}


class TimeParseError(IgctlError):
    pass


def resolve_tz(name: str | None) -> tzinfo | None:
    """A tzinfo for ``name`` (IANA name, ``UTC`` or ``+05:30``); None means system local time."""
    if not name or name.lower() == "local":
        return None
    if name.upper() in ("UTC", "Z", "GMT"):
        return UTC
    if m := _OFFSET.match(name.strip()):
        sign, hours, minutes = m.group(1), int(m.group(2)), int(m.group(3) or 0)
        if hours > 23 or minutes > 59:
            raise TimeParseError(f"Invalid UTC offset '{name}' (hours 0-23, minutes 0-59).")
        delta = timedelta(hours=hours, minutes=minutes)
        return timezone(-delta if sign == "-" else delta)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        pass
    # Windows has no system tz database; dateutil (a croniter dependency) ships its own.
    try:
        from dateutil import tz as dateutil_tz

        if (found := dateutil_tz.gettz(name)) is not None:
            return found
    except ImportError:  # pragma: no cover
        pass
    raise TimeParseError(
        f"Unknown time zone '{name}'. Use an IANA name like 'Europe/London', 'UTC' or '+05:00'."
    )


def _localize(naive: datetime, tz: tzinfo | None) -> datetime:
    # A naive datetime's astimezone() interprets it as system local time (DST-aware).
    return naive.astimezone() if tz is None else naive.replace(tzinfo=tz)


def _parse_clock(text: str) -> time:
    m = _CLOCK.match(text.strip())
    if not m:
        raise TimeParseError(f"Invalid time of day '{text}' (expected HH:MM, e.g. 18:00 or 6pm).")
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if ampm:
        if not 1 <= hour <= 12:
            raise TimeParseError(f"Invalid 12-hour time '{text}'.")
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    if hour > 23 or minute > 59:
        raise TimeParseError(f"Invalid time of day '{text}'.")
    return time(hour, minute)


def parse_when(text: str, tz: str | tzinfo | None = None, now: datetime | None = None) -> datetime:
    """Parse ``text`` into an aware UTC datetime.

    Accepted forms: ``now``; relative ``+30m``, ``+2h``, ``+1d``, ``+1h30m``, ``in 2h``;
    ``today 18:00``, ``tomorrow 9am``, ``friday 18:00``; a bare ``18:00`` (next occurrence);
    and ISO-8601 (``2026-10-12T18:00``, ``2026-10-12 18:00+02:00``). Naive times are
    interpreted in ``tz`` (IANA name or tzinfo; None = system local time).
    """
    zone = resolve_tz(tz) if isinstance(tz, str) or tz is None else tz
    now_utc = (now or datetime.now(UTC)).astimezone(UTC)
    s = " ".join(text.strip().split())
    low = s.lower()
    if not s:
        raise TimeParseError("Empty schedule time.")
    if low == "now":
        return now_utc

    if m := _RELATIVE.match(low):
        seconds = sum(int(n) * _UNITS[u.lower()] for n, u in _REL_PART.findall(m.group(1)))
        return now_utc + timedelta(seconds=seconds)

    local_now = now_utc.astimezone(zone) if zone else now_utc.astimezone()
    first, _, rest = low.partition(" ")
    day: date | None = None
    if first in ("today", "tomorrow"):
        day = local_now.date() + timedelta(days=1 if first == "tomorrow" else 0)
    elif first in _WEEKDAYS:
        ahead = (_WEEKDAYS[first] - local_now.weekday()) % 7
        day = local_now.date() + timedelta(days=ahead)
    if day is not None:
        if not rest:
            raise TimeParseError(f"Add a time of day, e.g. '{first} 18:00'.")
        clock = _parse_clock(rest)
        result = _localize(datetime.combine(day, clock), zone)
        if first in _WEEKDAYS and result <= now_utc:  # "friday 9:00" on a Friday at 10:00
            result = _localize(datetime.combine(day + timedelta(days=7), clock), zone)
        return result.astimezone(UTC)

    if _CLOCK.match(low):
        clock = _parse_clock(low)
        result = _localize(datetime.combine(local_now.date(), clock), zone)
        if result <= now_utc:
            result = _localize(datetime.combine(local_now.date() + timedelta(days=1), clock), zone)
        return result.astimezone(UTC)

    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise TimeParseError(
            f"Can't understand time '{text}'. Try 'now', '+2h', 'tomorrow 18:00' "
            "or an ISO datetime like 2026-10-12T18:00."
        ) from None
    if dt.tzinfo is None:
        dt = _localize(dt, zone)
    return dt.astimezone(UTC)


def format_local(value: str | datetime, tz: str | tzinfo | None = None) -> str:
    """Render a stored UTC timestamp in ``tz`` (default system local) as ``YYYY-MM-DD HH:MM TZ``."""
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    zone = resolve_tz(tz) if isinstance(tz, str) or tz is None else tz
    local = dt.astimezone(zone) if zone else dt.astimezone()
    name = local.tzname() or ""
    if not name or len(name) > 5 or " " in name:  # Windows gives e.g. "Pakistan Standard Time"
        name = local.strftime("%z")
        name = f"UTC{name[:3]}:{name[3:]}" if name else ""
    return f"{local:%Y-%m-%d %H:%M} {name}".strip()
