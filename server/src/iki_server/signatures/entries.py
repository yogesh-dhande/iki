"""The signed decisions still in force on a page of records (ADR 0126)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from uuid import UUID

from django.db.models import Exists, OuterRef, Q

from iki_server.entities.models import Entity, Revision, RevisionKind
from iki_server.entities.values import shape
from iki_server.signatures.models import Signature
from iki_spec.model import Field, State
from iki_spec.model import Signature as Wanted


@dataclass(frozen=True)
class Reversal(Wanted):
    """A reversal keeps the original entry's recorded meaning and eligible roles."""

    entry_roles: tuple[str, ...] = ()


@dataclass
class _Visits:
    state: str = ""
    entered_at: int = 0
    earlier: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class Entry:
    state: str
    earlier: set[str]
    reversals: tuple[Reversal, ...]

    def transitions(self, declaration: Field, current: str) -> Mapping[str, State]:
        if self.state != current:
            return declaration.states
        result = {}
        for name, target in declaration.states.items():
            if not target.signatures:
                target = replace(target, signatures=self.reversals, reason="required")
            elif name in self.earlier:
                target = replace(target, reason="required")
            result[name] = target
        return result


def _reversals(
    ids: list[UUID], records: Mapping[UUID, Signature], entity: UUID, number: int
) -> tuple[Reversal, ...]:
    from iki_server.signatures.services import parsed

    found = []
    for ref in ids:
        record = records.get(ref)
        doc = (
            parsed(record)
            if record and record.entity_id == entity and record.revision + 1 == number
            else {}
        )
        if doc.get("change", {}).get("reversal"):
            continue  # This signature returned the record to unsigned work.
        slot = doc.get("signature", {})
        label = slot.get("label", "unavailable entry signature")
        found.append(
            Reversal(
                meaning=slot.get("meaning", "review"),
                label=f"Reversal of {label}",
                statement=f"I reverse the decision signed as {label}.",
                entry_roles=tuple(slot.get("roles", [])),
            )
        )
    return tuple(found)


def for_records(entities: Sequence[Entity]) -> dict[tuple[UUID, str], Entry]:
    """Read carried signatures in bulk. History tables use today's field names even
    after migrations and cross-deployment restores. Revision numbers bind the state
    values in those tables to the signatures that actually set them."""
    types = {e.pk: e.type for e in entities if e.registry_id is None and e.revision > 1}
    carried = {
        (owner, number): ids
        for owner, number, ids in Revision.objects.filter(
            entity_id__in=types,
            kind=RevisionKind.STATE_CHANGED,
        )
        .exclude(signatures=[])
        .values_list("entity_id", "number", "signatures")
    }
    if not carried:
        return {}
    groups: dict[str, set[UUID]] = defaultdict(set)
    for owner, _ in carried:
        groups[types[owner]].add(owner)
    visits: dict[tuple[UUID, str], _Visits] = defaultdict(_Visits)
    changed = Revision.objects.filter(
        entity_id=OuterRef("entity_id"),
        number=OuterRef("revision"),
        kind=RevisionKind.STATE_CHANGED,
    )
    for type_name, owners in groups.items():
        for part in shape(type_name).parts:
            fields = [
                name for name, f in part.fields.items() if not f.removed and f.type == "state"
            ]
            if part.entry or not fields:
                continue
            rows = (
                part.history._default_manager.filter(
                    Q(revision=1) | Exists(changed),
                    entity_id__in=owners,
                )
                .order_by("entity_id", "revision")
                .values("entity_id", "revision", *fields)
            )
            for row in rows.iterator():
                for name in fields:
                    visit = visits[row["entity_id"], name]
                    if row[name] and row[name] != visit.state:
                        if visit.state:
                            visit.earlier.add(visit.state)
                        visit.state = row[name]
                        visit.entered_at = row["revision"]
    active = {
        key: carried[key[0], visit.entered_at]
        for key, visit in visits.items()
        if (key[0], visit.entered_at) in carried
    }
    records = Signature.objects.only("document", "entity_id", "revision").in_bulk(
        {ref for ids in active.values() for ref in ids}
    )
    result = {}
    for key, ids in active.items():
        visit = visits[key]
        slots = _reversals(ids, records, key[0], visit.entered_at)
        if slots:
            result[key] = Entry(visit.state, visit.earlier, slots)
    return result
