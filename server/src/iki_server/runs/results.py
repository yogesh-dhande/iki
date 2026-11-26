"""Writing a run's results: checked against what its action declared, then all or nothing
(ADR 0044).

The command's ``output.json`` (``run-output/v1``) says which input entities it changes
and which entities it creates. Before anything is written, every part is checked against
the action as it was launched:

- it updates only inputs the action declares it updates, only the entities it was given
  there, and only the declared fields
- it creates only the declared types (or their subtypes), no more than the cardinality,
  each derived from input entities the action says it derives from

Then, in one transaction, every value is checked against its entity type as installed
now, written as the person who launched the run (with ``via="run"``), and recorded:
which revision each entity got from the run (``RunEffect``, the provenance of the values
it set) and which inputs, at which revisions, each new entity came from (``Lineage``).

The launcher's access is checked again: a run doesn't outlive the role that started it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from django.db import transaction

from iki_server.amounts import services as amounts
from iki_server.api.problems import Problem
from iki_server.entities import access, assignments, holds, registry, unique
from iki_server.entities import services as entities
from iki_server.entities.identity import hash_of, identity_of
from iki_server.entities.models import Entity, UniqueValue
from iki_server.entities.values import Invalid, shape, subtypes
from iki_server.files.models import File
from iki_server.files.scan_state import require_usable
from iki_server.org import permissions
from iki_server.org.models import registry_of
from iki_server.org.services import Refused
from iki_server.runs import input_files, makings
from iki_server.runs.models import (
    ActionRun,
    EffectKind,
    ExternalOrigin,
    Lineage,
    Recalculation,
    RunEffect,
    RunInput,
    RunUpload,
)
from iki_server.runs.services import TIER_PERMISSIONS, is_a
from iki_spec import outputs, schemas
from iki_spec.model import Action, Creates, Software, action_from_dict

__all__ = [
    "Check",
    "Checked",
    "Failed",
    "InvalidOutput",
    "Rejected",
    "check_line",
    "checked",
    "commit",
    "invalid",
    "pointer",
    "write",
]

#: The most problems one output reports; past this, the rest are summed up.
MAX_ERRORS = 100


class InvalidOutput(Exception):  # noqa: N818 - reads as "the output is invalid"
    """The output doesn't fit what the action declared, or its entity types."""

    def __init__(self, errors: list[dict[str, str]]) -> None:
        self.errors = errors[:MAX_ERRORS]
        if len(errors) > MAX_ERRORS:
            self.errors.append(
                {"pointer": "#", "detail": f"…and {len(errors) - MAX_ERRORS} more problems."}
            )
        super().__init__(f"The output has {len(errors)} problem(s).")


class Failed(Exception):  # noqa: N818 - reads as "the run failed"
    """The command said, with an ``error`` message about the run's entity, that it failed
    on it (ADR 0055). Nothing is written."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class Rejected(Exception):  # noqa: N818 - reads as "the results were rejected"
    """The results fit, but can't be written now: say, an entity was archived meanwhile."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Check:
    run: ActionRun
    action: Action
    #: input name → entity ID → the revision the run read
    inputs: dict[str, dict[UUID, int]]
    errors: list[dict[str, str]] = field(default_factory=list)
    #: (Unique column, supplied value) → first inline external create.
    external_keys: dict[tuple[str, str], str] = field(default_factory=dict)
    #: Project record → first inline create with a supplied external match key (ADR 0082).
    external_creates: dict[UUID, str] = field(default_factory=dict)
    _files: set[UUID] | None = None
    _inputs_read: bool = False

    def error(self, pointer: str, detail: str) -> None:
        self.errors.append({"pointer": pointer, "detail": detail})

    def may_attach(self, file_id: UUID) -> bool:
        """Results may attach the run's own uploads, the files its file params name (such as
        a structure file it imports), and files its inputs already had: not any file the
        launcher can read, which would share it into the run's project."""
        if self._files is None:
            self._files = set(
                RunUpload.objects.filter(run=self.run, upload__file__isnull=False).values_list(
                    "upload__file_id", flat=True
                )
            ) | input_files.of_params(self.run)
        if file_id in self._files or self._inputs_read:
            return file_id in self._files
        self._inputs_read = True
        read = {e: r for given in self.inputs.values() for e, r in given.items()}
        for entity in Entity.objects.filter(pk__in=read, purged_at__isnull=True):
            fields = shape(entity.type).fields()
            for name, value in entities.values_at(entity, read[entity.pk]).items():
                if name in fields:
                    self._files |= input_files.file_ids(fields[name], value)
        return file_id in self._files


