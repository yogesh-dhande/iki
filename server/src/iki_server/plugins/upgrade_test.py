"""Upgrading a plugin's sample data, for ``iki plugin test --upgrade-from`` (ADR 0094).

The CLI builds the plugin as it was at an earlier commit, and as it is now, then runs
this once per step, each with its build on the ``PYTHONPATH``::

    python -m iki_server.plugins.upgrade_test before|after|rollback STATE

``STATE`` is a JSON file, read and written back. ``before`` migrates the earlier build
and creates the ``samples``, each in two revisions that hold different values where it
can, and keeps what each revision of their history holds; ``after`` migrates the current
one and checks they're intact, each revision of their history holding what it did, as
plugins' migrations have renamed and converted it since (ADR 0148); ``rollback`` deploys
the earlier one again and checks their values, and their history's, came back. Each adds
what's wrong to ``problems``.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from iki_server.plugins.operations import same_value
from iki_spec.builtin import using_core
from iki_spec.model import Field, included_reference, stored_in_list_table

if TYPE_CHECKING:
    from iki_server.entities.models import Entity


def main(argv: list[str] | None = None) -> int:
    step, state_file, *core = argv if argv is not None else sys.argv[1:]
    os.environ["DJANGO_SETTINGS_MODULE"] = "iki_server.plugins.harness_settings"
    import django

    with using_core(Path(core[0]) if core else None):
        django.setup()
        _run(step, Path(state_file))
    return 0


def _run(step: str, state_file: Path) -> None:
    state: dict[str, Any] = json.loads(state_file.read_text("utf-8"))
    state.setdefault("problems", [])
    log = io.StringIO()
    try:
        _deploy(log)
        {"before": _before, "after": _after, "rollback": _rollback}[step](state)
    except _StepError as stop:
        state["problems"].append(str(stop))
    finally:
        state.setdefault("logs", {})[step] = log.getvalue()
        state_file.write_text(json.dumps(state, indent=2, default=str), "utf-8")


class _StepError(Exception):
    """The step can't go on: the message says why."""


def _deploy(log: io.StringIO) -> None:
    """Migrates and syncs, as a deploy does: one database role, so no grants."""
    from iki_server.entities import constraints, unique
    from iki_server.plugins import sync
    from iki_server.plugins.deploy import DeployError, migrate_deployment

    try:
        report = migrate_deployment(log)
    except (DeployError, unique.Clash, constraints.Clash) as error:
        raise _StepError(f"The deploy refused it: {error}") from None
    except Exception as error:
        raise _StepError(f"A migration failed: {type(error).__name__}: {error}") from None
    log.write("".join(f"{line}\n" for line in sync.describe(report.synced)))


def _before(state: dict[str, Any]) -> None:
    from django.utils import timezone

    from iki_server.accounts.models import User
    from iki_server.entities import services
    from iki_server.org.models import Project, ProjectMembership, ProjectRole, Team
    from iki_server.setup.models import Installation
    from iki_server.signatures.services import history_seen

    Installation.objects.update_or_create(
        pk=1, defaults={"organization_name": "Upgrade test", "setup_completed_at": timezone.now()}
    )
    user = User.objects.create_user("harness@example.invalid", "Upgrade test")
    project = Project.objects.create(team=Team.objects.create(name="Upgrade test"), name="Samples")
    ProjectMembership.objects.create(project=project, user=user, role=ProjectRole.ADMIN)
    made = []
    for sample in state["samples"]:
        entity = _make(user, project, sample)
        history = [services.values_and_entry_at(entity, n) for n in range(1, entity.revision + 1)]
        made.append(
            {
                **_read(entity),
                "from": sample["from"],
                "units": _units(entity),
                "history": [_as_json(entity, values) for values, _ in history],
                "entry_revisions": [entry[1] if entry else None for _, entry in history],
                "after": sample.get("after", {}),
            }
        )
    state["made"] = made
    # What migrations do to kept history from here is what the samples' is read through.
    state["history_seen"] = history_seen()


