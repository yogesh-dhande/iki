"""Lists of rows (ADR 0137) as a complex has them (ADR 0161): each row's fields checked as a
type's are, each role a lab's own type refines (ADR 0164) held to its count and what it
refers to, stored a column per row field, and every surface showing them.

The bispecifics' chains are real ones, KEGG DRUG's (data/fc_fusions_and_bispecifics.fasta):
emicizumab's two heavy chains and the light chain both share (D10821), and amivantamab's two
heavy chains and two light chains (D11894), whose disulphides pair A with C and B with D."""

from pathlib import Path
from typing import Any, NamedTuple

import pytest
from django.db import DatabaseError, connection, transaction
from org_world import World
from test_api_entities import api_as, create, patch
from test_graph import walk
from test_run_results import _downloads_all, _failed, done, output
from test_runs import launch, world
from test_states import imported_values
from test_workers import keep_connection, worker

from iki_server.entities import states
from iki_server.entities.models import Entity
from iki_server.files import services as files
from iki_server.files.services import EMPTY_SHA256
from iki_server.graph.models import Edge

pytestmark = pytest.mark.django_db

__all__ = ["keep_connection", "world"]  # the fixtures, used here too

BISPECIFIC = "acme/protein.Bispecific"


def _fasta(name: str) -> dict[str, str]:
    found: dict[str, str] = {}
    header = ""
    for line in (Path(__file__).parent / "data" / name).read_text().splitlines():
        if line.startswith(">"):
            header = line[1:].split()[0]
            found[header] = ""
        else:
            found[header] += line.strip()
    return found


KEGG = _fasta("fc_fusions_and_bispecifics.fasta")


def dna(world: World, bases: str = "ACGT") -> Any:
    made = create(api_as(world), world.project, {"bases": bases})
    assert made.status_code == 201, made.content
    return made.json()


class Chains(NamedTuple):
    """Emicizumab's chains, and a DNA linker."""

    heavy: Any
    other_heavy: Any
    light: Any
    linker: Any


def chain(world: World, name: str) -> Any:
    made = create(api_as(world), world.project, {"sequence": KEGG[name]}, "acme/protein.Protein")
    assert made.status_code == 201, made.content
    return made.json()


def chains(world: World) -> Chains:
    return Chains(
        chain(world, "D10821-H1"), chain(world, "D10821-H2"), chain(world, "D10821-L"), dna(world)
    )


def components(c: Chains, linker: bool = False) -> list[dict[str, Any]]:
    """Emicizumab's parts: a row each, with its copies. Its light chain is both arms', so
    two copies of it."""
    rows = [
        {"part": c.heavy["id"], "copies": 1, "role": "heavy"},
        {"part": c.other_heavy["id"], "copies": 1, "role": "heavy"},
        {"part": c.light["id"], "copies": 2, "role": "light"},
    ]
    if linker:
        rows.append({"part": c.linker["id"], "copies": 1, "role": "linker"})
    return rows


def errors(response: Any) -> dict[str, str]:
    assert response.status_code == 422, response.content
    return {e["pointer"]: e["detail"] for e in response.json()["errors"]}


def test_rows_are_saved_and_read_with_what_they_refer_to(world: World) -> None:
    c = chains(world)
    api = api_as(world)
    made = create(
        api,
        world.project,
        {"name": "BS-1", "components": components(c, linker=True)},
        BISPECIFIC,
    )
    assert made.status_code == 201, made.content
    body = made.json()
    rows = body["values"]["components"]
    # A reference to an identity holds its registry entry (ADR 0115), titled by the record.
    assert rows[2] == {"part": rows[2]["part"], "copies": 2, "role": "light"}
    named = [body["refs"][r["part"]]["hrid"] for r in rows]
    assert named == [c.heavy["hrid"], c.other_heavy["hrid"], c.light["hrid"], c.linker["hrid"]]
    found = api.get(f"/entities/{body['hrid']}").json()
    assert found["values"]["components"] == rows
    # The rows are iki/core.Complex's (ADR 0161), and the lab's type says which roles they
    # take, and nothing else (ADR 0164).
    field = next(
        f
        for f in api.get(f"/entity-types/{BISPECIFIC}").json()["fields"]
        if f["name"] == "components"
    )
    assert field["items"] == "row"
    assert [f["name"] for f in field["row"]["fields"]] == ["part", "copies", "role"]
    assert field["row"]["by"] == "role"
    assert field["row"]["closed"] is True
    assert [r["name"] for r in field["row"]["roles"]] == ["heavy", "light", "linker"]
    assert field["row"]["roles"][0] == {
        "name": "heavy",
        "label": "Heavy",
        "min": 1,
        "max": 2,
        "to": {"part": ["acme/protein.Protein"]},
    }


