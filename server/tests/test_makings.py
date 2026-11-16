"""How entities were made, and who says so (ADR 0106): a run of an action that lists its
software records what it made with it; an import keeps what its record says about how its
source made the entity, as that source's word."""

from typing import Any

import pytest
from org_world import World
from test_api_entities import api_as
from test_run_imports import SHA, importing, stored
from test_run_results import done, output
from test_runs import world
from test_workers import keep_connection, worker

from iki_server.core import db_roles
from iki_server.entities.models import ProjectLink
from iki_server.org.models import Project, ProjectMembership, ProjectRole
from iki_server.runs.models import ActionRun, EntityMaking

pytestmark = pytest.mark.django_db

__all__ = ["keep_connection", "world"]  # the fixtures, used here too

SEQUENCE = {"type": "protein.Protein", "values": {"name": "ABL1", "sequence": "MKVLEIC"}}
ALPHAFOLD_DB = {
    "source": "AlphaFold DB",
    "id": "AF-P69905-F1",
    "version": "4",
    "url": "https://alphafold.ebi.ac.uk/entry/P69905",
    "sha256": SHA,
}


@pytest.fixture(autouse=True)
def storage(s3: Any) -> Any:
    return s3


def ran(
    world: World, software: list[dict[str, str]], *created: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A run of an action that lists ``software``, completed with ``created``."""
    run = importing(world, stored(world)).json()
    kept = ActionRun.objects.get(pk=run["id"])
    kept.spec["software"] = software
    kept.save(update_fields=["spec"])
    pod = worker(world)
    job = pod.lease().json()
    return run, done(pod, job, output(creates=list(created)))


def made(world: World, run: dict[str, Any], principal: str = "viewer") -> list[dict[str, Any]]:
    (effect,) = api_as(world).get(f"/runs/{run['id']}").json()["effects"]
    body: list[dict[str, Any]] = (
        api_as(world, principal).get(f"/entities/{effect['hrid']}/making").json()
    )
    return body


def test_a_run_records_what_its_actions_software_made(world: World) -> None:
    run, answer = ran(world, [{"name": "ImmuneBuilder", "version": "1.2"}], SEQUENCE)

    assert answer["run_status"] == "succeeded", answer
    (row,) = made(world, run)
    assert row == {
        "by": "iki",
        "kind": "computational",
        "method": "",
        "software": "ImmuneBuilder",
        "version": "1.2",
        "run": {"id": run["id"], "hrid": run["hrid"], "action": "protein.import_proteins"},
        "source": None,
        "recorded_at": row["recorded_at"],
    }


def test_an_action_that_lists_no_software_records_nothing(world: World) -> None:
    run, answer = ran(world, [], SEQUENCE)

    assert answer["run_status"] == "succeeded", answer
    assert made(world, run) == []


def test_a_run_names_which_of_its_actions_software_made_each_entity(world: World) -> None:
    software = [{"name": "ABodyBuilder2", "version": "1.2"}, {"name": "Vina", "version": "1.2.5"}]

    run, answer = ran(world, software, {**SEQUENCE, "made_with": ["vina"]})

    assert answer["run_status"] == "succeeded", answer
    assert [(r["software"], r["version"]) for r in made(world, run)] == [("Vina", "1.2.5")]


@pytest.mark.parametrize(
    ("software", "pointer", "detail"),
    [
        (
            [{"name": "Vina", "version": "1.2.5"}],
            "#/creates/0/made_with/0",
            "`AlphaFold` isn't among the action's software (Vina).",
        ),
        (
            [],
            "#/creates/0/made_with",
            "The action lists no `software`: list what its runs make entities with, then name "
            "which made this one.",
        ),
    ],
)
def test_a_run_can_only_name_its_actions_own_software(
    world: World, software: list[dict[str, str]], pointer: str, detail: str
) -> None:
    run, answer = ran(world, software, {**SEQUENCE, "made_with": ["AlphaFold"]})

    assert answer["run_status"] == "failed"
    assert answer["errors"] == [{"pointer": pointer, "detail": detail}]
    assert not EntityMaking.objects.filter(run_id=run["id"]).exists()


def test_an_import_keeps_what_its_record_says_made_it_as_its_sources_word(
    world: World,
) -> None:
    said = {
        "kind": "computational",
        "software": [{"name": "AlphaFold", "version": "2.3.2"}],
        "details": {"mean_plddt": 97.1},
    }

    run, answer = ran(world, [], {**SEQUENCE, "origin": {**ALPHAFOLD_DB, "made": said}})

    assert answer["run_status"] == "succeeded", answer
    (row,) = made(world, run)
    assert row["by"] == "source"
    assert (row["kind"], row["software"], row["version"]) == ("computational", "AlphaFold", "2.3.2")
    assert row["source"] == {
        "source": "AlphaFold DB",
        "id": "AF-P69905-F1",
        "version": "4",
        "url": "https://alphafold.ebi.ac.uk/entry/P69905",
    }
    hrid = api_as(world).get(f"/runs/{run['id']}").json()["effects"][0]["hrid"]
    (origin,) = api_as(world).get(f"/entities/{hrid}/origins").json()
    assert origin["made"] == {**said, "method": ""}


def test_a_source_can_say_how_without_saying_with_what(world: World) -> None:
    said = {"kind": "experimental", "method": "X-RAY DIFFRACTION"}
    pdb = {**ALPHAFOLD_DB, "source": "RCSB PDB", "id": "4HHB", "made": said}

    run, answer = ran(world, [], {**SEQUENCE, "origin": pdb})

    assert answer["run_status"] == "succeeded", answer
    (row,) = made(world, run)
    assert (row["by"], row["kind"], row["method"], row["software"]) == (
        "source",
        "experimental",
        "X-RAY DIFFRACTION",
        "",
    )


def test_iki_records_come_before_a_sources_word(world: World) -> None:
    said = {"kind": "computational", "software": [{"name": "AlphaFold", "version": "2.3.2"}]}
    created = {**SEQUENCE, "origin": {**ALPHAFOLD_DB, "made": said}}

    run, answer = ran(world, [{"name": "PDBFixer", "version": "1.12"}], created)

    assert answer["run_status"] == "succeeded", answer
    assert [(r["by"], r["software"]) for r in made(world, run)] == [
        ("iki", "PDBFixer"),
        ("source", "AlphaFold"),
    ]


def test_a_reader_who_cant_see_the_run_still_sees_how_it_was_made(world: World) -> None:
    run, _ = ran(world, [{"name": "ImmuneBuilder", "version": "1.2"}], SEQUENCE)
    entity = ActionRun.objects.get(pk=run["id"]).effects.get().entity
    elsewhere = Project.objects.create(team=world.team, name="Elsewhere")
    ProjectMembership.objects.create(
        project=elsewhere, user=world.users["team_member"], role=ProjectRole.VIEWER
    )
    ProjectLink.objects.create(entity=entity, project=elsewhere)

    (row,) = api_as(world, "team_member").get(f"/entities/{entity.hrid}/making").json()

    assert (row["software"], row["run"]) == ("ImmuneBuilder", None)


def test_an_entity_found_again_keeps_each_making(world: World) -> None:
    ran(world, [{"name": "ImmuneBuilder", "version": "1.2"}], SEQUENCE)
    run, answer = ran(world, [{"name": "ImmuneBuilder", "version": "1.3"}], SEQUENCE)

    assert answer["run_status"] == "succeeded", answer
    assert [r["version"] for r in made(world, run)] == ["1.2", "1.3"]


def test_makings_are_append_only() -> None:
    # The app's database role may only insert them, as for origins (ADR 0025).
    assert "iki_entity_making" in db_roles.append_only_tables()


# --- Searching on it ----------------------------------------------------------------------------


def found(world: World, made: Any, op: str = "has") -> list[str]:
    body = {"type": "protein.Protein", "where": {"field": "made", "op": op, "value": made}}
    response = api_as(world, "viewer").post("/entities/search", body)
    assert response.status_code == 200, response.content
    return sorted(e["values"]["name"] for e in response.json()["items"])


def protein(name: str, sequence: str, **origin: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "type": "protein.Protein",
        "values": {"name": name, "sequence": sequence},
    }
    if origin:
        out["origin"] = {**ALPHAFOLD_DB, **origin}
    return out


def test_a_search_finds_entities_by_how_they_were_made(world: World) -> None:
    alphafold = {"kind": "computational", "software": [{"name": "AlphaFold", "version": "2.3.2"}]}
    xray = {"kind": "experimental", "method": "X-RAY DIFFRACTION"}
    ran(world, [{"name": "ImmuneBuilder", "version": "1.2"}], protein("predicted here", "MKA"))
    ran(world, [], protein("from AlphaFold DB", "MKB", made=alphafold))
    ran(world, [], protein("from the PDB", "MKC", id="4HHB", made=xray))
    # One of each in one entity: Iki ran PDBFixer on a structure AlphaFold predicted.
    ran(
        world,
        [{"name": "PDBFixer", "version": "1.12"}],
        protein("fixed here", "MKD", id="AF-2", made=alphafold),
    )
    ran(world, [], protein("by hand", "MKE"))

    assert found(world, {"software": "alphafold"}) == ["fixed here", "from AlphaFold DB"]
    assert found(world, {"software": "AlphaFold", "version": "2.3.2"}) == [
        "fixed here",
        "from AlphaFold DB",
    ]
    assert found(world, {"software": "AlphaFold", "version": "3.0"}) == []
    assert found(world, {"kind": "experimental"}) == ["from the PDB"]
    assert found(world, {"by": "iki"}) == ["fixed here", "predicted here"]
    # One condition, one making: AlphaFold is a source's word, not Iki's record.
    assert found(world, {"software": "AlphaFold", "by": "iki"}) == []
    assert found(world, {"software": "PDBFixer", "by": "iki"}) == ["fixed here"]
    # Made in any way it records, or not.
    assert "by hand" not in found(world, True, "is_set")
    assert found(world, False, "is_set") == ["by hand"]


@pytest.mark.parametrize(
    ("value", "pointer", "detail"),
    [
        (
            "AlphaFold",
            "#/where/value",
            "must be an object with any of `software`, `version`, `kind` or `by`.",
        ),
        (
            {},
            "#/where/value",
            "must be an object with any of `software`, `version`, `kind` or `by`.",
        ),
        (
            {"tool": "x"},
            "#/where/value/tool",
            "isn't one: give `software`, `version`, `kind` or `by`.",
        ),
        ({"by": "me"}, "#/where/value/by", "must be `iki` or `source`."),
        ({"kind": "guessed"}, "#/where/value/kind", "must be `experimental` or `computational`."),
        ({"software": ""}, "#/where/value/software", "must be some text, up to 100 characters."),
    ],
)
def test_a_search_on_how_it_was_made_says_whats_wrong(
    world: World, value: Any, pointer: str, detail: str
) -> None:
    body = {"where": {"field": "made", "op": "has", "value": value}}
    response = api_as(world, "viewer").post("/entities/search", body)
    assert response.status_code == 422, response.content
    assert response.json()["errors"] == [{"pointer": pointer, "detail": detail}]
