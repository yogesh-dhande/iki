"""A plugin's export format (ADR 0230): the liquid_handling example's transfers, downloaded
as a Tecan worklist that its writer, a WebAssembly module, writes in the export sandbox.

The worklist is what Freedom EVOware and FluentControl read: an aspirate (``A;``), a
dispense (``D;``) and a wash (``W;``) per transfer, CRLF line ends, each well by its
position on its plate counted down the columns (B3 of a 384-well plate is 34). It's written
as Iki's own formats are: from each record's values at the revision asked for, the plates
as they were then, its download audited with the file's SHA-256 and the writer's plugin,
commit and module digest, and what it couldn't hold listed. The tests run the writer as a
development server does (``IKI_EXPORT_SANDBOX=local``), each export a fresh child process,
and through the sandbox's launcher over TCP, as a deployment does."""

from __future__ import annotations

import hashlib
import io
import threading
import zipfile
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests
import writer_modules as wat
from api_client import Api
from django.test import override_settings
from org_world import World, build_world

from iki_server.audit.models import AuditEvent
from iki_server.entities import plugin_writers, record_exports, services
from iki_server.org.models import Project, ProjectMembership, ProjectRole
from iki_server.tasks.backend import Retry
from iki_spec import writer_sandbox

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "examples" / "plugins" / "liquid_handling" / "writers" / "tecan-worklist.wasm"
WRITER = "acme/liquid_handling.tecan-worklist"
PLATE = "acme/liquid_handling.Plate"
TRANSFER = "acme/liquid_handling.Transfer"

if not MODULE.is_file():  # pragma: no cover - make test-wasm builds it
    pytest.skip("make test-wasm builds the Tecan writer's module", allow_module_level=True)


@pytest.fixture(autouse=True)
def local_sandbox() -> Iterator[None]:
    with override_settings(IKI_EXPORT_SANDBOX="local", IKI_ALLOW_LOCAL_PLUGINS=True):
        yield


@pytest.fixture
def world(s3: Any) -> World:
    return build_world()


def api_as(world: World, principal: str = "viewer") -> Api:
    return Api(world.browser(principal))


def plate(world: World, label: str, wells: str = "plate_96", **values: Any) -> Any:
    return services.create(
        world.users["editor"], PLATE, world.project, {"label": label, "wells": wells, **values}
    ).entity


def transfer(world: World, source: Any, destination: Any, **values: Any) -> Any:
    given = {
        "source": str(source.pk),
        "source_well": "A1",
        "destination": str(destination.pk),
        "destination_well": "A1",
        "volume": 5.0,
        **values,
    }
    return services.create(world.users["editor"], TRANSFER, world.project, given).entity


def export(api: Api, record: Any, **params: Any) -> Any:
    query = "&".join(f"{k}={v}" for k, v in {"format": WRITER, **params}.items())
    return api.get(f"/entities/{record.hrid}/export?{query}")


def downloaded(api: Api, made: dict[str, Any]) -> bytes:
    response = api.get(f"/entity-exports/{made['id']}/content")
    assert response.status_code == 302, response.content
    fetched = requests.get(response["Location"], timeout=30)
    assert fetched.status_code == 200
    return fetched.content


def lines(*transfers: tuple[str, ...]) -> bytes:
    """A worklist, as EVOware reads one: each transfer's A, D and W lines."""
    out = []
    for (
        source,
        barcode,
        labware,
        at,
        destination,
        its_barcode,
        its_labware,
        to,
        volume,
        lc,
    ) in transfers:
        out += [
            f"A;{source};{barcode};{labware};{at};;{volume};{lc};;;",
            f"D;{destination};{its_barcode};{its_labware};{to};;{volume};{lc};;;",
            "W;",
        ]
    return "".join(f"{line}\r\n" for line in out).encode()


def test_the_type_offers_its_plugins_format(world: World) -> None:
    found = api_as(world).get(f"/entity-types/{TRANSFER}").json()
    (offered,) = found["exports"]
    assert offered["export"] == "plugin"
    assert offered["writer"] == WRITER
    assert offered["fields"]["volume"] == "volume"
    assert offered["formats"] == [
        {
            "format": WRITER,
            "label": "Tecan worklist",
            "extension": "gwl",
            "media_type": "text/x-tecan-gwl",
            "description": offered["formats"][0]["description"],
            "many": "file",
            "plugin": "acme/liquid_handling",
        }
    ]


