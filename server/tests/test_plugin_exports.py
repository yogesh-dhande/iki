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
from structlog.testing import capture_logs

from iki_server.audit.models import AuditEvent
from iki_server.entities import exports, plugin_writers, record_exports, services
from iki_server.org.models import Project, ProjectMembership, ProjectRole
from iki_server.tasks.backend import Retry
from iki_spec import writer_sandbox
from iki_spec.model import PluginExport
from iki_spec.writers import abi as writer_abi
from iki_spec.writers import protocol
from iki_spec.writers.protocol import Answer

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "examples" / "plugins" / "liquid_handling" / "writers" / "tecan-worklist.wasm"
WRITER = "acme/liquid_handling.tecan_worklist"
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


def test_a_zip_written_after_an_edit_is_the_zip_of_the_revisions_asked_for(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same records, same revisions, same file (ADR 0229): a ZIP written in the background
    after one of its transfers was edited is the one written at once, its files named as
    they were, not as earlier revisions now are."""
    writer = plugin_writers.find(WRITER)
    assert writer is not None
    monkeypatch.setattr(plugin_writers, "find", lambda _: replace(writer, many="zip"))
    source, assay = plate(world, "Src"), plate(world, "Dst")
    picked = [transfer(world, source, assay, source_well=w) for w in ("A1", "B1")]
    api = api_as(world)
    body = {"format": WRITER, "entities": [t.hrid for t in picked]}
    at_once = api.post("/entity-exports", body).json()
    assert at_once["state"] == "ready", at_once
    monkeypatch.setattr(record_exports, "INLINE", 0)
    later = api.post("/entity-exports", body).json()
    assert later["state"] == "queued"
    services.update(world.users["editor"], picked[0], {"volume": 4.0}, expected_revision=1)
    assert record_exports.write_record_export.func(SimpleNamespace(attempt=1), later["id"]) == (
        "ready"
    )
    written = api.get(f"/entity-exports/{later['id']}").json()
    assert written["sha256"] == at_once["sha256"]
    assert downloaded(api, written) == downloaded(api, at_once)


def test_a_writer_is_given_the_same_request_for_a_revision_after_a_later_edit(
    world: World,
) -> None:
    """ABI 1's record request (ADR 0230) holds what the revision holds and which record and
    revision it is, never whether it's still the latest: a writer can't write the same
    revision as different bytes once the record changes."""
    moved = transfer(world, plate(world, "Src"), plate(world, "Dst"))
    match = exports.format_of(TRANSFER, WRITER)
    assert match is not None
    export_, chosen = match
    assert isinstance(export_, PluginExport)
    viewer = world.users["viewer"]
    before = exports.gather(viewer, moved, 1, export_, chosen)
    assert isinstance(before.record, plugin_writers.PluginRecord)
    request = writer_abi.encoder(1).record(before.record.gathered)
    assert set(request) == {
        "hrid",
        "id",
        "name",
        "revised_at",
        "revision",
        "roles",
        "type",
        "withheld",
    }
    services.update(world.users["editor"], moved, {"volume": 4.0}, expected_revision=1)
    moved.refresh_from_db()
    after = exports.gather(viewer, moved, 1, export_, chosen)
    assert isinstance(after.record, plugin_writers.PluginRecord)
    assert writer_abi.encoder(1).record(after.record.gathered) == request
    assert after.name == before.name


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


def test_a_writer_that_fails_partway_names_the_record_it_failed_on(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trap on the second of three records fails the export saying which record, and
    the log has its ID (ADR 0230)."""
    source, assay = plate(world, "Src"), plate(world, "Dst")
    picked = [transfer(world, source, assay, source_well=w) for w in ("A1", "B1", "C1")]
    real = protocol.Session.call
    calls: list[str] = []

    def trapping_on_the_second(session: protocol.Session, entry: str, request: bytes) -> Any:
        if entry == "record":
            calls.append(entry)
            if len(calls) == 2:
                raise protocol.SessionFailed("panicked", "The writer failed: it stopped.")
        return real(session, entry, request)

    monkeypatch.setattr(protocol.Session, "call", trapping_on_the_second)
    with capture_logs() as logged:
        made = (
            api_as(world)
            .post("/entity-exports", {"format": WRITER, "entities": [t.hrid for t in picked]})
            .json()
        )
    assert made["state"] == "failed", made
    assert f"failed on {picked[1].hrid}" in made["error"]
    assert picked[0].hrid not in made["error"] and picked[2].hrid not in made["error"]
    failed = [e for e in logged if e["event"] == "writer_failed"]
    assert [(e["record"], e["hrid"], e["code"]) for e in failed] == [
        (str(picked[1].pk), picked[1].hrid, "panicked")
    ]


def test_a_deployments_writer_limits_reach_the_sandbox(world: World) -> None:
    """ADR 0230: the deployment's limits go in each export's header, so its writer's file
    is held to them: here, an output limit lowered below the worklist's size."""
    moved = transfer(world, plate(world, "Src"), plate(world, "Dst"))
    with override_settings(IKI_EXPORT_LIMITS={"max_output": 8}):
        response = export(api_as(world), moved)
    assert response.status_code == 422, response.content
    assert response.json()["code"] == "writer_failed"
    assert "over the limit of 8 bytes" in response.json()["detail"]
    assert export(api_as(world), moved).status_code == 200


class _Sandbox:
    """A sandbox whose module reads ``abi``: it keeps each request it's sent."""

    def __init__(self, abi: int) -> None:
        self.abi = abi
        self.sent: list[tuple[str, bytes]] = []
        self.session = self

    def open(self, header: Any) -> int:
        return self.abi

    def call(self, entry: str, request: bytes) -> Answer:
        self.sent.append((entry, request))
        return Answer(0, b"", [], 0)

    def close(self) -> None: ...


def test_each_request_is_built_for_the_abi_its_module_reads(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a second ABI that gives records a field the first doesn't, a module of each
    gets its own ABI's requests, and a file says which ABI wrote it."""

    class Abi2(writer_abi.Abi1):
        abi = 2

        def record(self, record: writer_abi.Record) -> dict[str, Any]:
            return {**super().record(record), "project": "Kinase panel"}

    monkeypatch.setitem(writer_abi.ENCODERS, 2, Abi2())
    monkeypatch.setattr(writer_abi, "NEWEST", 2)
    moved = transfer(world, plate(world, "Src"), plate(world, "Dst"))
    writer = plugin_writers.find(WRITER)
    assert writer is not None
    found = exports.format_of(moved.type, WRITER)
    assert found is not None
    export_, chosen = found
    assert isinstance(export_, PluginExport)
    gathered = exports.gather(world.users["viewer"], moved, 1, export_, chosen).record
    assert isinstance(gathered, plugin_writers.PluginRecord)
    sent = {}
    for abi in (1, 2):
        sandbox = _Sandbox(abi)
        monkeypatch.setattr(plugin_writers, "_connect", lambda *_, sandbox=sandbox: sandbox)
        file = plugin_writers.PluginWriter(writer)
        file.begin(1)
        file.record(gathered)
        file.end()
        assert file.identity["abi"] == str(abi)
        sent[abi] = dict(sandbox.sent)
    assert sent[1]["begin"].startswith(b'{"abi":1,') and sent[2]["begin"].startswith(b'{"abi":2,')
    assert b'"project"' not in sent[1]["record"]
    assert sent[2]["record"].replace(b',"project":"Kinase panel"', b"") == sent[1]["record"]
    assert sent[1]["end"] == sent[2]["end"] == b"{}"

    # A module whose ABI this server doesn't run is the sandbox's to refuse; if it
    # doesn't, the server writes nothing.
    monkeypatch.setattr(plugin_writers, "_connect", lambda *_: _Sandbox(3))
    with pytest.raises(plugin_writers.SandboxUnavailable, match="newer than this Iki runs"):
        plugin_writers.PluginWriter(writer).begin(1)


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