def _check_files(check: Check, at: str, type_name: str, values: Mapping[str, Any]) -> None:
    if type_name not in registry.types():
        return  # reported when it's written
    fields = shape(type_name).fields()
    for name, value in values.items():
        f = fields.get(name)
        if f is None:
            continue
        if any(not check.may_attach(file_id) for file_id in input_files.file_ids(f, value)):
            check.error(
                f"{at}/values/{name}",
                "Attach only files this run uploaded (once they're ready), its file params' "
                "files, or its inputs' files.",
            )


@dataclass
class Checked:
    """A run's output, checked against its action: ready to write."""

    check: Check
    updates: list[_Update]
    creates: list[_Create]
    #: Creates in a file (ADR 0056): a bulk import writes them, with these.
    creates_file: UUID | None


def commit(run: ActionRun, document: Mapping[str, Any]) -> None:
    """Writes the run's results, or nothing. Call inside a transaction holding the run.

    Raises :class:`InvalidOutput`, :class:`Rejected` or :class:`Failed`; whichever,
    nothing is written. A ``creates_file`` isn't read here: a bulk import writes its
    creates, with the rest (:mod:`iki_server.runs.imports`).
    """
    write(checked(run, document))


def write(
    done: Checked,
    *,
    save_effects: Callable[[list[RunEffect]], object] = RunEffect.objects.bulk_create,
) -> dict[str, UUID]:
    """Writes what :func:`checked` checked. Raises :class:`InvalidOutput` (writing nothing)
    for values that don't fit the entities' types as installed now. Returns what each
    keyed create made or found, by its key, for a creates file's lines (ADR 0140).
    A bulk import stages the effects until it has seen what the file's lines filled."""
    from iki_server.runs import triggers

    check = done.check
    # Each person a run assigns records to is told once, of them all (ADR 0122).
    with (
        transaction.atomic(),
        triggers.writing(check.run),
        assignments.batch(check.run.requested_by),
    ):
        verified = _current_calculation_inputs(check)
        effects = _write_updates(check, done.updates)
        made = _write_creates(check, done.creates, effects)
        if check.errors:
            raise InvalidOutput(check.errors)  # rolls back what was written
        if effects:
            save_effects(effects)
        if verified:
            Recalculation.objects.filter(
                entity_id__in=verified, action=check.action.qualified_name
            ).delete()
    return made


def checked(run: ActionRun, document: Mapping[str, Any]) -> Checked:
    """The output checked against the run's action and its launcher's access now. Raises
    :class:`InvalidOutput`, :class:`Rejected` or :class:`Failed`."""
    issues = schemas.check("run-output", document)
    if issues:
        raise InvalidOutput([{"pointer": _pointer(*i.path), "detail": i.message} for i in issues])
    user = run.requested_by
    if not user.is_active:
        raise Rejected("not_allowed", f"{user.name} was deactivated while the run ran.")
    if run.project.archived_at is not None:
        raise Rejected("not_allowed", "Its project was archived while the run ran.")
    if not permissions.can(user, TIER_PERMISSIONS[run.tier], run.project):
        raise Rejected("not_allowed", f"{user.name} can no longer run this action in its project.")
    inputs: dict[str, dict[UUID, int]] = {}
    for row in RunInput.objects.filter(run=run).order_by("name", "position"):
        inputs.setdefault(row.name, {})[row.entity_id] = row.revision
    check = Check(run, action_from_dict(run.spec), inputs)
    for problem in outputs.created_refs_in_updates(
        check.action, document.get("updates") or {}, INSTALLED
    ):
        check.error(problem.pointer, problem.detail)
    updates = _updates(check, document.get("updates") or {})
    creates = _creates(check, document.get("creates") or [])
    creates_file = _creates_file(check, document.get("creates_file"))
    _messages(check, document.get("messages") or [])
    if check.errors:
        raise InvalidOutput(check.errors)
    for message in document.get("messages") or []:
        if message["level"] == "error" and "entity" in message:
            raise Failed(message["text"])
    return Checked(check, updates, creates, creates_file)


