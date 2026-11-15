"""Detached plugins (ADR 0091): their data stays and can be read, but nothing changes it."""

import os
from collections.abc import Callable, Iterator
from io import StringIO
from typing import Any, cast

import pytest
from django.core.management import call_command
from django.db import DatabaseError, connection, transaction
from org_world import World
from test_api_entities import api_as
from test_runs import COMPUTE, launch, protein
from test_workers import keep_connection, queued, worker, world

from iki_server.accounts.models import User
from iki_server.audit.models import AuditEvent
from iki_server.core import db_roles
from iki_server.entities import registry, services, values
from iki_server.entities.models import Entity
from iki_server.files.models import Blob, File
from iki_server.org.models import Project, ProjectMembership, ProjectRole, Team
from iki_server.org.services import Refused
from iki_server.plugins import sync
from iki_server.plugins.models import PluginState
from iki_server.runs import catalog, queue
from iki_server.runs.models import (
    ActionRun,
    FiringState,
    ProjectTrigger,
    RunStatus,
    TriggerFiring,
)
from iki_server.tasks.runner import Runner

pytestmark = pytest.mark.django_db

__all__ = ["keep_connection", "world"]  # fixtures, from test_workers

APP_ROLE = f"iki_test_detached_{os.environ.get('PYTEST_XDIST_WORKER', 'main')}"
PLUGINS = ("dna", "protein", "structure_prediction")


def record(states: dict[str, str] | None = None, version: str = "1.0.0") -> dict[str, Any]:
    """What ``iki deploy build`` writes as ``lock.json``: every fixture plugin, at a commit."""
    return {
        "plugins": [
            {
                "name": name,
                "state": (states or {}).get(name, "active"),
                "version": version,
                "sha": "a" * 40,
            }
            for name in PLUGINS
        ]
    }