def test_each_row_field_is_checked_where_it_is(world: World) -> None:
    rows = components(chains(world))
    rows[0] = {"role": "heavy", "copies": 0, "colour": "red"}
    rows[1] = {**rows[1], "role": "Heavy chain"}
    found = errors(
        create(api_as(world), world.project, {"name": "BS-1", "components": rows}, BISPECIFIC)
    )
    assert found == {
        "#/values/components/0/part": "is required.",
        "#/values/components/0/copies": "must be at least 1.",
        "#/values/components/0/colour": "isn't one of the row's fields: part, copies, role.",
        "#/values/components/1/role": "doesn't match the pattern `^[a-z][a-z0-9]*(_[a-z0-9]+)*$`.",
    }


def test_each_role_takes_its_count_and_its_types(world: World) -> None:
    c = chains(world)
    api = api_as(world)
    no_light = [row for row in components(c, linker=True) if row["role"] != "light"]
    found = errors(create(api, world.project, {"name": "B", "components": no_light}, BISPECIFIC))
    assert found == {"#/values/components": "needs at least 1 light row, and has 0."}
    wrong = components(c)
    wrong[0]["part"] = c.linker["id"]
    found = errors(create(api, world.project, {"name": "B", "components": wrong}, BISPECIFIC))
    assert found == {
        "#/values/components/0/part": "must refer to a acme/protein.Protein in a heavy row, "
        "not a acme/dna.DNASequence."
    }
    # A role the lab's type doesn't name isn't one its rows take, though a protein
    # complex's may be anything.
    other = [*components(c), {"part": c.linker["id"], "copies": 1, "role": "tag"}]
    found = errors(create(api, world.project, {"name": "B", "components": other}, BISPECIFIC))
    assert found == {"#/values/components/3/role": "isn't one of its roles: heavy, light, linker."}
    # Its own rule: two heavy chains in all.
    three = [{**components(c)[0], "copies": 2}, *components(c)[1:]]
    found = errors(create(api, world.project, {"name": "B", "components": three}, BISPECIFIC))
    assert found == {"#/values/components": "It has two heavy chains in all."}


def test_a_part_is_one_row_with_its_copies(world: World) -> None:
    """Emicizumab's arms share a light chain: that's one row of it, with two copies, the
    one way to write it, so its parts have one entry (ADR 0161). Two rows of one protein
    are refused, saying to give its copies."""
    c = chains(world)
    api = api_as(world)
    twice = [
        *components(c)[:2],
        {**components(c)[2], "copies": 1},
        {**components(c)[2], "copies": 1},
    ]
    found = errors(create(api, world.project, {"name": "B", "components": twice}, BISPECIFIC))
    assert found == {
        "#/values/components/3/part": f"lists {c.light['hrid']} again: give each part once, "
        "with how many copies of it there are."
    }
    made = create(api, world.project, {"name": "B", "components": components(c)}, BISPECIFIC)
    assert made.status_code == 201, made.content
    again = create(
        api, world.project, {"name": "B again", "components": components(c)[::-1]}, BISPECIFIC
    )
    assert again.status_code == 200 and again.json()["hrid"] == made.json()["hrid"]
    # One copy of the light chain is another molecule: an arm without one.
    half = [*components(c)[:2], {**components(c)[2], "copies": 1}]
    other = create(api, world.project, {"name": "B half", "components": half}, BISPECIFIC)
    assert other.status_code == 201 and other.json()["hrid"] != made.json()["hrid"]


def test_the_parts_are_the_molecule_and_its_arms_are_a_fact(world: World) -> None:
    """Amivantamab's heavy chain A pairs with light chain C and B with D. Its parts are its
    identity; how they pair is its arms, its registry entry's fact. A with D and B with C, a
    mispaired assembly of the same four chains (Schaefer et al., PNAS 108:11187, 2011), is
    the same molecule's impurity: registering it with those arms is refused, as any other
    fact of an entry that has one is, and the entry keeps its own."""
    a, b, c, d = (chain(world, f"D11894-{x}") for x in "ABCD")
    api = api_as(world)
    parts = [
        {"part": a["id"], "copies": 1, "role": "heavy"},
        {"part": c["id"], "copies": 1, "role": "light"},
        {"part": b["id"], "copies": 1, "role": "heavy"},
        {"part": d["id"], "copies": 1, "role": "light"},
    ]
    arms = [
        {"heavy": a["id"], "light": c["id"], "target": "EGFR"},
        {"heavy": b["id"], "light": d["id"], "target": "MET"},
    ]
    made = create(
        api, world.project, {"name": "Amivantamab", "components": parts, "arms": arms}, BISPECIFIC
    )
    assert made.status_code == 201, made.content
    mispaired = [{**arms[0], "light": d["id"]}, {**arms[1], "light": c["id"]}]
    found = create(
        api,
        world.project,
        {"name": "Mispaired", "components": parts[::-1], "arms": mispaired},
        BISPECIFIC,
    )
    assert found.status_code == 422, found.content
    assert "#/values/arms" in errors(found)
    entry = api.get(f"/entities/{made.json()['hrid']}").json()
    # References to identities hold their registry entries (ADR 0115).
    assert [(r["heavy"], r["light"]) for r in entry["values"]["arms"]] == [
        (a["entry"]["id"], c["entry"]["id"]),
        (b["entry"]["id"], d["entry"]["id"]),
    ]


