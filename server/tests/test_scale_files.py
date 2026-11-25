"""Load test (ADR 0177): a project's Files stay quick at a million files.

A project holds ``IKI_SCALE_FILES`` files (1,000,000 by default), copied in bulk as an
instrument core's would accumulate: plates of Sanger traces, flow cytometry runs, images
and PDFs, most with the day their data was acquired, some without. A few files from
another project are held by its records, so both of the list's sets are read. Then a
viewer opens the list's pages: the first, the next, by name, by acquisition, by format,
by a range of days, and searched by part of a name. Each must answer within the budget,
and a name search is planned through the trigram index. ``make check-scale-files`` runs
it; a thousand files run with every check, so the copies are known to load first.
"""

import os
import statistics
import time
from collections.abc import Iterator
from typing import Any

import pytest
from api_client import Api
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from org_world import World, build_world

from iki_server.entities import services as entities
from iki_server.files import listing
from iki_server.files.models import Blob, File
from iki_server.org.models import Project, ProjectMembership, ProjectRole

pytestmark = pytest.mark.django_db(transaction=True)

FILES = int(os.environ.get("IKI_SCALE_FILES", "1000000"))
SMALL = 1_000
LIMIT = 50
#: What a page may take at the server, at the median of a few tries.
BUDGET = 0.5
STRUCTURE = "acme/protein.ProteinStructure"

#: The n-th copy's name: plate, well, clone and read, as a sequencer names its traces; or
#: a cytometer's run, a microscope's image, a certificate.
NAME = """
CASE n %% 4
  WHEN 0 THEN 'plate-' || lpad((n / 96)::text, 5, '0') || '_'
    || chr(65 + (n %% 96) / 12) || lpad((1 + n %% 12)::text, 2, '0')
    || '_clone-' || n || '-F.ab1'
  WHEN 1 THEN 'flow-run-' || n || '_tube-' || (n %% 48) || '.fcs'
  WHEN 2 THEN 'IMG_' || lpad(n::text, 7, '0') || '.tif'
  ELSE 'coa-lot-' || n || '.pdf'
END
"""
MEDIA_TYPE = """
CASE n %% 4
  WHEN 0 THEN 'application/x-abi'
  WHEN 1 THEN 'application/vnd.isac.fcs'
  WHEN 2 THEN 'image/tiff'
  ELSE 'application/pdf'
END
"""
#: Traces, runs and images know when their data was acquired; certificates don't. Any
#: day of six years from 2020.
ACQUIRED_ON = "CASE WHEN n %% 4 = 3 THEN NULL ELSE date '2020-01-01' + n %% 2190 END"
#: Each cytometer run says which cytometer and which of 800 people ran it (ADR 0224).
OPERATOR = "'user' || lpad(((n / 4) %% 800)::text, 4, '0')"
FACTS = f"""
CASE WHEN n %% 4 = 1 THEN jsonb_build_object(
  'instrument', jsonb_build_object('value', 'LSRFortessa X-20', 'stated_in', 'FCS keyword $CYT'),
  'operator', jsonb_build_object('value', {OPERATOR}, 'stated_in', 'FCS keyword $OP'))
ELSE '{{}}'::jsonb END
"""
STATED_TEXT = f"CASE WHEN n %% 4 = 1 THEN 'LSRFortessa X-20' || chr(10) || {OPERATOR} ELSE '' END"