def _creates_file(check: Check, given: object) -> UUID | None:
    if given is None:
        return None
    if not isinstance(given, str):
        # The worker uploads it and puts its ID here.
        check.error(_pointer("creates_file"), "Give the uploaded file's ID.")
        return None
    try:
        file_id = UUID(given)
    except ValueError:
        check.error(_pointer("creates_file"), "Give the uploaded file's ID.")
        return None
    own = (
        RunUpload.objects.filter(run=check.run, upload__file_id=file_id)
        .select_related("upload__file__blob")
        .first()
    )
    if own is None or own.upload.file is None:
        check.error(_pointer("creates_file"), "Give a file this run uploaded, once it's ready.")
        return None
    try:
        require_usable(own.upload.file.blob)
    except Problem as error:
        check.error(_pointer("creates_file"), error.detail)
        return None
    return file_id


pointer = outputs.pointer
_pointer = pointer


class _Installed:
    """:class:`outputs.Types`, from the types this deployment has installed."""

    def is_a(self, type_name: str, wanted: str) -> bool:
        return is_a(type_name, wanted)

    def ref_fields(self, type_name: str) -> frozenset[str]:
        if type_name not in registry.types():
            return frozenset()  # as its create then reports
        return frozenset(
            name
            for name, f in shape(type_name).fields().items()
            if f.type == "ref"
            or (f.type == "list" and f.items is not None and f.items.type == "ref")
        )

    def fillable(self, type_name: str) -> tuple[frozenset[str], frozenset[str]] | None:
        if type_name not in registry.types():
            return None
        form = shape(type_name)
        return frozenset(identity_of(form.entity_type)), frozenset(form.entry_fields())


INSTALLED = _Installed()


# --- Checking against the action ---------------------------------------------------------------


@dataclass(frozen=True)
class _Update:
    pointer: str
    entity: UUID
    values: Mapping[str, Any]
    expected_revision: int | None


def _updates(check: Check, given: Mapping[str, Any]) -> list[_Update]:
    declared = {u.input: set(u.fields) for u in check.action.updates}
    out: list[_Update] = []
    for name, items in given.items():
        if name not in declared:
            allowed = ", ".join(sorted(declared)) or "none"
            check.error(
                _pointer("updates", name),
                f"The action doesn't declare that it updates `{name}` (it updates: {allowed}).",
            )
            continue
        seen: set[UUID] = set()
        for index, item in enumerate(items):
            at = ("updates", name, index)
            entity = UUID(item["entity"])
            if entity not in check.inputs.get(name, {}):
                check.error(_pointer(*at, "entity"), f"It wasn't given as `{name}`.")
                continue
            if entity in seen:
                check.error(_pointer(*at, "entity"), "Each entity can be updated once.")
                continue
            seen.add(entity)
            if "reason" in item and item["reason"] != check.run.change_reason:
                check.error(
                    _pointer(*at, "reason"), "Only the operator’s launch reason may be recorded."
                )
            for field_name in item["values"]:
                if field_name not in declared[name]:
                    check.error(
                        _pointer(*at, "values", field_name),
                        f"The action doesn't declare that it sets `{field_name}`.",
                    )
            entity_type = Entity.objects.values_list("type", flat=True).get(pk=entity)
            _check_files(check, _pointer(*at), entity_type, item["values"])
            revision = check.inputs[name][entity] if item.get("require_unchanged") else None
            out.append(_Update(_pointer(*at), entity, item["values"], revision))
    return out


