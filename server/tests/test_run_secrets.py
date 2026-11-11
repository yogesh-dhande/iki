"""Run secrets (ADR 0054): the deployment keeps each plugin's secrets; a run gets only those
its action declares, and only their names are kept or shown."""

import dataclasses
from pathlib import Path

import pytest
from org_world import World
from test_api_entities import api_as
from test_runs import world
from test_workers import keep_connection, queued, worker

from iki_server.runs import catalog, secrets
from iki_server.runs.models import ActionRun, Job
from iki_spec.model import Secret

pytestmark = pytest.mark.django_db

__all__ = ["keep_connection", "world"]  # the fixtures, used here too


@pytest.fixture
def kept(tmp_path: Path, settings: object) -> Path:
    """The deployment's run secrets: an empty directory until a test adds some."""
    settings.IKI_RUN_SECRETS_DIR = str(tmp_path)  # type: ignore[attr-defined]
    return tmp_path


def test_an_action_needing_a_secret_the_deployment_hasnt_set_cant_run(kept: Path) -> None:
    compute = catalog.action("protein.compute_properties")
    assert compute is not None
    action = dataclasses.replace(
        compute,
        secrets=(Secret("LAB_TOKEN"), Secret("ELN_TOKEN"), Secret("NCBI_API_KEY", required=False)),
    )
    assert catalog.availability(action).reason == (
        "It needs `LAB_TOKEN` and `ELN_TOKEN`, which this deployment hasn't set. An Iki "
        "administrator can add them to the run secrets as `protein.LAB_TOKEN`, "
        "`protein.ELN_TOKEN`."
    )
    (kept / "protein.LAB_TOKEN").write_text("lab")
    (kept / "protein.ELN_TOKEN").write_text("eln")
    # The optional one may stay unset.
    assert catalog.availability(action).available


def test_each_plugin_gets_only_its_own_secrets(kept: Path) -> None:
    (kept / "dna.LAB_TOKEN").write_text("dna's\n")
    (kept / "protein.EMPTY").write_text("\n")
    (kept / "protein.HUGE").write_bytes(b"x" * (secrets.MAX_BYTES + 1))
    declared = ["LAB_TOKEN", {"name": "EMPTY"}, {"name": "HUGE"}]
    assert secrets.for_run("protein", declared) == {}
    assert secrets.for_run("dna", declared) == {"LAB_TOKEN": "dna's"}


def test_a_run_gets_the_secrets_it_declares_and_keeps_only_their_names(
    world: World, kept: Path
) -> None:
    (kept / "protein.LAB_TOKEN").write_text("s3cr3t\n")
    (kept / "protein.OTHER").write_text("not for it")
    run = queued(world)
    spec = ActionRun.objects.get(pk=run["id"]).spec
    declared = [{"name": "LAB_TOKEN", "required": True, "description": ""}, "UNSET"]
    ActionRun.objects.filter(pk=run["id"]).update(spec={**spec, "secrets": declared})
    job = worker(world).lease().json()
    assert job["job"]["secrets"] == {"LAB_TOKEN": "s3cr3t"}
    assert Job.objects.get(pk=job["job"]["id"]).secrets == ["LAB_TOKEN"]
    (attempt,) = api_as(world).get(f"/runs/{run['id']}/attempts").json()
    assert attempt["secrets"] == ["LAB_TOKEN"]
    assert "s3cr3t" not in str(api_as(world).get(f"/runs/{run['id']}").json())


def test_runs_that_declare_no_secrets_get_none(world: World, kept: Path) -> None:
    (kept / "protein.LAB_TOKEN").write_text("s3cr3t")
    queued(world)
    job = worker(world).lease().json()
    assert "secrets" not in job["job"]
    assert Job.objects.get(pk=job["job"]["id"]).secrets == []


def test_actions_say_which_secrets_the_deployment_has_set_never_their_values(
    world: World, kept: Path
) -> None:
    def declared() -> list[dict[str, object]]:
        found: list[dict[str, object]] = (
            api_as(world, "viewer").get("/actions/protein.fetch_proteins").json()["secrets"]
        )
        return found

    assert declared() == [
        {
            "name": "UNIPROT_TOKEN",
            "required": False,
            "description": "A token for UniProt's higher rate limits.",
            "key": "protein.UNIPROT_TOKEN",
            "set": False,
        }
    ]
    (kept / "protein.UNIPROT_TOKEN").write_text("t0ken")
    assert declared()[0]["set"] is True
    assert "t0ken" not in str(declared())
