"""The Python SDK (ikisci) against the real server, as someone's notebook uses it: signing
in with a code entered in the browser, finding and changing entities, running an action and
waiting for it, downloading a file, and renewing and ending the sign-in."""

import hashlib
import re
from collections.abc import Callable, Iterator
from datetime import date, timedelta
from importlib import import_module
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx2
import pytest
from django.apps import apps as django_apps
from django.utils import timezone
from oauth2_provider.models import get_access_token_model, get_refresh_token_model
from org_world import World, post
from real_server import Server, free_port, serve
from test_files import api_as as browser_api
from test_files import send
from test_files import start as start_upload
from test_project_files import add, other, protein, sample, structure
from test_registrations import CSV, LAB, PLAIN, lab, put
from test_run_results import done, output, weight
from test_worker_e2e import eventually, server, world
from test_workers import worker

import iki
from iki import auth
from iki.schema import FileOut
from iki_server.entities.models import Revision
from iki_server.files.models import Upload
from iki_server.files.storage import DEFAULT_PART_SIZE
from iki_server.org.models import Project

pytestmark = pytest.mark.django_db(transaction=True)

__all__ = ["lab", "other", "server", "world"]  # the fixtures, used here

PDB = b"ATOM      1  N   MET A   1      27.340  24.430   2.614  1.00  9.67           N\n"


def checksum(token: str) -> str:
    """How Iki finds a token: it keeps only this, never the token."""
    return hashlib.sha256(token.encode()).hexdigest()


