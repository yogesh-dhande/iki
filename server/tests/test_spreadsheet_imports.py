"""Importing a spreadsheet into records (ADR 0178): a workbook read, its columns mapped,
every row previewed as it would be written, then written by one run, all or nothing."""

from typing import Any

import pytest
from api_client import Api, problem
from org_world import World, build_world
from test_files import add, run_tasks
from workbook_files import XLSX_TYPE, Date, csv_text, xlsx

from iki_server.audit.models import AuditEvent
from iki_server.entities import services
from iki_server.entities.models import Entity
from iki_server.org.models import Project, ProjectMembership, ProjectRole
from iki_server.runs.models import ActionRun, ExternalOrigin
from iki_server.spreadsheets.models import PreviewRow, SavedMapping, SpreadsheetImport

pytestmark = pytest.mark.django_db

SAMPLE = "acme/protein.Sample"
PLASMID = "acme/dna.Plasmid"
PROTEIN = "acme/protein.Protein"

# Human kinase domains, as a structural biology lab's sample manifest lists them.
ABL1 = "GPSGVSPNYDKWEMERTDITMKHKLGGGQYGEVYEGVWKKYSLTVAVKTLKEDTMEVEEFLKEAAVMKEIKH"
# Another construct someone in the project also named abl1.
ABL2 = (
    "MLEICLKLVGCKSKKGLSSSSSCYLEEALQRPVASDFEPQGLSEAARWNSKENLLAGPSENDPNLFVALYDFVASGDNTLSI"
    "TKGEKLRVLGYNHNGEWCEAQTKNGQGWVPSNYITPVN"
)
EGFR = "GSGEAPNQALLRILKETEFKKIKVLGSGAFGTVYKGLWIPEGEKVKIPVAIKELREATSPKANKEILDEAYV"

MANIFEST: list[list[Any]] = [
    ["Label", "Protein", "Purity (%)", "Conc. (ug/mL)", "Tags", "Due on"],
    ["ABL1-KD-001", "ABL1", 98.5, 1250, "kinase; crystallography", Date(46388)],
    ["ABL1-KD-002", "ABL1", 97.2, 980, "kinase", Date(46395)],
    ["EGFR-KD-001", "EGFR", 95.0, "2.1 mg/mL", "kinase; screening", "2027-01-15"],
]


@pytest.fixture(autouse=True)
def storage(s3: Any) -> Any:
    return s3


@pytest.fixture
def world() -> World:
    return build_world()


def api_as(world: World, principal: str = "editor") -> Api:
    return Api(world.browser(principal))


def workbook(
    world: World, data: bytes, name: str = "manifest.xlsx", principal: str = "editor"
) -> str:
    """A workbook added to the project: its file's ID."""
    media = XLSX_TYPE if name.endswith(".xlsx") else "text/csv"
    added = add(api_as(world, principal), world, data, name=name, media_type=media)
    return str(added["file"]["id"])


def proteins(world: World) -> dict[str, dict[str, Any]]:
    api = api_as(world)
    out = {}
    for name, sequence in (("ABL1", ABL1), ("EGFR", EGFR)):
        made = api.post(
            "/entities",
            {
                "type": PROTEIN,
                "project": str(world.project.pk),
                "values": {"name": name, "sequence": sequence},
            },
        )
        assert made.status_code == 201, made.content
        out[name] = made.json()
    return out


def started(
    world: World,
    data: bytes,
    type_name: str = SAMPLE,
    name: str = "manifest.xlsx",
    *,
    principal: str = "editor",
) -> dict[str, Any]:
    file_id = workbook(world, data, name, principal)
    api = api_as(world, principal)
    made = api.post(
        f"/projects/{world.project.pk}/spreadsheet-imports", {"file": file_id, "type": type_name}
    )
    assert made.status_code == 201, made.content
    assert made.json()["state"] == "reading"
    run_tasks()
    body: dict[str, Any] = api.get(f"/spreadsheet-imports/{made.json()['id']}").json()
    return body


def seen(imported: dict[str, Any]) -> dict[str, str]:
    """If-Match: the revision of the import as it was read."""
    return {"If-Match": f'"{imported["revision"]}"'}


def change(
    world: World, imported: dict[str, Any], body: dict[str, Any], principal: str = "editor"
) -> Any:
    """PATCHes the import from the revision it was read at."""
    return api_as(world, principal).patch(
        f"/spreadsheet-imports/{imported['id']}", body, headers=seen(imported)
    )


def settle() -> None:
    """Runs tasks until none are ready: a preview's steps enqueue the next."""
    for _ in range(50):
        if not run_tasks():
            return


def previewed(world: World, imported: dict[str, Any], principal: str = "editor") -> dict[str, Any]:
    api = api_as(world, principal)
    asked = api.post(f"/spreadsheet-imports/{imported['id']}/preview", headers=seen(imported))
    assert asked.status_code == 202, asked.content
    settle()
    body: dict[str, Any] = api.get(f"/spreadsheet-imports/{imported['id']}").json()
    assert body["state"] == "previewed", (body["error"], body["preview"])
    return body


def rows(world: World, imported: dict[str, Any], *outcomes: str) -> list[dict[str, Any]]:
    query = "".join(f"&outcome={o}" for o in outcomes)
    found = api_as(world).get(f"/spreadsheet-imports/{imported['id']}/rows?limit=100{query}")
    assert found.status_code == 200, found.content
    items: list[dict[str, Any]] = found.json()["items"]
    return items


