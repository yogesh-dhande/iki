"""Electronic signatures travel in bundles (ADR 0096, ADR 0126): an export carries each
record's signatures as they were signed, ``iki bundle verify`` checks them offline against
the bundle's own history, and an import brings them, still verifying, with their key."""

import argparse
import io
import json
from pathlib import Path
from typing import Any

import pytest
from api_client import Api
from org_world import World
from test_bundle_imports import PUBLIC, applied, bundle_of, entities_of, protein, upload
from test_bundle_imports import world as world  # a fixture
from test_signatures import lot, ready, signed, standing
from test_states import ANALYST, QA, REVIEWER, roles

from iki_cli import bundle as cli
from iki_server.accounts.models import User
from iki_server.bundles import keys as keys_module
from iki_server.bundles import reading
from iki_server.entities import services
from iki_server.entities.models import Entity, Revision
from iki_server.signatures import offline, proof
from iki_server.signatures import services as signature_services
from iki_server.signatures.models import ForeignSigningKey, Signature, SigningKey

pytestmark = pytest.mark.django_db

TRUSTED = {"test": (PUBLIC,)}


def signed_lot(world: World, **values: Any) -> list[Any]:
    """A lot set to In review by its analyst's signature, with one of the two signatures
    approving it needs."""
    ready(world)
    roles(world, editor=(ANALYST,), admin=(REVIEWER,), viewer=(QA,))
    made = lot(world, **values)
    signed(world, "editor", made["id"], "in_review", reason="Ready")
    signed(world, "admin", made["id"], "approved", reason="Data checked")
    return [Entity.objects.get(pk=made["id"])]


def verify(path: Path, *args: str) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    parsed = argparse.Namespace(bundle=path, trust=[], key=[], json=False)
    for flag, value in zip(args[::2], args[1::2], strict=True):
        getattr(parsed, flag.removeprefix("--")).append(value)
    code = cli.run_verify(parsed, out, err)
    return code, out.getvalue() + err.getvalue()


def test_a_bundle_carries_its_records_signatures(world: World, tmp_path: Path) -> None:
    path = bundle_of(world, tmp_path, lambda: signed_lot(world))
    bundle = reading.read(path, TRUSTED, limit=1024**3)
    assert bundle.manifest["counts"]["signatures"] == 2
    [key] = bundle.manifest["signing_keys"]
    assert key["deployment"] == "test" and key["key"].startswith("ed25519:")
    lines = list(bundle.lines("signatures.ndjson"))
    assert [(line["state"], line["index"]) for line in lines] == [("in_review", 0), ("approved", 0)]
    [record] = bundle.lines("entities.ndjson")
    assert record["revisions"][1]["signatures"] == [lines[0]["id"]]
    found = offline.verify(bundle)
    assert [(v.status, v.valid) for v in found] == [("carried", True), ("pending", True)]
    assert found[0].manifestation.startswith("Authored by Editor on ")
    # The command says so, and exits 0.
    code, said = verify(path, "--trust", f"test={PUBLIC}")
    assert code == 0, said
    assert "2 signatures, all valid." in said
    # Unless it doesn't trust the bundle, or the keys that signed its signatures.
    code, said = verify(path)
    assert code == 1
    assert "you don't trust test's key" in said
    assert "key: It's signed by a key this deployment doesn't trust" in said
    code, said = verify(path, "--key", key["key"])
    assert code == 0, said
    assert "you don't trust test's key" in said  # only its signatures were checked


