"""The live Google Cloud test's inventory of the project, and what it counts as left or
taken away, checked offline: they need no project, so they run everywhere, CI included,
while test_gcp_live.py's checks don't. The project is shared with other teams, so the
inventory never takes a listing it couldn't make as empty, and tearing down may remove
nothing that was there before."""

import inspect
import json
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests
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


@pytest.mark.parametrize("location", ["https://elsewhere.test/", "http://oauth2.googleapis.com/"])
def test_the_operator_token_is_only_posted_to_tokeninfo(
    monkeypatch: pytest.MonkeyPatch, location: str
) -> None:
    endpoint = "https://oauth2.googleapis.com/tokeninfo"
    sent = []

    def answer(
        adapter: requests.adapters.HTTPAdapter, request: requests.PreparedRequest, **kwargs: Any
    ) -> requests.Response:
        sent.append(request.url)
        assert sent == [endpoint]
        assert request.body == "access_token=sample"
        response = requests.Response()
        response.request = request
        response.url = endpoint
        response.status_code = 307
        response.headers["Location"] = location
        response._content = b""
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", answer)
    gcp = SimpleNamespace(access_token=lambda: "sample")
    with pytest.raises(AssertionError, match="the token's info: HTTP 307"):
        live.Gcp.identity(gcp)  # type: ignore[arg-type]
    assert sent == [endpoint]


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


@pytest.mark.parametrize(
    ("section", "line"),
    [
        ("buckets", "gke-lab-archive"),
        ("buckets", "k8s1-lab-backups"),
        ("secrets", "gke-lab-token"),
        ("service accounts", "gkegw1-lab@shared-lab.iam.gserviceaccount.com"),
        ("sql instances", "gke-lab-db\tRUNNABLE"),
        ("artifact registry", "projects/shared-lab/locations/us/repositories/gke-lab"),
        ("networks", "gke-lab-vpc"),
    ],
)
def test_a_gke_looking_name_exempts_only_what_gke_makes(section: str, line: str) -> None:
    """GKE makes firewalls, load balancers' parts and node templates for any cluster, which
    come and go with it; never buckets, secrets or the rest, whatever they're named."""
    before = project(**{section.replace(" ", "_"): {line}})
    assert live.leftovers(before, project(), SETTINGS) == [
        f"{section}: {line} is gone, which was there before"
    ]


@pytest.mark.parametrize(
    ("section", "line"),
    [
        ("firewalls", "gke-lab-cluster-1a2b-all\tdefault"),
        ("firewalls", "k8s2-lab-l4-fw\tdefault"),
        ("forwarding rules", "gkegw1-lab-gateway-x1"),
        ("backend services", "gkegw1-lab-backend"),
        ("health checks", "k8s1-lab-hc"),
        ("url maps", "gkegw1-lab-map"),
        ("target https proxies", "gkegw1-lab-proxy"),
        ("ssl certificates", "gkegw1-lab-cert"),
        ("instance templates", "gke-lab-cluster-default-pool-1a2b3c4d"),
    ],
)
def test_what_gke_makes_for_another_cluster_may_go(section: str, line: str) -> None:
    before = project(**{section.replace(" ", "_"): {line}})
    assert live.leftovers(before, project(), SETTINGS) == []
    # And what's left of it after tearing down, the deployment's cluster's, is reported.
    assert live.leftovers(project(), before, SETTINGS) == [f"{section}: {line}"]


def test_a_gke_looking_bucket_made_during_the_run_isnt_taken_for_the_deployments() -> None:
    """Another team's, made meanwhile: neither left by the run nor gone."""
    assert live.leftovers(project(), project(buckets={"gke-lab-archive"}), SETTINGS) == []


REGION_URL = (
    f"https://www.googleapis.com/compute/v1/projects/{SETTINGS.project}/regions/{SETTINGS.region}"
)
SHARED_ROUTER = REGION_URL + "/routers/other-router"
AUTO_ADDRESS = {
    "name": "provider-address",
    "region": REGION_URL,
    "purpose": "NAT_AUTO",
    "users": [SHARED_ROUTER],
}


