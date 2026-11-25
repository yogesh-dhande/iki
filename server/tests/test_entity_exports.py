"""Exports (ADR 0171): a record as the files other tools read, at any revision, for anyone
who can read it, each download in the audit log."""

import hashlib
import io
from importlib.metadata import version
from typing import Any

import pytest
from api_client import Api, problem
from Bio import SeqIO
from Bio.SeqRecord import SeqRecord
from org_world import World, build_world
from test_oauth import bearer, signed_in_token

from iki_server.audit.models import AuditEvent
from iki_server.entities.models import Entity

pytestmark = pytest.mark.django_db

BASES = "ATGACCATGATTACGGATTCACTGGCCGTCGTTTTACAACGTCGTGACTGGGAAAACCCTGGCG"
FEATURES = [
    {"type": "CDS", "name": "lacZ alpha", "start": 1, "end": 30, "strand": "forward"},
    {"type": "misc_feature", "name": "across", "start": 60, "end": 5, "strand": "reverse"},
]


@pytest.fixture
def world() -> World:
    return build_world()


def api_as(world: World, principal: str = "editor") -> Api:
    return Api(world.browser(principal))


def plasmid(world: World, **values: Any) -> dict[str, Any]:
    made = api_as(world).post(
        "/entities",
        {
            "type": "acme/dna.Plasmid",
            "project": str(world.project.pk),
            "values": {"bases": BASES, "name": "pDemo", "features": FEATURES, **values},
        },
    )
    assert made.status_code == 201, made.content
    return dict(made.json())


def read(text: str) -> SeqRecord:
    """The file, as Biopython reads GenBank: an independent reader of what Iki wrote."""
    found: SeqRecord = SeqIO.read(io.StringIO(text), "genbank")  # type: ignore[no-untyped-call]
    return found


def exported(api: Api, ref: str, **params: Any) -> Any:
    query = "&".join(f"{k}={v}" for k, v in {"format": "genbank", **params}.items())
    return api.get(f"/entities/{ref}/export?{query}")


def test_a_type_says_which_files_it_exports(world: World) -> None:
    api = api_as(world, "viewer")
    types = {t["name"]: t for t in api.get("/entity-types").json()}
    assert types["acme/dna.Plasmid"]["exports"] == [
        {
            "export": "sequence",
            "formats": [
                {
                    "format": "genbank",
                    "label": "GenBank",
                    "extension": "gb",
                    "media_type": "chemical/seq-na-genbank",
                    "description": "The bases, topology and features, with their qualifiers.",
                    "many": "file",
                },
                {
                    "format": "fasta",
                    "label": "FASTA",
                    "extension": "fasta",
                    "media_type": "chemical/seq-na-fasta",
                    "description": "The bases, with the record's ID, name and topology in "
                    "its header. FASTA holds no features.",
                    "many": "file",
                },
            ],
            "sequence": "bases",
            "features": "features",
        }
    ]
    # A vector's sequence is its plasmid's, by reference, with its own features added.
    vector = types["acme/dna.Vector"]["exports"][0]
    assert (vector["export"], vector["sequence"], vector["features"]) == (
        "sequence",
        "plasmid",
        "annotated_features",
    )
    assert types["acme/dna.DNASequence"]["exports"] == []


def test_a_record_downloads_as_genbank_and_the_download_is_audited(world: World) -> None:
    made = plasmid(world, topology="circular", description="A demonstration plasmid")
    response = exported(api_as(world, "viewer"), made["hrid"])
    assert response.status_code == 200, response.content
    assert response["Content-Type"] == "chemical/seq-na-genbank"
    assert response["Content-Disposition"] == 'attachment; filename="pDemo.gb"'
    assert response["Cache-Control"] == "no-store"
    content = response.content
    record = read(content.decode())
    assert str(record.seq) == BASES
    assert record.annotations["topology"] == "circular"
    assert record.description == "A demonstration plasmid"
    assert [(f.type, f.qualifiers.get("label")) for f in record.features] == [
        ("CDS", ["lacZ alpha"]),
        ("misc_feature", ["across"]),
    ]
    assert "complement(join(60..64,1..5))" in content.decode()
    event = AuditEvent.objects.get(action="entity.exported")
    assert event.actor == world.users["viewer"]
    assert event.project == world.project
    assert event.target_id == made["id"]
    assert event.data == {
        "format": "genbank",
        "revision": 1,
        "latest": True,
        "file": "pDemo.gb",
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
        "left_out": [],
        # Which code wrote it (ADR 0230): Iki's own GenBank writer, at Iki's version.
        "writer": {"builtin": "genbank", "version": version("iki-server")},
    }


