"""Cron schedules, as scheduled triggers write them (ADR 0080).

Five fields, as POSIX cron and GitHub Actions' `schedule` read them: minute (0 to 59), hour
(0 to 23), day of the month (1 to 31), month (1 to 12, or JAN to DEC) and day of the week
(0 to 6, or SUN to SAT; 7 is Sunday too). Each is `*`, a value, a range `a-b`, a step
`*/n` or `a-b/n`, or a list of those. When both day fields are restricted, a day matching
either fires, as cron's.

Times are wall-clock times in a timezone (UTC unless the trigger names one). A time that
doesn't exist that day (a daylight-saving spring forward) is skipped; one that happens
twice fires once, the first time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

__all__ = ["MIN_INTERVAL", "Schedule", "ScheduleError", "parse", "zone"]

#: The shortest time between two runs a schedule may ask for, as GitHub's.
MIN_INTERVAL = timedelta(minutes=5)

_MONTHS = {
    m: i
    for i, m in enumerate(
        ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1
    )
}
_DAYS = {d: i for i, d in enumerate(["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"])}
_FIELDS: tuple[tuple[str, int, int, dict[str, int]], ...] = (
    ("minute", 0, 59, {}),
    ("hour", 0, 23, {}),
    ("day of the month", 1, 31, {}),
    ("month", 1, 12, _MONTHS),
    ("day of the week", 0, 7, _DAYS),
)
#: How far ahead to look for the next run: past any February 29th.
_HORIZON_DAYS = 366 * 8 + 2


class ScheduleError(ValueError):
    """Why a schedule can't be read, in a sentence."""


@dataclass(frozen=True)
class Schedule:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    #: Whether each day field was `*`: cron's rule for when both are restricted.
    any_day: bool
    any_weekday: bool

    def _day_matches(self, when: datetime) -> bool:
        if when.month not in self.months:
            return False
        day = when.day in self.days
        weekday = (when.isoweekday() % 7) in self.weekdays
        if self.any_day:
            return weekday
        if self.any_weekday:
            return day
        return day or weekday

    def next_after(self, after: datetime, tz: ZoneInfo) -> datetime | None:
        """The first time strictly after `after` (aware) the schedule fires in `tz`, in UTC,
        or None if it never does (February 30th)."""
        local = after.astimezone(tz)
        day = local.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
        for _ in range(_HORIZON_DAYS):
            if self._day_matches(day):
                for hour in sorted(self.hours):
                    for minute in sorted(self.minutes):
                        wall = day.replace(hour=hour, minute=minute)
                        found = _aware(wall, tz)
                        if found is not None and found > after:
                            # In UTC: an ambiguous local time compares unequal across zones.
                            return found.astimezone(UTC)
            day += timedelta(days=1)
        return None


def _aware(wall: datetime, tz: ZoneInfo) -> datetime | None:
    """The wall-clock time in `tz`, or None if it doesn't exist there that day."""
    first = wall.replace(tzinfo=tz, fold=0)
    # A time that doesn't exist comes back changed from a round trip through UTC.
    if first.astimezone(UTC).astimezone(tz).replace(tzinfo=None) != wall:
        return None
    return first


def parse(text: str) -> Schedule:
    """The schedule `text` writes. Raises :class:`ScheduleError`, saying why it can't be read."""
    parts = text.split()
    if len(parts) != 5:
        raise ScheduleError(
            "A schedule has five fields: minute, hour, day of the month, month and day of the "
            f"week, such as `0 6 * * 1-5`. This has {len(parts)}."
        )
    sets = [_field(part, *spec) for part, spec in zip(parts, _FIELDS, strict=True)]
    weekdays = frozenset(d % 7 for d in sets[4])  # 7 is Sunday too
    schedule = Schedule(
        minutes=sets[0],
        hours=sets[1],
        days=sets[2],
        months=sets[3],
        weekdays=weekdays,
        any_day=parts[2] == "*",
        any_weekday=parts[4] == "*",
    )
    gap = _shortest_gap(schedule)
    if gap < MIN_INTERVAL:
        minutes = int(gap.total_seconds() // 60)
        raise ScheduleError(
            f"It runs {minutes} minute{'s' if minutes != 1 else ''} apart, and a schedule runs "
            f"at most every {int(MIN_INTERVAL.total_seconds() // 60)} minutes."
        )
    return schedule


def zone(name: str) -> ZoneInfo:
    """The IANA timezone `name`, such as `Europe/Zurich`. Raises :class:`ScheduleError`."""
    if name not in available_timezones():
        raise ScheduleError(
            f"`{name}` isn't a timezone. Use an IANA name, such as `Europe/Zurich`."
        )
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:  # pragma: no cover - listed, yet not found
        raise ScheduleError(f"`{name}` isn't a timezone.") from None


def _shortest_gap(schedule: Schedule) -> timedelta:
    """The shortest time between two runs within an hour, or across one into the next."""
    minutes = sorted(schedule.minutes)
    gaps = [b - a for a, b in pairwise(minutes)]
    if _consecutive(schedule.hours):
        gaps.append(60 - minutes[-1] + minutes[0])
    return timedelta(minutes=min(gaps)) if gaps else timedelta(hours=1)


def _consecutive(hours: frozenset[int]) -> bool:
    """Whether a run can be followed by one in the next hour (23 by 0, too)."""
    return any((h + 1) % 24 in hours for h in hours)


def _field(text: str, name: str, low: int, high: int, names: dict[str, int]) -> frozenset[int]:
    found: set[int] = set()
    for item in text.split(","):
        found |= _item(item, name, low, high, names)
    return frozenset(found)


def _item(text: str, name: str, low: int, high: int, names: dict[str, int]) -> set[int]:
    body, _, step_text = text.partition("/")
    step = 1
    if step_text:
        if not step_text.isdigit() or int(step_text) < 1:
            raise ScheduleError(
                f"`{text}`: a step, after `/`, is a whole number from 1, in the {name}."
            )
        step = int(step_text)
    if body == "*":
        start, end = low, high
    elif "-" in body:
        first, _, last = body.partition("-")
        start, end = _value(first, name, low, high, names), _value(last, name, low, high, names)
        if start > end:
            raise ScheduleError(f"`{text}`: a range in the {name} goes from low to high.")
    else:
        start = _value(body, name, low, high, names)
        end = high if step_text else start
    return set(range(start, end + 1, step))


def _value(text: str, name: str, low: int, high: int, names: dict[str, int]) -> int:
    if text.upper() in names:
        return names[text.upper()]
    if not text.isdigit() or not low <= int(text) <= high:
        allowed = f"{low} to {high}"
        if names:
            allowed += f", or {next(iter(names))} to {list(names)[-1]}"
        raise ScheduleError(f"`{text}` isn't a {name}: those are {allowed}.")
    return int(text)
