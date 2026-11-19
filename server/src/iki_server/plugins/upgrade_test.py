"""Upgrading a plugin's sample data, for ``iki plugin test --upgrade-from`` (ADR 0094).

The CLI builds the plugin as it was at an earlier commit, and as it is now, then runs
this once per step, each with its build on the ``PYTHONPATH``::

    python -m iki_server.plugins.upgrade_test before|after|rollback STATE

``STATE`` is a JSON file, read and written back. ``before`` migrates the earlier build
and creates the ``samples``; ``after`` migrates the current one and checks they're intact;
``rollback`` deploys the earlier one again and checks their values came back. Each adds
what's wrong to ``problems``.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from typing import Any


def main(argv: list[str] | None = None) -> int:
    step, state_file = (argv if argv is not None else sys.argv[1:])[:2]
    os.environ["DJANGO_SETTINGS_MODULE"] = "iki_server.plugins.harness_settings"
    import django

    django.setup()
    state: dict[str, Any] = json.loads(Path(state_file).read_text("utf-8"))
    state.setdefault("problems", [])
    log = io.StringIO()
    try:
        _deploy(log)
        {"before": _before, "after": _after, "rollback": _rollback}[step](state)
    except _StepError as stop:
        state["problems"].append(str(stop))
    finally:
        state.setdefault("logs", {})[step] = log.getvalue()
        Path(state_file).write_text(json.dumps(state, indent=2, default=str), "utf-8")
    return 0


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
    from iki_server.entities.services import Invalid
    from iki_server.org.models import Project, ProjectMembership, ProjectRole, Team
    from iki_server.setup.models import Installation

    Installation.objects.update_or_create(
        pk=1, defaults={"organization_name": "Upgrade test", "setup_completed_at": timezone.now()}
    )
    user = User.objects.create_user("harness@example.invalid", "Upgrade test")
    project = Project.objects.create(team=Team.objects.create(name="Upgrade test"), name="Samples")
    ProjectMembership.objects.create(project=project, user=user, role=ProjectRole.ADMIN)
    made = []
    for sample in state["samples"]:
        try:
            saved = services.create(user, sample["type"], project, sample["values"], by_run=True)
        except Invalid as invalid:
            raise _StepError(
                f"The sample from {sample['from']} isn't a valid {sample['type']} at the earlier "
                f"commit: {invalid}"
            ) from None
        made.append({**_read(saved.entity), "from": sample["from"]})
    state["made"] = made


def _read(entity: Any) -> dict[str, Any]:  # noqa: ANN401 - an Entity
    from iki_server.entities import services
    from iki_server.entities.models import Revision
    from iki_server.entities.values import shape, to_json

    fields = shape(entity.type).fields()
    values = services.current_values_of([entity]).get(entity.pk, {})
    return {
        "id": str(entity.pk),
        "hrid": entity.hrid,
        "type": entity.type,
        "revision": entity.revision,
        "revisions": Revision.objects.filter(entity=entity).count(),
        "values": {n: to_json(fields[n], v) for n, v in values.items() if n in fields},
    }


def _after(state: dict[str, Any]) -> None:
    from iki_server.entities import services
    from iki_server.entities.models import Entity

    for sample in state.get("made", ()):
        entity = Entity.objects.filter(pk=sample["id"]).first()
        if entity is None:
            state["problems"].append(f"{sample['hrid']} ({sample['from']}) is gone.")
            continue
        now = _read(entity)
        if (now["revision"], now["revisions"]) != (sample["revision"], sample["revisions"]):
            state["problems"].append(
                f"{sample['hrid']} ({sample['from']}) was at revision {sample['revision']} with "
                f"{sample['revisions']} in its history, and is now at {now['revision']} with "
                f"{now['revisions']}."
            )
        for number in range(1, entity.revision + 1):
            try:
                services.values_at(entity, number)
            except Exception as error:
                state["problems"].append(
                    f"{sample['hrid']}'s revision {number} can't be read: {error}"
                )
        sample["after"] = now["values"]


def _rollback(state: dict[str, Any]) -> None:
    from iki_server.entities.models import Entity

    for sample in state.get("made", ()):
        entity = Entity.objects.filter(pk=sample["id"]).first()
        if entity is None:
            state["problems"].append(f"{sample['hrid']} is gone after the rollback.")
            continue
        back = _read(entity)["values"]
        lost = sorted(name for name, value in sample["values"].items() if back.get(name) != value)
        if lost:
            state["problems"].append(
                f"After the rollback, {sample['hrid']} ({sample['from']}) doesn't have the "
                f"{', '.join(lost)} it had."
            )


if __name__ == "__main__":
    sys.exit(main())
