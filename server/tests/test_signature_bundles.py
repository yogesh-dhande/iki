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
from test_bundle_imports import PUBLIC, applied, bundle_of, entities_of, upload
from test_bundle_imports import world as world  # a fixture
from test_signatures import lot, ready, signed, standing
from test_states import ANALYST, QA, REVIEWER, roles

from iki_cli import bundle as cli
from iki_server.bundles import reading
from iki_server.entities.models import Entity, Revision
from iki_server.signatures import offline
from iki_server.signatures.models import ForeignSigningKey, Signature, SigningKey

pytestmark = pytest.mark.django_db

TRUSTED = {"test": (PUBLIC,)}


def signed_lot(world: World) -> list[Any]:
    """A lot set to In review by its analyst's signature, with one of the two signatures
    approving it needs."""
    ready(world)
    roles(world, editor=(ANALYST,), admin=(REVIEWER,), viewer=(QA,))
    made = lot(world)
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
