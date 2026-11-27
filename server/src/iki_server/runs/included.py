"""The entities a run reads through its inputs' references (ADR 0105).

An action's input can ``include`` reference fields of its type, such as a structure's
``chains``, or a list of rows' references, such as a complex's ``components.part`` (ADR
0161). When a run is launched, each input entity's references
are pinned, as its inputs are: the entity each names, at the revision it has then. The run
reads them at those revisions under each input entity's ``included``. Its launcher
authorizes the inputs and each lease checks that the run's project can still read the
complete input document.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import UUID

from iki_server.accounts.models import User
from iki_server.api.problems import Problem
from iki_server.entities import access, registry
from iki_server.entities.models import Entity
from iki_server.entities.values import UNREADABLE_REFERENCE, reference_positions, shape, to_json
from iki_server.files.models import File
from iki_server.files.scan_state import ScanStatus, statuses_for_blobs
from iki_server.org.models import Project
from iki_server.org.services import Refused
from iki_server.runs.models import ActionRun, RunInclusion, RunInput
from iki_spec.model import Action, included_reference


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
    covered_materials: tuple[str, ...] | None = None


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
                f = included_reference(fields, reference)
                if f is None:
                    continue
                if "." in reference:
                    # A list of rows' references (ADR 0161): each row's, in order.
                    rows_name, _, cell = reference.partition(".")
                    rows = to_json(fields[rows_name], values.get(rows_name)) or []
                    ids = [row[cell] for row in rows if row.get(cell) is not None]
                else:
                    value = to_json(f, values.get(reference))
                    ids = value if isinstance(value, list) else [] if value is None else [value]
                wanted += [
                    (name, position, reference, index, UUID(str(i)), entity)
                    for index, i in enumerate(ids)
                ]
    if not wanted:
        return []
    targets = {w[4] for w in wanted}
    readable = access.visible(user) if user is not None else Entity.objects.filter(registry=None)
    readable = readable.filter(purged_at__isnull=True)
    found = {e.pk: e for e in readable.filter(pk__in=targets)}
    # A reference to a registry entry (ADR 0115) reads a record of it: the one in the
    # project of the entity that refers to it, or else one the user can read, which the
    # launch then asks to add to the run's project as it would any input from another.
    records: dict[UUID, list[Entity]] = defaultdict(list)
    for record in readable.filter(entry_id__in=targets - found.keys()).order_by("created_at"):
        assert record.entry_id is not None  # noqa: S101 - filtered so
        records[record.entry_id].append(record)
    entry_ids = set(records) | {e.entry_id for e in found.values() if e.entry_id is not None}
    entries = dict(Entity.objects.filter(pk__in=entry_ids).values_list("pk", "revision"))
    options = _options(targets - found.keys() - records.keys())
    registries = (
        dict(
            Project.objects.filter(pk__in={w[5].owner_project_id for w in wanted}).values_list(
                "pk", "registry_id"
            )
        )
        if options
        else {}
    )
    errors: dict[str, list[str]] = defaultdict(list)
    pins = []
    for name, position, reference, index, pk, owner in wanted:
        target = found.get(pk)
        if target is None and records.get(pk):
            held = [r for r in records[pk] if r.owner_project_id == owner.owner_project_id]
            target = (held or records[pk])[0]
        if target is None and pk in options:
            # A lab list's option, read by every project in its registry (ADR 0270): the
            # one the record's own project's registry has, as a reference can name no other.
            option = options[pk]
            if owner.owner_project_id is not None and option.registry_id == registries.get(
                owner.owner_project_id
            ):
                target = option
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


def _options(ids: set[UUID]) -> dict[UUID, Entity]:
    """Of ``ids``, lab lists' options (ADR 0270): registry entries no project has a record of."""
    listed = access.list_types()
    if not ids or not listed:
        return {}
    return {
        e.pk: e
        for e in Entity.objects.filter(
            pk__in=ids, registry__isnull=False, type__in=sorted(listed), purged_at__isnull=True
        )
    }


