"""A search's results as CSV (ADR 0175): the grid's columns and values, as the reader can
read them, every page of them streamed, each download in the audit log, and no cell a
spreadsheet would run as a formula (OWASP's CSV injection guidance).

The proteins are human ones, with UniProt's accessions and masses: insulin (P01308,
11,981 Da), ABL1 (P00519, 122,873 Da) and SRC (P12931, 59,835 Da).
"""

import csv
import io
import json
from typing import Any

import pytest
from api_client import Api, problem
from org_world import World, build_world
from test_api_entities import api_as
from test_oauth import bearer, signed_in_token

from iki_server.accounts.models import User
from iki_server.api import csvs
from iki_server.api import entities as api_entities
from iki_server.audit.models import AuditEvent
from iki_server.entities import search, services
from iki_server.org.models import Project

pytestmark = pytest.mark.django_db

PROTEIN = "acme/protein.Protein"


@pytest.fixture
def world() -> World:
    return build_world()


def protein(
    user: User, project: Project, name: str, sequence: str, **values: Any
) -> services.Saved:
    """As a run records one, so its computed mass can be given."""
    return services.create(
        user, PROTEIN, project, {"name": name, "sequence": sequence, **values}, by_run=True
    )


@pytest.fixture
def lab(world: World) -> dict[str, Any]:
    editor = world.users["editor"]
    insulin = protein(
        editor,
        world.project,
        "Insulin",
        "MALWMRLLPLLALLALWGPDPAAAFVNQHLCGSHLVEALYLVCGERGFFYTPKTRREAEDLQVGQVELGGGPG",
        molecular_weight=11981,
        uniprot_ids=["P01308"],
        host="Homo sapiens",
        temperature_optimum=37,
    ).entity
    abl1 = protein(
        editor,
        world.project,
        "ABL1",
        "MLEICLKLVGCKSKKGLSSSSSCYLEEALQRPVASDFEPQGLSEAARWNSKENLLAGPSENDPNLFVALYDFVASGDNTLSITKGEKLRVLG",
        molecular_weight=122873,
        uniprot_ids=["P00519"],
        temperature_optimum=-20,
    ).entity
    return {"insulin": insulin, "abl1": abl1}


def download(api: Api, **params: Any) -> tuple[Any, list[list[str]]]:
    query = {k: json.dumps(v) if k == "where" else v for k, v in params.items()}
    response = api.get("/entities/search.csv", **query)
    if response.status_code != 200:
        return response, []
    text = response.getvalue().decode()
    return response, list(csv.reader(io.StringIO(text)))


def by_header(rows: list[list[str]]) -> list[dict[str, str]]:
    header, *body = rows
    return [dict(zip(header, row, strict=True)) for row in body]


def test_a_types_results_download_with_the_grids_columns_and_values(
    world: World, lab: dict[str, Any]
) -> None:
    response, rows = download(
        api_as(world, "viewer"),
        type=PROTEIN,
        project=str(world.project.pk),
        sort="name",
    )
    assert response.status_code == 200
    assert response.streaming
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    assert response["Cache-Control"] == "no-store"
    assert response["Content-Disposition"] == 'attachment; filename="Kinase Atlas Proteins.csv"'
    header = rows[0]
    assert header[0] == "ID"
    # A quantity's unit is in its heading, in the unit the grid shows: a mass in kDa.
    assert "Molecular weight (kDa)" in header
    assert "Temperature optimum (°C)" in header
    assert header[-3:] == ["Last changed", "Revision", "UUID"]
    found = by_header(rows)
    assert [r["Name"] for r in found] == ["ABL1", "Insulin"]
    insulin = found[1]
    assert insulin["ID"] == lab["insulin"].hrid
    assert insulin["Molecular weight (kDa)"] == "11.981"
    assert insulin["Temperature optimum (°C)"] == "37"
    assert insulin["Uniprot ids"] == "P01308"
    assert insulin["Host"] == "Homo sapiens"
    assert insulin["Revision"] == "1"
    assert insulin["UUID"] == str(lab["insulin"].pk)
    assert insulin["Last changed"].endswith("Z")
    # A negative number stays a number.
    assert found[0]["Temperature optimum (°C)"] == "-20"
    assert found[0]["Molecular weight (kDa)"] == "122.873"


