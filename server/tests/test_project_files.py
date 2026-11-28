"""A project's Files (ADR 0177): found by name, format, acquisition, sample and run, with
what uses each, and nothing from a project the reader can't read."""

import datetime as dt
import uuid
from pathlib import Path
from typing import Any

import pytest
from api_client import Api, problem
from django.db import connection, transaction
from django.db.utils import DatabaseError
from org_world import World, build_world
from scan_fixtures import clean_blob
from test_files import run_tasks
from test_runs import launch, world
from test_workers import keep_connection

from iki_server.accounts.models import User
from iki_server.entities import services
from iki_server.entities.models import Entity
from iki_server.files import listing
from iki_server.files import metadata as stated
from iki_server.files import services as files
from iki_server.files.models import File, Upload
from iki_server.org.models import Project, ProjectMembership, ProjectRole
from iki_server.runs.models import ActionRun, RunFile

pytestmark = pytest.mark.django_db

__all__ = ["keep_connection", "world"]  # the fixtures, used here too

SAMPLE = "acme/protein.Sample"
STRUCTURE = "acme/protein.ProteinStructure"
PROTEIN = "acme/protein.Protein"
_blobs = iter(range(1, 10_000))


def api_as(world: World, principal: str = "editor") -> Api:
    return Api(world.browser(principal))


def add(
    project: Project,
    name: str,
    media_type: str = "chemical/x-pdb",
    *,
    size: int = 1024,
    acquired: str = "",
    source: str = "",
    by: User | None = None,
    facts: dict[str, dict[str, str]] | None = None,
    read_groups: list[dict[str, str]] | None = None,
) -> File:
    """A file, as an upload or an import leaves one (its bytes aren't needed here)."""
    n = next(_blobs)
    blob = clean_blob(sha256=f"{n:064x}", size=size, storage_uri=f"s3://iki/{n}")
    return File.objects.create(
        blob=blob,
        name=name,
        media_type=media_type,
        project=project,
        created_by=by,
        acquired_on=dt.date.fromisoformat(acquired[:10]) if acquired else None,
        acquired_at=acquired,
        acquired_from=source or ("given" if acquired else ""),
        facts=facts or {},
        read_groups=read_groups or [],
        stated_text=stated.search_text(facts or {}, read_groups or []),
    )


def names(response: Any) -> list[str]:
    assert response.status_code == 200, response.content
    return [f["name"] for f in response.json()["items"]]