def _messages(check: Check, given: list[Mapping[str, Any]]) -> None:
    read = {entity for entities in check.inputs.values() for entity in entities}
    for index, item in enumerate(given):
        if "entity" in item and UUID(item["entity"]) not in read:
            check.error(_pointer("messages", index, "entity"), "It isn't one of the run's inputs.")


@dataclass(frozen=True)
class _Create:
    pointer: str
    type: str
    values: Mapping[str, Any]
    #: parent entity → (input name, revision read)
    parents: dict[UUID, tuple[str, int]]
    #: Where it came from, checked: ExternalOrigin's fields.
    origin: dict[str, Any] | None = None
    #: Its name in ``creates``, which later creates' ref fields can give (``$create``).
    key: str | None = None
    #: The unique field an entity in the run's project it matches has (ADR 0082).
    match_on: str | None = None
    #: The action's software that made it (ADR 0106): what Iki records it was made with.
    made_with: tuple[Software, ...] = ()
    #: Its values that only fill an entry's empty fields (ADR 0140).
    fills: frozenset[str] = frozenset()
    #: Physical inventory is recorded and placed only when this fresh create succeeds.
    receipt: Mapping[str, Any] | None = None
    destination: UUID | None = None


def _origin(check: Check, at: tuple[object, ...], given: Mapping[str, Any]) -> dict[str, Any]:
    """An entity's origin as ExternalOrigin keeps it. The schema checked its shape."""
    origin: dict[str, Any] = {
        "source": given["source"],
        "identifier": given["id"],
        "version": given.get("version", ""),
        "url": given.get("url", ""),
        "sha256": given.get("sha256", ""),
        "retrieved_at": None,
        "file_id": None,
    }
    if "retrieved_at" in given:
        try:
            when = dt.datetime.fromisoformat(given["retrieved_at"])
        except ValueError:
            when = None
        if when is None or when.tzinfo is None:
            check.error(
                _pointer(*at, "origin", "retrieved_at"),
                "Give a date and time with a time zone, such as 2026-11-11T09:30:00Z.",
            )
        else:
            origin["retrieved_at"] = when
    if "file" in given:
        file_id = UUID(given["file"])
        if file_id not in input_files.of_params(check.run):
            check.error(
                _pointer(*at, "origin", "file"),
                "Name a file the run was given in a file param.",
            )
        origin["file_id"] = file_id
    origin["made"] = given.get("made")
    return origin


def _made_with(
    check: Check, at: tuple[object, ...], given: list[str] | None
) -> tuple[Software, ...]:
    """The action's software that made a create: all of it, unless the output names some."""
    if given is None:
        return check.action.software
    declared = {tool.name.casefold(): tool for tool in check.action.software}
    if not declared:
        check.error(
            _pointer(*at, "made_with"),
            "The action lists no `software`: list what its runs make entities with, then name "
            "which made this one.",
        )
        return ()
    out: list[Software] = []
    for index, name in enumerate(given):
        tool = declared.get(name.casefold())
        if tool is None:
            listed = ", ".join(t.name for t in check.action.software)
            check.error(
                _pointer(*at, "made_with", index),
                f"`{name}` isn't among the action's software ({listed}).",
            )
        else:
            out.append(tool)
    return tuple(out)


def _creates(check: Check, given: list[Mapping[str, Any]]) -> list[_Create]:
    counts: dict[int, int] = {}
    keys: set[str] = set()
    out: list[_Create] = []
    for index, item in enumerate(given):
        at = ("creates", index)
        outputs.keys(item, at, keys, INSTALLED, check.error)
        create = _create(check, item, at, counts)
        if create is not None:
            out.append(create)
    return out


def created_refs(item: Mapping[str, Any]) -> list[tuple[str, str, tuple[int, ...]]]:
    """Each ``{"$create": key}`` in a create's ref fields (and lists of refs): (field, key,
    list index if any)."""
    return outputs.created_refs(item, INSTALLED)