def options_by_registry(ids: Iterable[UUID]) -> dict[UUID, UUID]:
    """Of ``ids``, lab lists' options (ADR 0270), each with its registry."""
    listed = access.list_types()
    wanted = set(ids)
    if not wanted or not listed:
        return {}
    return dict(
        Entity.objects.filter(
            pk__in=wanted, registry__isnull=False, type__in=sorted(listed)
        ).values_list("pk", "registry_id")
    )


def options_of(project: Project, ids: Iterable[UUID]) -> set[UUID]:
    """Of ``ids``, the options of ``project``'s registry's lab lists (ADR 0270): every
    one of its readers reads them, and a run there reads them without a record of them."""
    return {pk for pk, at in options_by_registry(ids).items() if at == project.registry_id}


def is_option(entity: Entity) -> bool:
    """Whether ``entity`` is a lab list's option (ADR 0270), which its registry's every
    project reads, rather than a record a project holds."""
    return entity.registry_id is not None and entity.type in access.list_types()


def reads(entities: Mapping[str, Sequence[Entity]], pins: Iterable[Pin]) -> dict[str, list[Entity]]:
    """What the run reads, by input: its entities, then what their references name. Each
    must be in the run's project, as its inputs must, except a lab list's option, which is
    its registry's (ADR 0270)."""
    out = {name: list(chosen) for name, chosen in entities.items()}
    for p in pins:
        if not is_option(p.entity):
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


def with_coverage(
    action: Action, entities: Mapping[str, Sequence[Entity]], pins: list[Pin], project: UUID
) -> list[Pin]:
    """Pin which direct inputs each included request covers for declared material results."""
    from iki_server.entities import requests
    from iki_server.entities.models import Request

    if not any((spec := requests.result_spec(c.type)) and spec.material for c in action.creates):
        return pins
    requests_here = set(
        Request.objects.filter(
            service_id=project,
            entity_id__in=[pin.entity.pk for pin in pins if requests.spec_of(pin.entity.type)],
        ).values_list("entity_id", flat=True)
    )
    candidates = {e.pk for group in entities.values() for e in group}
    bound = []
    for pin in pins:
        if pin.entity.pk in requests_here:
            materials = requests.materials_of(pin.entity, pin.revision)
            covered = tuple(sorted(str(pk) for pk in candidates if requests.covers(materials, pk)))
            pin = replace(pin, covered_materials=covered)
        bound.append(pin)
    return bound


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
            covered_materials=p.covered_materials,
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
            covered_materials=i.covered_materials,
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
            *([] if p.covered_materials is None else [list(p.covered_materials)]),
        ]
        for p in pins
    )


def _included_path(pin: Pin | RunInclusion) -> str:
    return f"/{pin.input}/{pin.position}/included/{pin.field}/{pin.index}"


def _value_path(prefix: str, at: tuple[str | int, ...]) -> str:
    return f"{prefix}/values/{'/'.join(map(str, at))}"


def reference_mask(
    project: Project,
    entities: Mapping[str, Sequence[Entity]],
    entry_revisions: Mapping[UUID, int | None],
    pins: Sequence[Pin],
) -> list[str]:
    """Pin the positions shown as unreadable in the command's input document."""
    from iki_server.entities.services import values_and_entry_at

    slots: dict[str, UUID] = {}
    entries: set[UUID] = set()

    def collect(entity: Entity, revision: int, entry_revision: int | None, prefix: str) -> None:
        values, entry = values_and_entry_at(entity, revision, entry_revision)
        fields = shape(entity.type).fields()
        slots.update(
            (_value_path(prefix, at), pk) for at, pk in reference_positions(fields, values)
        )
        if entry is not None:
            entries.add(entry[0])

    for name, group in entities.items():
        for index, given in enumerate(group):
            collect(given, given.revision, entry_revisions[given.pk], f"/{name}/{index}")
    for pin in pins:
        collect(pin.entity, pin.revision, pin.entry_revision, _included_path(pin))
    readable = entries | access.project_reference_ids(project, set(slots.values()) - entries)
    return sorted(path for path, pk in slots.items() if pk not in readable)


