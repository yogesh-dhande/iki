"""The live Google Cloud test's inventory of the project, and what it counts as left or
taken away, checked offline: they need no project, so they run everywhere, CI included,
while test_gcp_live.py's checks don't. The project is shared with other teams, so the
inventory never takes a listing it couldn't make as empty, and tearing down may remove
nothing that was there before."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import test_gcp_live as live

SETTINGS = live.Settings(
    project="shared-lab",
    name="iki-test-11190800",
    region="us-central1",
    dr_region="us-east1",
    multi_region="us",
    zones=("us-central1-b", "us-central1-c"),
    data_access=(),
    token_command="true",
    workdir=Path("/nonexistent"),
)
APIS = "compute.googleapis.com\nstorage.googleapis.com\ncloudkms.googleapis.com\n"
DENIED = "ERROR: (gcloud.storage.buckets.list) [me@x] does not have storage.buckets.list access"


class FakeGcp:
    """gcloud in a project with some APIs on: each listing answers as `answers` says, by the
    start of its arguments (each answer in turn, the last for good), else with nothing."""

    def __init__(self, answers: dict[tuple[str, ...], list[tuple[int, str, str]]]) -> None:
        self.settings = SETTINGS
        self.answers = {("services", "list"): [(0, APIS, "")], **answers}
        self.calls: list[tuple[str, ...]] = []

    def run(self, *argv: str, check: bool = True) -> Any:
        assert argv[0] == "gcloud" and not check
        assert argv[-2:] == ("--project", SETTINGS.project), argv
        args = argv[1:-2]
        self.calls.append(args)
        for start, replies in self.answers.items():
            if args[: len(start)] == start:
                code, out, err = replies.pop(0) if len(replies) > 1 else replies[0]
                return SimpleNamespace(returncode=code, stdout=out, stderr=err)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def gcloud_json(self, *args: str) -> Any:
        assert args[:2] == ("projects", "get-iam-policy")
        return {"bindings": [{"role": "roles/owner", "members": ["user:lab@x"]}]}


@pytest.fixture(autouse=True)
def no_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live, "LISTING_WAIT", 0)


def test_a_listing_that_fails_makes_the_inventory_incomplete_not_empty() -> None:
    """Denied, the buckets aren't nothing: they're unknown."""
    gcp = FakeGcp({("storage", "buckets", "list"): [(1, "", DENIED)]})
    with pytest.raises(live.IncompleteInventoryError, match=r"buckets: .*storage\.buckets\.list"):
        live.inventory(gcp, SETTINGS)  # type: ignore[arg-type]
    assert gcp.calls.count(live.LISTINGS["buckets"]) == live.LISTING_ATTEMPTS


def test_a_listing_that_fails_once_is_tried_again() -> None:
    gcp = FakeGcp(
        {("storage", "buckets", "list"): [(1, "", "ERROR: 503 backend error"), (0, "lab\n", "")]}
    )
    found = live.inventory(gcp, SETTINGS)  # type: ignore[arg-type]
    assert found.listings["buckets"] == {"lab"}


def test_an_empty_listing_that_succeeds_is_empty() -> None:
    found = live.inventory(FakeGcp({}), SETTINGS)  # type: ignore[arg-type]
    assert found.listings["buckets"] == set()
    assert found.bindings == {("roles/owner", "user:lab@x")}


def test_only_an_api_thats_off_lists_nothing() -> None:
    """Cloud SQL's API is off: the project can have nothing there, nor can the run make or
    delete anything, until it's on."""
    off = (1, "", "ERROR: API [sqladmin.googleapis.com] not enabled on project")
    gcp = FakeGcp({("sql",): [off], ("container", "binauthz"): [off]})
    found = live.inventory(gcp, SETTINGS)  # type: ignore[arg-type]
    assert found.listings["sql instances"] == set() and found.binauthz is None
    assert found.listings["apis"] == set(APIS.split())


def test_without_the_apis_listing_nothing_is_known() -> None:
    gcp = FakeGcp({("services", "list"): [(1, "", "ERROR: permission denied")]})
    with pytest.raises(live.IncompleteInventoryError, match="apis"):
        live.inventory(gcp, SETTINGS)  # type: ignore[arg-type]