def _sql(statement: str, params: list[Any] | None = None) -> list[tuple[Any, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(statement, params)
        return cursor.fetchall() if cursor.description else []


def fill(project: Project, files: int) -> None:
    """``files`` files in ``project``, each with a blob of its own, newest last."""
    _sql(
        "INSERT INTO iki_blob (id, sha256, size, storage_uri, registered_external, "
        "object_version, created_at) "
        "SELECT (lpad(to_hex(1577836800000 + n), 12, '0') || '70008000' "
        "  || lpad(to_hex(n), 12, '0'))::uuid, "
        "lpad(to_hex(n), 64, '0'), 1024 + n %% 5000000, 's3://iki/scale/' || n, false, '', "
        "now() "
        "FROM generate_series(1, %s) AS n",
        [files],
    )
    _sql(
        "INSERT INTO iki_file (id, blob_id, name, media_type, project_id, created_at, "  # noqa: S608 - its own SQL
        "acquired_on, acquired_at, acquired_from, facts, stated_text) "
        "SELECT (lpad(to_hex(1577836800000 + n), 12, '0') || '70018000' "
        "  || lpad(to_hex(n), 12, '0'))::uuid, "
        "(lpad(to_hex(1577836800000 + n), 12, '0') || '70008000' "
        "  || lpad(to_hex(n), 12, '0'))::uuid, "
        f"{NAME}, {MEDIA_TYPE}, %s, "
        "timestamptz '2020-01-01 00:00:00+00' + n * interval '1 second', "
        f"{ACQUIRED_ON}, coalesce(({ACQUIRED_ON})::text, ''), "
        "CASE WHEN n %% 4 = 3 THEN '' ELSE 'abif' END, "
        f"{FACTS}, {STATED_TEXT} "
        "FROM generate_series(1, %s) AS n",
        [project.pk, files],
    )
    for table in ("iki_blob", "iki_file"):
        _sql(f"VACUUM (ANALYZE) {table}")


def _timed(client: Api, query: dict[str, Any], path: str = "/files") -> tuple[float, Any]:
    """The median of five tries, after one to warm up; and what it answered."""
    answer = client.get(path, **query)
    assert answer.status_code == 200, (query, answer.content[:300])
    took = []
    for _ in range(5):
        began = time.perf_counter()
        again = client.get(path, **query)
        took.append(time.perf_counter() - began)
        assert again.status_code == 200, (query, again.content[:300])
    return statistics.median(took), answer.json()


def _plans(client: Api, query: dict[str, Any]) -> list[dict[str, Any]]:
    """How Postgres plans each statement that reads files for the page ``query`` asks for,
    printed as it ran, for the load test's report."""
    with CaptureQueriesContext(connection) as ran:
        answer = client.get("/files", **query)
    assert answer.status_code == 200, answer.content[:300]
    statements = [
        q["sql"]
        for q in ran.captured_queries
        if q["sql"].startswith("SELECT") and 'FROM "iki_file"' in q["sql"]
    ]
    plans = []
    with transaction.atomic(), connection.cursor() as cursor:
        for statement in statements:
            # The name search's probe of the trigram index, as it runs: on its own.
            probe = f"LIMIT {listing.NAMED_MAX + 1}" in statement
            for setting in ("enable_seqscan", "enable_indexscan", "enable_indexonlyscan"):
                cursor.execute(
                    "SELECT set_config(%s, %s, true)", [setting, "off" if probe else "on"]
                )
            cursor.execute(f"EXPLAIN (FORMAT JSON) {statement}")
            plans.append(cursor.fetchone()[0][0]["Plan"])
            cursor.execute(f"EXPLAIN (ANALYZE, BUFFERS, COSTS OFF) {statement}")
            print("\n".join(row[0] for row in cursor.fetchall()))  # noqa: T201 - the report
    return plans


def _nodes(plan: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield plan
    for child in plan.get("Plans", []):
        yield from _nodes(child)


@pytest.mark.parametrize(
    "files",
    [
        pytest.param(SMALL, id="small"),
        pytest.param(FILES, id="scale", marks=pytest.mark.scale),
    ],
)
def test_a_projects_files_stay_quick(files: int) -> None:
    world: World = build_world()
    fill(world.project, files)
    # Files from elsewhere that the project's records hold: the list's other set.
    elsewhere = Project.objects.create(team=world.team, name="Structure Core")
    editor = world.users["editor"]
    ProjectMembership.objects.create(project=elsewhere, user=editor, role=ProjectRole.EDITOR)
    for n in range(3):
        blob = Blob.objects.create(
            sha256=f"{files + n + 1:064x}", size=4096, storage_uri=f"s3://iki/held/{n}"
        )
        held = File.objects.create(
            blob=blob, name=f"held-clone-{n}.pdb", media_type="chemical/x-pdb", project=elsewhere
        )
        entities.create(editor, STRUCTURE, world.project, {"structure": str(held.pk)})
    client = Api(world.browser("viewer"))
    project = str(world.project.pk)
    base = {"project": project, "limit": LIMIT}

    took: dict[str, float] = {}
    took["the first page"], first = _timed(client, base)
    assert len(first["items"]) == LIMIT
    # Newest first: the three held files, added last.
    assert first["items"][0]["name"] == "held-clone-2.pdb"
    took["the next page"], after = _timed(client, base | {"cursor": first["next_cursor"]})
    assert len(after["items"]) == LIMIT
    pages: dict[str, dict[str, Any]] = {
        "oldest first": base | {"sort": "oldest"},
        "by name": base | {"sort": "name"},
        "by name, Z to A": base | {"sort": "name_desc"},
        "latest acquired first": base | {"sort": "acquired"},
        "earliest acquired first": base | {"sort": "acquired_oldest"},
        "Sanger traces": base | {"format": "application/x-abi"},
        "images": base | {"format": "image/*"},
        "acquired in a week": base
        | {"acquired_from": "2020-01-15", "acquired_to": "2020-01-21", "sort": "acquired"},
        "a clone, by part of its name": base | {"q": f"clone-{files // 2 // 4 * 4}-"},
        "a lot's certificate": base | {"q": f"LOT-{files // 3 // 4 * 4 + 3}."},
        "two letters": base | {"q": "ab"},
        # What a run states (ADR 0224): a person who ran a few hundred, and the cytometer
        # that ran a quarter of them.
        "who ran it": base | {"q": f"user{files // 2 // 4 % 800:04d}"},
        "the cytometer": base | {"q": "fortessa"},
    }
    for name, query in pages.items():
        took[name], answer = _timed(client, query)
        assert answer["items"], name
        if answer["next_cursor"]:
            took[f"{name}, the next page"], _ = _timed(
                client, query | {"cursor": answer["next_cursor"]}
            )
    took["the count"], counted = _timed(client, {"project": project}, "/files/count")
    assert counted == {"total": 1000, "more_than_total": True}
    took["the formats"], formats = _timed(client, {"project": project}, "/files/formats")
    assert {f["media_type"] for f in formats} >= {"application/x-abi", "image/tiff"}
    for name, seconds in took.items():
        print(f"{name}: {seconds * 1000:.0f} ms")  # noqa: T201 - the load test's report

    # Part of a name, through the trigram index, not by reading every name.
    plans = _plans(client, base | {"q": f"clone-{files // 2 // 4 * 4}-"})
    used = {n.get("Index Name") for p in plans for n in _nodes(p)}
    stated = _plans(client, base | {"q": f"user{files // 2 // 4 % 800:04d}"})
    stated_used = {n.get("Index Name") for p in stated for n in _nodes(p)}
    # The newest first page of the files added to the project: read in ID order by its
    # index, not sorted. (The files held from elsewhere come from the records' uses; with
    # every other file in this one project, Postgres finds those few by reading the rest,
    # which the times above include.)
    own, *_ = _plans(client, base)
    sorted_files = [
        n
        for n in _nodes(own)
        if n["Node Type"] == "Sort" and any("iki_file" in k for k in n.get("Sort Key", []))
    ]
    if files > SMALL:
        # At a handful, reading them all is as good, and Postgres knows it.
        assert "iki_file_name_trgm" in used, plans
        # And what they state through its own, beside the names'.
        assert {"iki_file_name_trgm", "iki_file_stated_trgm"} <= stated_used, stated
        # Then the page is of those few, not of every file read in order.
        assert not any(
            n["Node Type"] == "Seq Scan" and n.get("Relation Name") == "iki_file"
            for p in plans
            for n in _nodes(p)
        ), plans
        assert not sorted_files, own
        slow = {name: seconds for name, seconds in took.items() if seconds > BUDGET}
        assert not slow, slow