def test_a_bundle_with_no_signatures_checks_out_only_by_its_trusted_key(
    world: World, tmp_path: Path
) -> None:
    """An auditor's pipeline gates on the exit status and `valid`: a bundle signed by
    another key, under the same deployment's name, doesn't pass for being self-consistent,
    and one with nothing to check by the keys given doesn't either."""
    path = bundle_of(world, tmp_path, lambda: [protein(world, "MKTAYIAK", name="ABL1")])
    other = "ed25519:" + "A" * 43
    code, said = verify(path, "--trust", f"test={PUBLIC}")
    assert code == 0, said
    assert "It carries no electronic signatures." in said
    code, said = verify(path, "--trust", f"test={other}")
    assert code == 1
    assert f"It doesn't check out: it's signed by {PUBLIC}, which isn't a key you gave" in said
    code, said = verify(path)
    assert code == 1
    assert "nothing it holds is trusted: give test's key with --trust." in said
    # With only signing keys, there's nothing they check.
    code, said = verify(path, "--key", other)
    assert code == 1
    assert "only its signatures can be checked by the keys given with --key" in said

    def as_json(*args: str) -> tuple[int, dict[str, Any]]:
        out = io.StringIO()
        parsed = argparse.Namespace(bundle=path, trust=[], key=[], json=True)
        for flag, value in zip(args[::2], args[1::2], strict=True):
            getattr(parsed, flag.removeprefix("--")).append(value)
        code = cli.run_verify(parsed, out, io.StringIO())
        return code, json.loads(out.getvalue())

    code, body = as_json("--trust", f"test={other}")
    assert (code, body["valid"], body["bundle"]["trusted"]) == (1, False, False)
    assert body["checked"] == "bundle" and "isn't a key you gave" in body["why"]
    code, body = as_json("--trust", f"test={PUBLIC}")
    assert (code, body["valid"], body["checked"]) == (0, True, "bundle")
    assert "why" not in body


def test_offline_verification_finds_changed_history(world: World, tmp_path: Path) -> None:
    path = bundle_of(world, tmp_path, lambda: signed_lot(world))
    bundle = reading.read(path, TRUSTED, limit=1024**3)
    [record] = bundle.lines("entities.ndjson")
    line = next(bundle.lines("signatures.ndjson"))
    keys = {k["key"]: k["deployment"] for k in bundle.manifest["signing_keys"]}
    for h in record["history"]:
        h["values"]["label"] = "LOT Z"
    found = offline._one(line, keys, record, {})
    assert not found.valid
    assert [c.name for c in found.checks if not c.ok] == ["record"]
    # Or a signature moved onto another revision, or with its content swapped.
    moved = {**line, "revision": 2}
    assert "row" in [c.name for c in offline._one(moved, keys, record, {}).checks if not c.ok]
    swapped = {**line, "content": line["content"].replace("LOT A", "LOT Z")}
    assert "content" in [c.name for c in offline._one(swapped, keys, None, {}).checks if not c.ok]


def vouching(line: dict[str, Any], changes: list[dict[str, Any]]) -> dict[str, Any]:
    """``line`` with ``since``: ``changes``, vouched for by this deployment's signing key,
    as its export would have them."""
    return {**line, "since": signature_services.read_through(line, changes)}


