"""The lists across projects (ADR 0176): how many records of each type someone can read,
and each record's projects, by the rules that read one record. A record only in a project
the reader isn't in is in no list and no count.

The sequences are real ones: the M13 forward (-20) and reverse sequencing primers, and the
T7 promoter.
"""

import re
from typing import Any, cast

import pytest
from org_world import World, build_world
from test_api_entities import api_as

from iki_server.entities import counts, services
from iki_server.entities.models import Entity, ProjectLink
from iki_server.org.models import Project, ProjectMembership, ProjectRole

pytestmark = pytest.mark.django_db

DNA = "acme/dna.DNASequence"
PROTEIN = "acme/protein.Protein"
M13_FORWARD = "GTAAAACGACGGCCAGT"
M13_REVERSE = "CAGGAAACAGCTATGAC"
T7_PROMOTER = "TAATACGACTCACTATAGGG"


@pytest.fixture
def world() -> World:
    return build_world()


@pytest.fixture
def lab(world: World) -> dict[str, Any]:
    """Two DNA sequences and a protein in Kinase Atlas; a sequence in a project of the other
    team's that the viewer isn't in; and one of Kinase Atlas's linked into it too."""
    editor = world.users["editor"]
    elsewhere = Project.objects.create(team=world.other_team, name="Vector Reference QC")
    ProjectMembership.objects.create(
        project=elsewhere, user=world.users["mixed"], role=ProjectRole.EDITOR
    )
    forward = cast(
        Entity, services.create(editor, DNA, world.project, {"bases": M13_FORWARD}).entity
    )
    reverse = services.create(editor, DNA, world.project, {"bases": M13_REVERSE}).entity
    insulin = services.create(
        editor, PROTEIN, world.project, {"name": "Insulin", "sequence": "GIVEQCCTSICSLYQLENYCN"}
    ).entity
    hidden = services.create(world.users["mixed"], DNA, elsewhere, {"bases": T7_PROMOTER}).entity
    ProjectLink.objects.create(entity=forward, project=elsewhere)
    return {
        "elsewhere": elsewhere,
        "forward": forward,
        "reverse": reverse,
        "insulin": insulin,
        "hidden": hidden,
    }


def counted(world: World, principal: str, **params: Any) -> Any:
    response = api_as(world, principal).get("/entities/counts", **params)
    assert response.status_code == 200, response.json()
    return response.json()


def test_counts_what_the_reader_can_read_by_type(world: World, lab: dict[str, Any]) -> None:
    found = counted(world, "viewer")
    # By label; the type itself, not its abstract parents; the T7 promoter isn't counted.
    assert found == {
        "types": [
            {"type": DNA, "label": "DNA sequence", "count": 2},
            {"type": PROTEIN, "label": "Protein", "count": 1},
        ],
        "total": 3,
    }


def test_a_record_only_in_a_project_the_reader_isnt_in_is_in_no_list_or_count(
    world: World, lab: dict[str, Any]
) -> None:
    viewer = api_as(world, "viewer")
    hidden = lab["hidden"]

    listed = viewer.post("/entities/search", {"type": DNA}).json()["items"]
    assert {e["hrid"] for e in listed} == {lab["forward"].hrid, lab["reverse"].hrid}
    by_text = viewer.post("/entities/search", {"q": hidden.hrid}).json()["items"]
    assert by_text == []
    assert viewer.get("/entities", type=DNA).json()["items"] != []
    assert hidden.hrid not in {e["hrid"] for e in viewer.get("/entities").json()["items"]}
    csv = viewer.get("/entities/search.csv", type=DNA).getvalue().decode()
    assert hidden.hrid not in csv
    assert counted(world, "viewer")["total"] == 3

    # Nor in a count of the project, which the viewer can't ask for.
    response = viewer.get("/entities/counts", project=str(lab["elsewhere"].pk))
    assert response.status_code == 404
    assert viewer.post("/entities/search", {"project": str(lab["elsewhere"].pk)}).status_code == 404

    # Someone in both projects reads both, and counts the linked one once.
    both = counted(world, "mixed")
    assert both["types"][0] == {"type": DNA, "label": "DNA sequence", "count": 3}
    assert both["total"] == 4