def check_line(
    check: Check, item: Mapping[str, Any], line: int, keys: frozenset[str] = frozenset()
) -> _Create | None:
    """One line of a ``creates_file``, as an item of ``creates`` is checked; its problems
    point at ``#/creates_file/<line>``. Its values are checked as it's written. ``keys``:
    the keys of the output's ``creates``, which a line may refer to (ADR 0140)."""
    at = ("creates_file", line)
    found = outputs.creates_line(check.action, item, at, keys, INSTALLED, check.error)
    if found is None:
        return None
    return _create(check, item, at, None, found)


def _create(
    check: Check,
    item: Mapping[str, Any],
    at: tuple[object, ...],
    counts: dict[int, int] | None,
    found: tuple[int, Creates] | None = None,
) -> _Create | None:
    """``counts``: how many of each declared effect so far; None for a creates file, whose
    line ``found`` is the declared create :func:`outputs.creates_line` found it is."""
    if found is None:
        found = outputs.declared(check.action, item, at, counts, INSTALLED, check.error)
    if found is None:
        return None
    _, effect = found
    if counts is None:
        _no_files(check, _pointer(*at), item["type"], item["values"])
    else:
        _check_files(check, _pointer(*at), item["type"], item["values"])
    fills = outputs.fills(item, at, INSTALLED, check.error)
    parents = _parents(check, effect, item.get("derived_from"), at)
    origin = _origin(check, at, item["origin"]) if "origin" in item else None
    made_with = _made_with(check, at, item.get("made_with"))
    if parents is None:
        return None
    destination = None
    if effect.receive_into is not None:
        ids = check.inputs.get(effect.receive_into, {})
        if len(ids) != 1:
            check.error(
                _pointer(*at, "receipt"), "The run must have exactly one destination container."
            )
            return None
        destination = next(iter(ids))
    return _Create(
        _pointer(*at),
        item["type"],
        item["values"],
        parents,
        origin,
        item.get("key"),
        effect.match_on,
        made_with,
        fills,
        item.get("receipt"),
        destination,
    )


def _no_files(check: Check, at: str, type_name: str, values: Mapping[str, Any]) -> None:
    if type_name not in registry.types():
        return  # Writing it says there's no such type.
    fields = shape(type_name).fields()
    for name, value in values.items():
        f = fields.get(name)
        if f is not None and input_files.files_in(f, value):
            check.error(
                f"{at}/values/{name}",
                "Creates in a file can't attach files: give these in `creates`.",
            )


def _parents(
    check: Check, effect: Creates, given: list[str] | None, at: tuple[object, ...]
) -> dict[UUID, tuple[str, int]] | None:
    allowed = {
        entity: (name, revision)
        for name in effect.derived_from
        for entity, revision in check.inputs.get(name, {}).items()
    }
    if given is None:
        if len(allowed) > 1:
            check.error(
                _pointer(*at, "derived_from"),
                f"Say which input entities it was made from: its inputs "
                f"({', '.join(effect.derived_from)}) hold {len(allowed)}.",
            )
            return None
        return allowed
    parents: dict[UUID, tuple[str, int]] = {}
    for index, raw in enumerate(given):
        entity = UUID(raw)
        if entity not in allowed:
            names = ", ".join(effect.derived_from) or "none"
            check.error(
                _pointer(*at, "derived_from", index),
                f"It isn't one of the inputs {effect.type} is derived from ({names}).",
            )
            continue
        parents[entity] = allowed[entity]
    return parents


# --- Writing -----------------------------------------------------------------------------------


def invalid(check: Check, at: str, error: Invalid, *, values: str = "values") -> None:
    """Notes an entity's problems (field → messages) at ``at``, the create or update."""
    for name, messages in error.errors.items():
        where = f"{at}/type" if name == "type" else f"{at}/{values}/{name}"
        check.error(where, " ".join(messages))


def entry_made(saved: entities.Saved) -> dict[str, Any]:
    """A run effect's registry entry and the entry's revision the save made (ADR 0115), if
    it made one: so the entry's values name the run, as the record's do."""
    made = saved.entry_revision
    if made is None:
        return {}
    return {"entry_id": made.entity_id, "entry_revisions": [made.number]}