def test_a_transfer_downloads_as_a_worklist_pinned_and_audited(world: World) -> None:
    source = plate(
        world, "Compound source 1", "plate_384", barcode="CP-0412", labware_type="384 Well Greiner"
    )
    assay = plate(world, "Assay plate 7", barcode="AP-0007", labware_type="96 Well Corning Flat")
    moved = transfer(
        world,
        source,
        assay,
        source_well="B3",
        destination_well="B3",
        volume=2.5,
        liquid_class="DMSO Free Single",
    )
    api = api_as(world)
    response = export(api, moved)
    assert response.status_code == 200, response.content
    assert response["Content-Type"] == "text/x-tecan-gwl"
    assert response["Content-Disposition"] == f'attachment; filename="{moved.hrid}.gwl"' or (
        response["Content-Disposition"].endswith('.gwl"')
    )
    assert response["Iki-Left-Out"] == "0"
    first = lines(
        (
            "Compound source 1",
            "CP-0412",
            "384 Well Greiner",
            "34",
            "Assay plate 7",
            "AP-0007",
            "96 Well Corning Flat",
            "18",
            "2.5",
            "DMSO Free Single",
        ),
    )
    assert response.content == first

    event = AuditEvent.objects.get(action="entity.exported")
    assert event.actor == world.users["viewer"]
    assert event.target_id == str(moved.pk)
    assert event.data["format"] == WRITER
    assert event.data["sha256"] == hashlib.sha256(first).hexdigest()
    assert event.data["writer"] == {
        "plugin": "acme/liquid_handling",
        "writer": WRITER,
        "commit": "local",
        "module_sha256": hashlib.sha256(MODULE.read_bytes()).hexdigest(),
        "abi": "1",
    }

    # Changed since: the first revision is still the same file, its plate as it was then.
    services.update(world.users["editor"], moved, {"volume": 4.0}, expected_revision=1)
    services.update(world.users["editor"], source, {"label": "Renamed"}, expected_revision=1)
    assert export(api, moved, revision=1).content == first
    latest = export(api, moved).content
    assert latest.startswith(b"A;Renamed;CP-0412;384 Well Greiner;34;;4;")


def test_a_volume_in_another_unit_is_given_in_the_roles(world: World) -> None:
    source, assay = plate(world, "Src"), plate(world, "Dst")
    moved = transfer(world, source, assay, volume={"value": 0.0025, "unit": "mL"})
    assert b";;2.5;" in export(api_as(world), moved).content


def test_what_the_worklist_cant_hold_is_noted(world: World) -> None:
    source, assay = plate(world, "Src;1"), plate(world, "Dst")
    moved = transfer(world, source, assay, volume=1.2345)
    response = export(api_as(world), moved)
    assert response.status_code == 200, response.content
    assert response.content.startswith(b"A;Src_1;;;1;;1.23;")
    assert response["Iki-Left-Out"] == "2"
    left_out = AuditEvent.objects.get(action="entity.exported").data["left_out"]
    assert any("1.23" in note for note in left_out)


def test_a_transfer_the_writer_refuses_is_422_and_not_audited(world: World) -> None:
    source, assay = plate(world, "Src"), plate(world, "Dst")
    moved = transfer(world, source, assay, source_well="Q1")
    response = export(api_as(world), moved)
    assert response.status_code == 422, response.content
    assert response.json()["code"] == "writer_refused"
    assert "Q1 isn't a well of a 96-well plate" in response.json()["detail"]
    assert not AuditEvent.objects.filter(action="entity.exported").exists()