def test_an_earlier_revision_downloads_as_it_was(world: World) -> None:
    made = plasmid(world, topology="circular")
    api = api_as(world)
    changed = api.patch(
        f"/entities/{made['id']}",
        {"values": {"name": "pDemo2", "features": FEATURES[:1]}, "reason": "Drop one"},
        headers={"If-Match": '"1"'},
    )
    assert changed.status_code == 200, changed.content
    then = exported(api, made["id"], revision=1)
    assert then["Content-Disposition"] == 'attachment; filename="pDemo (revision 1).gb"'
    text = then.content.decode()
    assert text.startswith("LOCUS       pDemo ")
    assert '/label="across"' in text
    assert "(revision 1, not its latest)" in " ".join(text.split())
    now = exported(api, made["id"]).content.decode()
    assert now.startswith("LOCUS       pDemo2 ") and "across" not in now
    events = AuditEvent.objects.filter(action="entity.exported").order_by("occurred_at")
    assert [(e.data["revision"], e.data["latest"]) for e in events] == [(1, False), (2, True)]
    problem(exported(api, made["id"], revision=3), 404, "not_found")


def test_fasta_names_it_and_its_topology(world: World) -> None:
    made = plasmid(world, topology="circular")
    response = exported(api_as(world), made["hrid"], format="fasta")
    assert response["Content-Type"] == "chemical/seq-na-fasta"
    assert response["Content-Disposition"] == 'attachment; filename="pDemo.fasta"'
    assert response.content.decode().splitlines()[0] == (
        f">{made['hrid']} pDemo [topology=circular]"
    )
    assert response["Iki-Left-Out"] == "1"
    assert AuditEvent.objects.get(action="entity.exported").data["left_out"] == [
        "FASTA holds no features: its 2 features aren't in it."
    ]


def test_only_someone_who_can_read_it_downloads_it(world: World) -> None:
    made = plasmid(world)
    problem(exported(api_as(world, "outsider"), made["id"]), 404, "not_found")
    _, tokens = signed_in_token(world, scope="read", projects=[world.project])
    assert (
        bearer(
            tokens["access_token"], "GET", f"/entities/{made['id']}/export?format=genbank"
        ).status_code
        == 200
    )
    assert not AuditEvent.objects.filter(action="entity.exported", actor=world.users["outsider"])


def test_a_type_that_exports_nothing_has_nothing_to_download(world: World) -> None:
    made = api_as(world).post(
        "/entities",
        {
            "type": "acme/dna.DNASequence",
            "project": str(world.project.pk),
            "values": {"bases": "ACGT"},
        },
    )
    problem(exported(api_as(world), made.json()["id"]), 404, "no_export")