def _current_calculation_inputs(check: Check) -> set[UUID]:
    """Lock calculation inputs and their evidence through commit. This also protects
    results from older worker versions that did not request a revision check."""
    from iki_server.runs import triggers

    names = {
        t.input
        for t in triggers.triggers().values()
        if (t.recheck or t.watch_included)
        and t.plugin == check.action.plugin
        and t.run == check.action.name
    }
    if not names:
        return set()
    pins = {}
    direct = set()
    for source in check.run.inputs.filter(name__in=names).select_related("entity"):
        direct.add(source.entity_id)
        pins[source.entity_id] = source.revision
        if source.entity.entry_id and source.entry_revision is not None:
            pins[source.entity.entry_id] = source.entry_revision
    for evidence in check.run.inclusions.filter(input__in=names).select_related("entity"):
        pins[evidence.entity_id] = evidence.revision
        if evidence.entity.entry_id and evidence.entry_revision is not None:
            pins[evidence.entity.entry_id] = evidence.entry_revision
    for entity in Entity.objects.select_for_update(no_key=True).filter(pk__in=pins).order_by("pk"):
        if entity.revision != pins[entity.pk]:
            raise Rejected("conflict", f"{entity.hrid} changed after this calculation read it.")
    return direct


def _write_updates(check: Check, updates: list[_Update]) -> list[RunEffect]:
    effects = []
    user = check.run.requested_by
    for update in updates:
        entity = Entity.objects.select_for_update().get(pk=update.entity)
        if not access.can_edit(user, entity, acting_in=check.run.project):
            raise Rejected(
                "not_allowed", f"{user.name} can no longer edit {entity.hrid}, which it updates."
            )
        try:
            saved = entities.update(
                user,
                entity,
                update.values,
                expected_revision=(
                    entity.revision
                    if update.expected_revision is None
                    else update.expected_revision
                ),
                by_run=True,
                reason=check.run.change_reason,
                hold_reason=holds.run_reason(check.run),
                acting_in=check.run.project,
            )
        except Invalid as error:
            invalid(check, update.pointer, error)
            continue
        except entities.Stale as error:
            raise Rejected(
                "conflict",
                f"{entity.hrid} changed after this run read revision {update.expected_revision}. "
                f"It is at revision {error.current} now.",
            ) from None
        except Refused as error:
            raise Rejected("conflict", f"{entity.hrid}: {error}") from None
        effects.append(
            RunEffect(
                run=check.run,
                entity_id=entity.pk,
                kind=EffectKind.UPDATED,
                revision=saved.revision.number if saved.revision is not None else None,
                **entry_made(saved),
            )
        )
    return effects


def _with_created(check: Check, create: _Create, made: dict[str, UUID]) -> dict[str, Any] | None:
    """Its values with each ``{"$create": key}`` replaced by the entity that create made,
    or None (with the problem noted) if that create couldn't be written."""
    values = dict(create.values)
    refs = created_refs({"type": create.type, "values": values})
    for name, key, where in refs:
        if key not in made:
            check.error(
                "/".join((create.pointer, "values", name, *map(str, where))),
                f"It refers to the create with the key {key!r}, which couldn't be written.",
            )
            return None
    for name, key, where in refs:
        if where:
            values[name] = list(values[name])
            values[name][where[0]] = str(made[key])
        else:
            values[name] = str(made[key])
    return values


def _kind(saved: entities.Saved, *, matched: bool) -> str:
    if matched:
        # A record that came again unchanged was found, as a reused duplicate is.
        return EffectKind.UPDATED if saved.revision is not None else EffectKind.REUSED
    return EffectKind.REUSED if saved.reused else EffectKind.CREATED


def _external_key(create: _Create) -> tuple[str, str] | None:
    """The unique column and its value, including when subtypes inherit that column."""
    field = create.match_on
    if field is None or create.values.get(field) is None or create.type not in registry.types():
        return None
    form = shape(create.type)
    if field not in form.fields():
        return None
    return unique.key(form.part_of(field).model, field), str(create.values[field])