def address_inventory(*addresses: dict[str, Any]) -> live.Inventory:
    routers = ["other-router", SETTINGS.name, SETTINGS.name + "-nat"]
    gcp = FakeGcp(
        {
            ("compute", "routers", "list"): [
                (0, "\n".join(f"{router}\t{SETTINGS.region}" for router in routers), "")
            ],
            ("compute", "addresses", "list"): [(0, json.dumps(addresses), "")],
        }
    )
    return live.inventory(gcp, SETTINGS)  # type: ignore[arg-type]


def test_automatic_addresses_of_another_router_can_rotate(tmp_path: Path) -> None:
    before = address_inventory(AUTO_ADDRESS)
    after = address_inventory({**AUTO_ADDRESS, "name": "replacement"})
    assert live.leftovers(before, after, SETTINGS) == []
    before.write(tmp_path / "before.txt")
    assert json.dumps(AUTO_ADDRESS, sort_keys=True) in (tmp_path / "before.txt").read_text()


@pytest.mark.parametrize(
    "change",
    [
        {"name": SETTINGS.name + "-address"},
        {"labels": {"iki-test": "true"}},
        {"labels": {"iki-deployment": SETTINGS.name}},
        {"users": [REGION_URL + "/routers/" + SETTINGS.name]},
        {"users": [REGION_URL + "/routers/" + SETTINGS.name + "-nat"]},
        {"users": []},
        {"users": [SHARED_ROUTER, REGION_URL + "/routers/second"]},
        {"users": [REGION_URL + "/routers/unlisted"]},
        {"users": [SHARED_ROUTER.replace(SETTINGS.region, "us-east1")]},
        {"users": [SHARED_ROUTER.replace(SETTINGS.project, "another-project")]},
        {"users": [SHARED_ROUTER.replace("www.googleapis.com", "unrelated.example")]},
        {"users": [SHARED_ROUTER.replace("/routers/", "/subnetworks/")]},
    ],
)
def test_run_marked_or_ambiguous_auto_nat_addresses_stay_strict(change: dict[str, Any]) -> None:
    row = {**AUTO_ADDRESS, **change}
    listing = address_inventory(row)
    empty = address_inventory()
    line = f"addresses: {row['name']}\t{SETTINGS.region}"
    assert live.leftovers(listing, empty, SETTINGS) == [line + " is gone, which was there before"]
    assert live.leftovers(empty, listing, SETTINGS) == [line]


def test_a_provider_looking_name_cannot_exempt_a_manual_address() -> None:
    row = {**AUTO_ADDRESS, "name": "nat-auto-ip-123", "purpose": "GCE_ENDPOINT"}
    assert live.leftovers(address_inventory(row), address_inventory(), SETTINGS) == [
        f"addresses: nat-auto-ip-123\t{SETTINGS.region} is gone, which was there before"
    ]


def test_a_shared_address_that_acquired_the_run_label_is_reported() -> None:
    after = address_inventory({**AUTO_ADDRESS, "labels": {"iki-deployment": SETTINGS.name}})
    assert live.leftovers(address_inventory(AUTO_ADDRESS), after, SETTINGS) == [
        f"addresses: provider-address\t{SETTINGS.region}"
    ]


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


# --- The project's Binary Authorization policy ------------------------------------------

#: The policy as `gcloud container binauthz policy export` gives an untouched project's.
UNTOUCHED: dict[str, Any] = {
    "name": "projects/shared-lab/policy",
    "updateTime": "2026-11-01T10:00:00.000000Z",
    "etag": '"a1b2"',
    "globalPolicyEvaluationMode": "ENABLE",
    "defaultAdmissionRule": {
        "enforcementMode": "ENFORCED_BLOCK_AND_AUDIT_LOG",
        "evaluationMode": "ALWAYS_ALLOW",
    },
}
#: Another team's: everything allowed by default, but its production cluster runs only
#: attested images.
ATTESTED: dict[str, Any] = {
    **UNTOUCHED,
    "clusterAdmissionRules": {
        "us-central1.other-team-prod": {
            "evaluationMode": "REQUIRE_ATTESTATION",
            "enforcementMode": "ENFORCED_BLOCK_AND_AUDIT_LOG",
            "requireAttestationsBy": ["projects/shared-lab/attestors/built-by-ci"],
        }
    },
}
#: The deployment's, while it's up (modules/binary-authorization).
DEPLOYMENTS: dict[str, Any] = {
    **UNTOUCHED,
    "admissionWhitelistPatterns": [
        {"namePattern": f"us-central1-docker.pkg.dev/shared-lab/{SETTINGS.name}-server/**"}
    ],
    "defaultAdmissionRule": {
        "enforcementMode": "ENFORCED_BLOCK_AND_AUDIT_LOG",
        "evaluationMode": "ALWAYS_DENY",
    },
}


