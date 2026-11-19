"""Bundles: a project's entities, with their history, files and authors, signed (ADR 0096)."""

import hashlib
import io
import tarfile
import threading
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection

from iki_server.accounts.models import User
from iki_server.audit.models import AuditEvent
from iki_server.bundles import export, keys, reading
from iki_server.entities import services
from iki_server.entities.models import Entity
from iki_server.files import storage
from iki_server.files.models import Blob, File
from iki_server.org.models import Project, ProjectMembership, ProjectRole, Team

pytestmark = pytest.mark.django_db

#: The test settings' signing key's public key.
TEST_KEY = "ed25519:riAtz4Nv0SFcXARze-_ieUqB027N1ie892Fk7U0Ao-4"
TRUSTED = {"test": (TEST_KEY,)}
LIMIT = 1024**3
PDB = b"HEADER    KINASE\nATOM      1  N   MET A   1\n"


def person(email: str) -> User:
    return User.objects.create_user(email=email, name=email.split("@")[0].title())


def stored(project: Project, data: bytes, name: str = "1abc.pdb") -> File:
    sha = hashlib.sha256(data).hexdigest()
    key = storage.blob_key(sha)
    storage.client().put_object(Bucket=storage.bucket(), Key=key, Body=data)
    blob = Blob.objects.create(sha256=sha, size=len(data), storage_uri=storage.uri(key))
    return File.objects.create(blob=blob, name=name, media_type="chemical/x-pdb", project=project)


@pytest.fixture
def lab(s3: Any) -> dict[str, Any]:
    team = Team.objects.create(name="Structural biology")
    kinases = Project.objects.create(team=team, name="Kinases")
    shared = Project.objects.create(team=team, name="Shared")
    alice, bob = person("alice@example.org"), person("bob@example.org")
    ProjectMembership.objects.create(project=kinases, user=alice, role=ProjectRole.ADMIN)
    ProjectMembership.objects.create(project=kinases, user=bob, role=ProjectRole.EDITOR)
    ProjectMembership.objects.create(project=shared, user=alice, role=ProjectRole.EDITOR)
    # Another project's record of a type without an identity, which a reference may name
    # (one of a type with an identity names a registry entry the project has: ADR 0115).
    elsewhere = services.create(
        alice,
        "acme/protein.ProteinStructure",
        shared,
        {"structure": str(stored(shared, PDB + b"END\n", "2xyz.pdb").pk), "method": "xray"},
    ).entity
    earlier = services.create(
        alice, "acme/protein.Protein", kinases, {"sequence": "MKA", "name": "Earlier"}
    ).entity
    kinase = services.create(
        bob,
        "acme/protein.Protein",
        kinases,
        {"sequence": "MKT", "name": "ABL1", "source": str(earlier.pk)},
        reason="From the LIMS",
    ).entity
    kinase = Entity.objects.get(pk=kinase.pk)
    services.update(alice, kinase, {"source": str(elsewhere.pk)}, expected_revision=1)
    pdb = stored(kinases, PDB)
    structure = services.create(
        alice,
        "acme/protein.ProteinStructure",
        kinases,
        {"structure": str(pdb.pk), "method": "xray", "chains": [str(kinase.pk)]},
    ).entity
    structure = Entity.objects.get(pk=structure.pk)
    services.update(bob, structure, {"method": "nmr"}, expected_revision=1, reason="Re-solved")
    return {
        "kinases": kinases,
        "alice": alice,
        "bob": bob,
        "kinase": Entity.objects.get(pk=kinase.pk),
        "earlier": earlier,
        "elsewhere": elsewhere,
        "structure": Entity.objects.get(pk=structure.pk),
        "pdb": pdb,
    }


def exported(lab: dict[str, Any], tmp_path: Path, **kwargs: Any) -> tuple[export.Exported, Path]:
    out = tmp_path / "bundle.tar.gz"
    made = export.write(
        lab["kinases"],
        [lab["structure"]],
        out,
        by=kwargs.pop("by", lab["alice"]),
        reason=kwargs.pop("reason", "Validated structures, for production"),
        selection=f"{lab['structure'].hrid} and what it refers to",
    )
    return made, out