def committed(world: World, imported: dict[str, Any], **body: Any) -> dict[str, Any]:
    api = api_as(world)
    asked = api.post(f"/spreadsheet-imports/{imported['id']}/commit", body, headers=seen(imported))
    assert asked.status_code == 202, asked.content
    settle()
    out: dict[str, Any] = api.get(f"/spreadsheet-imports/{imported['id']}").json()
    return out


def values(entity: Entity) -> dict[str, Any]:
    found: dict[str, Any] = services.current_values(services.specific(entity))
    return found


def records(world: World, type_name: str = SAMPLE, key: str = "label") -> dict[str, Entity]:
    """The project's records of the type, by a field's value, in HRID order."""
    found = Entity.objects.filter(type=type_name, owner_project=world.project).order_by("hrid")
    return {str(values(e)[key]): e for e in found}


def mapped(imported: dict[str, Any]) -> dict[str, str | None]:
    return {c["header"]: c["field"] for c in imported["columns"]}


# --- The whole way ----------------------------------------------------------------------------


def test_a_sample_manifest_is_imported_as_records(world: World) -> None:
    kinases = proteins(world)
    imported = started(world, xlsx(("Samples", MANIFEST)))
    assert imported["state"] == "mapping"
    assert [s["name"] for s in imported["sheets"]] == ["Samples"]
    assert imported["sheet"] == "Samples"
    assert imported["header_row"] == 1
    assert imported["first_row"] == 2
    # Iki maps each header to the field it names, a quantity's with the unit it says.
    assert mapped(imported) == {
        "Label": "label",
        "Protein": "protein",
        "Purity (%)": "purity",
        "Conc. (ug/mL)": "concentration",
        "Tags": "tags",
        "Due on": "due_on",
    }
    assert imported["columns"][3]["unit"] == "ug/mL"
    # Reading them the way the sheet writes them is checked, and said.
    changed = change(world, imported, {"locale": {"decimal": "point"}})
    assert changed.status_code == 200, changed.content
    assert changed["ETag"] == f'"{imported["revision"] + 1}"'

    checked = previewed(world, changed.json())
    assert checked["preview"]["counts"]["create"] == 3
    assert checked["preview"]["counts"]["problem"] == 0

    done = committed(world, checked)
    assert done["state"] == "imported", done
    run = ActionRun.objects.get(pk=done["run"]["id"])
    made = records(world)
    assert list(made) == ["ABL1-KD-001", "ABL1-KD-002", "EGFR-KD-001"]
    first = values(made["ABL1-KD-001"])
    # A reference to a registered type is to its entry (ADR 0115).
    assert first["protein"] == Entity.objects.get(pk=kinases["ABL1"]["id"]).entry_id
    # 1,250 µg/mL, in the field's mg/mL.
    assert first["concentration"] == pytest.approx(1.25)
    assert first["tags"] == ["kinase", "crystallography"]
    assert str(first["due_on"]) == "2027-01-01"
    assert values(made["EGFR-KD-001"])["concentration"] == pytest.approx(2.1)
    # HRIDs in row order.
    hrids = [made[k].hrid for k in ("ABL1-KD-001", "ABL1-KD-002", "EGFR-KD-001")]
    assert hrids == sorted(hrids)
    # Each record says where it came from: the workbook, its sheet and its row.
    origin = ExternalOrigin.objects.get(entity=made["ABL1-KD-002"])
    assert origin.source == "XLSX workbook"
    assert origin.identifier == "Samples, row 3"
    assert str(origin.file_id) == done["file"]["id"]
    assert origin.run_id == run.pk
    assert run.requested_by == world.users["editor"]
    assert AuditEvent.objects.filter(
        action="spreadsheet.imported", actor=world.users["editor"]
    ).exists()
    # Its records are a search away: what the run wrote.
    listed = api_as(world, "viewer").get(f"/entities?run={run.hrid}&limit=10").json()
    assert sorted(e["hrid"] for e in listed["items"]) == hrids
    searched = api_as(world).post("/entities/search", {"run": str(run.pk), "type": SAMPLE}).json()
    assert len(searched["items"]) == 3
    hidden = api_as(world, "outsider").get(f"/entities?run={run.hrid}")
    assert hidden.status_code == 422


def test_a_csv_files_rows_are_named_by_their_row(world: World) -> None:
    proteins(world)
    sheet = [["Label", "Protein"], ["ABL1-KD-101", "ABL1"], [], ["ABL1-KD-102", "abl1"]]
    imported = started(world, csv_text(sheet).encode(), name="manifest.csv")
    assert imported["kind"] == "csv" and imported["sheet"] == "manifest"
    checked = previewed(world, imported)
    assert checked["preview"]["counts"]["blank"] == 1
    done = committed(world, checked)
    assert done["state"] == "imported", done["run_problems"]
    origin = ExternalOrigin.objects.get(entity=records(world)["ABL1-KD-102"])
    assert (origin.source, origin.identifier) == ("CSV file", "row 4")


# --- How numbers and dates read ----------------------------------------------------------------

# A manifest a lab in Germany exported: decimal commas, dates day first.
EUROPEAN: list[list[Any]] = [
    ["Label", "Purity (%)", "Conc. (mg/mL)", "Due on", "Bemerkung"],
    ["ABL1-KD-201", "98,5", "1,234", "03/04/2027", "gelöst in Puffer"],
    ["ABL1-KD-202", "97", "2,5", "28/04/2027", "Lösung trüb"],
]