def scoped(field: str, key: str) -> dict[str, Any]:
    rule = {"evaluationMode": "ALWAYS_DENY", "enforcementMode": "DRYRUN_AUDIT_LOG_ONLY"}
    return {**UNTOUCHED, field: {key: rule}}


@pytest.mark.parametrize(
    "policy",
    [
        ATTESTED,
        scoped("kubernetesNamespaceAdmissionRules", "lab-prod"),
        scoped("kubernetesServiceAccountAdmissionRules", "lab-prod:deployer"),
        scoped("istioServiceIdentityAdmissionRules", "spiffe://lab/ns/prod/sa/api"),
        {**UNTOUCHED, "admissionWhitelistPatterns": [{"namePattern": "gcr.io/lab/*"}]},
        {**UNTOUCHED, "globalPolicyEvaluationMode": "DISABLE"},
        {k: v for k, v in UNTOUCHED.items() if k != "globalPolicyEvaluationMode"},
        {**UNTOUCHED, "defaultAdmissionRule": {"evaluationMode": "REQUIRE_ATTESTATION"}},
        {
            **UNTOUCHED,
            "defaultAdmissionRule": {
                "evaluationMode": "ALWAYS_ALLOW",
                "enforcementMode": "DRYRUN_AUDIT_LOG_ONLY",
            },
        },
        # A field Google adds later counts until it's known to say nothing.
        {**UNTOUCHED, "platformPolicies": ["lab"]},
    ],
)
@pytest.mark.usefixtures("tools_are_there")
def test_a_policy_of_the_projects_own_stops_the_run_before_anything_is_made(
    policy: dict[str, Any],
) -> None:
    """Applying the deployment's would replace it, and destroying puts back the untouched
    policy, not this one: so any policy that admits otherwise is left alone."""
    before = replace(project(), binauthz=policy)
    with pytest.raises(AssertionError, match="Binary Authorization policy of its own"):
        live.check_preconditions(Preflight(), SETTINGS, before)  # type: ignore[arg-type]


class Preflight:
    """What the preconditions ask gcloud and Google's token info."""

    settings = SETTINGS

    def gcloud(self, *args: str, **kwargs: Any) -> str:
        assert args[:2] == ("projects", "describe")
        return SETTINGS.project

    def identity(self) -> str:
        return "deployer@shared-lab.iam.gserviceaccount.com"


@pytest.fixture
def tools_are_there(monkeypatch: pytest.MonkeyPatch) -> None:
    import iki_live

    monkeypatch.setattr(iki_live, "tool", lambda name: f"/usr/bin/{name}")


