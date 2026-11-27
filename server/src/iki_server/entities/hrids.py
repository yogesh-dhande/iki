"""Human-readable IDs: ``PRT-000123``, one sequence per prefix, never reused.

The sequences are created at deploy time by ``iki-server sync_plugins``, and
never dropped, so a number is never handed out twice, even if a plugin is
removed and comes back. Numbers outgrow their padding rather than wrap.

A sequence's number is spent even when its transaction rolls back. A check that
writes records only to roll them back (a sheet's, ADR 0228) does so under
:func:`provisional`, whose records are numbered apart, so no HRID is skipped.
"""

import itertools
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.db.utils import ProgrammingError

from iki_spec.model import EntityType

#: Numbers for records a check writes and rolls back, while one is under way.
_provisional: ContextVar["itertools.count[int] | None"] = ContextVar(
    "iki_provisional_hrids", default=None
)
#: Where provisional numbers start: 18 digits, far past any a sequence hands out (a
#: million records a day would take a billion years to reach them). Each check starts at
#: a random point among them, so two checks at once don't number theirs alike.
PROVISIONAL_FROM = 900_000_000_000_000_000
PROVISIONAL_SPREAD = 90_000_000_000_000_000


@contextmanager
def provisional() -> Iterator[None]:
    """Records made meanwhile are numbered from :data:`PROVISIONAL_FROM`, by no sequence.
    Only for writes that are rolled back: a committed record must never keep one."""
    start = PROVISIONAL_FROM + secrets.randbelow(PROVISIONAL_SPREAD)
    token = _provisional.set(itertools.count(start))
    try:
        yield
    finally:
        _provisional.reset(token)


def is_provisional(hrid: str) -> bool:
    """Whether ``hrid`` is one :func:`provisional` gave."""
    _, _, number = hrid.rpartition("-")
    return number.isdigit() and int(number) >= PROVISIONAL_FROM


def provisional_hrid(prefix: str) -> str | None:
    """The next provisional HRID with ``prefix``, while :func:`provisional` is under way;
    None otherwise, when it's a sequence's."""
    counter = _provisional.get()
    return None if counter is None else f"{prefix}-{next(counter)}"


def sequence_name(prefix: str) -> str:
    return f"iki_hrid_{prefix.lower()}"


def format_hrid(prefix: str, digits: int, number: int) -> str:
    return f"{prefix}-{number:0{digits}d}"


def next_hrid(entity_type: EntityType) -> str:
    """The next HRID of the type's own sequence, or, for a lab list's option, of the one
    sequence every list's options share (ADR 0295)."""
    prefix = entity_type.numbering_prefix
    if prefix is None:
        raise ValueError(f"{entity_type.qualified_name} has no HRIDs")
    if (made := provisional_hrid(prefix)) is not None:
        return made
    name = sequence_name(prefix)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT nextval(%s::regclass)", [name])
            (number,) = cursor.fetchone()
    except ProgrammingError as error:
        raise ImproperlyConfigured(
            f"The HRID sequence {name} doesn't exist; run `iki-server sync_plugins`."
        ) from error
    return format_hrid(prefix, entity_type.hrid_digits, number)
