"""Human-readable IDs: ``PRT-000123``, one sequence per prefix, never reused.

The sequences are created at deploy time by ``iki-server sync_plugins``, and
never dropped, so a number is never handed out twice, even if a plugin is
removed and comes back. Numbers outgrow their padding rather than wrap.
"""

from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.db.utils import ProgrammingError

from iki_spec.model import EntityType


def sequence_name(prefix: str) -> str:
    return f"iki_hrid_{prefix.lower()}"


def format_hrid(prefix: str, digits: int, number: int) -> str:
    return f"{prefix}-{number:0{digits}d}"


def next_hrid(entity_type: EntityType) -> str:
    if entity_type.hrid_prefix is None:
        raise ValueError(f"{entity_type.qualified_name} has no HRIDs")
    name = sequence_name(entity_type.hrid_prefix)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT nextval(%s::regclass)", [name])
            (number,) = cursor.fetchone()
    except ProgrammingError as error:
        raise ImproperlyConfigured(
            f"The HRID sequence {name} doesn't exist; run `iki-server sync_plugins`."
        ) from error
    return format_hrid(entity_type.hrid_prefix, entity_type.hrid_digits, number)