@pytest.mark.parametrize(
    "policy",
    [
        None,
        UNTOUCHED,
        # Written in another order, without its labels, and with its rule's fields reversed.
        {
            "defaultAdmissionRule": {
                "evaluationMode": "ALWAYS_ALLOW",
                "enforcementMode": "ENFORCED_BLOCK_AND_AUDIT_LOG",
            },
            "globalPolicyEvaluationMode": "ENABLE",
            "clusterAdmissionRules": {},
        },
    ],
)
@pytest.mark.usefixtures("tools_are_there")
def test_an_untouched_policy_or_none_lets_the_run_start(policy: Any) -> None:
    before = replace(project(), binauthz=policy)
    live.check_preconditions(Preflight(), SETTINGS, before)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("before", "after", "problem"),
    [
        # Another team's cluster rule, gone: the untouched policy isn't what was there.
        (ATTESTED, UNTOUCHED, True),
        (UNTOUCHED, DEPLOYMENTS, True),
        (None, DEPLOYMENTS, True),
        (UNTOUCHED, None, True),
        (UNTOUCHED, {**UNTOUCHED, "etag": '"c3d4"', "updateTime": "2026-11-20T10:00:00Z"}, False),
        # Its API was off before: turning it on made the untouched policy.
        (None, UNTOUCHED, False),
        (ATTESTED, {**ATTESTED, "etag": '"e5"'}, False),
        (
            ATTESTED,
            {
                **ATTESTED,
                "clusterAdmissionRules": {
                    "us-central1.other-team-prod": {
                        **ATTESTED["clusterAdmissionRules"]["us-central1.other-team-prod"],
                        "evaluationMode": "ALWAYS_ALLOW",
                    }
                },
            },
            True,
        ),
    ],
)
def test_tearing_down_must_leave_the_policy_admitting_what_it_did(
    before: Any, after: Any, problem: bool
) -> None:
    found = live.leftovers(
        replace(project(), binauthz=before), replace(project(), binauthz=after), SETTINGS
    )
    assert bool(found) == problem, found
    if problem:
        assert found == [
            "the Binary Authorization policy isn't what it was: "
            f"{json.dumps(before, sort_keys=True)} -> {json.dumps(after, sort_keys=True)}"
        ]


CLUSTERS = [
    {"name": "lab-dev", "location": "us-central1-a", "binaryAuthorization": {}},
    {
        "name": "lab-staging",
        "location": "us-east1",
        "binaryAuthorization": {"evaluationMode": "DISABLED"},
    },
    {
        "name": "lab-prod",
        "location": "us-central1",
        "binaryAuthorization": {"evaluationMode": "PROJECT_SINGLETON_POLICY_ENFORCE"},
    },
    {
        "name": "lab-mixed",
        "location": "europe-west4",
        "binaryAuthorization": {
            "evaluationMode": "POLICY_BINDINGS_AND_PROJECT_SINGLETON_POLICY_ENFORCE"
        },
    },
    {"name": "lab-old", "location": "us-west1-b", "binaryAuthorization": {"enabled": True}},
    {
        "name": "lab-platform",
        "location": "us-west1",
        "binaryAuthorization": {"evaluationMode": "POLICY_BINDINGS"},
    },
]


def run_resource(name: str, binauthz: str | None) -> dict[str, Any]:
    annotations = {"run.googleapis.com/binary-authorization": binauthz} if binauthz else {}
    return {
        "metadata": {
            "name": name,
            "labels": {"cloud.googleapis.com/location": "us-central1"},
            "annotations": {"run.googleapis.com/ingress": "all", **annotations},
        }
    }


@pytest.mark.usefixtures("tools_are_there")
def test_what_else_enforces_the_projects_policy_is_found_and_stops_the_run() -> None:
    """Each would refuse its own images while the deployment's policy is the project's."""
    gcp = FakeGcp(
        {
            live.ENFORCERS["clusters"]: [(0, json.dumps(CLUSTERS), "")],
            live.ENFORCERS["Cloud Run services"]: [
                (0, json.dumps([run_resource("api", "default"), run_resource("web", None)]), "")
            ],
            live.ENFORCERS["Cloud Run jobs"]: [
                (0, json.dumps([run_resource("etl", "default")]), "")
            ],
            ("services", "list"): [
                (0, APIS + "container.googleapis.com\nrun.googleapis.com\n", "")
            ],
        }
    )
    found = live.inventory(gcp, SETTINGS)  # type: ignore[arg-type]
    assert found.enforcing == {
        "cluster us-central1/lab-prod",
        "cluster europe-west4/lab-mixed",
        "cluster us-west1-b/lab-old",
        "Cloud Run service us-central1/api",
        "Cloud Run job us-central1/etl",
    }
    with pytest.raises(AssertionError, match="cluster us-central1/lab-prod"):
        live.check_preconditions(Preflight(), SETTINGS, found)  # type: ignore[arg-type]


def test_what_enforces_the_policy_cant_be_listed_is_unknown_not_nothing() -> None:
    gcp = FakeGcp(
        {
            ("services", "list"): [(0, APIS + "run.googleapis.com\n", "")],
            live.ENFORCERS["Cloud Run jobs"]: [(1, "", "ERROR: permission denied")],
        }
    )
    with pytest.raises(live.IncompleteInventoryError, match="Cloud Run jobs"):
        live.inventory(gcp, SETTINGS)  # type: ignore[arg-type]


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


