"""Many records as one file (ADR 0229): a selection, or a table's whole search, as FASTA,
GenBank or SDF, each record pinned at the revision it had when asked for, written in the
background past a hundred, kept a week, and each download in the audit log.

Each file is read back by an independent reader (Biopython for FASTA and GenBank, RDKit
for SDF) and by the importer of the plugin whose records it holds, which must make the
same records of it."""

import hashlib
import io
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
import requests
from api_client import Api, problem
from Bio import SeqIO
from django.core.management import call_command
from django.utils import timezone
from org_world import World, build_world
from rdkit import Chem
from rdkit.Chem import rdDepictor
from test_oauth import bearer, signed_in_token

from iki_server.audit.models import AuditEvent
from iki_server.entities import access, exports, plugin_writers, record_exports, services
from iki_server.entities.models import ProjectLink, RecordExport
from iki_server.org import holds
from iki_server.org.models import Project, ProjectMembership, ProjectRole
from iki_server.org.services import Refused
from iki_server.tasks.runner import Runner
from iki_spec.model import PluginExport

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parents[2]
PROTEIN_IMPORT = ROOT / "plugins" / "protein" / "scripts" / "imports.py"
DNA_IMPORT = ROOT / "plugins" / "dna" / "scripts" / "imports.py"
SDF_SCRIPT = ROOT / "plugins" / "small_molecule" / "scripts" / "sdf.py"
FILE_ID = "0190e9a4-0000-7000-8000-000000000001"

#: Human haemoglobin's alpha chain (UniProt P69905), 142 residues: two lines of 70 and one
#: of 2 in FASTA.
HBA = (
    "MVLSPADKTNVKAAWGKVGAHAGEYGAEALERMFLSFPTTKTYFPHFDLSHGSAQVKGHGKKVADALTNAVAHV"
    "DDMPNALSALSDLHAHKLRVDPVNFKLLSHCLLVTLAAHLPAEFTPAVHASLDKFLASVSTVLTSKYR"
)
#: Its beta chain (P68871), 147 residues.
HBB = (
    "MVHLTPEEKSAVTALWGKVNVDEVGGEALGRLLVVYPWTQRFFESFGDLSTPDAVMGNPKVKAHGKKVLGAFSDG"
    "LAHLDNLKGTFATLSELHCDKLHVDPENFRLLGNVLVCVLAHHFGKEFTPPVQAAYQKVVAGVANALAHKYH"
)
#: The start of pUC19's lacZ alpha, as the sequence tests have it.
BASES = "ATGACCATGATTACGGATTCACTGGCCGTCGTTTTACAACGTCGTGACTGGGAAAACCCTGGCG"
#: (SMILES, standard InChIKey, name): aspirin, (S)-ibuprofen with its one stereocentre,
#: and (E)-tamoxifen, whose double bond's configuration is the drug.
COMPOUNDS = [
    ("CC(=O)Oc1ccccc1C(=O)O", "BSYNRYMUTXBXSQ-UHFFFAOYSA-N", "aspirin"),
    ("CC(C)Cc1ccc([C@H](C)C(=O)O)cc1", "HEFNNWSXXWATRW-JTQLQIEISA-N", "dexibuprofen"),
    (
        "CC/C(=C(\\c1ccccc1)c1ccc(OCCN(C)C)cc1)c1ccccc1",
        "NKANXQFJJICGDU-QPLCGJKRSA-N",
        "tamoxifen",
    ),
]


@pytest.fixture
def world(s3: Any) -> World:
    return build_world()


def api_as(world: World, principal: str = "viewer") -> Api:
    return Api(world.browser(principal))


def protein(world: World, sequence: str, **values: Any) -> Any:
    return services.create(
        world.users["editor"],
        "acme/protein.Protein",
        world.project,
        {"sequence": sequence, **values},
    ).entity


def plasmid(world: World, bases: str = BASES, **values: Any) -> Any:
    return services.create(
        world.users["editor"],
        "acme/dna.Plasmid",
        world.project,
        {"bases": bases, "topology": "linear", **values},
    ).entity


def molblock(smiles: str) -> str:
    """As the small_molecule plugin computes a compound's Molfile: 2D, kekulized."""
    mol = Chem.MolFromSmiles(smiles)
    rdDepictor.SetPreferCoordGen(True)
    rdDepictor.Compute2DCoords(mol)
    return str(Chem.MolToMolBlock(mol))


def compound(world: World, smiles: str, key: str, name: str, **values: Any) -> Any:
    return services.create(
        world.users["editor"],
        "acme/protein.Compound",
        world.project,
        {"smiles": smiles, "inchikey": key, "name": name, "molfile": molblock(smiles), **values},
        by_run=True,
    ).entity


