"""Read append-only revision metadata in the vocabulary of the installed plugin."""

from collections.abc import Iterable
from dataclasses import dataclass

from django.db.models import Q

from iki_server.entities import registry
from iki_server.entities.models import Entity, Revision
from iki_server.entities.values import shape
from iki_server.plugins.models import HistoryChange


def history_changes(entity: Entity, seen: int) -> list[HistoryChange]:
    """What plugins' migrations have done to the record's kept history since ``seen``
    (:func:`history_seen`), in the order they did it (ADR 0148): only changes to the tables
    its fields are in, as those tables were named then."""
    rows = HistoryChange.objects.filter(pk__gt=seen).order_by("-pk")
    return _of_parts({p.name for p in shape(entity.type).parts}, rows)


def _of_parts(parts: set[str], rows: Iterable[HistoryChange]) -> list[HistoryChange]:
    kept: list[HistoryChange] = []
    for row in rows:  # newest first, so each is matched to the tables as named then
        if row.kind == HistoryChange.Kind.RETYPE:
            named = {_retyped(p, row.to, row.name) for p in parts}
            if named != parts:
                kept.append(row)
                parts = named
        elif row.part in parts:
            kept.append(row)
    return kept[::-1]


def _retyped(part: str, new: str, old: str) -> str:
    """A part's name (``acme/lab.Sample``, ``acme/lims:acme/lab.Sample#entry``) before its
    type ``old`` was renamed ``new``."""
    named, hash_, entry = part.partition("#")
    plugin, colon, of = named.rpartition(":")
    return f"{plugin}{colon}{old if of == new else of}{hash_}{entry}"


@dataclass
class FieldNames:
    renames: list[HistoryChange]

    @classmethod
    def for_type(cls, type_name: str, *, entry: bool = False) -> "FieldNames":
        if type_name not in registry.types():
            return cls([])
        parts = {p.name for p in shape(type_name).parts if p.entry == entry}
        rows = HistoryChange.objects.filter(
            kind__in=[HistoryChange.Kind.RENAME, HistoryChange.Kind.RETYPE]
        ).order_by("-pk")
        return cls([r for r in _of_parts(parts, rows) if r.kind == HistoryChange.Kind.RENAME])

    def current(self, name: str, seen: int) -> str:
        for rename in self.renames:
            if rename.pk > seen and rename.name == name:
                name = rename.to
        return name

    def changed(self, revision: Revision) -> list[str]:
        return [self.current(name, revision.history_seen) for name in revision.changed_fields]

    def setting(self, name: str) -> Q:
        """Match the spelling valid when each revision was written, even if a name was reused."""
        matches = Q(pk__in=[])
        period = Q()
        for rename in reversed(self.renames):
            if rename.to == name or rename.name == name:
                matches |= period & Q(history_seen__gte=rename.pk, changed_fields__contains=[name])
                if rename.name == name:
                    return matches
                name = rename.name
                period = Q(history_seen__lt=rename.pk)
        return matches | (period & Q(changed_fields__contains=[name]))