@pytest.fixture
def deployed(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Installs the fixture plugins as a lockfile would, some of them detached."""

    def deploy(*detached: str, version: str = "1.0.0") -> None:
        found = record(dict.fromkeys(detached, "detached"), version)
        monkeypatch.setattr(registry, "installed", lambda: found)

    return deploy


@pytest.fixture
def lab() -> dict[str, Any]:
    team = Team.objects.create(name="Biologics")
    first = Project.objects.create(team=team, name="Antibodies", allow_sharing=True)
    second = Project.objects.create(team=team, name="Enzymes")
    alice = User.objects.create_user(email="alice@example.org", name="Alice")
    for project in (first, second):
        ProjectMembership.objects.create(project=project, user=alice, role=ProjectRole.EDITOR)
    return {"first": first, "second": second, "alice": alice}


def structure_file(project: Project) -> File:
    blob = Blob.objects.create(sha256="a" * 64, size=1024, storage_uri="s3://iki/a")
    return File.objects.create(
        blob=blob, name="1abc.pdb", media_type="chemical/x-pdb", project=project
    )


# --- A detached type's entities ----------------------------------------------------------


def test_a_detached_plugins_entities_can_be_read_not_changed(
    lab: dict[str, Any], deployed: Callable[..., None]
) -> None:
    alice, first, second = lab["alice"], lab["first"], lab["second"]
    saved = services.create(alice, "dna.DNASequence", first, {"bases": "ACGT", "name": "pUC19"})
    entity = cast("Entity", saved.entity)
    services.link(alice, entity, second)
    deployed(*PLUGINS)
    why = (
        "dna.DNASequence is from the dna plugin, which is detached: "
        "its entities can be read but not changed."
    )

    assert services.current_values(entity)["name"] == "pUC19"
    with pytest.raises(values.Invalid) as invalid:
        services.create(alice, "dna.DNASequence", first, {"bases": "GGCC"})
    assert invalid.value.errors == {"type": [why]}
    with pytest.raises(Refused, match=why):
        services.update(alice, entity, {"name": "pBR322"}, expected_revision=1)
    with pytest.raises(Refused, match=why):
        services.set_archived(alice, entity, True)
    third = Project.objects.create(team=first.team, name="Vaccines")
    ProjectMembership.objects.create(project=third, user=alice, role=ProjectRole.EDITOR)
    with pytest.raises(Refused, match=why):
        services.link(alice, entity, third)
    # Taking it out of a project only narrows who reads it.
    assert services.unlink(alice, entity, second)

    deployed()
    assert services.update(alice, entity, {"name": "pBR322"}, expected_revision=1).revision


def test_a_detached_extensions_fields_keep_their_values(
    lab: dict[str, Any], deployed: Callable[..., None]
) -> None:
    alice, first = lab["alice"], lab["first"]
    pdb = str(structure_file(first).pk)
    kept = cast(
        "Entity",
        services.create(
            alice, "protein.ProteinStructure", first, {"structure": pdb, "job_id": "af-1"}
        ).entity,
    )
    deployed("structure_prediction")
    why = ["is from the structure_prediction plugin, which is detached: it can't change."]

    # The type is protein's, so its own fields still change; the extension's don't.
    made = services.create(alice, "protein.ProteinStructure", first, {"structure": pdb})
    assert services.current_values(made.entity)["job_id"] is None
    with pytest.raises(values.Invalid) as invalid:
        services.create(alice, "protein.ProteinStructure", first, {"structure": pdb, "job_id": "x"})
    assert invalid.value.errors == {"job_id": why}
    edited = services.update(alice, kept, {"method": "xray"}, expected_revision=1)
    assert edited.revision is not None
    with pytest.raises(values.Invalid) as invalid:
        services.update(alice, kept, {"job_id": "af-2"}, expected_revision=2)
    assert invalid.value.errors == {"job_id": why}
    assert services.current_values(kept)["job_id"] == "af-1"


# --- The database --------------------------------------------------------------------------


@pytest.fixture
def as_app_role() -> Iterator[Callable[[str], None]]:
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE ROLE {APP_ROLE} NOLOGIN")

    def run(statement: str) -> None:
        # The test's transaction would keep the role past a statement that works.
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(f"SET LOCAL ROLE {APP_ROLE}")
            cursor.execute(statement)
            cursor.execute("RESET ROLE")

    yield run


def grant() -> None:
    call_command("grant_app_privileges", "--role", APP_ROLE, stdout=StringIO())


def problems() -> list[str]:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(f"SET LOCAL ROLE {APP_ROLE}")
        found = db_roles.problems()
        cursor.execute("RESET ROLE")
        return found


def test_a_detached_plugins_tables_are_read_only_to_the_app(
    as_app_role: Callable[[str], None], deployed: Callable[..., None]
) -> None:
    deployed("structure_prediction")
    tables = registry.detached_tables()
    assert tables
    assert all(t.startswith("structure_prediction_") for t in tables)
    grant()
    assert problems() == []
    for table in tables:
        as_app_role(f"SELECT count(*) FROM {table}")  # noqa: S608 - a table name from Django
        with pytest.raises(DatabaseError, match="permission denied"):
            as_app_role(f"DELETE FROM {table}")  # noqa: S608
    as_app_role("DELETE FROM protein_proteinstructure")  # still active
    with pytest.raises(DatabaseError, match="permission denied"):
        as_app_role("DELETE FROM iki_plugin_state")

    # Reattached, its tables can be written again once the deploy grants them.
    deployed()
    grant()
    as_app_role(f"DELETE FROM {tables[0]}")  # noqa: S608


def test_a_web_pod_that_could_write_a_detached_plugin_never_turns_ready(
    as_app_role: Callable[[str], None], deployed: Callable[..., None]
) -> None:
    grant()
    deployed("structure_prediction")
    found = problems()
    assert found
    assert all(p.split()[-1].startswith("structure_prediction_") for p in found)
    assert {p.split()[1] for p in found} == {"INSERT", "UPDATE", "DELETE"}


# --- Deploys are audited -------------------------------------------------------------------


def changes() -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (e.action, e.target_label, e.data)
        for e in AuditEvent.objects.filter(action__startswith="plugin.").order_by("occurred_at")
    ]


def test_each_deploy_records_what_it_changes_about_plugins(
    deployed: Callable[..., None],
) -> None:
    deployed()
    assert sync._states() == [
        "dna installed",
        "protein installed",
        "structure_prediction installed",
    ]
    assert sync._states() == []
    deployed("structure_prediction")
    assert sync._states() == ["structure_prediction detached"]
    deployed(version="1.1.0")
    assert sync._states() == [
        "dna updated",
        "protein updated",
        "structure_prediction reattached",
    ]
    assert changes()[3:] == [
        ("plugin.detached", "structure_prediction", {"state": ["active", "detached"]}),
        ("plugin.updated", "dna", {"version": ["1.0.0", "1.1.0"]}),
        ("plugin.updated", "protein", {"version": ["1.0.0", "1.1.0"]}),
        (
            "plugin.reattached",
            "structure_prediction",
            {"state": ["detached", "active"], "version": ["1.0.0", "1.1.0"]},
        ),
    ]
    assert changes()[0] == (
        "plugin.installed",
        "dna",
        {"state": "active", "version": "1.0.0", "sha": "a" * 40},
    )
    assert PluginState.objects.get(name="structure_prediction").state == "active"


def test_a_build_without_a_lockfile_records_nothing() -> None:
    assert registry.installed() is None
    assert sync._states() == []
    assert registry.detached() == frozenset()


# --- Nothing runs for it -------------------------------------------------------------------


def test_a_detached_plugins_actions_cant_be_launched(
    world: World, deployed: Callable[..., None]
) -> None:
    editor = api_as(world, "editor")
    abl = protein(editor, world.project)
    deployed("protein", "structure_prediction")
    assert catalog.availability(catalog.action(COMPUTE)) == catalog.Availability(  # type: ignore[arg-type]
        False,
        "The protein plugin is detached, so its actions can't run. Its data can still be "
        "read. An Iki administrator can reattach it in the deployment's lockfile.",
    )
    refused = launch(editor, world.project, {"protein": abl["id"]})
    assert refused.status_code == 409, refused.content
    assert "The protein plugin is detached" in refused.json()["detail"]
    listed = {a["name"]: a for a in editor.get("/actions").json()}
    assert listed[COMPUTE]["available"] is False


def test_runs_queued_before_it_was_detached_end_saying_why(
    world: World, deployed: Callable[..., None]
) -> None:
    first, second = queued(world), queued(world)
    pod = worker(world)
    deployed("protein", "structure_prediction")
    why = "The protein plugin was detached before it started, so it didn't run."

    # A worker that asks first is given nothing, and the run it would have had ends.
    assert pod.lease().status_code == 204
    leased = ActionRun.objects.get(pk=first["id"])
    assert (leased.status, leased.error) == (
        RunStatus.CANCELLED,
        {"code": "plugin_detached", "message": why},
    )
    # The maintenance job ends the rest.
    assert queue.end_detached() == 1
    swept = ActionRun.objects.get(pk=second["id"])
    assert (swept.status, swept.error) == (
        RunStatus.CANCELLED,
        {"code": "plugin_detached", "message": why},
    )
    events = AuditEvent.objects.filter(action="run.cancelled").order_by("occurred_at")
    assert [(e.actor, e.data) for e in events] == [(None, {"reason": why})] * 2
    assert queue.end_detached() == 0


def test_a_running_run_is_asked_to_stop(world: World, deployed: Callable[..., None]) -> None:
    run = queued(world)
    pod = worker(world)
    job = pod.lease().json()
    deployed("protein", "structure_prediction")
    assert queue.end_detached() == 1
    stopping = ActionRun.objects.get(pk=run["id"])
    assert stopping.cancel_requested_at is not None
    assert AuditEvent.objects.get(action="run.cancel_requested").data == {
        "reason": "The protein plugin was detached, so it was stopped."
    }
    assert pod.report(job, "heartbeat").json()["cancel_requested"] is True
    assert pod.report(job, "fail", reason="interrupted").json() == {"run_status": "cancelled"}
    assert ActionRun.objects.get(pk=run["id"]).status == RunStatus.CANCELLED
    assert queue.end_detached() == 0


def test_a_detached_plugins_triggers_are_skipped_and_stay_on(
    world: World, deployed: Callable[..., None]
) -> None:
    turned_on = api_as(world, "admin").post(
        f"/projects/{world.project.pk}/triggers/protein.weigh_new_proteins/enable", {}
    )
    assert turned_on.status_code == 200, turned_on.content
    protein(api_as(world, "editor"), world.project)
    deployed("protein", "structure_prediction")
    Runner(queues=["default"]).run_ready()
    firing = TriggerFiring.objects.get()
    assert (firing.state, firing.error) == (
        FiringState.SKIPPED,
        {
            "code": "plugin_detached",
            "message": "The protein plugin is detached, so its triggers don't run.",
        },
    )
    assert ProjectTrigger.objects.get().enabled
    assert ActionRun.objects.count() == 0
    # Nor can one be turned on.
    refused = api_as(world, "admin").post(
        f"/projects/{world.project.pk}/triggers/protein.weigh_changed_proteins/enable", {}
    )
    assert refused.status_code == 409, refused.content
    assert "The protein plugin is detached" in refused.json()["detail"]


# --- People can tell -----------------------------------------------------------------------


def test_the_api_says_what_a_detached_plugin_keeps(
    world: World, deployed: Callable[..., None]
) -> None:
    editor = api_as(world, "editor")
    abl = protein(editor, world.project)
    assert editor.get(f"/entities/{abl['id']}").json()["can"] == {"edit": True}
    deployed("structure_prediction")
    structure = editor.get("/entity-types/protein.ProteinStructure").json()
    assert structure["detached"] is False
    fields = {f["name"]: f["detached"] for f in structure["fields"]}
    assert fields["job_id"] is True
    assert fields["structure"] is False

    deployed("protein", "structure_prediction")
    assert editor.get("/entity-types/protein.Protein").json()["detached"] is True
    assert editor.get("/entity-types/dna.DNASequence").json()["detached"] is False
    assert editor.get(f"/entities/{abl['id']}").json()["can"] == {"edit": False}