def ask(api: Api, body: dict[str, Any]) -> Any:
    return api.post("/entity-exports", body)


def downloaded(api: Api, made: dict[str, Any]) -> bytes:
    response = api.get(f"/entity-exports/{made['id']}/content")
    assert response.status_code == 302, response.content
    fetched = requests.get(response["Location"], timeout=30)
    assert fetched.status_code == 200
    return fetched.content


def run_tasks() -> int:
    return Runner(queues=["default"]).run_ready()


def imported(tmp_path: Path, script: Path, content: bytes, name: str, **params: Any) -> Any:
    """What a plugin's import makes of a file, run as its worker runs it."""
    path = tmp_path / name
    path.write_bytes(content)
    run = {
        "params": {"file": FILE_ID, **params},
        "inputs": {},
        "files": {
            FILE_ID: {
                "name": name,
                "path": str(path),
                "size": len(content),
                "media_type": "text/plain",
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        },
    }
    (tmp_path / "input.json").write_text(json.dumps(run))
    (tmp_path / "out").mkdir(exist_ok=True)
    result = subprocess.run(  # noqa: S603 - our own script
        [sys.executable, str(script), *(["import"] if script == SDF_SCRIPT else [])],
        env={
            "IKI_INPUT": str(tmp_path / "input.json"),
            "IKI_OUTPUT": str(tmp_path / "out.json"),
            "IKI_OUT_DIR": str(tmp_path / "out"),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads((tmp_path / "out.json").read_text())


# --- FASTA of proteins ----------------------------------------------------------------------------


def test_a_selection_of_proteins_is_one_fasta_file_each_pinned_and_audited(
    world: World, tmp_path: Path
) -> None:
    alpha = protein(world, HBA, name="HBA1", description="Hemoglobin subunit alpha")
    beta = protein(world, HBB, name="HBB")
    api = api_as(world)
    response = ask(api, {"format": "protein-fasta", "entities": [beta.hrid, str(alpha.pk)]})
    assert response.status_code == 201, response.content
    made = response.json()
    assert made["state"] == "ready"
    assert (made["count"], made["done"]) == (2, 2)
    assert made["file_name"] == "Kinase Atlas Proteins (2).fasta"
    assert made["writer"]["builtin"] == "protein-fasta"
    content = downloaded(api, made)
    assert hashlib.sha256(content).hexdigest() == made["sha256"]
    assert len(content) == made["size"]
    text = content.decode()
    # In the order picked; NCBI's 70 residues a line.
    assert text.splitlines()[:4] == [
        f">{beta.hrid} HBB",
        HBB[:70],
        HBB[70:140],
        HBB[140:],
    ]
    assert f">{alpha.hrid} HBA1 Hemoglobin subunit alpha\n" in text
    read = list(SeqIO.parse(io.StringIO(text), "fasta"))  # type: ignore[no-untyped-call]
    assert [(r.id, str(r.seq)) for r in read] == [(beta.hrid, HBB), (alpha.hrid, HBA)]
    # The protein plugin's import makes the same proteins of it.
    output = imported(tmp_path, PROTEIN_IMPORT, content, "hemoglobin.fasta")
    assert [c["values"]["sequence"] for c in output["creates"]] == [HBB, HBA]
    assert output["creates"][1]["values"]["description"] == "HBA1 Hemoglobin subunit alpha"

    asked = AuditEvent.objects.get(action="entities.export_requested")
    assert asked.actor == world.users["viewer"]
    assert asked.project == world.project
    assert asked.data["records"] == [
        {"id": str(beta.pk), "hrid": beta.hrid, "revision": 1},
        {"id": str(alpha.pk), "hrid": alpha.hrid, "revision": 1},
    ]
    taken = AuditEvent.objects.get(action="entities.exported")
    assert taken.target_id == made["id"]
    assert taken.data["sha256"] == made["sha256"]
    assert taken.data["size"] == len(content)
    assert taken.data["records"] == asked.data["records"]
    assert taken.data["writer"] == made["writer"]
    assert taken.data["left_out"] == {}


def test_a_proteins_organism_is_in_brackets_as_ncbi_writes_it(world: World) -> None:
    human: Any = services.create(
        world.users["editor"],
        "iki/core.Organism",
        world.project,
        {"scientific_name": "Homo sapiens", "taxon_id": 9606},
    ).entity
    alpha = protein(world, HBA, name="HBA1", organism=str(human.entry_id or human.pk))
    api = api_as(world)
    response = api.get(f"/entities/{alpha.hrid}/export?format=protein-fasta")
    assert response.status_code == 200, response.content
    assert response["Content-Type"] == "chemical/seq-aa-fasta"
    assert response.content.decode().splitlines()[0] == f">{alpha.hrid} HBA1 [Homo sapiens]"


def test_records_of_types_that_dont_share_a_format_are_refused(world: World) -> None:
    alpha = protein(world, HBA)
    linear = plasmid(world)
    response = ask(api_as(world), {"format": "fasta", "entities": [alpha.hrid, linear.hrid]})
    assert response.status_code == 422, response.content
    assert "Proteins" in response.json()["errors"][0]["detail"]
    assert not RecordExport.objects.exists()


# --- A table's search, as GenBank -----------------------------------------------------------------


def test_a_tables_whole_search_is_one_genbank_file_in_its_order(
    world: World, tmp_path: Path
) -> None:
    first = plasmid(world, name="pDemo-1", description="First construct")
    second = plasmid(world, BASES + "GGATCC", name="pDemo-2")
    plasmid(world, BASES + "AAGCTT", name="pOther")
    api = api_as(world)
    response = ask(
        api,
        {
            "format": "genbank",
            "search": {
                "type": "acme/dna.Plasmid",
                "project": str(world.project.pk),
                "q": "pDemo",
                "sort": {"field": "name", "descending": True},
            },
        },
    )
    assert response.status_code == 201, response.content
    made = response.json()
    assert made["selection"] == "All plasmids in Kinase Atlas matching the table's search"
    content = downloaded(api, made)
    records = list(SeqIO.parse(io.StringIO(content.decode()), "genbank"))  # type: ignore[no-untyped-call]
    assert [(r.name, str(r.seq)) for r in records] == [
        ("pDemo-2", BASES + "GGATCC"),
        ("pDemo-1", BASES),
    ]
    # Each says which record and revision it is, in its COMMENT.
    assert records[1].annotations["comment"] == f"Exported from Iki: {first.hrid} (revision 1)."
    output = imported(tmp_path, DNA_IMPORT, content, "plasmids.gb", topology="linear")
    assert [c["values"]["bases"] for c in output["creates"] if "bases" in c["values"]] == [
        BASES + "GGATCC",
        BASES,
    ]
    asked = AuditEvent.objects.get(action="entities.export_requested")
    assert asked.data["search"]["q"] == "pDemo"
    assert [r["hrid"] for r in asked.data["records"]] == [second.hrid, first.hrid]


def test_a_search_sorted_by_several_fields_is_exported_in_the_tables_order(
    world: World,
) -> None:
    """The file's order is the table's: its first sort, then each further one for what that
    ties, as ``POST /entities/search`` returns them, and the audit log keeps every sort."""
    plasmid(world, name="pTie-A")
    plasmid(world, BASES + "GGATCC", name="pTie-B")
    plasmid(world, BASES + "AAGCTT", name="pTie-C", topology="circular")
    api = api_as(world)
    searched = {
        "type": "acme/dna.Plasmid",
        "project": str(world.project.pk),
        "q": "pTie",
        "sort": {"field": "topology", "descending": True},
        "then": [{"field": "name", "descending": True}],
    }
    table = api.post("/entities/search", searched)
    assert table.status_code == 200, table.content
    shown = [item["hrid"] for item in table.json()["items"]]
    response = ask(api, {"format": "genbank", "search": searched})
    assert response.status_code == 201, response.content
    content = downloaded(api, response.json())
    records = list(SeqIO.parse(io.StringIO(content.decode()), "genbank"))  # type: ignore[no-untyped-call]
    # Topology sorts in its values' order, so circular is first, descending; the two
    # linear ones tie on it and go by name, descending: not by when they were made.
    assert [r.name for r in records] == ["pTie-C", "pTie-B", "pTie-A"]
    asked = AuditEvent.objects.get(action="entities.export_requested")
    assert [r["hrid"] for r in asked.data["records"]] == shown
    assert asked.data["search"]["then"] == [{"field": "name", "descending": True}]
    made = RecordExport.objects.get(pk=response.json()["id"])
    assert made.search is not None and made.search["then"] == asked.data["search"]["then"]


def test_a_search_with_a_key_exports_dont_know_is_refused(world: World) -> None:
    """A search part the export doesn't take is refused, not silently left out."""
    response = ask(
        api_as(world),
        {"format": "genbank", "search": {"type": "acme/dna.Plasmid", "order": ["name"]}},
    )
    assert response.status_code == 422, response.content


def test_a_search_that_finds_too_many_is_refused(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(record_exports, "MAX_RECORDS", 2)
    for n in range(3):
        protein(world, HBA[: 20 + n])
    response = ask(
        api_as(world), {"format": "protein-fasta", "search": {"type": "acme/protein.Protein"}}
    )
    assert response.status_code == 422, response.content
    assert "more than 2 records" in response.json()["errors"][0]["detail"]


# --- Pinning, the background, and limits ----------------------------------------------------------


def test_a_large_export_is_written_in_the_background_from_the_revisions_asked_for(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(record_exports, "INLINE", 1)
    monkeypatch.setattr(record_exports, "PROGRESS_EVERY", 1)
    first = plasmid(world, name="pBefore")
    second = plasmid(world, BASES + "GGATCC", name="pSecond")
    api = api_as(world)
    response = ask(api, {"format": "fasta", "entities": [first.hrid, second.hrid]})
    assert response.status_code == 202, response.content
    made = response.json()
    assert (made["state"], made["done"], made["count"]) == ("queued", 0, 2)
    assert api.get(f"/entity-exports/{made['id']}/content").status_code == 409
    # Edited after it was asked for, before it's written: the file is still revision 1.
    services.update(
        world.users["editor"], first, {"name": "pAfter"}, expected_revision=first.revision
    )
    assert run_tasks() == 1
    ready = api.get(f"/entity-exports/{made['id']}").json()
    assert (ready["state"], ready["done"]) == ("ready", 2)
    expires = datetime.fromisoformat(ready["expires_at"])
    assert timedelta(days=6, hours=23) < expires - timezone.now() <= timedelta(days=7)
    text = downloaded(api, made).decode()
    assert text.startswith(f">{first.hrid} pBefore [topology=linear]\n")
    assert "pAfter" not in text
    # Downloadable again, each time on the record.
    downloaded(api, made)
    assert AuditEvent.objects.filter(action="entities.exported").count() == 2
    assert [e["id"] for e in api.get("/entity-exports").json()["items"]] == [made["id"]]


def test_a_person_and_a_project_have_a_few_exports_waiting_at_once(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each waiting export spools a file and holds a writer: a person has at most three in
    the background, a project's records at most ten, and asks again once one's written."""
    monkeypatch.setattr(record_exports, "INLINE", 0)
    monkeypatch.setattr(record_exports, "MOST_WAITING_IN_A_PROJECT", 4)
    first = plasmid(world, name="pWaiting")
    body = {"format": "fasta", "entities": [first.hrid]}
    viewer = api_as(world, "viewer")
    for _ in range(3):
        assert ask(viewer, body).status_code == 202
    refused = ask(viewer, body)
    said = problem(refused, 429, "too_many_exports")
    assert said["detail"].startswith("You have 3 exports waiting to be written.")
    assert refused["Retry-After"] == "60"
    assert RecordExport.objects.filter(requested_by=world.users["viewer"]).count() == 3
    # Someone else's are their own; but the project's records are in four at most.
    editor = api_as(world, "editor")
    assert ask(editor, body).status_code == 202
    said = problem(ask(editor, body), 429, "too_many_exports")
    assert said["detail"].startswith(f"{world.project.name} has 4 exports waiting")
    # Once one is written, there's room again.
    assert run_tasks() == 4
    assert ask(viewer, body).status_code == 202
    # One its task gave up on long ago doesn't count.
    RecordExport.objects.filter(state="queued").update(
        created_at=timezone.now() - timedelta(days=2)
    )
    for _ in range(3):
        assert ask(viewer, body).status_code == 202
    # A few records, written while the request waits, are never refused for it.
    monkeypatch.setattr(record_exports, "INLINE", 100)
    assert ask(viewer, body).status_code == 201


def test_a_record_its_writer_refuses_fails_the_export_naming_it(world: World) -> None:
    written = compound(world, *COMPOUNDS[0])
    unwritten: Any = services.create(
        world.users["editor"],
        "acme/protein.Compound",
        world.project,
        {"smiles": "CC(=O)Nc1ccc(O)cc1", "inchikey": "RZVAJINKPMORJF-UHFFFAOYSA-N"},
    ).entity
    response = ask(api_as(world), {"format": "sdf", "entities": [written.hrid, unwritten.hrid]})
    assert response.status_code == 201, response.content
    made = response.json()
    assert made["state"] == "failed"
    assert list(made["refused"]) == [unwritten.hrid]
    assert "no structure to write yet" in made["refused"][unwritten.hrid]
    assert made["error"].startswith("1 record can't be written, so no file was made")
    assert made["sha256"] == ""


def test_a_file_past_its_size_limit_fails(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(record_exports, "MAX_BYTES", 100)
    alpha = protein(world, HBA)
    made = ask(api_as(world), {"format": "protein-fasta", "entities": [alpha.hrid]}).json()
    assert made["state"] == "failed"
    assert "export fewer records" in made["error"]


def test_a_selection_is_bounded(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    response = ask(api_as(world), {"format": "protein-fasta", "entities": ["PRT-000001"] * 10_001})
    assert response.status_code == 422
    assert "at most 10000 items" in response.json()["errors"][0]["detail"]
    monkeypatch.setattr(record_exports, "MAX_RECORDS", 1)
    alpha, beta = protein(world, HBA), protein(world, HBB)
    with pytest.raises(Refused, match="Pick at most 1 records for one file"):
        record_exports.start(
            world.users["viewer"], "protein-fasta", entities=[alpha.hrid, beta.hrid]
        )


def test_a_file_is_kept_a_week_unless_a_project_is_held(world: World) -> None:
    alpha = protein(world, HBA)
    api = api_as(world)
    kept = ask(api, {"format": "protein-fasta", "entities": [alpha.hrid]}).json()
    expired = ask(api, {"format": "protein-fasta", "entities": [alpha.hrid]}).json()
    RecordExport.objects.filter(pk__in=[kept["id"], expired["id"]]).update(
        expires_at=timezone.now() - timedelta(minutes=1)
    )
    hold = holds.place(world.users["org_admin"], world.project, "Smith v. Example Bio")
    call_command("maintain", stdout=io.StringIO())
    assert api.get(f"/entity-exports/{kept['id']}").json()["state"] == "ready"
    holds.release(world.users["org_admin"], hold, reason="The case settled.")
    call_command("maintain", stdout=io.StringIO())
    gone = api.get(f"/entity-exports/{expired['id']}").json()
    assert gone["state"] == "expired"
    response = api.get(f"/entity-exports/{expired['id']}/content")
    assert response.status_code == 409
    assert "expired" in response.json()["detail"]


def test_an_inline_export_whose_write_was_interrupted_is_failed_by_maintenance(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process that stops while writing an export its request waits for leaves it
    writing, with no task behind it: maintenance fails it once it's past any deadline. A
    background export's own task is left to finish it."""
    alpha = protein(world, HBA)
    api = api_as(world)

    def killed(made: RecordExport, **_: Any) -> str:
        made.state = "writing"
        made.save(update_fields=["state"])
        raise SystemExit  # the web process stops partway

    with monkeypatch.context() as patched:
        patched.setattr(record_exports, "_write", killed)
        with pytest.raises(SystemExit):
            record_exports.start(world.users["viewer"], "protein-fasta", entities=[alpha.hrid])
    stuck = RecordExport.objects.get()
    assert (stuck.state, stuck.background) == ("writing", False)
    monkeypatch.setattr(record_exports, "INLINE", 0)
    queued = ask(api, {"format": "protein-fasta", "entities": [alpha.hrid]}).json()
    assert queued["state"] == "queued"
    call_command("maintain", stdout=io.StringIO())
    # Not yet: it may still be being written.
    assert api.get(f"/entity-exports/{stuck.pk}").json()["state"] == "writing"
    RecordExport.objects.update(
        created_at=timezone.now()
        - timedelta(seconds=plugin_writers.inline_deadline())
        - record_exports.INTERRUPTED_AFTER
    )
    out = io.StringIO()
    call_command("maintain", stdout=out)
    assert "failed 1 interrupted record export(s)" in out.getvalue()
    failed = api.get(f"/entity-exports/{stuck.pk}").json()
    assert failed["state"] == "failed"
    assert failed["error"] == "Writing it was interrupted, so no file was made: export again."
    assert api.get(f"/entity-exports/{queued['id']}").json()["state"] == "queued"
    assert run_tasks() == 1
    assert api.get(f"/entity-exports/{queued['id']}").json()["state"] == "ready"


# --- Who may ------------------------------------------------------------------------------------


def test_only_records_the_requester_can_read_are_exported(world: World) -> None:
    alpha = protein(world, HBA)
    response = ask(api_as(world, "outsider"), {"format": "protein-fasta", "entities": [alpha.hrid]})
    assert response.status_code == 404
    assert response.json()["detail"] == f"There's no entity {alpha.hrid} you can read."
    # A search finds only what its reader can read, as the CSV download does: nothing here.
    response = ask(
        api_as(world, "outsider"),
        {"format": "protein-fasta", "search": {"type": "acme/protein.Protein"}},
    )
    assert response.status_code == 422
    assert "finds no records" in response.json()["errors"][0]["detail"]


def test_an_export_is_its_requesters_and_downloads_while_they_can_read_it(world: World) -> None:
    alpha = protein(world, HBA)
    made = ask(api_as(world), {"format": "protein-fasta", "entities": [alpha.hrid]}).json()
    other = api_as(world, "editor")
    assert other.get(f"/entity-exports/{made['id']}").status_code == 404
    assert other.get(f"/entity-exports/{made['id']}/content").status_code == 404
    assert other.get("/entity-exports").json()["items"] == []
    ProjectMembership.objects.filter(project=world.project, user=world.users["viewer"]).delete()
    # Out of the project, its export of the project's records is gone from them too.
    assert api_as(world).get(f"/entity-exports/{made['id']}/content").status_code == 404
    assert api_as(world).get("/entity-exports").json()["items"] == []
    assert not AuditEvent.objects.filter(action="entities.exported").exists()


def test_a_record_read_through_a_project_its_linked_into_is_exported(world: World) -> None:
    """Someone who reads a record only in a project it's linked into exports it, and their
    export is theirs while they can read that project."""
    alpha = protein(world, HBA)
    elsewhere = Project.objects.create(team=world.team, name="Structural Genomics")
    ProjectMembership.objects.create(
        project=elsewhere, user=world.users["outsider"], role=ProjectRole.VIEWER
    )
    ProjectLink.objects.create(entity_id=alpha.pk, project=elsewhere)
    api = api_as(world, "outsider")
    made = ask(api, {"format": "protein-fasta", "entities": [alpha.hrid]}).json()
    assert made["state"] == "ready"
    assert api.get(f"/entity-exports/{made['id']}").status_code == 200
    assert downloaded(api, made).startswith(f">{alpha.hrid}".encode())
    ProjectMembership.objects.filter(project=elsewhere, user=world.users["outsider"]).delete()
    assert api.get(f"/entity-exports/{made['id']}").status_code == 404


def test_an_export_of_two_projects_records_is_in_each_projects_audit_log(
    world: World,
) -> None:
    """Asking for and downloading a file of several projects' records names no one project,
    but filtering the audit log by either finds both events; a third project's doesn't."""
    elsewhere = Project.objects.create(team=world.team, name="Structural Genomics")
    unrelated = Project.objects.create(team=world.team, name="Assay Development")
    for place in (elsewhere, unrelated):
        ProjectMembership.objects.create(
            project=place, user=world.users["editor"], role=ProjectRole.EDITOR
        )
    alpha = protein(world, HBA)
    beta: Any = services.create(
        world.users["editor"], "acme/protein.Protein", elsewhere, {"sequence": HBB}
    ).entity
    api = api_as(world, "editor")
    made = ask(api, {"format": "protein-fasta", "entities": [alpha.hrid, beta.hrid]}).json()
    downloaded(api, made)
    events = AuditEvent.objects.filter(action__startswith="entities.export")
    assert {(e.action, e.project_id) for e in events} == {
        ("entities.export_requested", None),
        ("entities.exported", None),
    }
    assert all(
        sorted(e.data["projects"]) == sorted([str(world.project.pk), str(elsewhere.pk)])
        for e in events
    )
    auditor = world.browser("auditor")
    for place in (world.project, elsewhere):
        for action in ("entities.export_requested", "entities.exported"):
            query = {"project": str(place.pk), "action": action}
            page = auditor.get("/admin/audit/", query).content.decode()
            assert made["file_name"] in page, (place.name, action)
    page = auditor.get("/admin/audit/", {"project": str(unrelated.pk)}).content.decode()
    assert made["file_name"] not in page


def test_a_file_whose_references_its_requester_cant_read_now_isnt_downloaded(
    world: World,
) -> None:
    supplier = protein(world, HBA, name="A record it refers to")
    aspirin = compound(world, *COMPOUNDS[0], supplier=str(supplier.pk))
    api = api_as(world)
    made = ask(api, {"format": "sdf", "entities": [aspirin.hrid], "fields": ["supplier"]}).json()
    # What it read, the record it refers to among them: as its entry, which a ref names.
    read = {str(r) for r in RecordExport.objects.get(pk=made["id"]).references or []}
    assert {str(supplier.pk), str(supplier.entry_id)} & read
    # As if the record it names had moved out of the requester's reach.
    with patch.object(access, "all_references_readable", return_value=False):
        response = api.get(f"/entity-exports/{made['id']}/content")
    assert response.status_code == 409
    assert "can no longer read" in response.json()["detail"]


def test_a_read_only_token_exports(world: World) -> None:
    alpha = protein(world, HBA)
    _, tokens = signed_in_token(world, "viewer", scope="read", projects=[world.project])
    response = bearer(
        tokens["access_token"],
        "POST",
        "/entity-exports",
        {"format": "protein-fasta", "entities": [alpha.hrid]},
    )
    assert response.status_code == 201, response.content
    made = response.json()
    assert bearer(tokens["access_token"], "GET", f"/entity-exports/{made['id']}").status_code == 200


@pytest.mark.parametrize("background", [False, True], ids=["inline", "background"])
def test_a_token_given_some_projects_exports_only_what_they_hold(
    world: World, monkeypatch: pytest.MonkeyPatch, *, background: bool
) -> None:
    """ADR 0152: a token given some projects reads only what they hold, whether its export
    is written while the request waits or by a task afterwards. A record its records refer
    to in another project its person reads isn't read for it."""
    elsewhere = Project.objects.create(team=world.team, name="Supplier records")
    for principal in ("editor", "viewer"):
        ProjectMembership.objects.create(
            project=elsewhere, user=world.users[principal], role=ProjectRole.EDITOR
        )
    supplier: Any = services.create(
        world.users["editor"],
        "acme/protein.Protein",
        elsewhere,
        {"sequence": HBB, "name": "Haemoglobin beta, the supplier's"},
    ).entity
    # Referred to from Kinase Atlas while it was linked there, then unlinked.
    ProjectLink.objects.create(entity_id=supplier.pk, project=world.project)
    aspirin = compound(world, *COMPOUNDS[0], supplier=str(supplier.pk))
    ProjectLink.objects.filter(entity_id=supplier.pk, project=world.project).delete()
    if background:
        monkeypatch.setattr(record_exports, "INLINE", 0)
    _, tokens = signed_in_token(world, "viewer", scope="read", projects=[world.project])
    token = tokens["access_token"]
    response = bearer(
        token,
        "POST",
        "/entity-exports",
        {"format": "sdf", "entities": [aspirin.hrid], "fields": ["supplier"]},
    )
    assert response.status_code == (202 if background else 201), response.content
    if background:
        assert run_tasks() == 1
    made = RecordExport.objects.get(pk=response.json()["id"])
    assert made.state == "ready", made.error
    read = {str(r) for r in made.references or []}
    assert not {str(supplier.pk), str(supplier.entry_id)} & read
    shown = bearer(token, "GET", f"/entity-exports/{made.pk}").json()
    assert supplier.hrid not in json.dumps(shown)
    assert bearer(token, "GET", f"/entity-exports/{made.pk}/content").status_code == 302


def test_a_writer_is_given_no_reference_its_reader_cant_read(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record a plugin's writer reads through a reference is given as its reader may read
    it (ADR 0067, ADR 0230): what it refers to in turn, in a field, a list or a row, is
    "unreadable" where that's in a project its reader can't read."""
    store = Project.objects.create(team=world.team, name="Protein store")
    ProjectMembership.objects.create(
        project=store, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    editor = world.users["editor"]
    hidden: Any = services.create(
        editor, "acme/protein.Protein", store, {"sequence": HBB, "name": "Store's own"}
    ).entity
    shown = protein(world, HBA, name="Haemoglobin alpha")
    # Referred to from Kinase Atlas while it was linked there, then unlinked.
    link = ProjectLink.objects.create(entity_id=hidden.pk, project=world.project)
    sample = services.create(
        editor,
        "acme/protein.Sample",
        world.project,
        {
            "label": "Serum 12",
            "protein": str(hidden.pk),
            "material": str(shown.pk),
            "additions": [{"material": str(hidden.pk)}, {"material": str(shown.pk)}],
        },
    ).entity
    tube: Any = services.create(
        editor, "acme/protein.Tube", world.project, {"barcode": "TB-0001", "sample": str(sample.pk)}
    ).entity
    link.delete()
    writer = SimpleNamespace(roles={"sample": SimpleNamespace(type="ref", unit=None)})
    monkeypatch.setattr(plugin_writers, "find", lambda _: writer)
    wanted = PluginExport(export="acme/example.writer", fields={"sample": "sample"})

    def given(user: Any) -> dict[str, Any]:
        record = exports._plugin_record(user, tube, tube.revision, wanted, set())
        fields: dict[str, Any] = record.gathered.roles["sample"]["fields"]
        return fields

    # A reference names its record's entry (ADR 0115).
    seen = given(world.users["viewer"])
    assert seen["protein"] == "unreadable"
    assert seen["material"] == str(shown.entry_id)
    assert [a["material"] for a in seen["additions"]] == ["unreadable", str(shown.entry_id)]
    assert not {str(hidden.pk), str(hidden.entry_id)} & set(json.dumps(seen).split('"'))
    # Its editor reads the store, so the writer is given it.
    assert given(editor)["protein"] == str(hidden.entry_id)


# --- SDF of compounds -----------------------------------------------------------------------------


def test_compounds_are_one_sdf_with_their_fields_as_data(world: World, tmp_path: Path) -> None:
    supplier = protein(world, HBA, name="Not a supplier, but a record")
    made_compounds = [
        compound(world, *COMPOUNDS[0], synonyms=["acetylsalicylic acid", "ASA"]),
        compound(world, *COMPOUNDS[1], description="The active (S) enantiomer.\n\nOf ibuprofen."),
        compound(world, *COMPOUNDS[2], supplier=str(supplier.pk), stereochemistry="absolute"),
    ]
    api = api_as(world)
    types = {t["name"]: t for t in api.get("/entity-types").json()}
    (declared,) = types["acme/protein.Compound"]["exports"]
    assert declared["export"] == "compound"
    assert declared["structure"] == "molfile"
    assert "molfile" not in declared["data"] and "name" in declared["data"]
    assert [f["many"] for f in declared["formats"]] == ["file", "one"]
    response = ask(api, {"format": "sdf", "entities": [c.hrid for c in made_compounds]})
    assert response.status_code == 201, response.content
    made = response.json()
    assert made["fields"] == declared["data"]
    content = downloaded(api, made)
    text = content.decode()
    assert text.count("$$$$\n") == 3
    # RDKit reads each structure back as the compound it is, with its data.
    supplier_ = Chem.SDMolSupplier()
    supplier_.SetData(text)
    read = list(supplier_)
    assert [Chem.MolToInchiKey(m) for m in read] == [c[1] for c in COMPOUNDS]  # type: ignore[no-untyped-call]
    assert [m.GetProp("_Name") for m in read] == ["aspirin", "dexibuprofen", "tamoxifen"]
    first, second, third = read
    assert first.GetProp("IKI_HRID") == made_compounds[0].hrid
    assert first.GetProp("IKI_UUID") == str(made_compounds[0].pk)
    assert first.GetProp("IKI_REVISION") == "1"
    assert first.GetProp("synonyms") == "acetylsalicylic acid; ASA"
    assert third.GetProp("supplier") == supplier.hrid
    assert third.GetProp("stereochemistry") == "absolute"
    # A blank line would end the value, so it's left out, and the record says so.
    assert second.GetProp("description") == "The active (S) enantiomer.\nOf ibuprofen."
    assert "blank lines are left out" in second.GetProp("IKI_NOTES")
    assert made["notes"] == {made_compounds[1].hrid: [second.GetProp("IKI_NOTES")]}
    # Each record's Molfile says which record and revision it is, in its comment line.
    assert text.splitlines()[2] == f"Exported from Iki: {made_compounds[0].hrid} (revision 1)."
    # The small_molecule plugin's import makes the same compounds of it.
    output = imported(tmp_path, SDF_SCRIPT, content, "compounds.sdf")
    assert [(c["values"]["inchikey"], c["values"].get("name")) for c in output["creates"]] == [
        (key, name) for _, key, name in COMPOUNDS
    ]
    assert output["creates"][0]["values"]["synonyms"] == ["acetylsalicylic acid", "ASA"]


def test_an_sdf_carries_the_fields_chosen_and_only_those_it_can(world: World) -> None:
    aspirin = compound(world, *COMPOUNDS[0])
    api = api_as(world)
    response = ask(api, {"format": "sdf", "entities": [aspirin.hrid], "fields": ["name"]})
    made = response.json()
    assert made["fields"] == ["name"]
    supplier = Chem.SDMolSupplier()
    supplier.SetData(downloaded(api, made).decode())
    (mol,) = list(supplier)
    assert set(mol.GetPropNames()) == {"IKI_HRID", "IKI_UUID", "IKI_REVISION", "name"}
    for wrong in ("molfile", "nonesuch"):
        response = ask(api, {"format": "sdf", "entities": [aspirin.hrid], "fields": [wrong]})
        assert response.status_code == 422, wrong
    response = ask(api, {"format": "fasta", "entities": [aspirin.hrid], "fields": ["name"]})
    assert response.status_code == 422


def test_one_compound_downloads_as_a_molfile_or_an_sdf(world: World) -> None:
    long_name = "a compound whose name runs on " * 4
    aspirin = compound(world, COMPOUNDS[0][0], COMPOUNDS[0][1], long_name.strip())
    api = api_as(world)
    response = api.get(f"/entities/{aspirin.hrid}/export?format=molfile")
    assert response.status_code == 200, response.content
    assert response["Content-Type"] == "chemical/x-mdl-molfile"
    text = response.content.decode()
    lines = text.splitlines()
    assert len(lines[0]) <= 80 and long_name.startswith(lines[0])
    assert lines[3].endswith("V2000")
    assert lines[-1] == "M  END"
    assert Chem.MolToInchiKey(Chem.MolFromMolBlock(text)) == COMPOUNDS[0][1]  # type: ignore[no-untyped-call]
    assert response["Iki-Left-Out"] == "1"
    response = api.get(f"/entities/{aspirin.hrid}/export?format=sdf&fields=name&fields=smiles")
    assert response.status_code == 200, response.content
    event = AuditEvent.objects.filter(action="entity.exported").latest("id")
    assert event.data["fields"] == ["name", "smiles"]
    assert event.data["writer"]["builtin"] == "sdf"
    assert "<IKI_NOTES>" in response.content.decode()