def test_offline_verification_reads_history_through_the_migrations_since(
    world: World, tmp_path: Path
) -> None:
    """ADR 0148: a bundle's history is its records' as kept when it was exported, and each
    signature says what was done to it since it was signed, vouched for by the exporting
    deployment's signing key."""
    path = bundle_of(world, tmp_path, lambda: signed_lot(world))
    bundle = reading.read(path, TRUSTED, limit=1024**3)
    [record] = bundle.lines("entities.ndjson")
    line = next(bundle.lines("signatures.ndjson"))
    assert line["since"] is None
    keys = {k["key"]: k["deployment"] for k in bundle.manifest["signing_keys"]}
    # As a migration renaming `label` to `title` leaves it.
    for h in record["history"]:
        h["values"]["title"] = h["values"].pop("label")
    renamed = {"kind": "rename", "plugin": "acme/protein", "at": "2026-11-18"}
    renamed |= {"from": "label", "to": "title"}
    assert [c.name for c in offline._one(line, keys, record, {}).checks if not c.ok] == ["record"]
    vouched = vouching(line, [renamed])
    keys[vouched["since"]["key"]] = "test"
    found = offline._one(vouched, keys, record, {})
    assert found.valid, found.checks
    [check] = [c for c in found.checks if c.name == "record"]
    assert check.detail.endswith("read through what was done to it since: `label` renamed `title`.")
    # Changed after it: caught.
    for h in record["history"]:
        h["values"]["title"] = "LOT Z"
    found = offline._one(vouched, keys, record, {})
    assert [c.name for c in found.checks if not c.ok] == ["record"]
    # A conversion of what was signed with no unit doesn't follow from it.
    converted: dict[str, Any] = {"kind": "convert", "plugin": "acme/protein", "at": "2026-11-18"}
    converted |= {"field": "label", "from_unit": "g", "to_unit": "mg", "scale": 1e3, "offset": 0.0}
    found = offline._one(vouching(line, [converted]), keys, record, {})
    [check] = [c for c in found.checks if not c.ok]
    assert "`label` was signed in no unit, but a migration converted it from g" in check.detail
    # An empty quantity converts to an empty one.
    empty = {"values": {"mass": None, "masses": [1.5, None]}, "units": {"mass": "g", "masses": "g"}}
    grams = {**converted, "from_unit": "g", "to_unit": "mg"}
    changes = [{**grams, "field": "mass"}, {**grams, "field": "masses"}]
    assert proof.migrated(empty, changes)["values"] == {"mass": None, "masses": [1500.0, None]}
    # How a conversion converted is said, so a reader sees an implausible factor.
    kelvin = {**converted, "field": "t", "from_unit": "Cel", "to_unit": "K", "scale": 1.0}
    assert proof.described([grams | {"field": "mass"}, kelvin | {"offset": 273.15}]) == (
        "`mass` converted from g to mg (× 1000), `t` converted from Cel to K (× 1 + 273.15)"
    )


def test_what_was_done_since_is_vouched_for_by_a_key_the_reader_trusts(
    world: World, tmp_path: Path
) -> None:
    """Changes that would explain a difference count only if a key the reader trusts, as it
    trusts the signature's, vouches for them, for this signature: written into the bundle
    by anyone else, they explain nothing."""
    path = bundle_of(world, tmp_path, lambda: signed_lot(world, yield_mg=5.0))
    bundle = reading.read(path, TRUSTED, limit=1024**3)
    [record] = bundle.lines("entities.ndjson")
    line = next(bundle.lines("signatures.ndjson"))
    signer = {k["key"]: k["deployment"] for k in bundle.manifest["signing_keys"]}
    # Its history, changed: its label and yield swapped, with renames said to explain it.
    for h in record["history"]:
        v = h["values"]
        v["label"], v["yield_mg"] = v["yield_mg"], v["label"]
    forged = [
        {"kind": "rename", "plugin": "acme/protein", "at": "2026-11-18"} | {"from": a, "to": b}
        for a, b in (("label", "spare"), ("yield_mg", "label"), ("spare", "yield_mg"))
    ]
    vouched = vouching(line, forged)

    def record_check(line: dict[str, Any], keys: dict[str, str]) -> proof.Check:
        return next(c for c in offline._one(line, keys, record, {}).checks if c.name == "record")

    # Signed by a key the reader doesn't trust: the signature itself still checks out, and
    # the record check fails, saying whose key vouched.
    check = record_check(vouched, signer)
    assert not check.ok
    assert f"signing key {vouched['since']['key']}, which isn't trusted" in check.detail
    assert all(c.ok for c in offline._one(vouched, signer, record, {}).checks if c != check)
    # Unsigned, as a list; signed for another signature; or its signature not its own.
    for given in (
        {**line, "since": forged},
        {**vouched, "id": "019a0000-0000-7000-8000-000000000000"},
        {**vouched, "since": {**vouched["since"], "signature": line["signature"]}},
    ):
        assert not record_check(given, {**signer, vouched["since"]["key"]: "test"}).ok
    # Vouched for by a key the reader trusts, the record check reads it through it, and
    # says what it did.
    check = record_check(vouched, {**signer, vouched["since"]["key"]: "test"})
    assert check.ok, check.detail
    assert "`label` renamed `spare`, `yield_mg` renamed `label`, `spare` renamed " in check.detail