def test_cells_that_read_two_ways_are_flagged_until_the_locale_is_said(world: World) -> None:
    imported = started(world, csv_text(EUROPEAN, ";").encode("cp1252"), name="proben.csv")
    assert imported["separator"] == ";" and imported["encoding"] == "cp1252"
    assert imported["locale"] == {"decimal": None, "dates": None, "serial_dates": False}
    # The sheet's own cells, as they read: those that read two ways say so.
    readings = {r["field"]: r["cells"] for r in imported["readings"]}
    assert set(readings) == {"purity", "concentration", "due_on"}
    assert [(c["cell"], c["reads_as"], c["ambiguous"]) for c in readings["concentration"]] == [
        ("1,234", None, True),
        ("2,5", "2.5 mg/mL", False),
    ]
    # A quantity in another unit than its field's reads in both, as it would be kept.
    sample = {
        r["field"]: r["cells"] for r in started(world, xlsx(("Samples", MANIFEST)))["readings"]
    }
    assert [c["reads_as"] for c in sample["concentration"]] == [
        "1\xa0250 ug/mL = 1.25 mg/mL",
        "980 ug/mL = 0.98 mg/mL",
        "2.1 mg/mL",
    ]
    assert readings["due_on"][0]["problem"] == (
        "reads as 3 April 2027 day first, and 4 March 2027 month first: say which the sheet's "
        "dates are."
    )
    assert readings["due_on"][1]["reads_as"] == "2027-04-28"

    checked = previewed(world, imported)
    assert checked["preview"]["counts"]["problem"] == 1
    assert checked["preview"]["counts"]["ambiguous"] == 1
    (row,) = rows(world, checked, "problem")
    assert row["row"] == 2
    assert {(p["column"], p["kind"]) for p in row["problems"]} == {
        (2, "ambiguous"),
        (3, "ambiguous"),
    }

    said = change(world, checked, {"locale": {"decimal": "comma", "dates": "day_first"}})
    assert said.status_code == 200, said.content
    body = said.json()
    # Saying it starts the preview again.
    assert body["state"] == "mapping" and body["preview"] is None
    readings = {r["field"]: r["cells"] for r in body["readings"]}
    assert readings["concentration"][0]["reads_as"] == "1.234 mg/mL"
    assert readings["due_on"][0]["reads_as"] == "2027-04-03"

    done = committed(world, previewed(world, body))
    assert done["state"] == "imported", done["run_problems"]
    first = values(records(world)["ABL1-KD-201"])
    assert first["concentration"] == pytest.approx(1.234)
    assert first["purity"] == pytest.approx(98.5)
    assert str(first["due_on"]) == "2027-04-03"
    # The run keeps how they were read, with what was imported.
    run = ActionRun.objects.get(pk=done["run"]["id"])
    assert run.params["locale"] == {"decimal": "comma", "dates": "day_first", "serial_dates": False}
    event = AuditEvent.objects.get(action="spreadsheet.imported")
    assert event.data["locale"]["decimal"] == "comma"


def test_a_locale_must_be_one_iki_reads(world: World) -> None:
    imported = started(world, csv_text(EUROPEAN).encode(), name="proben.csv")
    wrong = change(world, imported, {"locale": {"decimal": "dot"}})
    assert wrong.status_code == 422


def test_excel_serial_dates_are_read_once_the_person_says_so(world: World) -> None:
    sheet: list[list[Any]] = [["Label", "Due on"], ["ABL1-KD-301", 46388]]
    imported = started(world, xlsx(("Samples", sheet)))
    (reading,) = imported["readings"][0]["cells"]
    assert reading["stored"] is True
    assert "Excel serial dates (46388 is 1 January 2027)" in reading["problem"]
    said = change(world, imported, {"locale": {"serial_dates": True}}).json()
    assert said["readings"][0]["cells"][0]["reads_as"] == "2027-01-01"
    committed(world, previewed(world, said))
    assert str(values(records(world)["ABL1-KD-301"])["due_on"]) == "2027-01-01"


# --- Problems ----------------------------------------------------------------------------------

BAD: list[list[Any]] = [
    ["Label", "Protein", "Purity (%)", "Conc. (mg/mL)", "Due on"],
    ["ABL1-KD-401", "ABL1", 98.5, 1.2, "2027-01-04"],
    ["ABL1-KD-402", "ABL2", 101, "12 mM", "2027-01-05"],
    ["", "EGFR", 95, 2.0, "next week"],
    ["ABL1-KD-401", "EGFR", 95, 2.0, "2027-01-06"],
]


def test_what_is_wrong_with_each_row_is_shown_by_its_row_and_column(world: World) -> None:
    proteins(world)
    imported = started(world, xlsx(("Samples", BAD)))
    checked = previewed(world, imported)
    assert checked["preview"]["counts"]["create"] == 1
    assert checked["preview"]["counts"]["problem"] == 3
    found = {r["row"]: r["problems"] for r in rows(world, checked, "problem")}
    assert sorted(found) == [3, 4, 5]
    by_column = {p["column"]: p["detail"] for p in found[3]}
    assert by_column[1] == "“ABL2” is no protein in Kinase Atlas, by its ID, HRID or name."
    assert "Purity" in by_column[2] and "100" in by_column[2]
    assert by_column[3].startswith("“12 mM” is in a unit Iki can't convert to mg/mL")
    # A required field's empty cell, and a date that isn't one.
    assert {p["column"] for p in found[4]} == {0, 4}
    # A unique value an earlier row has.
    assert found[5] == [
        {
            "row": 5,
            "column": 0,
            "field": "label",
            "detail": "Label “ABL1-KD-401” is row 2’s too: no two share it.",
            "kind": None,
        }
    ]


