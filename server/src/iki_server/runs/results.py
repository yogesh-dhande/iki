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
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from django.db import transaction

from iki_server.entities import access, registry
from iki_server.entities import services as entities
from iki_server.entities.models import Entity
from iki_server.entities.values import Invalid, shape
from iki_server.org import permissions
from iki_server.org.services import Refused
from iki_server.runs import input_files
from iki_server.runs.models import (
    ActionRun,
    EffectKind,
    ExternalOrigin,
    Lineage,
    RunEffect,
    RunInput,
    RunUpload,
)
from iki_server.runs.services import TIER_PERMISSIONS, is_a
from iki_spec import schemas
from iki_spec.model import Action, Creates, action_from_dict

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
        for entity in Entity.objects.filter(pk__in=read):
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


def write(done: Checked) -> None:
    """Writes what :func:`checked` checked. Raises :class:`InvalidOutput` (writing nothing)
    for values that don't fit the entities' types as installed now."""
    from iki_server.runs import triggers

    check = done.check
    with transaction.atomic(), triggers.writing(check.run):
        _write_updates(check, done.updates)
        _write_creates(check, done.creates)
        if check.errors:
            raise InvalidOutput(check.errors)  # rolls back what was written


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
    own = RunUpload.objects.filter(run=check.run, upload__file_id=file_id).exists()
    if not own:
        check.error(_pointer("creates_file"), "Give a file this run uploaded, once it's ready.")
        return None
    return file_id


def pointer(*parts: object) -> str:
    """A JSON pointer into the output, as problems name where they are."""
    return "#/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in parts)


_pointer = pointer


# --- Checking against the action ---------------------------------------------------------------


@dataclass(frozen=True)
class _Update:
    pointer: str
    entity: UUID
    values: Mapping[str, Any]


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
            for field_name in item["values"]:
                if field_name not in declared[name]:
                    check.error(
                        _pointer(*at, "values", field_name),
                        f"The action doesn't declare that it sets `{field_name}`.",
                    )
            entity_type = Entity.objects.values_list("type", flat=True).get(pk=entity)
            _check_files(check, _pointer(*at), entity_type, item["values"])
            out.append(_Update(_pointer(*at), entity, item["values"]))
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
    return origin


def _creates(check: Check, given: list[Mapping[str, Any]]) -> list[_Create]:
    counts: dict[int, int] = {}
    keys: set[str] = set()
    out: list[_Create] = []
    for index, item in enumerate(given):
        at = ("creates", index)
        for name, key, where in created_refs(item):
            if key not in keys:
                check.error(
                    _pointer(*at, "values", name, *where),
                    f"No earlier create has the key {key!r}: a create can refer only to "
                    "those before it.",
                )
        create = _create(check, item, at, counts)
        if "key" in item:
            if item["key"] in keys:
                check.error(_pointer(*at, "key"), f"Another create has the key {item['key']!r}.")
            keys.add(item["key"])
        if create is not None:
            out.append(create)
    return out


def created_refs(item: Mapping[str, Any]) -> list[tuple[str, str, tuple[int, ...]]]:
    """Each ``{"$create": key}`` in a create's ref fields (and lists of refs): (field, key,
    list index if any)."""
    found: list[tuple[str, str, tuple[int, ...]]] = []
    refs = _ref_fields(item.get("type"))
    for name, value in (item.get("values") or {}).items():
        if name not in refs:
            continue
        if _is_created_ref(value):
            found.append((name, value["$create"], ()))
        elif isinstance(value, list):
            found.extend(
                (name, v["$create"], (i,)) for i, v in enumerate(value) if _is_created_ref(v)
            )
    return found


def _ref_fields(type_name: object) -> set[str]:
    """Its type's ref fields and lists of refs; none if it isn't installed (as its create
    then reports)."""
    if not isinstance(type_name, str) or type_name not in registry.types():
        return set()
    return {
        name
        for name, f in shape(type_name).fields().items()
        if f.type == "ref" or (f.type == "list" and f.items is not None and f.items.type == "ref")
    }


def _is_created_ref(value: object) -> bool:
    return (
        isinstance(value, dict) and set(value) == {"$create"} and isinstance(value["$create"], str)
    )


def check_line(check: Check, item: Mapping[str, Any], line: int) -> _Create | None:
    """One line of a ``creates_file``, as an item of ``creates`` is checked; its problems
    point at ``#/creates_file/<line>``. Its values are checked as it's written."""
    return _create(check, item, ("creates_file", line), None)