class Unavailable(Refused):
    """The project's readers must be able to receive the complete pinned document.

    ``ends_with`` says why an attempt that meets it ends: ``file_quarantined`` or
    ``file_not_scanned`` for a file its scan holds (ADR 0216), else ``not_allowed``. (Not
    ``code``: the API reads a refusal's ``code`` as its problem's.)"""

    def __init__(
        self,
        detail: str = "The run needs every pinned input, reference and file in its project.",
        *,
        ends_with: str = "not_allowed",
    ) -> None:
        super().__init__(detail)
        self.ends_with = ends_with


class WaitingForScan(Unavailable):
    """Everything else is available, but files it reads are still being scanned (ADR
    0234): it waits for them, rather than ending. ``found`` is its manifest, ``pending``
    the files whose scans aren't done."""

    def __init__(self, found: Manifest, pending: set[UUID]) -> None:
        many = len(pending) > 1
        super().__init__(
            f"{'Files it reads are' if many else 'A file it reads is'} being scanned for "
            "malware. It starts once the scan finds "
            f"{'them' if many else 'it'} clean.",
            ends_with="file_scanning",
        )
        self.found = found
        self.pending = pending


@dataclass
class Manifest:
    """The command's exact input records and files at the run's pins."""

    inputs: dict[str, list[dict[str, Any]]]
    input_files: set[UUID] = field(default_factory=set)
    param_files: set[UUID] = field(default_factory=set)


def manifest(run: ActionRun) -> Manifest:
    """Read and authorize the same pinned documents for launch, reuse and each lease."""
    from iki_server.entities import requests
    from iki_server.entities.services import values_and_entry_at
    from iki_server.runs import input_files

    imported = run.imported or {}
    if imported.get("inputs") or imported.get("included"):
        raise Unavailable()
    result = Manifest(
        {name: [] for name in run.spec["inputs"]}, param_files=input_files.of_params(run)
    )
    references: set[UUID] = set()
    entries: set[UUID] = set()
    records: set[UUID] = set()
    mask = set(run.reference_mask)

    def document(row: RunInput | RunInclusion, prefix: str) -> dict[str, Any]:
        entity = row.entity
        if entity.purged_at is not None:
            raise Unavailable()
        records.add(entity.pk)
        values, entry = values_and_entry_at(entity, row.revision, row.entry_revision)
        fields = shape(entity.type).fields()
        shown = {n: to_json(fields[n], v) for n, v in values.items() if n in fields}
        for at, pk in reference_positions(fields, values):
            path = _value_path(prefix, at)
            if path in mask:
                containing: Any = shown
                for key in at[:-1]:
                    containing = containing[key]
                containing[at[-1]] = UNREADABLE_REFERENCE
                mask.remove(path)
            else:
                references.add(pk)
        for name in shown:
            result.input_files.update(input_files.file_ids(fields[name], values[name]))
        item = {
            "id": str(entity.pk),
            "hrid": entity.hrid,
            "type": entity.type,
            "schema_version": registry.types()[entity.type].schema_version,
            "revision": row.revision,
            "values": shown,
        }
        amount = registry.effective(entity.type).amount
        if amount is not None and amount.material is not None:
            item["material_field"] = amount.material
        if (sides := requests.run_sides([entity]).get(entity.pk)) is not None:
            item["request"] = sides
        if entity.entry_id is not None and entry is not None:
            entry_id, _ = entry
            entries.add(entry_id)
            item["entry_id"] = str(entry_id)
            if row.entry_revision is not None:
                item["entry_revision"] = row.entry_revision
            if entity.entry_id != entry_id:
                item["hrid"] = Entity.objects.values_list("hrid", flat=True).get(pk=entry_id)
        return item

    nested: dict[tuple[str, int], dict[str, list[dict[str, Any]]]] = defaultdict(dict)
    for pin in run.inclusions.select_related("entity").order_by(
        "input", "position", "field", "index"
    ):
        item = document(pin, _included_path(pin))
        if pin.covered_materials is not None:
            references.update(UUID(pk) for pk in pin.covered_materials)
            item["covered_materials"] = pin.covered_materials
        nested[(pin.input, pin.position)].setdefault(pin.field, []).append(item)
    for given in run.inputs.select_related("entity").order_by("name", "position"):
        item = document(given, f"/{given.name}/{given.position}")
        if includes := run.spec["inputs"].get(given.name, {}).get("include"):
            pinned = nested[(given.name, given.position)]
            item["included"] = {name: pinned.get(name, []) for name in includes}
        result.inputs.setdefault(given.name, []).append(item)

    # The record's pinned revision authorizes its historical registry identity after a move.
    named = records | (references - entries)
    if mask or access.project_reference_ids(run.project, named) != named:
        raise Unavailable()
    if result.param_files and access.project_files(run.project).filter(
        pk__in=result.param_files
    ).count() != len(result.param_files):
        raise Unavailable()
    ids = result.param_files | result.input_files
    if ids:
        files = {f.pk: f for f in File.objects.filter(pk__in=ids).only("pk", "name", "blob_id")}
        if ids != files.keys():
            raise Unavailable()
        scans = statuses_for_blobs({f.blob_id for f in files.values()})
        pending: set[UUID] = set()
        # A held file ends it at once, whatever else is still being scanned.
        for pk in sorted(ids):
            scan = scans[files[pk].blob_id]
            if scan.usable:
                continue
            if scan.state == "pending":
                pending.add(pk)
                continue
            try:
                scan.require_usable()
            except Problem as error:
                raise Unavailable(held(files[pk].name, scan), ends_with=error.code) from error
        if pending:
            raise WaitingForScan(result, pending)
    return result