def same_entity(run: ActionRun, type_name: str, values: Mapping[str, Any]) -> Entity | None:
    """The run's project's record of the registry entry a create would register again
    (ADR 0115), locked: a record with no match yet claims it (ADR 0082)."""
    entity_type = registry.types().get(type_name)
    if entity_type is None or not (identity := identity_of(entity_type)):
        return None
    # Match the identity as registration stores it, including resolved refs and file hashes.
    form = shape(type_name)
    fields = form.fields()
    refs = access.refs_for(run.requested_by, run.project)
    given = {}
    try:
        for name in identity:
            f = fields[name]
            value = values.get(name, f.default)
            if value is None and f.required:
                return None
            given[name] = form.field_value(f, value, refs)
        digest = hash_of(
            given,
            entity_type,
            lambda pk: File.objects.values_list("blob__sha256", flat=True).get(pk=pk),
        )
    except ValueError:
        return None  # The regular create reports invalid identity fields at their pointers.
    entries = Entity.objects.filter(
        registry_id=registry_of(run.project), identity_hash=digest
    ).values("pk")
    return (
        Entity.objects.select_for_update()
        .filter(owner_project=run.project, entry__in=entries, archived_at__isnull=True)
        .order_by("created_at", "pk")
        .first()
    )


def _repeated_target(check: Check, create: _Create, entity_id: UUID) -> bool:
    first = check.external_creates.get(entity_id)
    if first is None:
        return False
    check.error(
        f"{create.pointer}/values/{create.match_on}",
        f"It resolves to the same record as {first}. Each external record may come only once.",
    )
    return True


def _external_record(
    check: Check, create: _Create, values: Mapping[str, Any], key: tuple[str, str]
) -> Entity | None:
    """Lock the project's own matched record, or claim its registration if still unassigned."""
    run = check.run
    found = unique.owned(run.project, *key)
    match = Entity.objects.select_for_update().filter(pk__in=found).first()
    target = match if match is not None else same_entity(run, create.type, values)
    if target is None or _repeated_target(check, create, target.pk):
        return None
    if match is not None:
        return match

    field = create.match_on
    assert field is not None  # noqa: S101 - an external key has a match field
    holder = (
        target.entry_id
        if target.entry_id is not None and shape(create.type).part_of(field).entry
        else target.pk
    )
    other = (
        UniqueValue.objects.filter(entity_id=holder, key=key[0])
        .values_list("value", flat=True)
        .first()
    )
    if other is None:
        return target
    owner = registry.identity_owner(registry.types()[create.type])
    same = " and ".join(owner.identity) if owner is not None else "identity"
    check.error(
        f"{create.pointer}/values/{field}",
        f"It has the same {same} as {target.hrid}, whose {field} is {other} already: "
        "they'd be one entity, which can't be two records.",
    )
    return None


def update_match(
    check: Check, at: str, type_name: str, entity: Entity, values: Mapping[str, Any]
) -> entities.Saved | None:
    """Writes a create's values to the entity it matches, as a revision by the run. Notes
    why it can't, or raises :class:`Rejected` if the launcher can no longer edit it."""
    user = check.run.requested_by
    if entity.type not in subtypes(type_name):
        check.error(
            at + "/type",
            f"It matches {entity.hrid}, a {entity.type}, which isn't a {type_name}.",
        )
        return None
    if not access.can_edit(user, entity, acting_in=check.run.project):
        raise Rejected(
            "not_allowed", f"{user.name} can no longer edit {entity.hrid}, which it matches."
        )
    try:
        return entities.update(
            user,
            entity,
            values,
            expected_revision=entity.revision,
            by_run=True,
            reason=check.run.change_reason,
            hold_reason=holds.run_reason(check.run),
            acting_in=check.run.project,
        )
    except Invalid as error:
        invalid(check, at, error)
    except Refused as error:
        check.error(at, f"It matches {entity.hrid}, which can't change now: {error}")
    return None