def test_without_a_type_it_has_every_entitys_own_columns(world: World, lab: dict[str, Any]) -> None:
    _, rows = download(api_as(world, "viewer"), project=str(world.project.pk))
    assert rows[0] == ["ID", "Type", "Title", "Last changed", "Revision", "UUID"]
    assert [r[1] for r in rows[1:]] == ["Protein", "Protein"]


def test_it_has_the_columns_asked_for_in_their_order(world: World, lab: dict[str, Any]) -> None:
    _, rows = download(
        api_as(world, "viewer"),
        type=PROTEIN,
        columns=["uniprot_ids", "hrid", "molecular_weight"],
        sort="name",
    )
    assert rows == [
        ["Uniprot ids", "ID", "Molecular weight (kDa)"],
        ["P00519", lab["abl1"].hrid, "122.873"],
        ["P01308", lab["insulin"].hrid, "11.981"],
    ]
    response, _ = download(api_as(world, "viewer"), type=PROTEIN, columns=["colour"])
    assert problem(response, 422, "validation_failed")["errors"] == [
        {"parameter": "columns/0", "detail": "acme/protein.Protein has no column `colour`."}
    ]


def test_a_search_downloads_what_it_finds_in_its_order(world: World, lab: dict[str, Any]) -> None:
    warm = {"field": "temperature_optimum", "op": "gte", "value": 0}
    _, rows = download(api_as(world, "viewer"), type=PROTEIN, where=warm)
    assert [r["Name"] for r in by_header(rows)] == ["Insulin"]
    _, rows = download(
        api_as(world, "viewer"), type=PROTEIN, sort="temperature_optimum", descending=True
    )
    assert [r["Name"] for r in by_header(rows)] == ["Insulin", "ABL1"]