def still_scanning(
    action: Action,
    entities: Mapping[str, Sequence[Entity]],
    entry_revisions: Mapping[UUID, int | None],
    pins: Sequence[Pin],
    params: Mapping[str, Any],
) -> bool:
    """Whether a run of `action` with these would read a file whose scan isn't done: so it
    can't reuse an identical run's results until it is (ADR 0234). It reads them as the
    manifest would."""
    from iki_server.entities.services import values_and_entry_at
    from iki_server.runs import input_files

    ids: set[UUID] = set()
    for name, f in action.params.items():
        if params.get(name) is not None:
            ids |= input_files.file_ids(f, params[name])
    read = [(e, e.revision, entry_revisions.get(e.pk)) for g in entities.values() for e in g]
    read += [(p.entity, p.revision, p.entry_revision) for p in pins]
    for entity, revision, entry_revision in read:
        values, _ = values_and_entry_at(entity, revision, entry_revision)
        fields = shape(entity.type).fields()
        for name, value in values.items():
            if name in fields:
                ids |= input_files.file_ids(fields[name], value)
    if not ids:
        return False
    blobs = set(File.objects.filter(pk__in=ids).values_list("blob_id", flat=True))
    return any(s.state == "pending" for s in statuses_for_blobs(blobs).values())


def held(name: str, scan: ScanStatus) -> str:
    """Why a run can't read a file its scan holds (ADR 0216), and what can be done."""
    if scan.state == "infected":
        return (
            f"{name} is quarantined: the malware scanner flagged it. A Quarantine Admin can "
            "review it under Admin → Security → Quarantine; once it's released, run this again."
        )
    if scan.reason == "too_large":
        return (
            f"{name} is held: it's larger than this deployment scans, and its policy holds "
            "files it can't scan."
        )
    return f"{name} is held: the malware scanner couldn't inspect it completely."