def _create(
    check: Check,
    item: Mapping[str, Any],
    at: tuple[object, ...],
    counts: dict[int, int] | None,
) -> _Create | None:
    """``counts``: how many of each declared effect so far; None for a creates file."""
    found = next(
        ((n, c) for n, c in enumerate(check.action.creates) if is_a(item["type"], c.type)),
        None,
    )
    if found is None:
        allowed = ", ".join(c.type for c in check.action.creates) or "nothing"
        check.error(
            _pointer(*at, "type"),
            f"The action doesn't declare that it creates {item['type']} (it creates: {allowed}).",
        )
        return None
    number, effect = found
    if effect.cardinality == "one":
        if counts is None:
            check.error(
                _pointer(*at),
                f"The action creates at most one {effect.type} per run: give it in `creates`.",
            )
            return None
        counts[number] = counts.get(number, 0) + 1
        if counts[number] > 1:
            check.error(_pointer(*at), f"The action creates at most one {effect.type} per run.")
            return None
    if counts is None:
        if "key" in item or created_refs(item):
            check.error(
                _pointer(*at),
                "A creates file's lines can't refer to one another: give creates that do "
                "(with `key` and `$create`) in `creates`.",
            )
            return None
        _no_files(check, _pointer(*at), item["type"], item["values"])
    else:
        _check_files(check, _pointer(*at), item["type"], item["values"])
    parents = _parents(check, effect, item.get("derived_from"), at)
    origin = _origin(check, at, item["origin"]) if "origin" in item else None
    if parents is None:
        return None
    return _Create(_pointer(*at), item["type"], item["values"], parents, origin, item.get("key"))


def _no_files(check: Check, at: str, type_name: str, values: Mapping[str, Any]) -> None:
    if type_name not in registry.types():
        return  # Writing it says there's no such type.
    fields = shape(type_name).fields()
    for name, value in values.items():
        f = fields.get(name)
        holds_files = f is not None and (
            f.type == "file"
            or (f.type == "list" and f.items is not None and f.items.type == "file")
        )
        if holds_files and value not in (None, []):
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


def _write_updates(check: Check, updates: list[_Update]) -> None:
    user = check.run.requested_by
    for update in updates:
        entity = Entity.objects.select_for_update().get(pk=update.entity)
        if not access.can_edit(user, entity):
            raise Rejected(
                "not_allowed", f"{user.name} can no longer edit {entity.hrid}, which it updates."
            )
        try:
            saved = entities.update(
                user,
                entity,
                update.values,
                expected_revision=entity.revision,
                by_run=True,
            )
        except Invalid as error:
            invalid(check, update.pointer, error)
            continue
        except Refused as error:
            raise Rejected("conflict", f"{entity.hrid}: {error}") from None
        RunEffect.objects.create(
            run=check.run,
            entity_id=entity.pk,
            kind=EffectKind.UPDATED,
            revision=saved.revision.number if saved.revision is not None else None,
        )


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


def _write_creates(check: Check, creates: list[_Create]) -> None:
    run = check.run
    #: key → the entity its create made or reused
    made: dict[str, UUID] = {}
    for create in creates:
        values = _with_created(check, create, made)
        if values is None:
            continue
        try:
            saved = entities.create(
                run.requested_by,
                create.type,
                run.project,
                values,
                # A run can't answer "this looks like a duplicate": types that only warn
                # get a new entity, as they would if the person went ahead.
                allow_duplicate=True,
                by_run=True,
            )
        except Invalid as error:
            invalid(check, create.pointer, error)
            continue
        entity_id = saved.entity.pk
        if create.key is not None:
            made[create.key] = entity_id
        if RunEffect.objects.filter(run=run, entity_id=entity_id).exists():
            # The same entity, made twice: its type reuses duplicates.
            continue
        revision = saved.revision.number if saved.revision is not None else None
        RunEffect.objects.create(
            run=run,
            entity_id=entity_id,
            kind=EffectKind.REUSED if saved.reused else EffectKind.CREATED,
            revision=revision,
        )
        if create.origin is not None:
            # Kept for reused entities too: the run found the same record again.
            ExternalOrigin.objects.create(
                run=run, entity_id=entity_id, revision=revision, **create.origin
            )
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