def _place_received(
    check: Check, create: _Create, saved: entities.Saved, receipt: Mapping[str, Any]
) -> None:
    from iki_server.storage import services as storage

    if saved.reused or create.destination is None:
        raise Rejected("conflict", "A delivery must create a fresh record in its input container.")
    container = Entity.objects.get(pk=create.destination)
    record = Entity.objects.get(pk=saved.entity.pk)
    storage.move(
        check.run.requested_by,
        [storage.Wanted(record, container, receipt.get("position"))],
        reason=receipt["reason"],
        confirmations={record.pk: receipt.get("confirmation", "")},
        operation=check.run.pk,
    )


def _write_creates(
    check: Check, creates: list[_Create], effects: list[RunEffect]
) -> dict[str, UUID]:
    run = check.run
    by_entity = {effect.entity_id: effect for effect in effects}
    #: key → the entity its create made or reused
    made: dict[str, UUID] = {}
    for create in creates:
        values = _with_created(check, create, made)
        if values is None:
            continue
        key = _external_key(create)
        match = None
        if key is not None:
            first = check.external_keys.setdefault(key, create.pointer)
            if first != create.pointer:
                check.error(
                    f"{create.pointer}/values/{create.match_on}",
                    f"{first} has the same {create.match_on}. "
                    "Each external record may come only once.",
                )
                continue
            errors = len(check.errors)
            match = _external_record(check, create, values, key)
            if len(check.errors) > errors:
                continue
        if match is not None:
            if create.receipt is not None:
                raise Rejected("conflict", "A receipt cannot match an existing stock record.")
            saved = update_match(check, create.pointer, create.type, match, values)
            if saved is None:
                continue
        else:
            receipt = create.receipt
            try:
                saved = entities.create(
                    run.requested_by,
                    create.type,
                    run.project,
                    values,
                    by_run=True,
                    hold_reason=holds.run_reason(run),
                    fills=create.fills,
                    amount=receipt["amount"] if receipt else None,
                    amount_reason=receipt["reason"] if receipt else None,
                    reason=run.change_reason,
                )
            except amounts.Wrong as error:
                invalid(check, create.pointer, error, values="receipt")
                continue
            except Invalid as error:
                invalid(check, create.pointer, error)
                continue
        if create.receipt is not None:
            try:
                _place_received(check, create, saved, create.receipt)
            except Invalid as error:
                for messages in error.errors.values():
                    check.error(f"{create.pointer}/receipt", " ".join(messages))
                continue
        entity_id = saved.entity.pk
        if key is not None:
            # Registration may have found a record created since our lookup.
            if match is None and _repeated_target(check, create, entity_id):
                continue
            check.external_creates[entity_id] = create.pointer
        if create.key is not None:
            made[create.key] = entity_id
        effect = by_entity.get(entity_id)
        if effect is not None:
            # The same record, made twice: the same identity, registered again. Where this
            # one came from is kept too: one molecule is in several databases' records,
            # each the source of what's known of it (ADR 0154). What it filled in of its
            # entry was this run's too.
            if saved.entry_revision is not None:
                effect.entry_id = saved.entry_revision.entity_id
                effect.entry_revisions.append(saved.entry_revision.number)
            if create.origin is not None:
                again = ExternalOrigin.objects.create(
                    run=run, entity_id=entity_id, revision=None, **create.origin
                )
                makings.record(run.pk, entity_id, (), again)
            continue
        revision = saved.revision.number if saved.revision is not None else None
        effect = RunEffect(
            run=run,
            entity_id=entity_id,
            kind=_kind(saved, matched=match is not None),
            revision=revision,
            **entry_made(saved),
        )
        effects.append(effect)
        by_entity[entity_id] = effect
        origin = None
        if create.origin is not None:
            # Kept for reused entities too: the run found the same record again.
            origin = ExternalOrigin.objects.create(
                run=run, entity_id=entity_id, revision=revision, **create.origin
            )
        makings.record(run.pk, entity_id, create.made_with, origin)
        Lineage.objects.bulk_create(
            Lineage(
                entity_id=entity_id,
                parent_id=parent,
                parent_revision=revision,
                run=run,
                input=name,
            )
            for parent, (name, revision) in create.parents.items()
        )
    return made