def test_bootstrap_applies_both_live_run_labels(tmp_path: Path) -> None:
    class Bootstrap:
        def tofu(self, root: str, command: str, *args: str, log: str) -> None:
            assert root == "bootstrap"
            if command == "apply":
                (labels,) = [
                    arg.removeprefix("labels=") for arg in args if arg.startswith("labels=")
                ]
                assert json.loads(labels) == {"iki-test": "true", "iki-deployment": SETTINGS.name}
                raise RuntimeError("end of offline bootstrap")

    deployment = SimpleNamespace(settings=replace(SETTINGS, workdir=tmp_path), gcp=Bootstrap())
    with pytest.raises(RuntimeError, match="end of offline bootstrap"):
        live.stand_up(deployment)  # type: ignore[arg-type]


def test_setup_waits_for_a_backend_worker_to_register_through_the_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    polls = iter(
        [
            [],
            [{"tier": "vm", "status": "online"}, {"tier": "backend", "status": "offline"}],
            [{"tier": "backend", "status": "online"}],
        ]
    )
    calls: list[str] = []

    class Admin:
        def api(self, method: str, path: str, body: Any = None) -> Any:
            calls.append(f"{method} {path}")
            if method == "GET":
                return SimpleNamespace(json=lambda: {"items": next(polls)})
            return SimpleNamespace(status_code=201, json=lambda: [{"state": "queued"}])

    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    build = inspect.unwrap(live.setup_build)(deployment=None, compute_admin=Admin())
    assert build == {"state": "queued"}
    assert calls == ["GET /compute/workers?limit=100"] * 3 + ["POST /compute/setups"]


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


LAUNCH = "01a1d3a1-04c4-7657-86e7-9bdd0aa33e57"
PROVISIONER = "run-provisioner@shared-lab.iam.gserviceaccount.com"


def vm_activity(instance: str = "101") -> list[dict[str, Any]]:
    zone = SETTINGS.zones[0]
    path = f"projects/{SETTINGS.project}/zones/{zone}/instances/{SETTINGS.name}-run-a"
    labels = {**SETTINGS.labels, "iki-launch": LAUNCH}
    rows: list[dict[str, Any]] = []
    for method, phase in (("insert", "first"), ("insert", "last"), ("delete", "last")):
        payload: dict[str, Any] = {
            "methodName": "v1.compute.instances." + method,
            "resourceName": path,
        }
        if phase == "first":
            payload["authenticationInfo"] = {"principalEmail": PROVISIONER}
            payload["request"] = {"labels": [{"key": k, "value": v} for k, v in labels.items()]}
        rows.append(
            {
                "operation": {"id": f"{method}-{instance}", phase: True},
                "resource": {
                    "labels": {
                        "instance_id": instance,
                        "project_id": SETTINGS.project,
                        "zone": zone,
                    }
                },
                "protoPayload": payload,
            }
        )
    return rows


