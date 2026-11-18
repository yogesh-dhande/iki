"""The entities a run reads through its inputs' references (ADR 0105).

An action's input can ``include`` reference fields of its type, such as an antibody's
``heavy_chain`` and ``light_chain``. When a run is launched, each input entity's references
are pinned, as its inputs are: the entity each names, at the revision it has then. The run
reads them at those revisions, under each input entity's ``included``, whatever changes
after. A run reads only what its launcher can read and what's in its project, so the
references' entities must be both: ones from other projects are added to it as its
inputs are, when the launch asks.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from iki_server.accounts.models import User
from iki_server.entities import access, registry
from iki_server.entities.models import Entity
from iki_server.entities.values import shape, to_json
from iki_server.runs.models import ActionRun, RunInclusion
from iki_spec.model import Action


@dataclass(frozen=True)
class Pin:
    """An entity a run reads through a reference: whose, which, and at which revision."""

    input: str
    position: int
    field: str
    index: int
    entity: Entity
    revision: int
    #: For a record of a registry entry (ADR 0115): the entry's revision, pinned with it.
    entry_revision: int | None = None


class Unreadable(Exception):  # noqa: N818 - raised for its message, as Refused is
    """A reference's entity the run can't read, by input: why, for each."""

    def __init__(self, errors: dict[str, list[str]]) -> None:
        super().__init__("Some references can't be read.")
        self.errors = errors


def resolve(
    user: User | None,
    action: Action,
    entities: Mapping[str, Sequence[Entity]],
) -> list[Pin]:
    """The entities each input's included references name, now. Raises :class:`Unreadable`
    for ones `user` can't read. Whether they're in the run's project is checked with its
    inputs: see :func:`reads`."""
    from iki_server.entities.services import values_at

    wanted: list[tuple[str, int, str, int, UUID, Entity]] = []
    for name, put in action.inputs.items():
        if not put.include:
            continue
        for position, entity in enumerate(entities.get(name) or ()):
            fields = shape(entity.type).fields()
            values = values_at(entity, entity.revision)
            for reference in put.include:
                f = fields.get(reference)
                if f is None:
                    continue
                value = to_json(f, values.get(reference))
                ids = value if isinstance(value, list) else [] if value is None else [value]
                wanted += [
                    (name, position, reference, index, UUID(str(i)), entity)
                    for index, i in enumerate(ids)
                ]
    if not wanted:
        return []
    targets = {w[4] for w in wanted}
    readable = access.visible(user) if user is not None else Entity.objects.all()
    readable = readable.filter(purged_at__isnull=True)
    found = {e.pk: e for e in readable.filter(pk__in=targets)}
    # A reference to a registry entry (ADR 0115) reads a record of it: the one in the
    # project of the entity that refers to it, or else one the user can read, which the
    # launch then asks to add to the run's project as it would any input from another.
    records: dict[UUID, list[Entity]] = defaultdict(list)
    for record in readable.filter(entry_id__in=targets - found.keys()).order_by("created_at"):
        assert record.entry_id is not None  # noqa: S101 - filtered so
        records[record.entry_id].append(record)
    entries = dict(Entity.objects.filter(pk__in=list(records)).values_list("pk", "revision"))
    errors: dict[str, list[str]] = defaultdict(list)
    pins = []
    for name, position, reference, index, pk, owner in wanted:
        target = found.get(pk)
        if target is None and records.get(pk):
            held = [r for r in records[pk] if r.owner_project_id == owner.owner_project_id]
            target = (held or records[pk])[0]
        if target is None:
            label = reference.replace("_", " ")
            errors[name].append(f"{owner.hrid}'s {label} isn't an entity you can read.")
        else:
            pins.append(
                Pin(
                    name,
                    position,
                    reference,
                    index,
                    target,
                    target.revision,
                    entries.get(target.entry_id) if target.entry_id is not None else None,
                )
            )
    if errors:
        raise Unreadable(dict(errors))
    return pins


def reads(entities: Mapping[str, Sequence[Entity]], pins: Iterable[Pin]) -> dict[str, list[Entity]]:
    """What the run reads, by input: its entities, then what their references name. Each
    must be in the run's project, as its inputs must."""
    out = {name: list(chosen) for name, chosen in entities.items()}
    for p in pins:
        out.setdefault(p.input, []).append(p.entity)
    return out


def of_each(pins: Sequence[Pin], each: str, position: int) -> list[Pin]:
    """A batch's pins for the run of its `each` entity at `position`: that entity's, as the
    run's only one, and the other inputs', which every run of it shares."""
    return [
        Pin(p.input, 0, p.field, p.index, p.entity, p.revision, p.entry_revision)
        if p.input == each
        else p
        for p in pins
        if p.input != each or p.position == position
    ]


def record(run: ActionRun, pins: Iterable[Pin]) -> None:
    RunInclusion.objects.bulk_create(
        RunInclusion(
            run=run,
            input=p.input,
            position=p.position,
            field=p.field,
            index=p.index,
            entity=p.entity,
            revision=p.revision,
            entry_revision=p.entry_revision,
        )
        for p in pins
    )


def copy(run: ActionRun, original: ActionRun) -> None:
    """Pins what `original` read, for a run that reproduces it."""
    RunInclusion.objects.bulk_create(
        RunInclusion(
            run=run,
            input=i.input,
            position=i.position,
            field=i.field,
            index=i.index,
            entity_id=i.entity_id,
            revision=i.revision,
            entry_revision=i.entry_revision,
        )
        for i in RunInclusion.objects.filter(run=original).order_by("id")
    )


def keyed(pins: Iterable[Pin]) -> list[list[Any]]:
    """What the pins add to a run's reuse key: each entity at its revision, by place."""
    types = registry.types()
    return sorted(
        [
            p.input,
            p.position,
            p.field,
            p.index,
            str(p.entity.pk),
            p.revision,
            p.entity.type,
            types[p.entity.type].schema_version,
            *([] if p.entry_revision is None else [p.entry_revision]),
        ]
        for p in pins
    )


def documents(
    runs: Sequence[ActionRun],
) -> tuple[dict[tuple[UUID, str, int], dict[str, list[dict[str, Any]]]], set[UUID]]:
    """For a job's runs: each input entity's ``included``, by (run, input, position), as the
    run input has it, and the files their values hold."""
    from iki_server.entities.services import values_at
    from iki_server.runs import input_files

    out: dict[tuple[UUID, str, int], dict[str, list[dict[str, Any]]]] = defaultdict(dict)
    files: set[UUID] = set()
    rows = (
        RunInclusion.objects.filter(run__in=[r.pk for r in runs])
        .select_related("entity")
        .order_by("run_id", "input", "position", "field", "index")
    )
    types = registry.types()
    for row in rows:
        entity = row.entity
        values = values_at(entity, row.revision, row.entry_revision)
        fields = shape(entity.type).fields()
        for name, value in values.items():
            if name in fields:
                files |= input_files.file_ids(fields[name], value)
        out[(row.run_id, row.input, row.position)].setdefault(row.field, []).append(
            {
                "id": str(entity.pk),
                "hrid": entity.hrid,
                "type": entity.type,
                "schema_version": types[entity.type].schema_version,
                "revision": row.revision,
                "values": {n: to_json(fields[n], v) for n, v in values.items() if n in fields},
            }
        )
    return out, files