def test_key_rings_and_keys_that_cant_be_listed_fail_it_too() -> None:
    ring = f"projects/shared-lab/locations/us/keyRings/{SETTINGS.name}"
    gcp = FakeGcp(
        {
            ("kms", "keyrings", "list"): [(0, ring + "\n", "")],
            ("kms", "keys", "list"): [(1, "", "ERROR: permission denied")],
        }
    )
    with pytest.raises(live.IncompleteInventoryError, match=SETTINGS.name):
        live.inventory(gcp, SETTINGS)  # type: ignore[arg-type]
    gcp = FakeGcp({("kms", "keyrings", "list"): [(1, "", "ERROR: permission denied")]})
    with pytest.raises(live.IncompleteInventoryError, match="key rings"):
        live.inventory(gcp, SETTINGS)  # type: ignore[arg-type]


def project(**sections: set[str]) -> live.Inventory:
    listings: dict[str, set[str]] = {section: set() for section in live.LISTINGS}
    listings.update({k.replace("_", " "): v for k, v in sections.items()})
    return live.Inventory(listings, {}, set(), [], None)


def test_a_bucket_that_was_there_before_and_is_gone_fails_tearing_down() -> None:
    before = project(buckets={"existing-lab-data", "shared-lab_cloudbuild"})
    after = project(buckets={"shared-lab_cloudbuild"})
    assert live.leftovers(before, after, SETTINGS) == [
        "buckets: existing-lab-data is gone, which was there before"
    ]


def test_what_comes_and_goes_in_a_shared_project_isnt_counted_as_gone() -> None:
    """Another team's autoscaled VM and its disk, a database's automatic backup, and what
    GKE makes for another team's cluster; a database that's only changed state."""
    before = project(
        instances={"web-mig-x1z9\tus-central1-b"},
        disks={"web-mig-x1z9\tus-central1-b"},
        sql_backups={"lab-db\tSUCCESSFUL"},
        firewalls={"gke-lab-cluster-1a2b-all\tdefault", "lab-ssh\tdefault"},
        sql_instances={"lab-db\tRUNNABLE"},
    )
    after = project(firewalls={"lab-ssh\tdefault"}, sql_instances={"lab-db\tMAINTENANCE"})
    assert live.leftovers(before, after, SETTINGS) == []


def test_a_resource_of_before_being_deleted_is_gone_too() -> None:
    before = project(
        custom_roles={"projects/shared-lab/roles/labViewer\t"},
        sql_instances={"lab-db\tRUNNABLE", "old-db\tRUNNABLE"},
        log_buckets={"projects/shared-lab/locations/global/buckets/audit\tACTIVE"},
    )
    after = project(
        custom_roles={"projects/shared-lab/roles/labViewer\tTrue"},
        sql_instances={"lab-db\tPENDING_DELETE"},
        log_buckets={"projects/shared-lab/locations/global/buckets/audit\tDELETE_REQUESTED"},
    )
    assert live.leftovers(before, after, SETTINGS) == [
        "sql instances: lab-db is being deleted, which was there before",
        "sql instances: old-db\tRUNNABLE is gone, which was there before",
        "custom roles: projects/shared-lab/roles/labViewer is being deleted, which was there "
        "before",
        "log buckets: projects/shared-lab/locations/global/buckets/audit is being deleted, "
        "which was there before",
    ]


def test_what_google_cloud_keeps_of_the_deployment_is_still_allowed() -> None:
    """Its custom role and log bucket waiting to be deleted, made and gone in the run."""
    role = f"projects/shared-lab/roles/{SETTINGS.name.replace('-', '_')}_web\tTrue"
    logs = f"projects/shared-lab/locations/global/buckets/{SETTINGS.name}\tDELETE_REQUESTED"
    assert (
        live.leftovers(project(), project(custom_roles={role}, log_buckets={logs}), SETTINGS) == []
    )


# --- The VM tier's light stand-ins (48h) -------------------------------------------------

REPOSITORY = "us-central1-docker.pkg.dev/shared-lab/iki-test-11190800-plugins"
DIGEST = "sha256:1e1f6334529d1fb26a2ef619d7cffb30a7c7d7cc1cc7338ebee283a58d1aeaca"