def every(api: Api, **query: Any) -> list[str]:
    """Every page's names, following the cursors."""
    out: list[str] = []
    cursor = None
    while True:
        body = api.get("/files", **query, **({"cursor": cursor} if cursor else {})).json()
        out += [f["name"] for f in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            return out


@pytest.fixture
def other(world: World) -> Project:
    """A project of the same team that nobody in the world is a member of."""
    return Project.objects.create(team=world.team, name="Confidential Assays")


def structure(world: World, file: File, project: Project | None = None) -> Entity:
    user = world.users["editor"]
    saved = services.create(user, STRUCTURE, project or world.project, {"structure": str(file.pk)})
    return Entity.objects.get(pk=saved.entity.pk)


def protein(world: World) -> Entity:
    """The project's protein: its record, not the registry entry it shares an HRID with."""
    saved = services.create(world.users["editor"], PROTEIN, world.project, {"sequence": "MKV"})
    return Entity.objects.get(pk=saved.entity.pk)


def sample(world: World, label: str, certificate: File | None = None) -> Entity:
    rows = (
        [{"material": str(protein(world).pk), "certificate": str(certificate.pk)}]
        if certificate
        else []
    )
    saved = services.create(
        world.users["editor"], SAMPLE, world.project, {"label": label, "additions": rows}
    )
    return Entity.objects.get(pk=saved.entity.pk)


# --- Which files ---------------------------------------------------------------------------------


def test_a_projects_files_are_those_added_to_it_and_those_its_records_hold(
    world: World, other: Project
) -> None:
    here = add(world.project, "plate-7.pdb")
    there = add(other, "from-elsewhere.pdb")
    add(other, "never-seen.pdb")
    # Someone in both projects puts the other project's file on a record of this one.
    both = User.objects.create_user("both@example.com", "Both")
    for p in (world.project, other):
        ProjectMembership.objects.create(project=p, user=both, role=ProjectRole.EDITOR)
    services.create(both, STRUCTURE, world.project, {"structure": str(there.pk)})
    viewer = api_as(world, "viewer")
    assert names(viewer.get("/files", project=str(world.project.pk))) == [
        "from-elsewhere.pdb",
        here.name,
    ]
    # Across projects: the same, since the viewer reads no other project.
    assert set(every(viewer)) == {"from-elsewhere.pdb", "plate-7.pdb"}


def test_another_projects_files_never_appear(world: World, other: Project) -> None:
    add(world.project, "kinase-a1.ab1", "application/x-abi")
    secret = add(other, "kinase-secret.ab1", "application/x-abi", acquired="2026-03-14")
    viewer = api_as(world, "viewer")
    queries: list[dict[str, Any]] = [
        {},
        {"q": "kinase"},
        {"q": "secret"},
        {"format": "application/x-abi"},
        {"acquired_from": "2026-01-01"},
        {"sort": "name"},
        {"sort": "acquired"},
    ]
    for query in queries:
        assert "kinase-secret.ab1" not in every(viewer, **query), query
        count = viewer.get("/files/count", **query).json()
        assert count["total"] == (
            0 if query.get("q") == "secret" else 1 - bool(query.get("acquired_from"))
        )
    formats = viewer.every("/files/formats")
    assert formats == [
        {"id": "iki/core.abif", "label": "ABI trace", "media_type": "application/x-abi", "count": 1}
    ]
    # Its project, its file, and what uses it are as if they didn't exist.
    assert viewer.get("/files", project=str(other.pk)).status_code == 404
    for path in ("", "/records", "/runs", "/content"):
        assert viewer.get(f"/files/{secret.pk}{path}").status_code == 404
    # Search everything doesn't find it either.
    found = viewer.get("/search", q="kinase", kind="files").json()["groups"][0]
    assert [i["title"] for i in found["results"]] == ["kinase-a1.ab1"]
    assert found["total"] == 1


def test_someone_who_sees_a_project_but_cant_read_it_lists_none_of_its_files(
    world: World,
) -> None:
    add(world.project, "plate-7.pdb")
    # Org Admins see every project, and read only those they have a role in.
    admin = api_as(world, "org_admin")
    response = admin.get("/files", project=str(world.project.pk))
    assert response.status_code == 403
    assert every(admin) == []


# --- Finding them --------------------------------------------------------------------------------


def test_files_are_found_by_any_part_of_their_name_in_any_case(world: World) -> None:
    for name in ("Plate7_A01.ab1", "plate7_b02.AB1", "plate8_a01.ab1", "100%_done.txt"):
        add(world.project, name)
    viewer = api_as(world, "viewer")
    assert sorted(every(viewer, q="PLATE7")) == ["Plate7_A01.ab1", "plate7_b02.AB1"]
    assert every(viewer, q="_a01", sort="name") == ["Plate7_A01.ab1", "plate8_a01.ab1"]
    # Wildcards are text.
    assert every(viewer, q="%_") == ["100%_done.txt"]
    assert every(viewer, q="  ab1  ", sort="oldest")[0] == "Plate7_A01.ab1"


def test_files_are_found_by_what_they_state_as_by_their_name(world: World) -> None:
    """The instrument, serial and operator a file states (ADR 0224) are found as a name
    is: by any part, in any case, through the trigram index and without it."""
    fortessa = {
        "instrument": {"value": "LSRFortessa X-20", "stated_in": "FCS keyword $CYT"},
        "serial": {"value": "R658222R1012", "stated_in": "FCS keyword $CYTSN"},
        "operator": {"value": "Priya Raman", "stated_in": "FCS keyword $OP"},
    }
    canto = {"instrument": {"value": "FACSCanto II", "stated_in": "FCS keyword $CYT"}}
    add(world.project, "Tube_003.fcs", "application/vnd.isac.fcs", facts=fortessa)
    add(world.project, "Tube_004.fcs", "application/vnd.isac.fcs", facts=canto)
    add(world.project, "fortessa-qc-beads.txt", "text/plain")
    viewer = api_as(world, "viewer")
    assert every(viewer, q="fortessa", sort="name") == ["fortessa-qc-beads.txt", "Tube_003.fcs"]
    assert every(viewer, q="r658222") == ["Tube_003.fcs"]
    assert every(viewer, q="PRIYA") == ["Tube_003.fcs"]
    # Shorter than a trigram: read in the list's order, and still found.
    assert every(viewer, q="II") == ["Tube_004.fcs"]
    # Where a value is stated isn't what the file states.
    assert every(viewer, q="$CYT") == []
    assert viewer.get("/files/count", q="fortessa").json()["total"] == 2
    formats = viewer.every("/files/formats", q="canto")
    assert [(f["media_type"], f["count"]) for f in formats] == [("application/vnd.isac.fcs", 1)]
    (row,) = viewer.get("/files", q="r658222").json()["items"]
    assert row["facts"] == {
        "instrument": fortessa["instrument"],
        "manufacturer": None,
        "serial": fortessa["serial"],
        "instrument_name": None,
        "operator": fortessa["operator"],
        "software": None,
        "protocol": None,
    }


def test_files_are_found_by_format_and_by_a_family_of_formats(world: World) -> None:
    add(world.project, "a.ab1", "application/x-abi")
    add(world.project, "b.tif", "image/tiff")
    add(world.project, "c.png", "image/png")
    add(world.project, "d.fcs", "application/vnd.isac.fcs")
    add(world.project, "ligand.mol2", "application/vnd.sybyl.mol2")
    viewer = api_as(world, "viewer")
    assert sorted(every(viewer, format="image/*")) == ["b.tif", "c.png"]
    assert every(viewer, format="chemical/*") == ["ligand.mol2"]
    assert sorted(every(viewer, format=["application/x-abi", "image/png"])) == ["a.ab1", "c.png"]
    formats = viewer.every("/files/formats", project=str(world.project.pk), q=".")
    assert {f["media_type"]: f["count"] for f in formats} == {
        "application/x-abi": 1,
        "application/vnd.isac.fcs": 1,
        "image/png": 1,
        "image/tiff": 1,
        "application/vnd.sybyl.mol2": 1,
    }
    refused = viewer.get("/files", format="not a type")
    assert problem(refused, 422, "validation_failed")["errors"][0]["parameter"] == "format"


def test_a_picker_lists_only_what_its_field_takes(world: World) -> None:
    add(world.project, "small.pdb", size=10)
    add(world.project, "large.pdb", size=10_000)
    add(world.project, "notes.txt", "text/plain", size=10)
    viewer = api_as(world, "viewer")
    taken = every(viewer, accepts=["chemical/x-pdb", "chemical/x-mmcif"], max_size=100)
    assert taken == ["small.pdb"]


def test_plugin_format_from_upload_to_structure_field_and_filter(world: World, s3: Any) -> None:
    from test_api_entities import create
    from test_files import PDB
    from test_files import add as upload

    fmt = "acme/protein.model-pdb"
    api = api_as(world)
    declarations = {f["id"]: f for f in api.every("/formats")}
    model = declarations[fmt]
    assert model["viewer"] == "structure-pdb"
    assert model["canonical"] == "chemical/x-acme-model"
    assert model["extensions"] == [".model.pdb"]
    assert "acme/protein.relax_structure" in model["parsers"]

    # A generic name and media type leave the plugin's signature to recognize these bytes.
    data = b"REMARK 700 MODEL CANDIDATE A\n" + PDB
    made = upload(api, world, data, name="result.bin", media_type="application/octet-stream")
    assert made["file"]["media_type"] == model["canonical"]
    file_id = made["file"]["id"]
    saved = create(api, world.project, {"structure": file_id}, STRUCTURE)
    assert saved.status_code == 201, saved.content
    found_type = api.get(f"/entity-types/{STRUCTURE}").json()
    viewer = found_type["viewers"][0]
    assert viewer["viewer"] == "structure"
    assert saved.json()["values"][viewer["structure"]] == file_id

    # Previously stored aliases still pass the same field and produce one filter choice.
    legacy = add(world.project, "older.model.pdb", "chemical/x-acme-coordinates")
    assert create(api, world.project, {"structure": str(legacy.pk)}, STRUCTURE).status_code == 201
    add(world.project, "notes.unspecified", "application/x-unregistered")
    facets = api.every("/files/formats", accepts=[fmt])
    assert facets == [
        {"id": fmt, "label": "Model coordinates", "media_type": model["canonical"], "count": 2}
    ]
    for choice in (fmt, *model["media_types"]):
        assert sorted(every(api, format=choice)) == ["older.model.pdb", "result.bin"]
    assert api.every("/files/formats", q="notes") == [
        {
            "id": None,
            "label": "application/x-unregistered",
            "media_type": "application/x-unregistered",
            "count": 1,
        }
    ]


def test_files_are_found_by_when_their_data_was_acquired(world: World) -> None:
    add(world.project, "march.fcs", acquired="2026-03-14T09:21:07", source="fcs")
    add(world.project, "april.fcs", acquired="2026-04-02")
    add(world.project, "unknown.fcs")
    viewer = api_as(world, "viewer")
    assert every(viewer, acquired_from="2026-03-14", acquired_to="2026-03-31") == ["march.fcs"]
    assert every(viewer, acquired_from="2026-04-01") == ["april.fcs"]
    assert every(viewer, sort="acquired") == ["april.fcs", "march.fcs", "unknown.fcs"]
    assert every(viewer, sort="acquired_oldest") == ["march.fcs", "april.fcs", "unknown.fcs"]
    row = next(f for f in viewer.get("/files").json()["items"] if f["name"] == "march.fcs")
    assert row["acquired"]["at"] == "2026-03-14T09:21:07"
    assert row["acquired"]["on"] == "2026-03-14"
    assert row["acquired"]["source"] == "fcs"
    april = next(f for f in viewer.get("/files").json()["items"] if f["name"] == "april.fcs")
    assert april["acquired"]["source_label"] == "as given when it was added"
    unknown = next(f for f in viewer.get("/files").json()["items"] if f["name"] == "unknown.fcs")
    assert unknown["acquired"] is None


@pytest.mark.parametrize("sort", listing.SORTS)
def test_pages_meet_without_gaps_in_every_order(world: World, other: Project, sort: str) -> None:
    """Two sets of files merge into one order, page by page: none is skipped or repeated."""
    both = User.objects.create_user("both@example.com", "Both")
    for p in (world.project, other):
        ProjectMembership.objects.create(project=p, user=both, role=ProjectRole.EDITOR)
    made = []
    for i in range(13):
        day = f"2026-0{1 + i % 3}-1{i % 5}" if i % 4 else ""
        # Names that differ only in case, and a letter lowercased differently by locales.
        name = ["Alpha", "alpha", "BETA", "beta", "İstanbul", "ätze", "Zeta"][i % 7] + f"-{i}.pdb"
        if i % 3 == 0:
            elsewhere = add(other, name, acquired=day)
            services.create(both, STRUCTURE, world.project, {"structure": str(elsewhere.pk)})
        else:
            add(world.project, name, acquired=day)
        made.append(name)
    api = Api(_browser(both))
    one_page = every(api, project=str(world.project.pk), sort=sort, limit=100)
    paged = every(api, project=str(world.project.pk), sort=sort, limit=2)
    assert sorted(one_page) == sorted(made)
    assert paged == one_page
    across = every(api, sort=sort, limit=3)
    assert across == every(api, sort=sort, limit=100)
    # A cursor of one order isn't one of another.
    first = api.get("/files", sort=sort, limit=1).json()
    wrong = "name" if sort != "name" else "newest"
    refused = api.get("/files", sort=wrong, cursor=first["next_cursor"])
    assert problem(refused, 422, "validation_failed")["errors"][0]["parameter"] == "cursor"


def _browser(user: User) -> Any:
    from browser import Browser

    browser = Browser()
    browser.force_login(user)
    return browser


# --- Samples and runs ----------------------------------------------------------------------------


def test_a_samples_files_are_those_it_holds_and_those_a_run_read_with_it(
    world: World, other: Project
) -> None:
    certificate = add(world.project, "coa-lot-12.pdf", "application/pdf")
    trace = add(world.project, "trace.pdb")
    add(world.project, "unrelated.pdb")
    held = sample(world, "S-1", certificate)
    alone = sample(world, "S-2")
    editor = api_as(world, "editor")
    holder = structure(world, trace)
    ran = launch(
        editor,
        world.project,
        {
            "protein": [str(protein(world).pk)],
            "samples": [str(alone.pk)],
            "structures": [str(holder.pk)],
        },
        "acme/protein.flag_samples",
    )
    assert ran.status_code == 201, ran.content
    viewer = api_as(world, "viewer")
    assert every(viewer, sample=held.hrid) == ["coa-lot-12.pdf"]
    assert every(viewer, sample=str(alone.pk)) == ["trace.pdb"]
    detail = viewer.get(f"/files/{trace.pk}").json()
    assert [(s["hrid"], s["via_run"]["hrid"]) for s in detail["samples"]] == [
        (alone.hrid, ran.json()["hrid"])
    ]
    coa = viewer.get(f"/files/{certificate.pk}").json()
    assert [(s["hrid"], s["via_run"]) for s in coa["samples"]] == [(held.hrid, None)]
    ActionRun.objects.filter(pk=ran.json()["id"]).update(project=other)
    assert every(viewer, sample=str(alone.pk)) == []
    assert viewer.get("/files/count", sample=str(alone.pk)).json()["total"] == 0
    assert viewer.every("/files/formats", sample=str(alone.pk)) == []
    assert every(viewer, sample=held.hrid) == ["coa-lot-12.pdf"]
    unknown = viewer.get("/files", sample="SMP-999999")
    assert problem(unknown, 422, "validation_failed")["errors"][0]["parameter"] == "sample"


def test_a_run_lists_the_files_it_read_and_made(world: World, s3: Any) -> None:
    from test_run_imports import importing, stored

    file_id = stored(world)
    imported = importing(world, file_id)
    assert imported.status_code == 201, imported.content
    run = ActionRun.objects.get(pk=imported.json()["id"])
    assert list(RunFile.objects.filter(run=run).values_list("file_id", "role")) == [
        (uuid.UUID(file_id), "param")
    ]
    viewer = api_as(world, "viewer")
    assert every(viewer, run=run.hrid) == ["kinases.fasta"]
    runs = viewer.get(f"/files/{file_id}/runs").json()["items"]
    assert [(r["hrid"], r["roles"]) for r in runs] == [(run.hrid, ["param"])]
    rows = viewer.get("/files", run=str(run.pk)).json()["items"]
    assert rows[0]["used_by"] == {"records": 0, "runs": 1}
    # What a run used is kept as it was.
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("UPDATE iki_run_file SET role = 'made'")


def test_files_a_runs_worker_uploads_are_made_by_the_run(world: World, s3: Any) -> None:
    from test_run_imports import importing, stored
    from test_workers import worker

    imported = importing(world, stored(world)).json()
    pod = worker(world)
    job = pod.lease().json()
    started = pod.report(
        job,
        "uploads",
        name="made.pdb",
        media_type="chemical/x-pdb",
        size=0,
        sha256=files.EMPTY_SHA256,
    )
    assert started.status_code == 201, started.content
    run_tasks()
    made = Upload.objects.get(pk=started.json()["id"]).file
    assert made is not None
    viewer = api_as(world, "viewer")
    runs = viewer.get(f"/files/{made.pk}/runs").json()["items"]
    assert [(r["hrid"], r["roles"]) for r in runs] == [(imported["hrid"], ["made"])]
    origin = viewer.get(f"/files/{made.pk}").json()["origin"]
    assert origin["kind"] == "run" and origin["run"]["hrid"] == imported["hrid"]
    assert every(viewer, run=imported["hrid"], sort="name") == ["kinases.fasta", "made.pdb"]


def test_a_runs_input_files_are_those_its_inputs_held_when_it_was_launched(world: World) -> None:
    first, second = add(world.project, "first.pdb"), add(world.project, "second.pdb")
    holder = structure(world, first)
    alone = sample(world, "S-1")
    ran = launch(
        api_as(world, "editor"),
        world.project,
        {
            "protein": [str(protein(world).pk)],
            "samples": [str(alone.pk)],
            "structures": [str(holder.pk)],
        },
        "acme/protein.flag_samples",
    ).json()
    services.update(
        world.users["editor"], holder, {"structure": str(second.pk)}, expected_revision=1
    )
    viewer = api_as(world, "viewer")
    assert every(viewer, run=ran["hrid"]) == ["first.pdb"]


# --- What uses each ------------------------------------------------------------------------------


def test_used_by_names_each_record_and_the_revisions_that_held_it(world: World) -> None:
    file, other_file = add(world.project, "a.pdb"), add(world.project, "b.pdb")
    holder = structure(world, file)  # revision 1
    editor = world.users["editor"]
    services.update(editor, holder, {"method": "xray"}, expected_revision=1)  # 2
    services.update(editor, holder, {"structure": str(other_file.pk)}, expected_revision=2)  # 3
    services.update(editor, holder, {"method": "nmr"}, expected_revision=3)  # 4
    services.update(editor, holder, {"structure": str(file.pk)}, expected_revision=4)  # 5
    certificate = add(world.project, "coa.pdf", "application/pdf")
    held = sample(world, "S-1", certificate)
    viewer = api_as(world, "viewer")
    records = viewer.get(f"/files/{file.pk}/records").json()["items"]
    assert [(r["hrid"], r["revision"], r["spans"]) for r in records] == [
        (
            holder.hrid,
            5,
            [
                {"first": 1, "last": 2, "fields": ["structure"]},
                {"first": 5, "last": None, "fields": ["structure"]},
            ],
        )
    ]
    rows = {f["name"]: f["used_by"] for f in viewer.get("/files").json()["items"]}
    assert rows["a.pdb"] == {"records": 1, "runs": 0}
    assert rows["b.pdb"] == {"records": 1, "runs": 0}
    # A file in a list of rows is held by its row's field.
    coa = viewer.get(f"/files/{certificate.pk}/records").json()["items"]
    assert [(r["hrid"], r["spans"]) for r in coa] == [
        (held.hrid, [{"first": 1, "last": None, "fields": ["additions"]}])
    ]


def test_used_by_counts_and_lists_only_what_its_reader_can_read(
    world: World, other: Project
) -> None:
    file = add(world.project, "shared.pdb")
    both = User.objects.create_user("both@example.com", "Both")
    for p in (world.project, other):
        ProjectMembership.objects.create(project=p, user=both, role=ProjectRole.EDITOR)
    services.create(both, STRUCTURE, other, {"structure": str(file.pk)})
    mine = structure(world, file)
    viewer = api_as(world, "viewer")
    row = viewer.get("/files").json()["items"][0]
    assert row["used_by"] == {"records": 1, "runs": 0}
    records = viewer.get(f"/files/{file.pk}/records").json()["items"]
    assert [r["hrid"] for r in records] == [mine.hrid]
    assert Api(_browser(both)).get("/files").json()["items"][0]["used_by"]["records"] == 2


def test_where_a_file_came_from(world: World, s3: Any) -> None:
    from test_run_imports import stored

    uploaded = stored(world, "uploaded.fasta")
    imported = add(world.project, "imported.pdb")
    viewer = api_as(world, "viewer")
    assert viewer.get(f"/files/{uploaded}").json()["origin"]["kind"] == "upload"
    assert viewer.get(f"/files/{imported.pk}").json()["origin"] == {
        "kind": "import",
        "run": None,
        "uri": None,
        "version": None,
    }
    detail = viewer.get(f"/files/{uploaded}").json()
    assert detail["created_by"]["email"] == "editor@example.com"
    assert detail["sha256"] == files.EMPTY_SHA256


# --- Acquisition ---------------------------------------------------------------------------------


def test_whoever_adds_a_file_may_say_when_its_data_was_acquired(world: World, s3: Any) -> None:
    upload = files.start(
        world.users["editor"],
        world.project,
        name="plate.csv",
        media_type="text/csv",
        size=0,
        sha256=files.EMPTY_SHA256,
        acquired_at="2026-03-14 09:21",
    )
    run_tasks()
    upload.refresh_from_db()
    assert upload.file is not None
    assert (upload.file.acquired_on, upload.file.acquired_at, upload.file.acquired_from) == (
        dt.date(2026, 3, 14),
        "2026-03-14T09:21",
        "given",
    )


@pytest.mark.parametrize(
    ("given", "why"),
    [
        ("14/03/2026", "Give when it was acquired as a date"),
        ("2026-02-30", "Give when it was acquired as a date"),
        ("2026-03-14T25:00", "Give when it was acquired as a date"),
        ("1850-01-01", "That's not a day"),
        (
            (dt.datetime.now(tz=dt.UTC).date() + dt.timedelta(days=3)).isoformat(),
            "That's not a day",
        ),
    ],
)
def test_an_acquisition_date_must_be_a_real_day_not_in_the_future(
    world: World, s3: Any, given: str, why: str
) -> None:
    response = api_as(world).post(
        "/uploads",
        {
            "project": str(world.project.pk),
            "name": "plate.csv",
            "media_type": "text/csv",
            "size": 0,
            "sha256": files.EMPTY_SHA256,
            "acquired_at": given,
        },
    )
    body = problem(response, 422, "validation_failed")
    assert body["errors"][0]["pointer"] == "#/acquired_at"
    assert body["errors"][0]["detail"].startswith(why)


def test_a_files_own_format_says_when_its_data_was_acquired(world: World, s3: Any) -> None:
    """An AB1 trace uploaded through the API, its bytes in the store: its run start is read
    from them, and wins over what the uploader said."""
    from test_file_acquired import realistic_ab1
    from test_files import add as upload

    data = realistic_ab1()
    added = upload(
        api_as(world),
        world,
        data,
        name="A01_sample-12.ab1",
        media_type="application/x-abi",
        acquired_at="2026-01-01",
    )
    file = File.objects.get(pk=added["file"]["id"])
    assert file.acquired_from == "abif" and file.acquired_on is not None
    assert file.acquired_at.startswith(str(file.acquired_on))
    assert added["file"]["acquired"]["source_label"].startswith("from the ABIF trace")
    assert (
        api_as(world, "viewer")
        .get(
            "/files",
            acquired_from=file.acquired_on.isoformat(),
            acquired_to=file.acquired_on.isoformat(),
        )
        .json()["items"][0]["name"]
        == "A01_sample-12.ab1"
    )


def test_a_files_own_format_says_which_instrument_and_who(world: World, s3: Any) -> None:
    """A Cytek Aurora's FCS file, uploaded through the API: what it states is read from its
    bytes in the store, kept with it, shown on its page and in the list, and found by."""
    from test_files import add as upload

    data = (Path(__file__).parent / "data/fcs/cytek-aurora-header.fcs").read_bytes()
    added = upload(
        api_as(world),
        world,
        data,
        name="Jeremy 20200720-input.fcs",
        media_type="application/vnd.isac.fcs",
    )
    file = File.objects.get(pk=added["file"]["id"])
    assert file.facts == {
        "instrument": {"value": "Aurora", "stated_in": "FCS keyword $CYT"},
        "serial": {"value": "N0354", "stated_in": "FCS keyword $CYTSN"},
        "operator": {"value": "Admin", "stated_in": "FCS keyword $OP"},
    }
    assert file.read_groups == [] and file.stated_text == "Aurora\nN0354\nAdmin"
    viewer = api_as(world, "viewer")
    page = viewer.get(f"/files/{file.pk}").json()
    assert page["facts"]["instrument"] == {"value": "Aurora", "stated_in": "FCS keyword $CYT"}
    assert page["facts"]["manufacturer"] is None
    assert page["read_groups"] == []
    assert every(viewer, q="n0354") == ["Jeremy 20200720-input.fcs"]
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("UPDATE iki_file SET facts = '{}' WHERE id = %s", [file.pk])


def test_an_alignments_read_groups_are_kept_whole(world: World, s3: Any) -> None:
    from test_file_acquired import make_sam
    from test_files import add as upload

    data = make_sam([None, "2026-03-14"])
    added = upload(api_as(world), world, data, name="NA12878.sam", media_type="text/x-sam")
    viewer = api_as(world, "viewer")
    page = viewer.get(f"/files/{added['file']['id']}").json()
    assert [g["ID"] for g in page["read_groups"]] == ["HV7KCDSX5.1", "HV7KCDSX5.2"]
    assert page["read_groups"][1] == {
        "ID": "HV7KCDSX5.2",
        "DT": "2026-03-14",
        "LB": "lib1",
        "PL": "ILLUMINA",
        "PU": "HV7KCDSX5.1.ACGTACGT",
        "SM": "NA12878",
    }
    assert all(value is None for value in page["facts"].values())
    assert every(viewer, q="hv7kcdsx5.1.acgt") == ["NA12878.sam"]
    # The list's rows give the facts, not the read groups.
    assert "read_groups" not in viewer.get("/files").json()["items"][0]


def test_what_a_file_states_is_an_object_and_its_read_groups_a_list(world: World) -> None:
    add(world.project, "Tube_001.fcs", "application/vnd.isac.fcs")
    for column, value in (("facts", "[]"), ("read_groups", "{}")):
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO iki_file (id, blob_id, name, media_type, project_id, {column}) "  # noqa: S608 - two fixed names
                "SELECT gen_random_uuid(), blob_id, 'x.fcs', media_type, project_id, %s::jsonb "
                "FROM iki_file LIMIT 1",
                [value],
            )


def test_unknown_stays_unknown_never_the_time_it_was_added(world: World, s3: Any) -> None:
    upload = files.start(
        world.users["editor"],
        world.project,
        name="plate.csv",
        media_type="text/csv",
        size=0,
        sha256=files.EMPTY_SHA256,
    )
    run_tasks()
    upload.refresh_from_db()
    assert upload.file is not None and upload.file.acquired_on is None
    assert api_as(world, "viewer").get(f"/files/{upload.file.pk}").json()["acquired"] is None


def test_files_never_change_and_say_how_their_acquisition_is_known(world: World) -> None:
    file = add(world.project, "a.pdb", acquired="2026-03-14")
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("UPDATE iki_file SET acquired_at = '2026-03-15' WHERE id = %s", [file.pk])
    # A known acquisition says when and how; an unknown one says neither.
    with pytest.raises(DatabaseError), transaction.atomic():
        add(world.project, "b.pdb", acquired="2026-03-14", source="Not a source")


def test_build_world_is_isolated(world: World) -> None:
    """The second world's files are the second world's (a check of the helpers above)."""
    second = build_world("b-")
    add(second.project, "theirs.pdb")
    assert every(api_as(world, "viewer")) == []