def test_rows_with_problems_stop_the_import_unless_theyre_left_out(world: World) -> None:
    proteins(world)
    checked = previewed(world, started(world, xlsx(("Samples", BAD))))
    api = api_as(world)
    refused = api.post(f"/spreadsheet-imports/{checked['id']}/commit", {}, headers=seen(checked))
    assert problem(refused, 422, "validation_failed")["errors"][0]["pointer"] == "#/skip_problems"
    assert not Entity.objects.filter(type=SAMPLE).exists()
    done = committed(world, checked, skip_problems=True, reason="Rows 3-5 fixed in the next batch")
    assert done["state"] == "imported", done["run_problems"]
    assert list(records(world)) == ["ABL1-KD-401"]
    origin = ExternalOrigin.objects.get(entity=records(world)["ABL1-KD-401"])
    assert origin.identifier == "Samples, row 2"
    run = ActionRun.objects.get(pk=done["run"]["id"])
    assert run.reason == "Rows 3-5 fixed in the next batch"
    assert run.params["skip_problems"] is True


def test_a_unique_value_the_project_has_is_a_problem(world: World) -> None:
    proteins(world)
    made = api_as(world).post(
        "/entities",
        {"type": SAMPLE, "project": str(world.project.pk), "values": {"label": "ABL1-KD-001"}},
    )
    assert made.status_code == 201, made.content
    checked = previewed(world, started(world, xlsx(("Samples", MANIFEST))))
    (row,) = rows(world, checked, "problem")
    assert row["row"] == 2
    assert row["problems"][0]["detail"] == (
        f"Label “ABL1-KD-001” is {made.json()['hrid']}’s already: no two share it."
    )


def test_what_changed_since_the_preview_fails_the_run_and_says_where(world: World) -> None:
    proteins(world)
    checked = previewed(world, started(world, xlsx(("Samples", MANIFEST))))
    # Someone adds a sample with the same label before it's imported.
    made = api_as(world, "admin").post(
        "/entities",
        {"type": SAMPLE, "project": str(world.project.pk), "values": {"label": "ABL1-KD-002"}},
    )
    assert made.status_code == 201, made.content
    done = committed(world, checked)
    assert done["state"] == "failed"
    # Nothing was written: all or nothing.
    assert list(records(world)) == ["ABL1-KD-002"]
    assert [(p["row"], p["column"]) for p in done["run_problems"]] == [(3, 0)]


def test_a_plugin_that_changed_since_the_preview_means_previewing_again(world: World) -> None:
    checked = previewed(world, started(world, xlsx(("Samples", MANIFEST))))
    SpreadsheetImport.objects.filter(pk=checked["id"]).update(schema="0" * 64)
    refused = api_as(world).post(
        f"/spreadsheet-imports/{checked['id']}/commit", {}, headers=seen(checked)
    )
    assert "preview it again" in problem(refused, 409, "refused")["detail"]


# --- References ------------------------------------------------------------------------------


def test_references_are_found_by_hrid_or_name_in_the_project(world: World) -> None:
    kinases = proteins(world)
    other = Project.objects.create(team=world.team, name="Enzymes")
    ProjectMembership.objects.create(
        project=other, user=world.users["admin"], role=ProjectRole.EDITOR
    )
    # A protein named EGFR the editor can't see, and a second ABL1 they can.
    hidden = api_as(world, "admin").post(
        "/entities",
        {
            "type": PROTEIN,
            "project": str(other.pk),
            "values": {"name": "SRC", "sequence": "MGSNKSKPKDASQRRR"},
        },
    )
    assert hidden.status_code == 201, hidden.content
    second = (
        api_as(world)
        .post(
            "/entities",
            {
                "type": PROTEIN,
                "project": str(world.project.pk),
                "values": {
                    "name": "abl1",
                    "sequence": ABL2,
                },
            },
        )
        .json()
    )
    sheet: list[list[Any]] = [
        ["Label", "Protein"],
        ["S-1", kinases["EGFR"]["hrid"]],
        ["S-2", kinases["EGFR"]["id"]],
        ["S-3", "egfr"],
        ["S-4", "ABL1"],
        ["S-5", "SRC"],
        ["S-6", second["hrid"]],
    ]
    checked = previewed(world, started(world, xlsx(("Samples", sheet))))
    problems = {r["row"]: r["problems"][0]["detail"] for r in rows(world, checked, "problem")}
    both = ", ".join(sorted([kinases["ABL1"]["hrid"], second["hrid"]]))
    assert problems == {
        5: f"“ABL1” names 2 records here ({both}): give its HRID instead.",
        6: "“SRC” is no protein in Kinase Atlas, by its ID, HRID or name.",
    }
    done = committed(world, checked, skip_problems=True)
    assert done["state"] == "imported", done["run_problems"]
    egfr = Entity.objects.get(pk=kinases["EGFR"]["id"]).entry_id
    made = records(world)
    assert {k: values(e)["protein"] for k, e in made.items()} == {
        "S-1": egfr,
        "S-2": egfr,
        "S-3": egfr,
        "S-6": Entity.objects.get(pk=second["id"]).entry_id,
    }


# --- Duplicates --------------------------------------------------------------------------------

PUC19_MCS = "GAATTCGAGCTCGGTACCCGGGGATCCTCTAGAGTCGACCTGCAGGCATGCAAGCTT"
T7_PROMOTER = "TAATACGACTCACTATAGGGGAATTGTGAGCGGATAACAATTCC"
LAC_OPERATOR = "TTGTGAGCGGATAACAATTTCACACAGGAAACAGCTATGACCATG"
F1_ORI = "ACGCGCCCTGTAGCGGCGCATTAAGCGCGGCGGGTGTGGTGGTTACGCGCAGCGTGACCGCTACACTTGCCAGCGCCCTAGCGCC"


