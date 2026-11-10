"""Expressions for the CHECK constraints of generated plugin tables.

PostgreSQL can't use subqueries in a CHECK constraint, so checks on each item
of an array call small immutable SQL functions, created by this app's first
migration (``0001_initial``). Plugin migrations import these classes, so they
are API.
"""

from typing import Any

from django.db.models import BooleanField, F, Func, Value


class _Check(Func):
    output_field = BooleanField()

    def __init__(self, field: str, *values: Any) -> None:  # noqa: ANN401 - SQL literals
        super().__init__(F(field), *(Value(v) for v in values))


class IsPlainList(_Check):
    """One dimension, numbered from 1, without NULL items."""

    function = "iki_is_plain_list"


class AllMatch(_Check):
    """Every item matches the regular expression."""

    function = "iki_all_match"

    def __init__(self, field: str, pattern: str) -> None:
        super().__init__(field, pattern)


class NoneMatch(_Check):
    """No item matches the regular expression."""

    function = "iki_none_match"

    def __init__(self, field: str, pattern: str) -> None:
        super().__init__(field, pattern)


class AllLengthsBetween(_Check):
    function = "iki_all_lengths_between"

    def __init__(self, field: str, low: int | None, high: int | None) -> None:
        super().__init__(field, low, high)


class AllBetweenInt(_Check):
    function = "iki_all_between_int"

    def __init__(self, field: str, low: int | None, high: int | None) -> None:
        super().__init__(field, low, high)


class AllBetweenFloat(_Check):
    """Every item is finite, and within the bounds if any."""

    function = "iki_all_between_float"

    def __init__(self, field: str, low: float | None, high: float | None) -> None:
        super().__init__(field, low, high)
