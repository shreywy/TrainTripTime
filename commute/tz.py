"""Toronto local time.

Uses zoneinfo when the OS ships tz data (Linux / Raspberry Pi). Windows Python
builds often don't, so fall back to Ontario's DST rule rather than pull in a
dependency. All datetimes in this app are naive local time unless named *_utc.
"""
import datetime as dt

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("America/Toronto")
except Exception:
    TZ = None


def _nth_sunday(year, month, n):
    d = dt.date(year, month, 1)
    d += dt.timedelta(days=(6 - d.weekday()) % 7)
    return d + dt.timedelta(weeks=n - 1)


def utc_offset(local):
    if TZ:
        return local.replace(tzinfo=TZ).utcoffset()
    start = dt.datetime.combine(_nth_sunday(local.year, 3, 2), dt.time(2))
    end = dt.datetime.combine(_nth_sunday(local.year, 11, 1), dt.time(2))
    return dt.timedelta(hours=-4 if start <= local < end else -5)


def to_utc(local):
    return local - utc_offset(local)


def from_epoch(ts):
    utc = dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).replace(tzinfo=None)
    return utc + utc_offset(utc - dt.timedelta(hours=5))


def now():
    return from_epoch(dt.datetime.now(dt.timezone.utc).timestamp())


def at(date, secs):
    """GTFS service-day seconds (may exceed 24h) -> local datetime."""
    return dt.datetime.combine(date, dt.time()) + dt.timedelta(seconds=secs)