def test_each_row_names_the_projects_the_reader_can_read(world: World, lab: dict[str, Any]) -> None:
    forward = lab["forward"].hrid
    seen = {
        e["hrid"]: [p["name"] for p in e["projects"]]
        for e in api_as(world, "viewer").post("/entities/search", {"type": DNA}).json()["items"]
    }
    # It's in Vector Reference QC too, which the viewer isn't in: that isn't named.
    assert seen[forward] == ["Kinase Atlas"]
    both = {
        e["hrid"]: [p["name"] for p in e["projects"]]
        for e in api_as(world, "mixed").post("/entities/search", {"type": DNA}).json()["items"]
    }
    # Its owner first, then the others by name.
    assert both[forward] == ["Kinase Atlas", "Vector Reference QC"]
    assert both[lab["hidden"].hrid] == ["Vector Reference QC"]
    one = api_as(world, "mixed").get(f"/entities/{forward}").json()
    assert [p["slug"] for p in one["projects"]] == [world.project.slug, lab["elsewhere"].slug]


def test_counts_in_a_project_include_what_was_linked_into_it(
    world: World, lab: dict[str, Any]
) -> None:
    found = counted(world, "mixed", project=str(lab["elsewhere"].pk))
    assert found == {
        "types": [{"type": DNA, "label": "DNA sequence", "count": 2}],
        "total": 2,
    }
    assert counted(world, "viewer", project=str(world.project.pk))["total"] == 3


def test_archived_records_and_registry_entries_dont_count(
    world: World, lab: dict[str, Any]
) -> None:
    services.set_archived(world.users["editor"], lab["reverse"], archived=True)
    # Each sequence is a record of a registry entry, which belongs to no project.
    assert Entity.objects.filter(type=DNA, registry__isnull=False).count() == 3
    found = counted(world, "viewer")
    assert found["types"][0] == {"type": DNA, "label": "DNA sequence", "count": 1}
    assert found["total"] == 2


def test_someone_who_sees_projects_but_reads_none_counts_nothing(
    world: World, lab: dict[str, Any]
) -> None:
    # An org admin sees every project, and reads no project's data without a role in it.
    assert counted(world, "org_admin") == {"types": [], "total": 0}
    assert counted(world, "org_admin", project=str(world.project.pk)) == {
        "types": [],
        "total": 0,
    }


def test_a_count_that_would_be_slow_is_left_out(
    world: World, lab: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(counts, "COUNT_MAX", 0)
    found = counted(world, "viewer")
    assert found["total"] is None
    # Every type that can have records, uncounted: no abstract type, and none counted.
    names = {t["type"] for t in found["types"]}
    assert {DNA, PROTEIN} <= names
    assert "acme/dna.Sequence" not in names
    assert {t["count"] for t in found["types"]} == {None}
    assert [t["label"].lower() for t in found["types"]] == sorted(
        t["label"].lower() for t in found["types"]
    )


def test_counting_reads_each_readable_project_by_its_links_index(
    world: World, lab: dict[str, Any]
) -> None:
    """With sequential scans off, as Postgres plans when the tables are large, the count
    finds each project's records by the links' index (project, entity) of ADR 0103, and
    each record by its key: it reads what the reader can read, not every record."""
    from django.db import connection

    grouped = counts._counted(world.users["viewer"], world.project.pk)
    with connection.cursor() as cursor:
        # Statistics of this test's own rows, not whatever other tests on this database
        # left behind: with those, the plan depended on which tests ran before it.
        cursor.execute("ANALYZE iki_entity, iki_entity_project")
        cursor.execute("SET LOCAL enable_seqscan = off")
        plan = grouped.explain()
    # By either of the links' indexes: Postgres 18 can skip-scan the (entity, project) one
    # for a project, and on a test's few rows it may. What matters is that neither table is
    # read whole (an index or bitmap scan, never a sequential one), and that the links are
    # found by their project.
    assert "Seq Scan on iki_entity" not in plan, plan
    assert re.search(r"Index Cond: \(project_id = ", plan), plan