def test_a_plate_its_reader_cant_read_is_withheld_from_the_writer(world: World) -> None:
    store = Project.objects.create(team=world.team, name="Compound store")
    ProjectMembership.objects.create(
        project=store, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    hidden = services.create(
        world.users["editor"], PLATE, store, {"label": "Library 3", "wells": "plate_384"}
    ).entity
    moved = transfer(world, hidden, plate(world, "Dst"))
    response = export(api_as(world), moved)
    assert response.status_code == 422, response.content
    assert "isn't a record you can read" in response.json()["detail"]
    # Its editor can read both, so they get the worklist.
    assert export(api_as(world, "editor"), moved).content.startswith(b"A;Library 3;")


def test_only_those_who_can_read_a_transfer_export_it(world: World) -> None:
    moved = transfer(world, plate(world, "Src"), plate(world, "Dst"))
    assert export(api_as(world, "outsider"), moved).status_code == 404


def test_many_transfers_are_one_worklist_in_order(world: World) -> None:
    source, assay = plate(world, "Src", "plate_384"), plate(world, "Dst")
    first = transfer(world, source, assay, source_well="A1", destination_well="A1")
    second = transfer(world, source, assay, source_well="P24", destination_well="H12")
    api = api_as(world)
    response = api.post(
        "/entity-exports", {"format": WRITER, "entities": [second.hrid, first.hrid]}
    )
    assert response.status_code == 201, response.content
    made = response.json()
    assert made["state"] == "ready", made
    assert made["file_name"] == "Kinase Atlas Transfers (2).gwl"
    assert made["writer"]["module_sha256"] == hashlib.sha256(MODULE.read_bytes()).hexdigest()
    content = downloaded(api, made)
    assert content == lines(
        ("Src", "", "", "384", "Dst", "", "", "96", "5", ""),
        ("Src", "", "", "1", "Dst", "", "", "1", "5", ""),
    )
    assert hashlib.sha256(content).hexdigest() == made["sha256"]
    taken = AuditEvent.objects.get(action="entities.exported")
    assert taken.data["writer"] == made["writer"]
    assert taken.data["format"] == WRITER


def test_a_refused_transfer_fails_the_whole_worklist_naming_it(world: World) -> None:
    source, assay = plate(world, "Src"), plate(world, "Dst")
    good = transfer(world, source, assay)
    bad = transfer(world, source, assay, destination_well="I1")
    made = (
        api_as(world)
        .post("/entity-exports", {"format": WRITER, "entities": [good.hrid, bad.hrid]})
        .json()
    )
    assert made["state"] == "failed"
    assert list(made["refused"]) == [bad.hrid]
    assert made["sha256"] == ""
    assert not AuditEvent.objects.filter(action="entities.exported").exists()


def test_a_format_of_one_record_each_makes_a_zip_that_is_the_same_each_time(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = plugin_writers.find(WRITER)
    assert writer is not None
    monkeypatch.setattr(plugin_writers, "find", lambda _: replace(writer, many="zip"))
    source, assay = plate(world, "Src"), plate(world, "Dst")
    picked = [transfer(world, source, assay, source_well=w) for w in ("A1", "B1")]
    api = api_as(world)
    body = {"format": WRITER, "entities": [t.hrid for t in picked]}
    made = api.post("/entity-exports", body).json()
    assert made["state"] == "ready", made
    assert made["file_name"].endswith("(2).zip")
    content = downloaded(api, made)
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        entries = archive.infolist()
        assert [e.compress_type for e in entries] == [zipfile.ZIP_STORED] * 2
        assert [archive.read(e).split(b";")[4] for e in entries] == [b"1", b"2"]
        assert all(e.filename.endswith(".gwl") for e in entries)
    again = api.post("/entity-exports", body).json()
    assert again["sha256"] == made["sha256"]


def test_a_writer_that_fails_fails_the_export_and_is_counted(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    broken = tmp_path / "spins.wasm"
    broken.write_bytes(wat.compiled(wat.SPINS))
    real = plugin_writers.runnable

    def spinning(writer: Any) -> plugin_writers.Runnable:
        return replace(
            real(writer), path=str(broken), sha256=hashlib.sha256(broken.read_bytes()).hexdigest()
        )

    monkeypatch.setattr(plugin_writers, "runnable", spinning)
    counted: list[dict[str, str]] = []
    monkeypatch.setattr(plugin_writers.failures, "add", lambda n, attrs: counted.append(attrs))
    moved = transfer(world, plate(world, "Src"), plate(world, "Dst"))
    response = export(api_as(world), moved)
    assert response.status_code == 422, response.content
    assert response.json()["code"] == "writer_failed"
    assert "ran out of fuel" in response.json()["detail"]
    made = (
        api_as(world).post("/entity-exports", {"format": WRITER, "entities": [moved.hrid]}).json()
    )
    assert made["state"] == "failed"
    assert "ran out of fuel" in made["error"]
    assert [c["code"] for c in counted] == ["out_of_fuel", "out_of_fuel"]
    assert not AuditEvent.objects.filter(
        action__in=["entity.exported", "entities.exported"]
    ).exists()


def test_without_a_sandbox_a_plugins_format_is_503(world: World) -> None:
    moved = transfer(world, plate(world, "Src"), plate(world, "Dst"))
    with override_settings(IKI_EXPORT_SANDBOX=""):
        response = export(api_as(world), moved)
    assert response.status_code == 503
    assert response.json()["code"] == "sandbox_unavailable"
    assert response["Retry-After"] == "5"


@pytest.fixture
def launcher(monkeypatch: pytest.MonkeyPatch) -> Iterator[writer_sandbox.Sandbox]:
    """The sandbox's launcher on a port here, running the module this checkout built, as a
    deployment's does the image's."""
    box = writer_sandbox.Sandbox(("127.0.0.1", 0), modules=None, concurrency=1)
    threading.Thread(target=box.serve_forever, daemon=True).start()
    real = plugin_writers.runnable

    def built(writer: Any) -> plugin_writers.Runnable:
        with override_settings(IKI_EXPORT_SANDBOX="local"):
            return real(writer)

    monkeypatch.setattr(plugin_writers, "runnable", built)
    host, port = box.server_address[:2]
    assert isinstance(host, str)
    with override_settings(IKI_EXPORT_SANDBOX=f"tcp://{host}:{port}"):
        yield box
    box.shutdown()
    box.server_close()


def test_a_deployment_writes_through_the_sandboxs_launcher(
    world: World, launcher: writer_sandbox.Sandbox
) -> None:
    moved = transfer(world, plate(world, "Src"), plate(world, "Dst"))
    response = export(api_as(world), moved)
    assert response.status_code == 200, response.content
    assert response.content.startswith(b"A;Src;;;1;;5;")


def test_a_busy_sandbox_is_503_and_a_background_export_tries_again(
    world: World, launcher: writer_sandbox.Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    moved = transfer(world, plate(world, "Src"), plate(world, "Dst"))
    assert launcher.slots.acquire(blocking=False)  # another export holds its one slot
    response = export(api_as(world), moved)
    assert response.status_code == 503, response.content
    assert "busy" in response.json()["detail"]
    assert response["Retry-After"] == "5"
    monkeypatch.setattr(record_exports, "INLINE", 0)
    made = (
        api_as(world).post("/entity-exports", {"format": WRITER, "entities": [moved.hrid]}).json()
    )
    assert made["state"] == "queued"
    write = record_exports.write_record_export.func
    # Busy: it waits its turn again, a few times.
    with pytest.raises(Retry):
        write(SimpleNamespace(attempt=1), made["id"])
    assert api_as(world).get(f"/entity-exports/{made['id']}").json()["state"] == "queued"
    launcher.slots.release()
    assert write(SimpleNamespace(attempt=2), made["id"]) == "ready"
    content = downloaded(api_as(world), made)
    assert content.startswith(b"A;Src;;;1;;5;")


def test_a_background_export_fails_when_the_sandbox_stays_busy(
    world: World, launcher: writer_sandbox.Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    moved = transfer(world, plate(world, "Src"), plate(world, "Dst"))
    assert launcher.slots.acquire(blocking=False)
    monkeypatch.setattr(record_exports, "INLINE", 0)
    made = (
        api_as(world).post("/entity-exports", {"format": WRITER, "entities": [moved.hrid]}).json()
    )
    last = SimpleNamespace(attempt=record_exports.ATTEMPTS)
    assert record_exports.write_record_export.func(last, made["id"]) == "failed"
    failed = api_as(world).get(f"/entity-exports/{made['id']}").json()
    assert "busy" in failed["error"] and failed["error"].endswith("export again.")
    launcher.slots.release()