def test_a_machine_created_and_deleted_between_live_lists_is_verified_by_its_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The only in-flight list times out. The labelled VM is gone when the retry succeeds."""
    s = replace(SETTINGS, workdir=tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    production = tmp_path / "deploy/gcp/production"
    production.mkdir(parents=True)
    (production / "values-gcp.yaml").write_text(
        json.dumps(
            {
                "vmTier": {
                    "serviceAccountAnnotations": {"iam.gke.io/gcp-service-account": PROVISIONER}
                }
            }
        )
    )
    (tmp_path / "gcp-window.json").write_text('{"start":"2026-01-01T00:00:00Z"}')
    activity = vm_activity()
    reads: Iterator[AssertionError | str] = iter(
        [AssertionError("gcloud crashed (ReadTimeout)"), AssertionError("read timed out"), "[]"]
    )
    audits = iter([activity[:2], activity])
    delays: list[float] = []
    monkeypatch.setattr(time, "sleep", delays.append)

    class Gcp:
        def gcloud(self, *args: str) -> str:
            if args[:2] == ("logging", "read"):
                assert "cloudaudit.googleapis.com%2Factivity" in args[2]
                assert "protoPayload.request.labels" in args[3]
                return json.dumps(next(audits))
            assert args[:3] == ("compute", "instances", "list")
            reply = next(reads)
            if isinstance(reply, AssertionError):
                raise reply
            return reply

    d: Any = SimpleNamespace(settings=s, gcp=Gcp())
    assert live.machines(d) == []
    assert delays == [1, 2]
    live.check_attempt_machine(d, "iki-run-" + LAUNCH.replace("-", ""))
    assert json.loads((logs / "vm-activity.json").read_text()) == activity
    with pytest.raises(AssertionError, match="invalid VM-tier worker name"):
        live.check_attempt_machine(d, f"{s.name}-run-a")


def test_audit_requires_one_distinct_instance_even_if_only_one_was_deleted() -> None:
    a = vm_activity()
    assert live.audited_machine(a[::-1] + a, SETTINGS, PROVISIONER, LAUNCH)
    with pytest.raises(AssertionError, match="multiple GCE instances"):
        live.audited_machine(a + vm_activity("102")[:2], SETTINGS, PROVISIONER, LAUNCH)


def test_audit_requires_the_provisioner_launch_and_both_ownership_labels() -> None:
    assert not live.audited_machine(vm_activity(), SETTINGS, "other-account", LAUNCH)
    assert not live.audited_machine(vm_activity(), SETTINGS, PROVISIONER, "another-launch")
    for key in ("iki-test", "iki-deployment"):
        rows = vm_activity()
        labels = rows[0]["protoPayload"]["request"]["labels"]
        next(item for item in labels if item["key"] == key)["value"] = "wrong"
        with pytest.raises(AssertionError, match="ownership labels"):
            live.audited_machine(rows, SETTINGS, PROVISIONER, LAUNCH)
    for key in ("project_id", "zone"):
        rows = vm_activity()
        for row in rows:
            row["resource"]["labels"][key] = "other"
        assert not live.audited_machine(rows, SETTINGS, PROVISIONER, LAUNCH)
    rows = vm_activity()
    for row in rows:
        row["protoPayload"]["resourceName"] = row["protoPayload"]["resourceName"].replace(
            SETTINGS.name, SETTINGS.name + "other"
        )
    assert not live.audited_machine(rows, SETTINGS, PROVISIONER, LAUNCH)


def test_audit_needs_a_successful_insert_and_delete_of_the_very_same_instance() -> None:
    rows = vm_activity()
    assert not live.audited_machine([], SETTINGS, PROVISIONER, LAUNCH)
    assert not live.audited_machine(rows[:2], SETTINGS, PROVISIONER, LAUNCH)
    assert not live.audited_machine(
        rows[:2] + vm_activity("102")[2:], SETTINGS, PROVISIONER, LAUNCH
    )
    for index in range(3):
        rows = vm_activity()
        rows[index]["protoPayload"]["status"] = {"code": 7}
        assert not live.audited_machine(rows, SETTINGS, PROVISIONER, LAUNCH)
    rows = vm_activity()
    rows[1]["operation"]["id"] = "different-insert"
    assert not live.audited_machine(rows, SETTINGS, PROVISIONER, LAUNCH)
    rows = vm_activity()
    rows[2]["operation"] = {"id": "delete-101", "first": True}
    assert not live.audited_machine(rows, SETTINGS, PROVISIONER, LAUNCH)


def test_listing_errors_cannot_establish_that_the_vm_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays: list[float] = []
    monkeypatch.setattr(time, "sleep", delays.append)

    class Denied:
        def gcloud(self, *args: str) -> str:
            raise AssertionError("permission denied")

    class TimedOut:
        def gcloud(self, *args: str) -> str:
            raise AssertionError("gcloud crashed (ReadTimeout)")

    with pytest.raises(AssertionError, match="permission denied"):
        live.vm_listing(Denied())  # type: ignore[arg-type]
    assert not delays
    with pytest.raises(AssertionError, match="ReadTimeout"):
        live.vm_listing(TimedOut())  # type: ignore[arg-type]
    assert delays == [1, 2]