def test_every_page_is_streamed(
    world: World, lab: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A large result is written a page at a time, by keyset, never held whole."""
    monkeypatch.setattr(api_entities, "EXPORT_PAGE", 2)
    for n in range(3):
        protein(world.users["editor"], world.project, f"Fragment {n}", "MKV" + "A" * n)
    _, rows = download(api_as(world, "viewer"), type=PROTEIN, sort="name")
    assert [r["Name"] for r in by_header(rows)] == [
        "ABL1",
        "Fragment 0",
        "Fragment 1",
        "Fragment 2",
        "Insulin",
    ]


def test_a_page_that_takes_too_long_ends_the_file_saying_so(
    world: World, lab: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never a file that looks whole when it isn't."""
    monkeypatch.setattr(api_entities, "EXPORT_PAGE", 1)
    first = search.page

    def slow(found: Any, wanted: Any, cursor: str | None, limit: int) -> Any:
        if cursor is not None:
            raise search.TooSlow
        return first(found, wanted, cursor, limit)

    monkeypatch.setattr(search, "page", slow)
    _, rows = download(api_as(world, "viewer"), type=PROTEIN, columns=["name"], sort="name")
    assert rows == [
        ["Name"],
        ["ABL1"],
        ["The download stopped here: a page of it took too long to read."],
    ]


def test_only_what_the_reader_can_read(world: World, lab: dict[str, Any]) -> None:
    """Another project's records never appear, nor anything for someone who reads none."""
    other = build_world("b")
    protein(other.users["editor"], other.project, "SRC", "MGSNKSKPKDASQRRRSLEPAENVHGAG")
    _, rows = download(api_as(world, "viewer"), type=PROTEIN)
    assert sorted(r["Name"] for r in by_header(rows)) == ["ABL1", "Insulin"]
    response, rows = download(api_as(world, "outsider"), type=PROTEIN)
    assert response.status_code == 200
    assert len(rows) == 1
    # Asking for a project they can't read says nothing of it.
    response, _ = download(api_as(other, "viewer"), project=str(world.project.pk))
    problem(response, 404, "not_found")


def test_a_token_that_only_reads_downloads_it(world: World, lab: dict[str, Any]) -> None:
    _, tokens = signed_in_token(world, scope="read", projects=[world.project])
    response = bearer(tokens["access_token"], "GET", f"/entities/search.csv?type={PROTEIN}")
    assert response.status_code == 200
    assert b"Insulin" in response.getvalue()


def test_each_download_is_in_the_audit_log(world: World, lab: dict[str, Any]) -> None:
    warm = {"field": "temperature_optimum", "op": "gte", "value": 0}
    download(api_as(world, "viewer"), type=PROTEIN, project=str(world.project.pk), where=warm)
    event = AuditEvent.objects.get(action="search.exported")
    assert event.actor == world.users["viewer"]
    assert event.project == world.project
    assert event.data["format"] == "csv"
    assert event.data["file"] == "Kinase Atlas Proteins.csv"
    assert event.data["search"] == {
        "type": PROTEIN,
        "exact_type": False,
        "project": str(world.project.pk),
        "archived": None,
        "q": None,
        "collection": None,
        "where": warm,
        "sort": None,
        "descending": False,
    }
    assert event.data["columns"][0] == "hrid"
    # A search that can't run downloads nothing, so nothing is recorded.
    response, _ = download(api_as(world, "viewer"), type=PROTEIN, where={"field": "colour"})
    problem(response, 422, "validation_failed")
    assert AuditEvent.objects.filter(action="search.exported").count() == 1


@pytest.mark.parametrize(
    ("name", "cell"),
    [
        ('=HYPERLINK("https://example.com","x")', '\'=HYPERLINK("https://example.com","x")'),
        ("+1 555 0100", "'+1 555 0100"),
        ("-2+3+cmd|' /C calc'!A0", "'-2+3+cmd|' /C calc'!A0"),
        ("@SUM(1+1)", "'@SUM(1+1)"),
        ("\t=1+1", "'\t=1+1"),
        ("＝1+1", "'＝1+1"),  # noqa: RUF001 - a full-width equals sign
        ("-5", "-5"),
        ("ABL1; =1+1", "ABL1; =1+1"),
    ],
)
def test_no_cell_is_a_formula_a_spreadsheet_would_run(world: World, name: str, cell: str) -> None:
    """OWASP's CSV injection guidance: a cell starting =, +, -, @, a tab, a carriage return
    or their full-width forms starts with a quote; every cell is quoted, so a separator
    inside one can't start another. A number stays a number."""
    protein(world.users["editor"], world.project, name, "MKV")
    response = api_as(world, "viewer").get("/entities/search.csv", type=PROTEIN, columns=["name"])
    text = response.getvalue().decode()
    assert text.startswith('"Name"\r\n')
    (row,) = list(csv.reader(io.StringIO(text)))[1:]
    assert row == [cell]


def test_a_bad_where_points_at_the_mistake(world: World) -> None:
    response = api_as(world, "viewer").get("/entities/search.csv", type=PROTEIN, where="{")
    assert problem(response, 422, "validation_failed")["errors"] == [
        {"parameter": "where", "detail": "must be a condition, as JSON."}
    ]


@pytest.mark.django_db(transaction=True)
def test_served_over_asgi_each_page_is_read_as_it_is_sent(
    world: World, lab: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """In production a download is an async stream, each page read in the request's
    thread: Django warns (and pytest fails) if it had to read every page first."""
    from asgiref.sync import async_to_sync
    from django.test import AsyncClient

    monkeypatch.setattr(api_entities, "EXPORT_PAGE", 1)
    _, tokens = signed_in_token(world, scope="read", projects=[world.project])

    async def downloaded() -> tuple[Any, bytes]:
        got: Any = await AsyncClient().get(
            f"/api/v1/entities/search.csv?type={PROTEIN}&sort=name",
            secure=True,
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        return got, b"".join([chunk async for chunk in got.streaming_content])

    got, body = async_to_sync(downloaded)()
    assert got.status_code == 200
    rows = list(csv.reader(io.StringIO(body.decode())))
    assert [r["Name"] for r in by_header(rows)] == ["ABL1", "Insulin"]


@pytest.mark.parametrize(
    ("value", "cell"),
    [
        ("=1+1", "'=1+1"),
        ("\r=1+1", "'\r=1+1"),
        ("\n=1+1", "'\n=1+1"),
        ("＠SUM(A1)", "'＠SUM(A1)"),  # noqa: RUF001 - a full-width at sign
        ("－1", "'－1"),  # noqa: RUF001 - a full-width minus
        ("+5", "'+5"),
        (-20, "-20"),
        ("-0.5", "-0.5"),
        ("-1e-09", "-1e-09"),
        ("-1e-09+1", "'-1e-09+1"),
        ("GAATTC", "GAATTC"),
        (None, ""),
    ],
)
def test_a_cell_that_would_run_starts_with_a_quote(value: object, cell: str) -> None:
    assert csvs.cell(value) == cell
