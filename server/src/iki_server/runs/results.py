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

__all__ = ["InvalidOutput", "Rejected", "commit"]

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


class Rejected(Exception):  # noqa: N818 - reads as "the results were rejected"
    """The results fit, but can't be written now: say, an entity was archived meanwhile."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class _Check:
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
        """Results may attach the run's own uploads, and files its inputs already had: not
        any file the launcher can read, which would share it into the run's project."""
        if self._files is None:
            self._files = set(
                RunUpload.objects.filter(run=self.run, upload__file__isnull=False).values_list(
                    "upload__file_id", flat=True
                )
            )
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


def _check_files(check: _Check, at: str, type_name: str, values: Mapping[str, Any]) -> None:
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
                "Attach only files this run uploaded (once they're ready) or its inputs' files.",
            )


def commit(run: ActionRun, document: Mapping[str, Any]) -> None:
    """Writes the run's results, or nothing. Call inside a transaction holding the run.

    Raises :class:`InvalidOutput` or :class:`Rejected`; either way, nothing is written.
    """
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
    check = _Check(run, action_from_dict(run.spec), inputs)
    updates = _updates(check, document.get("updates") or {})
    creates = _creates(check, document.get("creates") or [])
    if check.errors:
        raise InvalidOutput(check.errors)
    with transaction.atomic():
        _write_updates(check, updates)
        _write_creates(check, creates)
        if check.errors:
            raise InvalidOutput(check.errors)  # rolls back what was written


def _pointer(*parts: object) -> str:
    return "#/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in parts)


# --- Checking against the action ---------------------------------------------------------------


@dataclass(frozen=True)
class _Update:
    pointer: str
    entity: UUID
    values: Mapping[str, Any]


def _updates(check: _Check, given: Mapping[str, Any]) -> list[_Update]:
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


@dataclass(frozen=True)
class _Create:
    pointer: str
    type: str
    values: Mapping[str, Any]
    #: parent entity → (input name, revision read)
    parents: dict[UUID, tuple[str, int]]
    #: Where it came from, checked: ExternalOrigin's fields.
    origin: dict[str, Any] | None = None


def _origin(check: _Check, at: tuple[object, ...], given: Mapping[str, Any]) -> dict[str, Any]:
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


def _creates(check: _Check, given: list[Mapping[str, Any]]) -> list[_Create]:
    counts: dict[int, int] = {}
    out: list[_Create] = []
    for index, item in enumerate(given):
        at = ("creates", index)
        found = next(
            ((n, c) for n, c in enumerate(check.action.creates) if is_a(item["type"], c.type)),
            None,
        )
        if found is None:
            allowed = ", ".join(c.type for c in check.action.creates) or "nothing"
            check.error(
                _pointer(*at, "type"),
                f"The action doesn't declare that it creates {item['type']} "
                f"(it creates: {allowed}).",
            )
            continue
        number, effect = found
        counts[number] = counts.get(number, 0) + 1
        if effect.cardinality == "one" and counts[number] > 1:
            check.error(_pointer(*at), f"The action creates at most one {effect.type} per run.")
            continue
        _check_files(check, _pointer(*at), item["type"], item["values"])
        parents = _parents(check, effect, item.get("derived_from"), at)
        origin = _origin(check, at, item["origin"]) if "origin" in item else None
        if parents is not None:
            out.append(_Create(_pointer(*at), item["type"], item["values"], parents, origin))
    return out


def _parents(
    check: _Check, effect: Creates, given: list[str] | None, at: tuple[object, ...]
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


def _invalid(check: _Check, at: str, error: Invalid, *, values: str = "values") -> None:
    for name, messages in error.errors.items():
        pointer = f"{at}/type" if name == "type" else f"{at}/{values}/{name}"
        check.error(pointer, " ".join(messages))


def _write_updates(check: _Check, updates: list[_Update]) -> None:
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
            _invalid(check, update.pointer, error)
            continue
        except Refused as error:
            raise Rejected("conflict", f"{entity.hrid}: {error}") from None
        RunEffect.objects.create(
            run=check.run,
            entity_id=entity.pk,
            kind=EffectKind.UPDATED,
            revision=saved.revision.number if saved.revision is not None else None,
        )


def _write_creates(check: _Check, creates: list[_Create]) -> None:
    run = check.run
    for create in creates:
        try:
            saved = entities.create(
                run.requested_by,
                create.type,
                run.project,
                create.values,
                # A run can't answer "this looks like a duplicate": types that only warn
                # get a new entity, as they would if the person went ahead.
                allow_duplicate=True,
                by_run=True,
            )
        except Invalid as error:
            _invalid(check, create.pointer, error)
            continue
        entity_id = saved.entity.pk
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