def test_a_record_about_a_sequence_is_its_sequence_with_its_own_name_and_annotation(
    world: World,
) -> None:
    """A plasmid's file is its sequence's, as the plasmid plugin's is: the sequence's bases,
    topology and features, with the record's name, description and annotation, read as
    both were at the record's revision."""
    sequence = plasmid(world, topology="circular", description="The sequence's own")
    api = api_as(world)
    vector = api.post(
        "/entities",
        {
            "type": "acme/dna.Vector",
            "project": str(world.project.pk),
            "values": {
                "plasmid": sequence["id"],
                "name": "pVec",
                "annotated_features": [
                    {
                        "type": "rep_origin",
                        "name": "ori",
                        "start": 31,
                        "end": 50,
                        "strand": "forward",
                    }
                ],
            },
        },
    )
    assert vector.status_code == 201, vector.content
    ref = vector.json()["hrid"]
    text = exported(api, ref).content.decode()
    record = read(text)
    assert record.name == "pVec"
    assert str(record.seq) == BASES and record.annotations["topology"] == "circular"
    assert record.description == "The sequence's own"
    labels = ["lacZ alpha", "across", "ori"]
    assert [f.qualifiers["label"][0] for f in record.features] == labels
    assert f"whose sequence is {sequence['hrid']}" in " ".join(text.split())
    # The vector changes, then its sequence's record does: the vector's revision 1 reads
    # the sequence as it was while revision 1 was the vector's latest.
    api.patch(
        f"/entities/{vector.json()['id']}",
        {"values": {"description": "A vector"}},
        headers={"If-Match": '"1"'},
    )
    changed = api.patch(
        f"/entities/{sequence['id']}", {"values": {"features": []}}, headers={"If-Match": '"1"'}
    )
    assert changed.status_code == 200, changed.content
    then = read(exported(api, ref, revision=1).content.decode())
    assert [f.qualifiers["label"][0] for f in then.features] == labels
    assert then.description == "The sequence's own"
    now = read(exported(api, ref).content.decode())
    assert [f.qualifiers["label"][0] for f in now.features] == ["ori"]
    assert now.description == "A vector"


def test_its_organism_is_its_source(world: World) -> None:
    from iki_server.entities import services as entities

    made_taxon = entities.create(
        world.users["editor"],
        "iki/core.Organism",
        world.project,
        {"taxon_id": 562, "scientific_name": "Escherichia coli"},
    )
    taxon = Entity.objects.get(pk=made_taxon.entity.pk)
    made = plasmid(world, organism=str(taxon.entry_id or taxon.pk))
    text = exported(api_as(world, "viewer"), made["hrid"]).content.decode()
    record = read(text)
    assert record.annotations["organism"] == "Escherichia coli"
    assert record.features[0].type == "source"
    assert record.features[0].qualifiers["db_xref"] == ["taxon:562"]


def test_a_vector_is_written_from_its_own_projects_record_of_its_sequence(world: World) -> None:
    """Two projects each hold a record of one sequence (ADR 0115), each with its own
    features: a vector's file is its project's record's, never the other's, which its
    readers may not see."""
    from iki_server.entities import entries
    from iki_server.org.models import Project, ProjectMembership, ProjectRole

    elsewhere = Project.objects.create(team=world.team, name="Elsewhere")
    ProjectMembership.objects.create(
        project=elsewhere, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    theirs = plasmid(world, features=[{**FEATURES[0], "name": "their own"}])
    entry = Entity.objects.get(pk=theirs["id"]).entry
    assert entry is not None
    added = entries.add_record(
        world.users["editor"], entry, elsewhere, {"name": "pOurs", "features": FEATURES[1:]}
    )
    ours = Entity.objects.get(pk=added.entity.pk)
    vector = api_as(world).post(
        "/entities",
        {
            "type": "acme/dna.Vector",
            "project": str(elsewhere.pk),
            "values": {"plasmid": str(entry.pk), "name": "pVec"},
        },
    )
    assert vector.status_code == 201, vector.content
    text = exported(api_as(world), vector.json()["id"]).content.decode()
    assert f"whose sequence is {ours.hrid}" in " ".join(text.split())
    assert "across" in text and "their own" not in text


def test_annotation_the_sequence_already_has_is_written_once(world: World) -> None:
    """A part annotation found where the sequence's own file already has that feature
    (the same type, name, place and strand) is one feature, with the file's qualifiers."""
    sequence = plasmid(world, topology="circular")
    found = {k: FEATURES[0][k] for k in ("type", "name", "start", "end", "strand")}
    vector = api_as(world).post(
        "/entities",
        {
            "type": "acme/dna.Vector",
            "project": str(world.project.pk),
            "values": {
                "plasmid": sequence["id"],
                "name": "pVec",
                "annotated_features": [{**found, "qualifiers": {"note": "matched 100%"}}],
            },
        },
    )
    assert vector.status_code == 201, vector.content
    record = read(exported(api_as(world), vector.json()["id"]).content.decode())
    assert [f.qualifiers["label"][0] for f in record.features] == ["lacZ alpha", "across"]