def _make(user: Any, project: Any, sample: dict[str, Any]) -> Any:  # noqa: ANN401 - Django models
    """A sample, in two revisions where it can be: first with only what it needs, its
    required fields and identity, then with the rest. Its history then holds two sets of
    values, so one that's lost, or read from the wrong revision, shows."""
    from iki_server.entities import services
    from iki_server.entities.identity import identity_of
    from iki_server.entities.registry import types
    from iki_server.entities.services import Invalid
    from iki_server.entities.values import shape

    entity_type = types().get(sample["type"])
    fields = shape(sample["type"]).fields() if entity_type is not None else {}
    values = _with_included(user, project, sample, fields)
    needed = set(identity_of(entity_type)) if entity_type is not None else set()
    first = {k: v for k, v in values.items() if k in needed or (k in fields and fields[k].required)}
    later = {k: v for k, v in values.items() if k not in first}
    try:
        if later:
            try:
                saved = services.create(user, sample["type"], project, first, by_run=True)
            except Invalid:
                pass  # it needs more than its required fields together: in one revision, then
            else:
                entity = cast("Entity", saved.entity)
                if saved.reused:  # another sample's, as it was: as one create would leave it
                    return entity
                return services.update(
                    user, entity, later, expected_revision=entity.revision, by_run=True
                ).entity
        return services.create(user, sample["type"], project, values, by_run=True).entity
    except Invalid as invalid:
        raise _StepError(
            f"The sample from {sample['from']} isn't a valid {sample['type']} at the earlier "
            f"commit: {invalid}"
        ) from None


def _with_included(
    user: Any,  # noqa: ANN401 - Django model
    project: Any,  # noqa: ANN401 - Django model
    sample: dict[str, Any],
    fields: dict[str, Field],
) -> dict[str, Any]:
    values = dict(sample["values"])
    for reference, named in sample.get("included", {}).items():
        field = included_reference(fields, reference)
        target = field.items if field is not None and field.type == "list" else field
        if field is None or target is None or target.type != "ref":
            raise _StepError(
                f"The sample from {sample['from']} has no reference {reference} "
                "at the earlier commit."
            )
        ids = [str(_make(user, project, other).pk) for other in named]
        if "." in reference:
            rows, _, cell = reference.partition(".")
            values[rows] = [
                {**row, cell: ids[i] if i < len(ids) else row.get(cell)}
                for i, row in enumerate(values.get(rows) or [])
            ]
        elif field.type == "list":
            values[reference] = ids
        else:
            values[reference] = ids[0] if ids else None
    return values


def _units(entity: Any) -> dict[str, str]:  # noqa: ANN401 - an Entity
    """The unit each quantity is kept in, as a migration converting it says it was."""
    from iki_server.entities.values import shape

    units = {}
    for name, field in shape(entity.type).fields().items():
        measured = field.items if field.type == "list" and field.items is not None else field
        if measured.unit:
            units[name] = measured.unit
    return units


def _read(entity: Any) -> dict[str, Any]:  # noqa: ANN401 - an Entity
    from iki_server.entities import services
    from iki_server.entities.models import Revision

    values = services.current_values_of([entity]).get(entity.pk, {})
    return {
        "id": str(entity.pk),
        "hrid": entity.hrid,
        "type": entity.type,
        "revision": entity.revision,
        "revisions": Revision.objects.filter(entity=entity).count(),
        "values": _as_json(entity, values),
    }


def _as_json(entity: Any, values: dict[str, Any]) -> dict[str, Any]:  # noqa: ANN401 - an Entity
    from iki_server.entities.values import shape, to_json

    fields = shape(entity.type).fields()
    return {n: to_json(fields[n], v) for n, v in values.items() if n in fields}


def _history(state: dict[str, Any], sample: dict[str, Any], entity: Any, when: str) -> None:  # noqa: ANN401 - an Entity
    """Whether each revision of the sample's history holds what it did before the upgrade,
    as plugins' migrations have kept it since: their renames and conversions (ADR 0148)."""
    from iki_server.entities import services
    from iki_server.entities.values import shape
    from iki_server.signatures import proof
    from iki_server.signatures.services import history_changes

    changes = [c.change() for c in history_changes(entity, state.get("history_seen", 0))]
    fields = shape(entity.type).fields()
    for number, then in enumerate(sample.get("history", ()), start=1):
        try:
            entry_revision = sample["entry_revisions"][number - 1]
            now = _as_json(entity, services.values_at(entity, number, entry_revision))
        except Exception as error:
            state["problems"].append(
                f"{sample['hrid']}'s revision {number} can't be read{when}: {error}"
            )
            continue
        try:
            kept = proof.migrated({"values": then, "units": sample.get("units", {})}, changes)
        except proof.Unexplained as unexplained:
            state["problems"].append(
                f"{sample['hrid']}'s revision {number} doesn't follow from what it held{when}: "
                f"{unexplained}"
            )
            continue
        expected = kept["values"]
        lost = sorted(
            n for n in expected if n in fields and not same_value(now.get(n), expected[n])
        )
        for name in now.keys() - expected.keys():
            if now[name] is not None and not same_value(now[name], _initial_value(fields[name])):
                state["problems"].append(
                    f"{sample['hrid']}'s earlier revision {number} gained {name} "
                    f"{json.dumps(now[name])}{when}."
                )
        for name in lost:
            state["problems"].append(
                f"{sample['hrid']}'s revision {number} ({sample['from']}) held {name} "
                f"{json.dumps(expected[name])}, and holds {json.dumps(now.get(name))}{when}."
            )


