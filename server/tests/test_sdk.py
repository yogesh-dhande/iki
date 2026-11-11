"""The Python SDK (ikisci) against the real server, as someone's notebook uses it: signing
in with a code entered in the browser, finding and changing entities, running an action and
waiting for it, downloading a file, and renewing and ending the sign-in."""

import hashlib
import re
from collections.abc import Callable, Iterator
from datetime import timedelta
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest
from django.apps import apps as django_apps
from django.utils import timezone
from oauth2_provider.models import get_access_token_model, get_refresh_token_model
from org_world import World, post
from test_files import api_as as browser_api
from test_files import send
from test_files import start as start_upload
from test_run_results import done, output, weight
from test_worker_e2e import eventually, server, world
from test_workers import worker

import iki
from iki import auth
from iki_server.entities.models import Revision

pytestmark = pytest.mark.django_db(transaction=True)

__all__ = ["server", "world"]  # the fixtures, used here

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


def signed_in(world: World, server: Any, sdk: list[str], monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(iki, "_say", approving(world, sdk))
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
        "protein.Protein",
        {"name": "ABL1", "sequence": "MKVLEICLK"},
        project=project,
        reason="From the paper's table 2",
    )
    assert made["hrid"].startswith("PRT-")
    found = iki.entities.search("protein.Protein", project=project, q="ABL1")
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


def test_a_notebook_runs_an_action_and_waits_for_it(
    world: World, server: Any, sdk: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    signed_in(world, server, sdk, monkeypatch)
    project = world.project.slug
    abl = iki.entities.create("protein.Protein", {"sequence": "MKVLEICLK"}, project=project)
    run = iki.run(
        "protein.compute_properties",
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
