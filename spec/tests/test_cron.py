"""Cron schedules, as scheduled triggers write them (ADR 0080)."""

from datetime import UTC, datetime

import pytest

from iki_spec import cron

UTC_ZONE = cron.zone("UTC")
ZURICH = cron.zone("Europe/Zurich")


def at(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def test_the_next_time_comes_after() -> None:
    weekdays = cron.parse("0 6 * * 1-5")
    # Saturday the 14th: next is Monday.
    assert weekdays.next_after(at(2026, 11, 14, 12), UTC_ZONE) == at(2026, 11, 16, 6)
    # Strictly after: at 06:00 on Monday, the next is Tuesday.
    assert weekdays.next_after(at(2026, 11, 16, 6), UTC_ZONE) == at(2026, 11, 17, 6)


@pytest.mark.parametrize(
    ("text", "after", "expected"),
    [
        ("*/15 * * * *", at(2026, 1, 1, 0, 7), at(2026, 1, 1, 0, 15)),
        ("5,35 9-17 * * *", at(2026, 1, 1, 17, 40), at(2026, 1, 2, 9, 5)),
        ("0 0 1 JAN *", at(2026, 6, 1), at(2027, 1, 1)),
        ("0 12 * * SUN", at(2026, 11, 14), at(2026, 11, 15, 12)),
        ("0 12 * * 7", at(2026, 11, 14), at(2026, 11, 15, 12)),
        ("0 0 29 2 *", at(2026, 3, 1), at(2028, 2, 29)),
        # Both day fields restricted: either matches, as cron's.
        ("0 0 13 * FRI", at(2026, 11, 14), at(2026, 11, 20)),
    ],
)
def test_fields(text: str, after: datetime, expected: datetime) -> None:
    assert cron.parse(text).next_after(after, UTC_ZONE) == expected


def test_times_are_wall_clock_times_in_the_timezone() -> None:
    nightly = cron.parse("30 2 * * *")
    # Zurich is UTC+1 in winter.
    assert nightly.next_after(at(2026, 1, 10, 12), ZURICH) == at(2026, 1, 11, 1, 30)
    # 02:30 doesn't exist on the 29th of March 2026, when clocks spring forward.
    assert nightly.next_after(at(2026, 3, 28, 12), ZURICH) == at(2026, 3, 30, 0, 30)
    # On the 25th of October it happens twice: it runs the first time only.
    first = nightly.next_after(at(2026, 10, 24, 12), ZURICH)
    assert first == at(2026, 10, 25, 0, 30)
    assert nightly.next_after(first, ZURICH) == at(2026, 10, 26, 1, 30)


def test_a_schedule_that_never_comes_round() -> None:
    assert cron.parse("0 0 30 2 *").next_after(at(2026, 1, 1), UTC_ZONE) is None


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("0 6 * *", "A schedule has five fields"),
        ("60 * * * *", "`60` isn't a minute: those are 0 to 59."),
        ("0 6 * FOO *", "`FOO` isn't a month: those are 1 to 12, or JAN to DEC."),
        ("0 6 * * 1-0", "`1-0`: a range in the day of the week goes from low to high."),
        ("*/0 * * * *", "a step, after `/`, is a whole number from 1"),
        ("* * * * *", "It runs 1 minute apart"),
        ("0,3 * * * *", "It runs 3 minutes apart"),
        ("58 * * * *", None),
        ("58,1 * * * *", "It runs 3 minutes apart"),
        ("*/5 * * * *", None),
    ],
)
def test_what_doesnt_read(text: str, message: str | None) -> None:
    if message is None:
        cron.parse(text)
        return
    with pytest.raises(cron.ScheduleError) as raised:
        cron.parse(text)
    assert message in str(raised.value)


def test_timezones_are_iana_names() -> None:
    assert cron.zone("Europe/Zurich").key == "Europe/Zurich"
    with pytest.raises(cron.ScheduleError, match="isn't a timezone"):
        cron.zone("CET+1")