def test_its_bonds_join_residues_of_its_parts_copies(world: World) -> None:
    """Emicizumab's disulfides as KEGG DRUG gives them: H137-L214, h133-L'214, H229-h225 and
    H232-h228 (D10821), by copy, are a complex's bonds (ADR 0161). Each end must be one of
    its parts, a copy it has and a residue in its sequence, a disulfide's a cysteine, and
    no residue is in two."""
    c = chains(world)
    api = api_as(world)

    def bond(part: Any, copy: int, residue: int, partner: Any, other: int, at: int) -> Any:
        return {
            "kind": "disulfide",
            "part": part["id"],
            "copy": copy,
            "residue": residue,
            "partner": partner["id"],
            "partner_copy": other,
            "partner_residue": at,
        }

    kegg = [
        bond(c.heavy, 1, 137, c.light, 1, 214),
        bond(c.other_heavy, 1, 133, c.light, 2, 214),
        bond(c.heavy, 1, 229, c.other_heavy, 1, 225),
        bond(c.heavy, 1, 232, c.other_heavy, 1, 228),
    ]
    wrong = [
        bond(c.heavy, 1, 137, c.linker, 1, 1),  # the linker isn't one of its parts
        bond(c.other_heavy, 2, 133, c.light, 2, 214),  # it has one copy of that heavy chain
        bond(c.heavy, 1, 449, c.light, 1, 214),  # the heavy chain has 448 residues
        bond(c.heavy, 1, 1, c.light, 1, 214),  # its first residue is a glutamine
        bond(c.light, 1, 214, c.light, 1, 214),  # a bond joins two
        bond(c.other_heavy, 1, 22, c.light, 2, 214),  # copy 2's C214 is bonded already
    ]
    found = errors(
        create(
            api,
            world.project,
            {"name": "B", "components": components(c), "bonds": [*kegg, *wrong]},
            BISPECIFIC,
        )
    )
    assert found == {
        "#/values/bonds/4/partner": f"{c.linker['hrid']} isn't one of its parts.",
        "#/values/bonds/5/copy": f"{c.other_heavy['hrid']} has 1 copy in it, so there's no copy 2.",
        "#/values/bonds/6/residue": f"{c.heavy['hrid']} has 448 residues, so there's no "
        "residue 449.",
        "#/values/bonds/7/residue": f"{c.heavy['hrid']}'s residue 1 is Q, and a disulfide joins "
        "two cysteines (C).",
        "#/values/bonds/8/partner_residue": "is the residue it starts from: a bond joins two.",
        "#/values/bonds/9/partner_residue": f"{c.light['hrid']}'s residue 214 (copy 2) is bonded "
        "already, in bond 2.",
    }
    made = create(
        api,
        world.project,
        {"name": "Emicizumab", "components": components(c), "bonds": kegg},
        BISPECIFIC,
    )
    assert made.status_code == 201, made.content
    entries = {x["id"]: x["entry"]["id"] for x in (c.heavy, c.other_heavy, c.light)}
    assert made.json()["values"]["bonds"] == [
        {**b, "part": entries[b["part"]], "partner": entries[b["partner"]]} for b in kegg
    ]


def test_rows_change_as_a_whole_and_each_revision_keeps_them(world: World) -> None:
    heavy, _, _, linker = chains(world)
    api = api_as(world)
    added = [{"material": heavy["id"], "amount": {"value": 5, "unit": "ug"}, "note": "first"}]
    body = create(
        api, world.project, {"label": "S-1", "additions": added}, "acme/protein.Sample"
    ).json()
    more = [*added, {"material": linker["id"], "added_on": "2026-11-18"}]
    changed = patch(
        api,
        body["hrid"],
        {"values": {"additions": more}, "reason": "Added the linker"},
        api.get(f"/entities/{body['hrid']}").headers["ETag"],
    )
    assert changed.status_code == 200, changed.content
    assert len(changed.json()["values"]["additions"]) == 2
    revisions = api.get(f"/entities/{body['hrid']}/revisions").json()["items"]
    edited = next(r for r in revisions if r["number"] == 2)
    assert edited["changed_fields"] == ["additions"]
    first = api.get(f"/entities/{body['hrid']}/revisions/1").json()
    assert first["values"]["additions"] == body["values"]["additions"]