def test_docker_hubs_images_are_copied_from_googles_mirror_and_named_by_their_digest() -> None:
    copy = live.mirrored(f"docker.io/library/python:3.13-alpine@{DIGEST}", REPOSITORY)
    assert copy.source == f"mirror.gcr.io/library/python@{DIGEST}"
    assert copy.destination == f"{REPOSITORY}/python:3.13-alpine"
    assert copy.pinned == f"{REPOSITORY}/python@{DIGEST}"
    other = live.mirrored(f"ghcr.io/acme/tools@{DIGEST}", REPOSITORY)
    assert (other.source, other.destination) == (
        f"ghcr.io/acme/tools@{DIGEST}",
        f"{REPOSITORY}/tools:live",
    )
    with pytest.raises(AssertionError, match="pinned by its digest"):
        live.mirrored("docker.io/library/python:3.13-alpine", REPOSITORY)


def test_the_live_check_runs_its_image_from_the_deployments_registry(tmp_path: Path) -> None:
    plugin, copy = live.live_check_plugin(tmp_path, REPOSITORY)
    environment = (plugin / "environments" / "machine.yaml").read_text()
    assert f"image: {REPOSITORY}/python@{DIGEST}\n" in environment
    assert "docker.io" not in environment.split("\nimage:")[1].splitlines()[0]
    # The repository's own copy is left as it is.
    assert (
        "image: docker.io/library/python:3.13-alpine@"
        in (live.LIVE_CHECK_DIR / "environments" / "machine.yaml").read_text()
    )
    assert copy.pinned == f"{REPOSITORY}/python@{DIGEST}"


def test_the_vm_tiers_template_is_a_small_machine_bound_to_the_live_check() -> None:
    template, binding = live.vm_compute(SETTINGS)
    assert template["provisioner"] == "gcp_gce"
    assert template["gcp"] == {
        "machine_type": "e2-small",
        "image": "projects/cos-cloud/global/images/family/cos-stable",
        "region": "us-central1",
        "zones": ["us-central1-b", "us-central1-c"],
    }
    assert template["volumes"] == {"reference": {"strategy": "clone_from_snapshot"}}
    assert binding["environment"] == live.VM_ENVIRONMENT
    assert binding["templates"] == [{"template": live.VM_TEMPLATE}]


def test_the_live_check_and_its_template_pass_validation(tmp_path: Path) -> None:
    """As the deployment's plugins are built: the plugin with its image moved, its
    environment bound to the template, which suits its volume and resources."""
    import iki_live

    plugin, _ = live.live_check_plugin(tmp_path, REPOSITORY)
    out = iki_live.build_plugins(tmp_path, {live.LIVE_CHECK: plugin}, live.vm_compute(SETTINGS))
    assert (out / "server" / "plugins" / "iki_plugin_acme__live_check").is_dir()


def test_waiting_on_the_vm_tier_asks_again_when_a_request_stalls_and_keeps_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long wait goes through the port-forward, which can stall for a moment: that's asked
    again, not a failure. Each time, what the VM tier's machines are is written down."""
    import time

    import requests

    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    settings = live.Settings(**{**SETTINGS.__dict__, "workdir": tmp_path})

    class Machines:
        def gcloud(self, *args: str) -> str:
            assert args[:3] == ("compute", "instances", "list")
            return '[{"name": "iki-test-11190800-run-1", "zone": "us-central1-b"}]'

        def run(self, *argv: str, check: bool = True) -> Any:
            assert not check
            return SimpleNamespace(returncode=1, stdout="", stderr="")

    answers: list[Any] = [
        requests.ReadTimeout("read timed out"),
        {"status": "running"},
        {"status": "succeeded"},
    ]

    def look() -> dict[str, Any]:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        found: dict[str, Any] = answer
        return found

    d: Any = SimpleNamespace(gcp=Machines(), settings=settings)
    assert live.followed(d, "the run", look, {"succeeded", "failed"}, 60) == {"status": "succeeded"}
    written = (tmp_path / "logs" / "machines.log").read_text()
    assert "asking for the run failed: read timed out" in written
    assert written.count("iki-test-11190800-run-1") == 4