def test_duplicates_are_found_in_the_registry_the_project_and_the_sheet(world: World) -> None:
    other = Project.objects.create(team=world.team, name="Cloning")
    ProjectMembership.objects.create(
        project=other, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    api = api_as(world)

    def plasmid(project: Project, name: str, bases: str) -> dict[str, Any]:
        made = api.post(
            "/entities",
            {"type": PLASMID, "project": str(project.pk), "values": {"name": name, "bases": bases}},
        )
        assert made.status_code == 201, made.content
        body: dict[str, Any] = made.json()
        return body

    registered = plasmid(other, "pUC19 MCS", PUC19_MCS)
    here = plasmid(world.project, "T7 promoter", T7_PROMOTER)
    inventory: list[list[Any]] = [
        ["Name", "Bases", "Topology"],
        ["pUC19 MCS (from Cloning)", PUC19_MCS, "circular"],
        ["T7 promoter", T7_PROMOTER.lower(), "Circular"],
        ["lac operator", LAC_OPERATOR, "circular"],
        ["lac operator, again", LAC_OPERATOR, "circular"],
        ["f1 ori", F1_ORI, "linear"],
    ]
    imported = started(world, xlsx(("Plasmids", inventory)), type_name=PLASMID)
    assert mapped(imported) == {"Name": "name", "Bases": "bases", "Topology": "topology"}
    checked = previewed(world, imported)
    assert {
        k: checked["preview"]["counts"][k] for k in ("register", "add", "reuse", "same", "problem")
    } == {
        "register": 2,
        "add": 1,
        "reuse": 1,
        "same": 1,
        "problem": 0,
    }
    outcomes = {r["row"]: (r["outcome"], r["match"], r["same_as"]) for r in rows(world, checked)}
    assert outcomes[2] == ("add", Entity.objects.get(pk=registered["id"]).hrid, None)
    assert outcomes[3] == ("reuse", here["hrid"], None)
    assert outcomes[4][0] == "register" and outcomes[5] == ("same", "", 4)
    assert outcomes[6][0] == "register"

    done = committed(world, checked)
    assert done["state"] == "imported", done["run_problems"]
    mine = Entity.objects.filter(type=PLASMID, owner_project=world.project)
    # T7 was the project's already; lac operator's two rows are one record.
    assert mine.count() == 4
    entry = Entity.objects.get(pk=registered["id"]).entry_id
    assert mine.filter(entry_id=entry).exists()


# --- Who may ----------------------------------------------------------------------------------


def test_only_people_who_can_add_records_to_the_project_import(world: World) -> None:
    file_id = workbook(world, xlsx(("Samples", MANIFEST)))
    body = {"file": file_id, "type": SAMPLE}
    path = f"/projects/{world.project.pk}/spreadsheet-imports"
    assert api_as(world, "viewer").post(path, body).status_code == 403
    assert api_as(world, "outsider").post(path, body).status_code == 404
    archived = api_as(world, "editor").post(
        f"/projects/{world.archived.pk}/spreadsheet-imports", body
    )
    assert archived.status_code in (403, 409)


def test_a_workbook_must_be_one_the_project_has(world: World) -> None:
    other = Project.objects.create(team=world.team, name="Cloning")
    ProjectMembership.objects.create(
        project=other, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    elsewhere = add(
        api_as(world),
        world,
        xlsx(("Samples", MANIFEST)),
        name="m.xlsx",
        media_type=XLSX_TYPE,
        project=str(other.pk),
    )["file"]["id"]
    made = api_as(world).post(
        f"/projects/{world.project.pk}/spreadsheet-imports", {"file": elsewhere, "type": SAMPLE}
    )
    assert problem(made, 422, "validation_failed")["errors"][0]["pointer"] == "#/file"
    for wrong in ("acme/protein.Nothing", "iki/core.Entity"):
        made = api_as(world).post(
            f"/projects/{world.project.pk}/spreadsheet-imports", {"file": elsewhere, "type": wrong}
        )
        assert made.status_code == 422


def test_a_workbook_a_record_of_the_project_holds_is_one_it_has(world: World) -> None:
    # The project's Files are those added to it and those its records hold (ADR 0177): the
    # importer takes any of them, as its picker lists them.
    other = Project.objects.create(team=world.team, name="Cloning")
    ProjectMembership.objects.create(
        project=other, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    held = add(
        api_as(world),
        world,
        xlsx(("Samples", MANIFEST)),
        name="vendor-coa.xlsx",
        media_type=XLSX_TYPE,
        project=str(other.pk),
    )["file"]["id"]
    abl1 = proteins(world)["ABL1"]
    services.create(
        world.users["editor"],
        SAMPLE,
        world.project,
        {"label": "ABL1-KD-000", "additions": [{"material": abl1["id"], "certificate": held}]},
    )
    made = api_as(world).post(
        f"/projects/{world.project.pk}/spreadsheet-imports", {"file": held, "type": SAMPLE}
    )
    assert made.status_code == 201, made.content
    assert made.json()["file"]["id"] == held
    # Someone who can't read it can't import it, though the project holds it.
    outsider = Project.objects.create(team=world.team, name="Elsewhere")
    ProjectMembership.objects.create(
        project=outsider, user=world.users["outsider"], role=ProjectRole.EDITOR
    )
    refused = api_as(world, "outsider").post(
        f"/projects/{outsider.pk}/spreadsheet-imports", {"file": held, "type": SAMPLE}
    )
    assert problem(refused, 422, "validation_failed")["errors"][0]["pointer"] == "#/file"


def test_an_import_is_its_requesters_until_its_committed(world: World) -> None:
    proteins(world)
    imported = started(world, xlsx(("Samples", MANIFEST)))
    url = f"/spreadsheet-imports/{imported['id']}"
    # Another Editor of the project neither sees nor changes it.
    assert api_as(world, "admin").get(url).status_code == 404
    assert change(world, imported, {"header_row": 1}, "admin").status_code == 404
    assert api_as(world, "outsider").get(url).status_code == 404
    listed = api_as(world, "admin").get(f"/projects/{world.project.pk}/spreadsheet-imports").json()
    assert listed["items"] == []
    committed(world, previewed(world, imported))
    # Once it's committed, the project's readers see it, as they see its run.
    seen = api_as(world, "viewer").get(url).json()
    assert seen["state"] == "imported" and seen["can"]["commit"] is False
    assert api_as(world, "outsider").get(url).status_code == 404
    assert api_as(world, "viewer").get(f"{url}/rows").status_code == 200


def test_an_editor_removed_before_the_run_starts_imports_nothing(world: World) -> None:
    proteins(world)
    checked = previewed(world, started(world, xlsx(("Samples", MANIFEST))))
    asked = api_as(world).post(
        f"/spreadsheet-imports/{checked['id']}/commit",
        {"skip_problems": True},
        headers=seen(checked),
    )
    assert asked.status_code == 202, asked.content
    ProjectMembership.objects.filter(project=world.project, user=world.users["editor"]).delete()
    ProjectMembership.objects.filter(project=world.project, team=world.other_team).delete()
    settle()
    found = SpreadsheetImport.objects.get(pk=checked["id"])
    assert found.state == "failed" and "can no longer add records" in found.error
    assert not Entity.objects.filter(type=SAMPLE).exists()


# --- Saved mappings ----------------------------------------------------------------------------


def test_a_saved_mapping_maps_the_next_sheet_of_its_kind(world: World) -> None:
    imported = started(world, csv_text(EUROPEAN, ";").encode("cp1252"), name="proben.csv")
    api = api_as(world)
    mapped_now = change(
        world,
        imported,
        {
            "locale": {"decimal": "comma", "dates": "day_first"},
            "columns": [
                {"index": 0, "field": "label"},
                {"index": 1, "field": "purity"},
                {"index": 2, "field": "concentration", "unit": "mg/mL"},
                {"index": 3, "field": "due_on"},
            ],
        },
    ).json()
    # Its mapping as it was read, before that change, isn't what's kept.
    stale = api.post(
        f"/spreadsheet-imports/{imported['id']}/mappings",
        {"name": "Plate reader export"},
        headers=seen(imported),
    )
    problem(stale, 412, "stale")
    assert not SavedMapping.objects.exists()
    saved = api.post(
        f"/spreadsheet-imports/{imported['id']}/mappings",
        {"name": "Plate reader export"},
        headers=seen(mapped_now),
    )
    assert saved.status_code == 201, saved.content
    assert saved.json()["headers"] == ["Label", "Purity (%)", "Conc. (mg/mL)", "Due on"]
    assert saved.json()["locale"]["decimal"] == "comma"
    again = api.post(
        f"/spreadsheet-imports/{imported['id']}/mappings",
        {"name": "Plate reader export"},
        headers=seen(mapped_now),
    )
    assert again.status_code == 422

    # Next week's export, its columns in another order.
    moved = [[r[3], r[0], r[2], r[1], r[4]] for r in EUROPEAN]
    second = started(world, csv_text(moved, ";").encode("cp1252"), name="proben-2.csv")
    assert second["mapping"]["name"] == "Plate reader export"
    assert mapped(second) == {
        "Due on": "due_on",
        "Label": "label",
        "Conc. (mg/mL)": "concentration",
        "Purity (%)": "purity",
        "Bemerkung": None,
    }
    assert second["locale"]["dates"] == "day_first"
    assert AuditEvent.objects.filter(action="spreadsheet_mapping.saved").count() == 1
    listed = api.get(f"/projects/{world.project.pk}/spreadsheet-mappings?type={SAMPLE}").json()
    assert [m["name"] for m in listed] == ["Plate reader export"]
    assert (
        api_as(world, "viewer").delete(f"/spreadsheet-mappings/{listed[0]['id']}").status_code
        == 403
    )
    assert api.delete(f"/spreadsheet-mappings/{listed[0]['id']}").status_code == 204


def test_a_committed_import_keeps_the_mapping_it_applied(world: World) -> None:
    owner, colleague = api_as(world), api_as(world, "via_team")
    original = started(world, b"Label,Purity (%)\nS-1,95\n", name="first.csv")
    ref = f"/spreadsheet-imports/{original['id']}"
    saved = owner.post(f"{ref}/mappings", {"name": "Instrument v1"}, headers=seen(original))
    assert saved.status_code == 201, saved.content
    mapping_id = saved.json()["id"]
    previewed(world, original)

    next_sheet = started(
        world, b"Label,Purity (%)\nS-2,85\n", name="second.csv", principal="via_team"
    )
    next_ref = f"/spreadsheet-imports/{next_sheet['id']}"
    changed = colleague.patch(
        next_ref, {"columns": [{"index": 0, "field": "label"}]}, headers=seen(next_sheet)
    )
    assert changed.status_code == 200, changed.content
    replacement = colleague.post(
        f"{next_ref}/mappings",
        {"name": "Instrument v2", "replace": mapping_id},
        headers=seen(changed.json()),
    )
    assert replacement.status_code == 201, replacement.content
    assert replacement.json()["columns"] == [{"header": "Label", "field": "label"}]

    accepted = owner.post(f"{ref}/commit", {}, headers=seen(original))
    assert accepted.status_code == 202, accepted.content
    pinned = accepted.json()["applied_mapping"]
    assert pinned["template"] == {"id": mapping_id, "name": "Instrument v1", "match": 1.0}
    assert [(c["index"], c["field"]) for c in pinned["columns"]] == [(0, "label"), (1, "purity")]
    settle()
    done = colleague.get(ref).json()
    assert done["state"] == "imported", done
    run = ActionRun.objects.get(pk=done["run"]["id"])
    assert run.params["header_row"] == pinned["header_row"]
    assert run.params["locale"] == pinned["locale"]
    assert run.params["columns"] == [
        {key: value for key, value in col.items() if key != "index"} for col in pinned["columns"]
    ]
    assert values(records(world)["S-1"])["purity"] == 95

    late = owner.post(f"{ref}/mappings", {"name": "Saved after the import"}, headers=seen(original))
    assert late.status_code == 201, late.content
    assert late.json()["id"] != mapping_id
    assert colleague.delete(f"/spreadsheet-mappings/{mapping_id}").status_code == 204
    history = colleague.get(ref).json()
    assert history["mapping"] == pinned["template"]
    assert history["applied_mapping"] == pinned


# --- Revisions --------------------------------------------------------------------------------

# A manifest with a sheet about it first: Iki starts on that one, two rows long.
ABOUT: list[list[Any]] = [
    ["Expression core: kinase domain constructs"],
    ["Batch 12, purified November 2026. Concentrations by A280."],
]
TITLED: list[list[Any]] = [["Kinase domains, batch 12"], [], *MANIFEST, ["Total", "", "", 3230]]


def stored(imported: dict[str, Any]) -> dict[str, Any]:
    found = SpreadsheetImport.objects.get(pk=imported["id"])
    return {
        "revision": found.revision,
        "sheet": found.sheet,
        "header_row": found.header_row,
        "first_row": found.first_row,
        "last_row": found.last_row,
        "skip": found.skip,
        "state": found.state,
        "round": found.round,
    }


def test_a_change_from_a_revision_it_has_moved_on_from_changes_nothing(world: World) -> None:
    imported = started(world, xlsx(("About", ABOUT), ("Samples", TITLED)))
    # Iki's reading is a revision of its own: what it imports to start with.
    assert imported["revision"] == 2 and imported["sheet"] == "About"
    url = f"/spreadsheet-imports/{imported['id']}"
    assert api_as(world).get(url)["ETag"] == '"2"'

    # The sheet is chosen; a header row typed while that was on its way was typed against
    # the revision before it, and its request arrives after.
    chose = change(world, imported, {"sheet": "Samples"})
    assert chose.status_code == 200, chose.content
    assert chose["ETag"] == '"3"' and chose.json()["revision"] == 3
    before = stored(imported)
    late = change(world, imported, {"header_row": 3})
    body = problem(late, 412, "stale")
    assert "revision 3" in body["detail"]
    assert late["ETag"] == '"3"'
    assert stored(imported) == before

    # A preview, a commit and a saved mapping of a revision it's moved on from are refused
    # too, and nothing starts.
    api = api_as(world)
    problem(api.post(f"{url}/preview", headers=seen(imported)), 412, "stale")
    problem(api.post(f"{url}/commit", {}, headers=seen(imported)), 412, "stale")
    problem(api.post(f"{url}/mappings", {"name": "Core"}, headers=seen(imported)), 412, "stale")
    assert stored(imported) == before
    assert not SavedMapping.objects.exists()

    # Without the revision it was read at, nothing is changed either.
    problem(api.patch(url, {"header_row": 3}), 428, "precondition_required")
    problem(api.post(f"{url}/preview"), 428, "precondition_required")
    problem(api.post(f"{url}/commit", {}), 428, "precondition_required")
    problem(api.post(f"{url}/mappings", {"name": "Core"}), 428, "precondition_required")
    wrong = api.patch(url, {"header_row": 3}, headers={"If-Match": '"3", "2"'})
    problem(wrong, 412, "precondition_failed")
    assert stored(imported) == before

    # From what it is now, it's saved.
    again = change(world, chose.json(), {"header_row": 3})
    assert again.status_code == 200, again.content
    assert (again.json()["header_row"], again.json()["first_row"]) == (3, 4)
    assert stored(imported)["revision"] == 4


def test_a_sheet_and_its_header_row_are_checked_together(world: World) -> None:
    imported = started(world, xlsx(("About", ABOUT), ("Samples", TITLED)))
    # Row 3 isn't one of About's two rows: nothing is saved, not even the rest.
    alone = change(world, imported, {"header_row": 3, "skip": [[2, 2]]})
    errors = problem(alone, 422, "validation_failed")["errors"]
    assert errors == [
        {"pointer": "#/header_row", "detail": "must be a row of the sheet, from 1 to 2, or 0."}
    ]
    assert stored(imported)["skip"] == [] and stored(imported)["revision"] == 2

    # With the sheet it's on, it's one change, checked against that sheet.
    both = change(world, imported, {"sheet": "Samples", "header_row": 3})
    assert both.status_code == 200, both.content
    body = both.json()
    assert (body["sheet"], body["header_row"], body["first_row"]) == ("Samples", 3, 4)
    assert mapped(body)["Purity (%)"] == "purity"
    assert body["revision"] == 3

    # Back to About with a row of Samples: refused whole, so Samples stays as it was.
    back = change(world, body, {"sheet": "About", "header_row": 5})
    assert problem(back, 422, "validation_failed")["errors"][0]["pointer"] == "#/header_row"
    assert stored(imported)["sheet"] == "Samples" and stored(imported)["header_row"] == 3


def test_another_sheet_takes_its_rows_afresh(world: World) -> None:
    imported = started(world, xlsx(("About", ABOUT), ("Samples", TITLED)))
    chosen = change(
        world,
        imported,
        {"sheet": "Samples", "header_row": 3, "last_row": 6, "skip": [[5, 5]]},
    ).json()
    assert (chosen["first_row"], chosen["last_row"], chosen["skip"]) == (4, 6, [[5, 5]])
    # Samples' rows mean nothing on About: its own are taken, as Iki first found them.
    about = change(world, chosen, {"sheet": "About"}).json()
    assert {k: about[k] for k in ("sheet", "header_row", "first_row", "last_row", "skip")} == {
        "sheet": "About",
        "header_row": 1,
        "first_row": 2,
        "last_row": None,
        "skip": [],
    }
    # Unless they're given with it.
    given = change(world, about, {"sheet": "Samples", "header_row": 3, "first_row": 5}).json()
    assert (given["header_row"], given["first_row"], given["skip"]) == (3, 5, [])


def test_a_preview_checks_the_revision_that_was_read(world: World) -> None:
    proteins(world)
    imported = started(world, xlsx(("Samples", MANIFEST)))
    checked = previewed(world, imported)
    # Checking it doesn't change what it imports: the same revision commits.
    assert checked["revision"] == imported["revision"]
    moved = change(world, checked, {"first_row": 3}).json()
    # What was checked isn't what it imports now: it's checked again before it's imported.
    refused = api_as(world).post(
        f"/spreadsheet-imports/{checked['id']}/commit", {}, headers=seen(checked)
    )
    problem(refused, 412, "stale")
    done = committed(world, previewed(world, moved))
    assert done["state"] == "imported", done["run_problems"]
    assert list(records(world)) == ["ABL1-KD-002", "EGFR-KD-001"]


# --- Workbooks Iki won't read ------------------------------------------------------------------


def test_a_workbook_that_cant_be_read_fails_with_why(world: World) -> None:
    imported = started(
        world, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 600, name="locked.xlsx"
    )
    assert imported["state"] == "failed"
    assert "remove the password" in imported["error"]


# --- Housekeeping ------------------------------------------------------------------------------


def test_a_discarded_import_keeps_nothing_of_its_preview(world: World) -> None:
    checked = previewed(world, started(world, xlsx(("Samples", MANIFEST))))
    assert PreviewRow.objects.filter(spreadsheet_import_id=checked["id"]).count() == 3
    assert api_as(world).delete(f"/spreadsheet-imports/{checked['id']}").status_code == 204
    settle()
    assert not PreviewRow.objects.filter(spreadsheet_import_id=checked["id"]).exists()
    assert SpreadsheetImport.objects.get(pk=checked["id"]).state == "discarded"
    # The workbook stays in the project.
    assert api_as(world).get(f"/files/{checked['file']['id']}").status_code == 200


def test_abandoned_imports_are_discarded(world: World) -> None:
    from datetime import timedelta

    from django.utils import timezone

    from iki_server.spreadsheets import services as sheets

    checked = previewed(world, started(world, xlsx(("Samples", MANIFEST))))
    SpreadsheetImport.objects.filter(pk=checked["id"]).update(
        updated_at=timezone.now() - timedelta(days=15)
    )
    assert sheets.expire() == 1
    assert SpreadsheetImport.objects.get(pk=checked["id"]).state == "discarded"


def test_a_preview_whose_runner_went_away_goes_on(world: World) -> None:
    from datetime import timedelta

    from django.utils import timezone

    from iki_server.spreadsheets import services as sheets
    from iki_server.tasks.models import TaskRecord

    imported = started(world, xlsx(("Samples", MANIFEST)))
    asked = api_as(world).post(
        f"/spreadsheet-imports/{imported['id']}/preview", headers=seen(imported)
    )
    assert asked.status_code == 202, asked.content
    # Its task is lost: the runner died with it.
    TaskRecord.objects.filter(task_path__endswith="preview_import").delete()
    SpreadsheetImport.objects.filter(pk=imported["id"]).update(
        updated_at=timezone.now() - timedelta(minutes=11)
    )
    assert sheets.recover() == 1
    settle()
    assert SpreadsheetImport.objects.get(pk=imported["id"]).state == "previewed"


def test_reserved_vendor_code_is_a_field_problem_before_a_tube_is_imported(world: World) -> None:
    from test_storage import TUBE

    uploaded = started(world, b"Barcode\nVendor-43\niKi:unassigned\n", TUBE, name="tubes.csv")
    checked = previewed(world, uploaded)
    problems = rows(world, checked, "problem")
    assert len(problems) == 1 and problems[0]["row"] == 3
    assert any(p["field"] == "barcode" and "IKI:" in p["detail"] for p in problems[0]["problems"])
    assert checked["preview"]["counts"]["create"] == 1
    refused = api_as(world).post(
        f"/spreadsheet-imports/{uploaded['id']}/commit", {}, headers=seen(checked)
    )
    assert problem(refused, 422, "validation_failed")["errors"][0]["pointer"] == "#/skip_problems"
    assert not records(world, TUBE, "barcode")
    done = committed(world, checked, skip_problems=True, reason="Import the valid supplier row")
    assert done["state"] == "imported"
    assert list(records(world, TUBE, "barcode")) == ["Vendor-43"]