def test_a_set_of_parts_is_one_identity_whatever_their_order(world: World) -> None:
    """ADR 0138: a complex's parts are its components, by part and copies, as a multiset:
    registered again in another order they're found, and with another role too, as roles
    aren't the molecule (ADR 0161)."""
    c = chains(world)
    api = api_as(world)
    rows = components(c, linker=True)
    made = create(api, world.project, {"name": "BS-1", "components": rows}, BISPECIFIC)
    assert made.status_code == 201, made.content
    again = [rows[3], rows[2], rows[1], rows[0]]
    found = create(api, world.project, {"name": "BS-1 again", "components": again}, BISPECIFIC)
    assert found.status_code == 200, found.content
    assert found.json()["hrid"] == made.json()["hrid"]
    # Two light chains of different proteins are another antibody's.
    other = create(api, world.project, {"sequence": KEGG["D11894-D"]}, "acme/protein.Protein")
    two = [
        *rows[:2],
        {**rows[2], "copies": 1},
        {**rows[2], "part": other.json()["id"], "copies": 1},
        rows[3],
    ]
    another = create(api, world.project, {"name": "BS-2", "components": two}, BISPECIFIC)
    assert another.status_code == 201, another.content
    assert another.json()["hrid"] != made.json()["hrid"]
    # Its components are its identity's, so they don't change.
    edited = patch(
        api,
        made.json()["hrid"],
        {"values": {"components": two}, "reason": "Two light"},
        api.get(f"/entities/{made.json()['hrid']}").headers["ETag"],
    )
    assert edited.status_code == 422, edited.content


def test_search_finds_a_row_with_every_value_given(world: World) -> None:
    c = chains(world)
    heavy, light = c.heavy, c.light
    api = api_as(world)
    made = create(
        api,
        world.project,
        {"name": "BS-1", "components": components(c, linker=True)},
        BISPECIFIC,
    ).json()

    def search(value: dict[str, Any]) -> list[str]:
        found = api.post(
            "/entities/search",
            {"type": BISPECIFIC, "where": {"field": "components", "op": "has", "value": value}},
        )
        assert found.status_code == 200, found.content
        return [e["hrid"] for e in found.json()["items"]]

    assert search({"part": heavy["hrid"], "role": "heavy"}) == [made["hrid"]]
    assert search({"part": light["hrid"], "role": "heavy"}) == []
    assert search({"part": light["hrid"], "copies": 2}) == [made["hrid"]]
    assert search({"part": light["hrid"], "copies": 1}) == []
    # Searching every complex finds the lab's own type's (ADR 0161).
    every = api.post(
        "/entities/search",
        {
            "type": "iki/core.Complex",
            "where": {"field": "components", "op": "has", "value": {"part": heavy["hrid"]}},
        },
    )
    assert every.status_code == 200, every.content
    assert [(e["hrid"], e["type"]) for e in every.json()["items"]] == [(made["hrid"], BISPECIFIC)]
    bad = api.post(
        "/entities/search",
        {"type": BISPECIFIC, "where": {"field": "components", "op": "has", "value": {"x": 1}}},
    )
    assert bad.status_code == 422


def test_rows_references_are_edges_of_the_graph(world: World) -> None:
    c = chains(world)
    linker = c.linker
    made = create(
        api_as(world),
        world.project,
        {"name": "BS-1", "components": components(c, linker=True)},
        BISPECIFIC,
    ).json()
    # Its components are its identity's, so its registry entry's (ADR 0138).
    edges = Edge.objects.filter(node_id=made["entry"]["id"], kind="reference")
    assert sorted((e.field, e.position) for e in edges) == [
        ("components.part", 0),
        ("components.part", 1),
        ("components.part", 2),
        ("components.part", 3),
    ]
    found = walk(world, linker["id"], "downstream")
    assert found["hrids"] == [made["hrid"]]