def test_offline_verification_finds_signed_fields_the_bundles_history_lacks(
    world: World, tmp_path: Path
) -> None:
    """Every field the record signed is compared, so history taken out of a bundle can't
    pass for history that holds what was signed."""
    path = bundle_of(world, tmp_path, lambda: signed_lot(world, yield_mg=5.0))
    bundle = reading.read(path, TRUSTED, limit=1024**3)
    [record] = bundle.lines("entities.ndjson")
    line = next(bundle.lines("signatures.ndjson"))
    keys = {k["key"]: k["deployment"] for k in bundle.manifest["signing_keys"]}
    assert offline._one(line, keys, record, {}).valid
    one = {**record, "history": [{**h, "values": dict(h["values"])} for h in record["history"]]}
    for h in one["history"]:
        del h["values"]["yield_mg"]
    found = offline._one(line, keys, one, {})
    assert [(c.name, c.detail) for c in found.checks if not c.ok] == [
        ("record", "The bundle's history of revision 1 isn't what was signed: `yield_mg` changed.")
    ]
    found = offline._one(line, keys, {**record, "history": []}, {})
    [check] = [c for c in found.checks if not c.ok]
    assert check.name == "record" and "`label`" in check.detail and "`yield_mg`" in check.detail


def test_the_bundle_command_reports_json(world: World, tmp_path: Path) -> None:
    path = bundle_of(world, tmp_path, lambda: signed_lot(world))
    out = io.StringIO()
    parsed = argparse.Namespace(bundle=path, trust=[f"test={PUBLIC}"], key=[], json=True)
    assert cli.run_verify(parsed, out, io.StringIO()) == 0
    body = json.loads(out.getvalue())
    assert body["valid"] is True and body["bundle"]["trusted"] is True
    assert [s["status"] for s in body["signatures"]] == ["carried", "pending"]


def test_an_import_brings_signatures_that_still_verify(world: World, tmp_path: Path) -> None:
    path = bundle_of(world, tmp_path, lambda: signed_lot(world))
    assert not Signature.objects.exists() and not SigningKey.objects.exists()
    api = Api(world.browser("admin"))
    done = applied(api, upload(api, world.project, path))
    assert done["state"] == "applied", done
    (row,) = entities_of(api, done)
    entity = Entity.objects.get(hrid=row["hrid_here"])
    found = list(Signature.objects.filter(entity=entity).order_by("signed_at"))
    assert len(found) == 2
    assert {str(s.bundle_import_id) for s in found} == {done["id"]}
    assert Revision.objects.get(entity=entity, number=2).signatures == [found[0].pk]
    key = ForeignSigningKey.objects.get()
    assert (key.public, key.deployment) == (found[0].key, "test")
    shown = standing(world, str(entity.pk), "admin")
    assert [s["status"] for s in shown] == ["pending", "carried"]
    assert all(s["verification"]["valid"] for s in shown), shown
    assert shown[1]["signer"]["name"] == "Editor"