def test_a_bundle_carries_entities_with_their_history_files_and_authors(
    lab: dict[str, Any], tmp_path: Path
) -> None:
    made, out = exported(lab, tmp_path)
    bundle = reading.read(out, TRUSTED, limit=LIMIT)
    assert bundle.trusted_as == "test"
    assert bundle.sha256 == made.sha256 == hashlib.sha256(out.read_bytes()).hexdigest()
    m = bundle.manifest
    assert m["source"] == {
        "deployment": "test",
        "url": m["source"]["url"],
        "iki": m["source"]["iki"],
        "key": TEST_KEY,
    }
    assert m["exported_by"]["email"] == "alice@example.org"
    assert m["reason"] == "Validated structures, for production"
    assert m["project"]["name"] == "Kinases"
    # The structure, and the protein its chains name now; the others only as references.
    # And the protein's registry entry (ADR 0115), which its record is of.
    assert m["counts"] == {
        "entities": 2,
        "entries": 1,
        "revisions": 4,
        "files": 1,
        "outside": 2,
        "runs": 0,
        "signatures": 0,
    }
    assert made.counts == m["counts"]
    plugins = {p["name"]: p for p in m["plugins"]}
    protein = plugins["acme/protein"]["types"]["acme/protein.Protein"]
    assert protein["schema_version"] == 3
    assert protein["units"]["temperature_optimum"] == "Cel"
    assert "acme/dna.Sequence" in plugins["acme/dna"]["types"]
    assert plugins["acme/prediction"]["extensions"] == {
        "acme/protein.Protein": {"units": {}},
        "acme/protein.ProteinStructure": {"units": {}},
    }

    entities = {e["hrid"]: e for e in bundle.lines("entities.ndjson")}
    kinase, structure = lab["kinase"], lab["structure"]
    assert set(entities) == {kinase.hrid, structure.hrid}
    k = entities[kinase.hrid]
    assert [(r["number"], r["kind"], r["reason"]) for r in k["revisions"]] == [
        (1, "created", "From the LIMS"),
        (2, "edited", ""),
    ]
    people = {p["id"]: p["email"] for p in bundle.lines("people.ndjson")}
    assert [people[r["user"]] for r in k["revisions"]] == ["bob@example.org", "alice@example.org"]
    assert people[k["created_by"]] == "bob@example.org"
    own = [h for h in k["history"] if h["table"] == "acme/protein.Protein"]
    assert [(h["revision"], h["values"]["source"]) for h in own] == [
        (1, str(lab["earlier"].entry_id)),
        (2, str(lab["elsewhere"].pk)),
    ]
    assert any(
        h["table"] == "iki/core.Sequence" and h["values"]["name"] == "ABL1" for h in k["history"]
    )
    s = entities[structure.hrid]
    values = [h["values"] for h in s["history"] if h["table"] == "acme/protein.ProteinStructure"]
    assert [v["method"] for v in values] == ["xray", "nmr"]
    assert values[0]["chains"] == [str(kinase.entry_id)]
    assert values[0]["structure"] == str(lab["pdb"].pk)

    (pdb,) = bundle.lines("files.ndjson")
    assert pdb["id"] == str(lab["pdb"].pk)
    assert pdb["registered"] is None
    assert b"".join(bundle.blob(pdb["sha256"])) == PDB

    outside = {o["id"]: o for o in bundle.lines("outside.ndjson")}
    assert outside[str(lab["elsewhere"].pk)] == {
        "id": str(lab["elsewhere"].pk),
        "hrid": lab["elsewhere"].hrid,
        "type": "acme/protein.ProteinStructure",
        "why": "another-project",
    }
    # Only the kinase's first revision referred to it.
    assert outside[str(lab["earlier"].entry_id)]["why"] == "history"

    event = AuditEvent.objects.get(action="bundle.exported")
    assert event.actor_id == lab["alice"].pk
    assert event.project_id == lab["kinases"].pk
    assert event.data["sha256"] == made.sha256
    assert event.data["reason"] == "Validated structures, for production"
    assert event.data["entities"] == 2