def _initial_value(field: Field) -> Any:  # noqa: ANN401 - JSON
    if field.has_default:
        return field.default
    if field.type == "state":
        return field.initial
    return [] if stored_in_list_table(field) else None


def _after(state: dict[str, Any]) -> None:
    from iki_server.entities.history import FieldNames, history_changes
    from iki_server.entities.models import Entity, RevisionKind
    from iki_server.entities.values import shape
    from iki_server.signatures import proof

    for sample in state.get("made", ()):
        entity = Entity.objects.filter(pk=sample["id"]).first()
        if entity is None:
            state["problems"].append(f"{sample['hrid']} ({sample['from']}) is gone.")
            continue
        now = _read(entity)
        revisions = list(entity.revisions.filter(number__gt=sample["revision"]).order_by("number"))
        expected_numbers = range(sample["revision"] + 1, now["revision"] + 1)
        if (
            now["revisions"] != sample["revisions"] + len(revisions)
            or now["revision"] < sample["revision"]
            or [r.number for r in revisions] != list(expected_numbers)
            or any(
                r.kind not in (RevisionKind.MIGRATED, RevisionKind.STATE_CHANGED)
                or r.via != "system"
                or r.user_id
                or not r.reason
                for r in revisions
            )
        ):
            state["problems"].append(
                f"{entity.hrid} has unexplained changes to its revision trail."
            )
        _history(state, sample, entity, "")
        changes = [c.change() for c in history_changes(entity, state["history_seen"])]
        try:
            before = proof.migrated(
                {"values": sample["values"], "units": sample["units"]}, changes
            )["values"]
        except proof.Unexplained as exc:
            state["problems"].append(f"{entity.hrid}: {exc}")
            continue
        fields = shape(entity.type).fields()
        names = FieldNames.for_type(entity.type)
        attributed = {n for r in revisions for n in names.changed(r)}
        if entity.entry is not None:
            entry_names = FieldNames.for_type(entity.type, entry=True)
            entry_changes = entity.entry.revisions.filter(
                number__gt=sample["entry_revisions"][-1], kind=RevisionKind.MIGRATED
            )
            attributed.update(n for r in entry_changes for n in entry_names.changed(r))
        declared = sample["after"]
        for name, value in declared.items():
            if name not in fields or not same_value(value, now["values"].get(name)):
                state["problems"].append(
                    f"{entity.hrid}'s after expects {name} {json.dumps(value)}, "
                    f"and the record holds {json.dumps(now['values'].get(name))}."
                )
        for name, value in now["values"].items():
            expected = before[name] if name in before else _initial_value(fields[name])
            if same_value(value, expected):
                continue
            if name not in attributed:
                state["problems"].append(
                    f"{entity.hrid}'s {name} changed without a migration revision."
                )
            if name not in declared:
                state["problems"].append(
                    f"Declare {entity.hrid}'s expected {name} in after on an upgrade fixture."
                )


def _rollback(state: dict[str, Any]) -> None:
    from iki_server.entities.models import Entity

    for sample in state.get("made", ()):
        entity = Entity.objects.filter(pk=sample["id"]).first()
        if entity is None:
            state["problems"].append(f"{sample['hrid']} is gone after the rollback.")
            continue
        back = _read(entity)["values"]
        lost = sorted(n for n, v in sample["values"].items() if not same_value(back.get(n), v))
        if lost:
            state["problems"].append(
                f"After the rollback, {sample['hrid']} ({sample['from']}) doesn't have the "
                f"{', '.join(lost)} it had."
            )
        _history(state, sample, entity, " after the rollback")


if __name__ == "__main__":
    sys.exit(main())