@pytest.fixture
def sdk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """The SDK, kept away from this machine's keychain and settings. Yields what it says."""
    # Its app, which a migration adds and each test's emptying of the database takes away.
    import_module("iki_server.oauth.migrations.0002_iki_cli").create(django_apps, None)
    monkeypatch.setenv("IKI_CONFIG_DIR", str(tmp_path / "iki"))
    for name in ("IKI_URL", "IKI_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(auth, "_keyring", lambda: None)
    said: list[str] = []
    monkeypatch.setattr(iki, "_say", said.append)
    yield said
    if iki._default is not None:
        iki._default.close()
        iki._default = None


def approving(world: World, said: list[str], principal: str = "editor") -> Callable[[str], None]:
    """Says as the SDK does, and when it shows a code, enters it in Iki and allows it all,
    as the person would in their browser."""

    def say(text: str) -> None:
        said.append(text)
        code = re.search(r"enter the code (\S+)", text)
        if code is None:
            return
        browser = world.browser(principal)
        entered = post(browser, "/oauth/device/", {"user_code": code[1]})
        assert entered.status_code == 302, entered.content
        allowed = post(
            browser, entered["Location"], {"granted": ["read", "write", "run"], "access": "all"}
        )
        assert allowed.status_code == 200, allowed.content

    return say


def signed_in(
    world: World,
    server: Server,
    sdk: list[str],
    monkeypatch: pytest.MonkeyPatch,
    principal: str = "editor",
) -> iki.Client:
    monkeypatch.setattr(iki, "_say", approving(world, sdk, principal))
    return iki.login(server.url, open_browser=False)


def test_a_notebook_signs_in_and_works_with_entities(
    world: World, server: Any, sdk: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    signed_in(world, server, sdk, monkeypatch)
    assert sdk[0] == (
        f"To sign in to Iki, open {server.url}/oauth/device/ and enter the code "
        + sdk[0].rsplit(" ", 1)[1]
    )
    kept = Path(auth.config_dir()) / "credentials.json"
    assert sdk[-1].startswith(f"Signed in to {server.url} as ")
    assert sdk[-1].endswith(f"allowed: read write run. Kept in {kept}.")
    assert iki.me()["email"] == world.users["editor"].email

    project = world.project.slug
    made = iki.entities.create(
        "acme/protein.Protein",
        {"name": "ABL1", "sequence": "MKVLEICLK"},
        project=project,
        reason="From the paper's table 2",
    )
    assert made["hrid"].startswith("PRT-")
    found = iki.entities.search("acme/protein.Protein", project=project, q="ABL1")
    assert [e["hrid"] for e in found] == [made["hrid"]]
    table = found.to_pandas()
    assert list(table.index) == [made["hrid"]]
    assert table.loc[made["hrid"], "name"] == "ABL1"
    assert table.loc[made["hrid"], "project"] == project

    changed = iki.entities.update(
        made["hrid"], {"name": "ABL1 kinase"}, revision=made["revision"], reason="Full name"
    )
    assert changed["revision"] == made["revision"] + 1
    # A change to a revision someone has since changed is refused, and nothing changes.
    with pytest.raises(iki.Conflict) as refused:
        iki.entities.update(
            made["hrid"], {"name": "ABL"}, revision=made["revision"], reason="Shorter"
        )
    assert refused.value.status == 412
    assert iki.entities.get(made["hrid"])["values"]["name"] == "ABL1 kinase"
    (latest, *_) = iki.entities.history(made["hrid"])
    assert latest["reason"] == "Full name"

    # It's recorded as the person's, done through the API with the SDK.
    recorded = Revision.objects.get(entity_id=made["id"], number=2)
    assert (recorded.user, recorded.via, recorded.reason) == (
        world.users["editor"],
        "api",
        "Full name",
    )
    assert recorded.client == "Iki CLI and Python SDK (iki-cli)"

    with pytest.raises(iki.NotFound):
        iki.entities.get("PRT-999999")


def test_sdk_reports_reserved_vendor_codes_on_create_and_update(
    world: World, server: Any, sdk: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    signed_in(world, server, sdk, monkeypatch)
    project = world.project.slug
    type_name = "acme/protein.Tube"
    with pytest.raises(iki.ApiError, match="IKI:") as denied:
        iki.entities.create(type_name, {"barcode": "iKi:invalid"}, project=project)
    assert denied.value.status == 422
    tube = iki.entities.create(type_name, {"barcode": "NORMAL-41"}, project=project)
    with pytest.raises(iki.ApiError, match="IKI:") as denied:
        iki.entities.update(
            tube["id"],
            {"barcode": "IKI:" + "0" * 32},
            revision=tube["revision"],
            reason="Supplier label change",
        )
    assert denied.value.status == 422
    assert iki.entities.get(tube["id"])["values"]["barcode"] == "NORMAL-41"


def test_a_notebook_editor_needs_the_sampling_role_to_draw_a_qa_sample(
    world: World, server: Server, sdk: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = signed_in(world, server, sdk, monkeypatch)
    sample = client.entities.create(
        "acme/protein.Sample", {"label": "S-1"}, project=world.project.slug, amount=(10, "uL")
    )
    ref = sample["hrid"]
    with pytest.raises(iki.Forbidden) as refused:
        client.entities.draw(ref, (1, "uL"), purpose="qa_sampling")
    assert "qa" in str(refused.value).lower()
    assert client.entities.amount(ref)["value"] == "10"
    assert client.entities.draw(ref, (1, "uL"))["movement"]["purpose"] == "routine"


def test_a_notebook_runs_an_action_and_waits_for_it(
    world: World, server: Any, sdk: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    signed_in(world, server, sdk, monkeypatch)
    project = world.project.slug
    abl = iki.entities.create("acme/protein.Protein", {"sequence": "MKVLEICLK"}, project=project)
    run = iki.run(
        "acme/protein.compute_properties",
        project=project,
        inputs={"protein": abl["hrid"]},
        reason="Weights for the summary table",
    )
    assert (run["status"], run["reason"]) == ("queued", "Weights for the summary table")
    with pytest.raises(iki.TimedOut):
        iki.wait(run, timeout=0.5, poll=0.1)

    pod = worker(world)
    job = pod.lease().json()
    assert pod.report(job, "logs", offset=0, text="Weighed 1 of 1\n").status_code == 200
    done(pod, job, output(updates=weight(dict(run), 1234.5)))

    finished = iki.wait(run, poll=0.1)
    assert finished["status"] == "succeeded"
    assert finished["effects"][0]["hrid"] == abl["hrid"]
    assert iki.entities.get(abl["hrid"])["values"]["molecular_weight"] == 1234.5
    assert iki.runs.log(run) == "Weighed 1 of 1\n"
    assert [r["hrid"] for r in iki.runs.list(project=project)] == [run["hrid"]]


def test_a_notebook_downloads_a_file_without_sending_its_sign_in_elsewhere(
    world: World, server: Any, sdk: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    uploads = browser_api(world)
    started = start_upload(uploads, world, PDB, name="abl.pdb").json()
    send(started, data=PDB)
    uploads.post(f"/uploads/{started['id']}/complete")
    added = eventually(
        lambda: (u := uploads.get(f"/uploads/{started['id']}").json())["state"] == "ready" and u
    )
    signed_in(world, server, sdk, monkeypatch)
    saved = iki.files.download(added["file"]["id"], tmp_path)
    assert Path(saved) == tmp_path / "abl.pdb"
    assert hashlib.sha256(Path(saved).read_bytes()).hexdigest() == hashlib.sha256(PDB).hexdigest()


def test_a_notebook_finds_the_same_files_as_the_files_list_and_reads_their_uses(
    world: World, other: Project, server: Server, sdk: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = signed_in(world, server, sdk, monkeypatch)
    project = world.project
    trace = add(project, "Plate-A.pdb", size=95, acquired="2026-03-14")
    notes = add(project, "Plate-B.txt", "text/plain", size=30, acquired="2026-03-15")
    add(project, "instrument.json", "application/json", size=19)
    aurora = {
        "instrument": {"value": "Aurora", "stated_in": "FCS keyword $CYT"},
        "operator": {"value": "Admin", "stated_in": "FCS keyword $OP"},
    }
    tube = add(project, "Tube_001.fcs", "application/vnd.isac.fcs", size=40, facts=aurora)
    hidden = add(other, "Plate-Z.pdb", acquired="2026-03-14")
    holders = [structure(world, trace), structure(world, trace)]
    specimen = sample(world, "S-1")
    target = protein(world)
    runs = [
        client.runs.start(
            "acme/protein.flag_samples",
            project=project.slug,
            inputs={
                "protein": str(target.pk),
                "samples": [str(specimen.pk)],
                "structures": [str(holder.pk)],
            },
        )
        for holder in holders
    ]
    # A revision stops holding the file after both runs have recorded their inputs.
    replacement = add(project, "replacement.pdb")
    client.entities.update(
        holders[0].hrid, {"structure": str(replacement.pk)}, revision=1, reason="New acquisition"
    )

    browser = browser_api(world)
    filters: list[dict[str, Any]] = [
        {"q": "pLaTe"},
        {"format": ["chemical/*", "text/plain"]},
        {"format": ["iki/core.pdb"]},
        {"acquired_from": date(2026, 3, 15)},
        {"acquired_to": "2026-03-14"},
        {"sample": specimen.hrid},
        {"run": runs[0]["hrid"]},
        {"accepts": "text/*"},
        {"max_size": 31},
        {"q": "AURORA"},
    ]
    for query in filters:
        web = browser.get("/files", project=str(project.pk), sort="name_desc", **query).json()
        rows = list(client.files.list(project=project.slug, sort="name_desc", page_size=1, **query))
        assert rows == web["items"], query
        assert client.files.count(project=project.slug, **query) == {
            "total": len(rows),
            "more_than_total": False,
        }, query
    assert [row["id"] for row in client.files.list(q="plate", sort="name", page_size=1)] == [
        str(trace.pk),
        str(notes.pk),
    ]
    assert client.files.count(q="plate")["total"] == 2
    with pytest.raises(iki.NotFound):
        list(client.files.list(project=str(other.pk)))
    with pytest.raises(iki.NotFound):
        client.files.count(project=str(other.pk))
    with pytest.raises(iki.NotFound) as denied:
        client.files.get(str(hidden.pk))
    assert denied.value.status == 404

    # What a file states (ADR 0224) reads as the browser reads it.
    stating = client.files.get(str(tube.pk))
    assert stating["facts"] == browser.get(f"/files/{tube.pk}").json()["facts"]
    assert stating["facts"]["operator"] == aurora["operator"]
    assert stating["facts"]["serial"] is None and stating["read_groups"] == []

    first = client.files.get(str(trace.pk), page_size=1)
    assert first["used_by"] == {"records": 2, "runs": 2}
    next_records, next_runs = first["records"]["next_cursor"], first["runs"]["next_cursor"]
    assert next_records and next_runs
    second = client.files.get(
        str(trace.pk), records_cursor=next_records, runs_cursor=next_runs, page_size=1
    )
    assert second["records"]["next_cursor"] is None and second["runs"]["next_cursor"] is None
    records = first["records"]["items"] + second["records"]["items"]
    assert [(r["hrid"], r["spans"]) for r in records] == [
        (holders[0].hrid, [{"first": 1, "last": 1, "fields": ["structure"]}]),
        (holders[1].hrid, [{"first": 1, "last": None, "fields": ["structure"]}]),
    ]
    using_runs = first["runs"]["items"] + second["runs"]["items"]
    assert [(r["hrid"], r["roles"]) for r in using_runs] == [
        (run["hrid"], ["input"]) for run in reversed(runs)
    ]


def test_a_notebook_registers_only_an_approved_versioned_object(
    world: World,
    server: Server,
    sdk: list[str],
    monkeypatch: pytest.MonkeyPatch,
    lab: dict[str, Any],
) -> None:
    port = str(free_port())
    env = {
        **server.env,
        "IKI_PORT": port,
        "IKI_PUBLIC_URL": f"http://127.0.0.1:{port}",
        "IKI_FILES_REGISTER_LOCATIONS": f"s3://{LAB}/runs/,s3://{PLAIN}/",
    }
    uri, version = put(lab, "plate.csv", CSV)
    put(lab, "plate.csv", b"newer bytes\n")
    unversioned, _ = put(lab, "plate.csv", CSV, bucket=PLAIN)
    with serve(env) as registering_server:
        client = signed_in(world, registering_server, sdk, monkeypatch)
        project = world.project.slug
        with pytest.raises(iki.Invalid, match="approved"):
            client.files.register(uri.replace("/runs/", "/elsewhere/"), project=project)
        with pytest.raises(iki.Invalid, match="doesn't keep versions"):
            client.files.register(unversioned, project=project)
        file = client.files.register(
            uri,
            project=project,
            version=version,
            name="Plate 7.csv",
            media_type="text/csv",
            acquired=date(2026, 3, 14),
            poll=0.1,
            timeout=30,
        )
        assert file["external"] and file["name"] == "Plate 7.csv"
        assert (file["sha256"], file["size"]) == (hashlib.sha256(CSV).hexdigest(), len(CSV))
        assert file["acquired"] is not None and file["acquired"]["on"] == "2026-03-14"
        detail = client.files.get(file["id"])
        assert detail["origin"] == {
            "kind": "registration",
            "uri": uri,
            "version": version,
            "run": None,
        }


def test_a_notebook_uploads_a_path_for_an_entity_file_field(
    world: World, server: Server, sdk: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    signed_in(world, server, sdk, monkeypatch)
    source = tmp_path / "abl.pdb"
    with source.open("wb") as out:
        # Two parts, with a short final part. Keep the fixture small on disk.
        out.seek(DEFAULT_PART_SIZE)
        out.write(PDB)
    with source.open("rb") as stream:
        expected = hashlib.file_digest(stream, "sha256").hexdigest()
    added: FileOut = iki.files.upload(
        source, project=world.project.slug, media_type="chemical/x-pdb", poll=0.1, timeout=90
    )
    assert (added["name"], added["size"], added["sha256"]) == (
        source.name,
        DEFAULT_PART_SIZE + len(PDB),
        expected,
    )
    structure = iki.entities.create(
        "acme/protein.ProteinStructure", {"structure": added["id"]}, project=world.project.slug
    )
    assert structure["values"]["structure"] == added["id"]


def test_a_notebook_uploads_a_stream_for_an_import(
    world: World, server: Server, sdk: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = signed_in(world, server, sdk, monkeypatch)
    fasta = b">example\nMKVLEICLK\n"
    header = b"instrument export\n"
    with BytesIO(header + fasta) as stream:
        stream.seek(len(header))
        added = client.files.upload(
            stream, project=str(world.project.pk), name="proteins.fasta", media_type="text/plain"
        )
        assert stream.tell() == len(header)
    assert added["sha256"] == hashlib.sha256(fasta).hexdigest()
    run = client.runs.start(
        "acme/protein.import_proteins", project=world.project.slug, params={"file": added["id"]}
    )
    assert run["files"][added["id"]] == {
        "name": "proteins.fasta",
        "media_type": "text/plain",
        "size": len(fasta),
    }


def test_a_notebook_reports_the_servers_check_failure(
    world: World, server: Server, sdk: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    class DamagedPart(httpx2.HTTPTransport):
        def handle_request(self, request: httpx2.Request) -> httpx2.Response:
            if request.method == "PUT":
                # Same length, different bytes. Completion succeeds, the stored checksum fails.
                request = httpx2.Request("PUT", request.url, content=b"x" * len(PDB))
            return super().handle_request(request)

    signed_in(world, server, sdk, monkeypatch)
    with (
        iki.Client(server.url, transport=DamagedPart()) as client,
        pytest.raises(iki.IkiError, match="don't match the SHA-256"),
    ):
        client.files.upload(BytesIO(PDB), name="abl.pdb", project=world.project.slug, timeout=30)
    upload = Upload.objects.get()
    assert upload.state == "failed" and upload.file_id is None


def test_a_viewer_is_told_they_cannot_upload_to_the_project(
    world: World, server: Server, sdk: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = signed_in(world, server, sdk, monkeypatch, "viewer")
    with pytest.raises(iki.Forbidden, match="project") as raised:
        client.files.upload(BytesIO(PDB), name="abl.pdb", project=world.project.slug)
    assert (raised.value.status, raised.value.code) == (403, "forbidden")
    assert not Upload.objects.exists()


def test_a_sign_in_is_renewed_and_ended(
    world: World, server: Any, sdk: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    signed_in(world, server, sdk, monkeypatch)
    first = auth.Store().load(server.url)
    assert first is not None
    # The access token expires at Iki, before the SDK thinks it will.
    expired = get_access_token_model().objects.filter(token_checksum=checksum(first.access_token))
    assert expired.update(expires=timezone.now() - timedelta(seconds=1)) == 1
    assert iki.me()["email"] == world.users["editor"].email
    renewed = auth.Store().load(server.url)
    assert renewed is not None
    assert renewed.access_token != first.access_token
    assert renewed.refresh_token != first.refresh_token

    iki.logout()
    assert sdk[-1] == f"Signed out of {server.url}."
    assert auth.Store().load(server.url) is None
    tokens = get_access_token_model().objects
    assert not tokens.filter(token_checksum=checksum(renewed.access_token)).exists()
    refresh = get_refresh_token_model().objects.get(token_checksum=checksum(renewed.refresh_token))
    assert refresh.revoked is not None
    with pytest.raises(iki.NotSignedIn, match=r"iki\.login"):
        iki.me()
