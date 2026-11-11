"""Lookups for searching text: ``containing`` and ``starting_with``.

Django's ``icontains`` compares ``UPPER(column::text)``, which no trigram
index on the column can serve. These compare with ``ILIKE`` instead, so a
``gin_trgm_ops`` index on the column (ADR 0037) makes substring search fast
on tables of any size. ``%``, ``_`` and ``\\`` in the value match themselves.
"""

from typing import Any

from django.db import models
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.models import Lookup
from django.db.models.sql.compiler import SQLCompiler


class Containing(Lookup):
    lookup_name = "containing"
    prefix = "%"

    def as_sql(self, compiler: SQLCompiler, connection: BaseDatabaseWrapper) -> tuple[str, Any]:
        lhs, params = self.process_lhs(compiler, connection)
        pattern = self.prefix + connection.ops.prep_for_like_query(self.rhs) + "%"
        return f"{lhs} ILIKE %s", [*params, pattern]


class StartingWith(Containing):
    lookup_name = "starting_with"
    prefix = ""


def register() -> None:
    for field in (models.CharField, models.TextField):
        field.register_lookup(Containing)
        field.register_lookup(StartingWith)
