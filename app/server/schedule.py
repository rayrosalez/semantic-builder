"""Next fire time of a Quartz cron expression (what Databricks job schedules use).

Supports the common subset: numbers, names (SUN, JAN), `*`, `?`, lists, ranges
and `/` steps in each of seconds, minutes, hours, day-of-month, month and
day-of-week. `L`, `W` and `#` aren't supported; next_fire returns None for them.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
_DAYS = {d: i + 1 for i, d in enumerate(["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"])}


def _field(expr: str, lo: int, hi: int, names: dict | None = None) -> set[int] | None:
    """Values matched by one field; None means unrestricted (`*` or `?`)."""
    if expr in ("*", "?"):
        return None
    out: set[int] = set()
    for part in expr.upper().split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
        if part in ("*", "?", ""):
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = _num(a, names), _num(b, names)
        else:
            start = _num(part, names)
            end = hi if step > 1 else start
        out.update(range(start, end + 1, step))
    return out


def _num(token: str, names: dict | None) -> int:
    if names and token in names:
        return names[token]
    if not token.isdigit():
        raise ValueError(f"Unsupported cron token {token!r}")
    return int(token)


def next_fire(cron: str, tz: str, after: datetime | None = None) -> datetime | None:
    """Next time (timezone-aware, in `tz`) the schedule fires after `after`."""
    try:
        parts = cron.split()
        if len(parts) not in (6, 7):
            return None
        sec, minute, hour, dom, month, dow = parts[:6]
        secs = sorted(_field(sec, 0, 59) or {0})
        mins = sorted(_field(minute, 0, 59) or range(60))
        hours = sorted(_field(hour, 0, 23) or range(24))
        doms = _field(dom, 1, 31)
        months = _field(month, 1, 12, _MONTHS)
        dows = _field(dow, 1, 7, _DAYS)
    except (ValueError, IndexError):
        return None
    zone = ZoneInfo(tz)
    now = (after or datetime.now(zone)).astimezone(zone)
    day = now.date()
    for _ in range(400):
        quartz_dow = (day.isoweekday() % 7) + 1  # SUN=1 .. SAT=7
        if ((months is None or day.month in months)
                and (doms is None or day.day in doms)
                and (dows is None or quartz_dow in dows)):
            for h in hours:
                for m in mins:
                    for s in secs:
                        t = datetime(day.year, day.month, day.day, h, m, s, tzinfo=zone)
                        if t > now:
                            return t
        day += timedelta(days=1)
    return None