def test_an_imported_signature_verifies_naming_people_and_entries_as_they_are_here(
    world: World, tmp_path: Path
) -> None:
    """ADR 0096, ADR 0122: an import names each person a record names as they are here (by
    their email, or as a placeholder) and each registry entry as it's registered here, in
    its fields and in its rows' cells. A signature on it still verifies here, and in a
    bundle exported from here, which says how the import named them."""
    from iki_server.bundles import export
    from iki_server.org.models import Project, ProjectRole

    admin = world.users["admin"]

    def made() -> list[Any]:
        maker = User.objects.create_user("ada@lab-a.example", "Ada Lovelace")
        helper = User.objects.create_user("rosalind@lab-a.example", "Rosalind Franklin")
        for person in (maker, helper):
            world.project.memberships.create(user=person, role=ProjectRole.EDITOR)
        # Human serum albumin's signal peptide (UniProt P02768, residues 1-18).
        albumin = protein(world, "MKWVTFISLLFLFSSAYS", name="albumin signal peptide")
        inputs = [{"protein": str(albumin.pk), "amount_mg": 2.5, "by": str(helper.pk)}]
        return signed_lot(world, made_by=str(maker.pk), protein=str(albumin.pk), inputs=inputs)

    path = bundle_of(world, tmp_path, made)
    # Here, Ada has an account of her own, Rosalind none, and the peptide is registered
    # already, by another project.
    ada = User.objects.create_user("ada@lab-a.example", "Ada Lovelace")
    elsewhere = Project.objects.create(team=world.team, name="Elsewhere")
    elsewhere.memberships.create(user=admin, role=ProjectRole.ADMIN)
    registered = Entity.objects.get(
        pk=services.create(
            admin, "acme/protein.Protein", elsewhere, {"sequence": "MKWVTFISLLFLFSSAYS"}
        ).entity.pk
    )
    api = Api(world.browser("admin"))
    done = applied(api, upload(api, world.project, path))
    assert done["state"] == "applied", done
    here = next(e for e in entities_of(api, done) if e["type"] == "acme/protein.Lot")
    entity = Entity.objects.get(hrid=here["hrid_here"])
    values = api.get(f"/entities/{entity.pk}").json()["values"]
    rosalind = User.objects.get(email="rosalind@lab-a.example")
    assert values["made_by"] == str(ada.pk)
    assert values["protein"] == str(registered.entry_id)
    assert [(r["protein"], r["by"]) for r in values["inputs"]] == [
        (str(registered.entry_id), str(rosalind.pk))
    ]
    [shown] = [s for s in standing(world, str(entity.pk), "admin") if s["status"] == "carried"]
    assert shown["verification"]["valid"], shown["verification"]
    [check] = [c for c in shown["verification"]["checks"] if c["name"] == "record"]
    assert check["detail"].endswith(
        "read through what was done to it since: `inputs`, `made_by` and `protein` naming "
        "people and registry entries as test does."
    )

    # What it's read through is signed by this deployment's key: changed in the database,
    # by anything but Iki, it explains nothing.
    kept = Signature.objects.get(pk=shown["id"])
    other, _ = keys_module.generate()
    forged = proof.canonical(json.loads(kept.read_through["document"]))
    for read_through, why in (
        ({}, "`inputs`, `made_by` and `protein` changed."),
        (
            {
                **kept.read_through,
                "signature": proof.b64(
                    keys_module.sign(keys_module.private_key(other), forged.encode())
                ),
            },
            "what was done to its history before it came here isn’t signed by this "
            "deployment’s key, as an import’s is.",
        ),
    ):
        _tamper(kept, read_through)
        [check] = [
            c
            for c in signature_services.verify(Signature.objects.get(pk=kept.pk))
            if c.name == "record"
        ]
        assert not check.ok and why in check.detail, check.detail
    _tamper(kept, kept.read_through)

    # Exported from here, it verifies offline, the bundle trusted or only the signing keys.
    out = tmp_path / "again.tar.gz"
    export.write(world.project, [entity], out, by=admin, reason="r", selection="s")
    code, said = verify(out, "--trust", f"test={PUBLIC}")
    assert code == 0, said
    assert "2 signatures, all valid." in said
    bundle = reading.read(out, TRUSTED, limit=1024**3)
    line = next(bundle.lines("signatures.ndjson"))
    assert line["since"] is not None
    keys = [arg for k in bundle.manifest["signing_keys"] for arg in ("--key", k["key"])]
    code, said = verify(out, *keys)
    assert code == 0, said
    # The signer's key alone doesn't vouch for how this import named them.
    code, said = verify(out, "--key", line["key"])
    assert code == 1
    assert f"test's signing key {line['since']['key']}, which isn't trusted." in said


def _tamper(signature: Signature, read_through: dict[str, Any]) -> None:
    """As a database superuser past the append-only triggers: what nothing in Iki can do."""
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SET session_replication_role = replica")
        cursor.execute(
            "UPDATE iki_signature SET read_through = %s WHERE id = %s",
            [json.dumps(read_through), signature.pk],
        )
        cursor.execute("SET session_replication_role = DEFAULT")