@pytest.mark.django_db(transaction=True)
def test_an_edit_while_it_exports_isnt_half_in_the_bundle(
    lab: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bundle is the database at one moment: an edit committed after the export read
    the structure's history, and before it read its revisions, isn't in it at all, so an
    import can't show revision 3 with revision 2's values."""
    structure = lab["structure"]
    found = export._find

    def edited_meanwhile(*args: Any) -> Any:
        read = found(*args)

        def edit() -> None:
            try:
                services.update(
                    lab["alice"],
                    Entity.objects.get(pk=structure.pk),
                    {"method": "xray"},
                    expected_revision=2,
                    reason="Back to the crystal structure",
                )
            finally:
                connection.close()

        editor = threading.Thread(target=edit)
        editor.start()
        editor.join()
        return read

    monkeypatch.setattr(export, "_find", edited_meanwhile)
    _made, out = exported(lab, tmp_path)

    assert Entity.objects.get(pk=structure.pk).revision == 3
    bundle = reading.read(out, TRUSTED, limit=LIMIT)
    (s,) = [e for e in bundle.lines("entities.ndjson") if e["hrid"] == structure.hrid]
    assert [r["number"] for r in s["revisions"]] == [1, 2]
    values = [h for h in s["history"] if h["table"] == "acme/protein.ProteinStructure"]
    assert [(h["revision"], h["values"]["method"]) for h in values] == [(1, "xray"), (2, "nmr")]


def test_only_a_project_admin_exports_with_a_reason_and_a_key(
    lab: dict[str, Any], tmp_path: Path, settings: Any
) -> None:
    with pytest.raises(export.ExportError, match="needs its Admin role"):
        exported(lab, tmp_path, by=lab["bob"])
    with pytest.raises(export.ExportError, match="Say why"):
        exported(lab, tmp_path, reason="  ")
    with pytest.raises(export.ExportError, match="belongs to another project"):
        export.write(
            lab["kinases"],
            [lab["elsewhere"]],
            tmp_path / "x",
            by=lab["alice"],
            reason="r",
            selection="",
        )
    settings.IKI_BUNDLE_SIGNING_KEY = ""
    with pytest.raises(export.ExportError, match="no bundle signing key"):
        exported(lab, tmp_path)
    assert not AuditEvent.objects.filter(action="bundle.exported").exists()
    assert not (tmp_path / "bundle.tar.gz").exists()


def rewrite(source: Path, target: Path, change: Any) -> None:
    """Copies the archive member by member, through ``change(name, data) -> [(name, data)]``."""
    with tarfile.open(source, "r:gz") as old, tarfile.open(target, "w:gz") as new:
        for info in old:
            stream = old.extractfile(info)
            assert stream is not None
            for name, data in change(info.name, stream.read()):
                added = tarfile.TarInfo(name)
                added.size = len(data)
                new.addfile(added, io.BytesIO(data))


def refused(path: Path, trusted: Any = TRUSTED) -> str:
    with pytest.raises(reading.BundleError) as raised:
        reading.read(path, trusted, limit=LIMIT)
    return str(raised.value)


def test_a_changed_bundle_is_refused(lab: dict[str, Any], tmp_path: Path) -> None:
    _, out = exported(lab, tmp_path)
    bad = tmp_path / "bad.tar.gz"

    def edit_entities(name: str, data: bytes) -> list[tuple[str, bytes]]:
        return [(name, data.replace(b"ABL1", b"ABL2") if name == "entities.ndjson" else data)]

    rewrite(out, bad, edit_entities)
    assert refused(bad) == "entities.ndjson isn't what its bundle.json says: it was changed."

    def edit_manifest(name: str, data: bytes) -> list[tuple[str, bytes]]:
        return [(name, data.replace(b"Validated", b"Approved") if name == "bundle.json" else data)]

    rewrite(out, bad, edit_manifest)
    assert "Its signature doesn't match its bundle.json" in refused(bad)

    def add(name: str, data: bytes) -> list[tuple[str, bytes]]:
        return [(name, data), *([("notes.ndjson", b"{}\n")] if name == "people.ndjson" else [])]

    rewrite(out, bad, add)
    assert refused(bad) == "It holds notes.ndjson, which its bundle.json doesn't list."

    def drop(name: str, data: bytes) -> list[tuple[str, bytes]]:
        return [] if name.startswith("blobs/") else [(name, data)]

    rewrite(out, bad, drop)
    assert refused(bad).startswith("It's missing blobs/sha256/")

    def escape(name: str, data: bytes) -> list[tuple[str, bytes]]:
        return [("../../etc/passwd" if name == "people.ndjson" else name, data)]

    rewrite(out, bad, escape)
    assert refused(bad) == "'../../etc/passwd' isn't something a bundle holds."

    bad.write_bytes(b"not a bundle")
    assert refused(bad).startswith("It isn't a bundle Iki can read")
    with pytest.raises(reading.BundleError, match="more than 10 bytes uncompressed"):
        reading.read(out, TRUSTED, limit=10)


def test_a_bundle_from_an_untrusted_key_reads_but_isnt_trusted(
    lab: dict[str, Any], tmp_path: Path, settings: Any
) -> None:
    other, public = keys.generate()
    settings.IKI_BUNDLE_SIGNING_KEY = other
    _, out = exported(lab, tmp_path)
    bundle = reading.read(out, TRUSTED, limit=LIMIT)
    assert bundle.manifest["source"]["key"] == public
    assert bundle.trusted_as is None
    # A trusted key for another deployment doesn't make it trusted as this one.
    assert reading.read(out, {"staging": (public,)}, limit=LIMIT).trusted_as is None
    assert reading.read(out, {"test": (TEST_KEY, public)}, limit=LIMIT).trusted_as == "test"


def test_keys_are_checked_where_they_are_configured() -> None:
    private, public = keys.generate()
    assert keys.public_text(keys.private_key(private)) == public
    assert keys.trusted(f" staging={public} , qa={public},staging={TEST_KEY}") == {
        "staging": (public, TEST_KEY),
        "qa": (public,),
    }
    assert keys.trusted("") == {}
    for bad, message in [
        ("staging", "isn't name=ed25519:key"),
        (f"no spaces={public}", "isn't name=ed25519:key"),
        ("staging=rsa:abc", "starts with ed25519:"),
        ("staging=ed25519:abc", "is 2 bytes, not Ed25519's 32"),
    ]:
        with pytest.raises(ValueError, match=message):
            keys.trusted(bad)
    with pytest.raises(ValueError, match="isn't base64url"):
        keys.private_key("!!!")


def test_the_commands_make_keys_and_verify_bundles(
    lab: dict[str, Any], tmp_path: Path, settings: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    call_command("bundle_key")
    made = capsys.readouterr()
    private = made.out.strip()
    keys.private_key(private)  # is a key
    assert f"Its public key is {keys.public_text(keys.private_key(private))}." in made.err
    call_command("bundle_key", "--public")
    assert capsys.readouterr().out.strip() == f"test={TEST_KEY}"

    _, out = exported(lab, tmp_path)
    with pytest.raises(CommandError, match="doesn't trust that key for test"):
        call_command("verify_bundle", str(out))
    shown = capsys.readouterr().out
    assert "From Kinases (Structural biology) on test" in shown
    assert "Exported by Alice <alice@example.org>" in shown
    assert "2 entities, 4 revisions, 1 files; 2 referred to but not in it." in shown
    settings.IKI_BUNDLE_TRUSTED_KEYS = TRUSTED
    call_command("verify_bundle", str(out))
    assert "This deployment trusts it as test." in capsys.readouterr().out
    (tmp_path / "junk").write_bytes(b"junk")
    with pytest.raises(CommandError, match="junk isn't a bundle Iki can read"):
        call_command("verify_bundle", str(tmp_path / "junk"))