def test_the_database_holds_rows_to_their_types_and_locks(world: World) -> None:
    made = create(
        api_as(world),
        world.project,
        {"name": "BS-1", "components": components(chains(world))},
        BISPECIFIC,
    ).json()
    other = create(api_as(world), world.project, {"label": "S-1"}, "acme/protein.Sample").json()
    with (
        pytest.raises(DatabaseError, match="can't refer to"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "UPDATE iki__core_complex__entry__components SET part_id = %s WHERE entity_id = %s",
            [other["id"], made["entry"]["id"]],
        )
    # A list of rows is part of its record, so a lock (ADR 0125) freezes it too.
    assert ("acme__protein_sample__additions", "entity_id") in states.guarded_tables()


# --- Files in rows ------------------------------------------------------------------------------
# A row's file field holds a file as a file field does (ADR 0137): a run that reads the row gets
# the file, and a run's results attach one in a row only as they could alone.

ATTACH_ONLY = (
    "Attach only files this run uploaded (once they're ready), its file params' files, or its "
    "inputs' files."
)


def stored(world: World, name: str, media_type: str = "application/pdf") -> str:
    """A file in the project, such as a certificate of analysis (empty, so it's ready)."""
    upload = files.start(
        world.users["editor"],
        world.project,
        name=name,
        media_type=media_type,
        size=0,
        sha256=EMPTY_SHA256,
    )
    upload.refresh_from_db()
    return str(upload.file_id)


def test_a_job_downloads_the_files_its_inputs_rows_hold(world: World, s3: Any) -> None:
    """A sample whose addition has its certificate, and whose lot's input has its own: both
    reach measure_purity's worker, the lot's through the sample's included lot (ADR 0105)."""
    heavy, *_ = chains(world)
    api = api_as(world)
    lot_coa, sample_coa = stored(world, "lot-coa.pdf"), stored(world, "addition-coa.pdf")
    lot = create(
        api,
        world.project,
        {"label": "L-1", "inputs": [{"protein": heavy["id"], "certificate": lot_coa}]},
        "acme/protein.Lot",
    )
    assert lot.status_code == 201, lot.content
    added = [{"material": heavy["id"], "certificate": sample_coa}]
    values = {"label": "S-1", "lot": lot.json()["id"], "additions": added}
    sample = create(api, world.project, values, "acme/protein.Sample")
    assert sample.status_code == 201, sample.content
    made = launch(
        api, world.project, {"sample": sample.json()["id"]}, "acme/protein.measure_purity"
    )
    assert made.status_code == 201, made.content
    pod = worker(world)
    job = pod.lease().json()
    assert sorted(job["files"]) == sorted([lot_coa, sample_coa])
    (given,) = job["inputs"]["sample"]
    assert [a["certificate"] for a in given["values"]["additions"]] == [sample_coa]
    _downloads_all(pod, job)


def _importing(world: World) -> tuple[Any, dict[str, Any], str]:
    """An import_samples run, leased, and its CSV's ID."""
    csv = stored(world, "samples.csv", "text/csv")
    run = launch(
        api_as(world), world.project, {}, "acme/protein.import_samples", params={"file": csv}
    )
    assert run.status_code == 201, run.content
    pod = worker(world)
    return pod, pod.lease().json(), csv


def _sample(material: str, certificate: str) -> dict[str, Any]:
    return {
        "type": "acme/protein.Sample",
        "values": {
            "label": f"S-{certificate[:8]}",
            "additions": [{"material": material, "certificate": certificate}],
        },
    }


def test_results_attach_files_in_rows_only_as_they_could_alone(world: World, s3: Any) -> None:
    heavy, *_ = chains(world)
    # A file the launcher can read and share, but that this run didn't upload or get.
    theirs = stored(world, "other-coa.pdf")
    pod, job, _ = _importing(world)
    errors = _failed(pod, job, output(creates=[_sample(heavy["id"], theirs)]))
    assert errors == {"#/creates/0/values/additions": ATTACH_ONLY}
    # Its file param's file it may: the CSV it imports from.
    pod, job, csv = _importing(world)
    body = done(pod, job, output(creates=[_sample(heavy["id"], csv)]))
    assert body["run_status"] == "succeeded", body
    (made,) = Entity.objects.filter(type="acme/protein.Sample")
    found = api_as(world).get(f"/entities/{made.hrid}").json()
    assert [a["certificate"] for a in found["values"]["additions"]] == [csv]


def test_creates_in_a_file_cant_attach_files_in_rows_either(world: World, s3: Any) -> None:
    heavy, *_ = chains(world)
    coa = stored(world, "coa.pdf")
    rows = [{"material": heavy["id"], "certificate": coa}]
    ended = imported_values(world, {"label": "S-1", "additions": rows})
    assert ended["status"] == "failed", ended
    assert "can't attach files" in str(ended["error"]), ended["error"]
