"""Slurm clusters as a compute target (ADR 0133): a worker on a cluster's login node leases
the container-tier jobs its cluster's Slurm templates take, many at once, each placed in a
partition and charged to its project's Slurm account, and says what Slurm reports of them.
The worker's own side, against a real Slurm, is test_slurm_e2e.py's."""

import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from api_client import problem
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone
from org_world import World, build_world
from test_api_entities import api_as
from test_run_results import done, output
from test_runs import TEMPLATE, protein
from test_workers import LOCAL, WorkerClient

from iki_server.audit.models import AuditEvent
from iki_server.entities import registry
from iki_server.runs import catalog, costs, provisioner, queue, services, slurm, workers
from iki_server.runs.models import (
    ActionRun,
    CostRecord,
    Job,
    RunStatus,
    SlurmAccount,
    SlurmJob,
    Worker,
)
from iki_server.settings import _image_policy
from iki_spec import schemas

pytestmark = pytest.mark.django_db


def test_image_signer_uses_both_selectors() -> None:
    issuer = {"issuer": "https://issuer.example"}

    def policy(**selectors: str) -> dict[str, Any] | None:
        return _image_policy(json.dumps({"iki": {**issuer, **selectors}}))

    assert policy(subject="release-2", subject_regexp=r"^release-\d$") == {
        "iki": {**issuer, "subject": "release-2"}
    }
    for selectors in ({}, {"subject": "release-٢", "subject_regexp": r"^release-\d$"}):
        with pytest.raises(ImproperlyConfigured):
            policy(**selectors)


COLABFOLD = "acme/prediction.predict_with_colabfold"
IMAGE = (
    "ghcr.io/acme/colabfold@sha256:2c26b46b68ffc68ff99b453c1d30413413422d706483bfa0f98a5e886266e7ae"
)
GI = 1024**3
POLICY = {
    "iki": {"issuer": "https://token.actions.githubusercontent.com", "subject_regexp": "^iki$"},
    "approvals": {
        "issuer": "https://token.actions.githubusercontent.com",
        "subject": "https://github.com/acme/iki-config/.github/workflows/build.yml@refs/heads/main",
        "repository": "ghcr.io/acme/approvals",
    },
}


def hpc(**given: Any) -> dict[str, Any]:
    """A Slurm template on the `hpc1` cluster's `gpu` partition."""
    return {
        **TEMPLATE,
        "name": "hpc-gpu",
        "provisioner": "slurm",
        "lifecycle": "ephemeral",
        "capacity": {
            "cpu": 64,
            "memory": 512 * GI,
            "gpu_count": 4,
            "gpu_memory": 80 * GI,
            "scratch_disk": None,
        },
        "max_concurrent": 2,
        "slurm": {
            "cluster": "hpc1",
            "partition": "gpu",
            "qos": "normal",
            "account": "rd-shared",
            "gpu_type": "a100",
            "max_walltime": 2 * 86400,
        },
        "price_per_cpu_hour": 0.02,
        "price_per_gpu_hour": 1.10,
        "price_per_memory_gib_hour": 0.001,
        "currency": "USD",
        **given,
    }


def with_cluster(request: pytest.FixtureRequest) -> dict[str, Any]:
    """Deploys compute where colabfold runs on the `hpc1` cluster for the test, and
    returns it."""
    monkeypatch: pytest.MonkeyPatch = request.getfixturevalue("monkeypatch")
    tmp_path: Path = request.getfixturevalue("tmp_path")
    data: dict[str, Any] = json.loads(Path(catalog._compute_file()).read_text())  # type: ignore[arg-type]
    data["templates"]["hpc-gpu"] = hpc()
    data["bindings"]["acme/prediction.colabfold"] = {
        "environment": "acme/prediction.colabfold",
        "templates": [["hpc-gpu", 0]],
    }
    monkeypatch.setenv("IKI_COMPUTE_FILE", str(tmp_path / "with-slurm.json"))
    use(data)
    # The next test reads its own.
    request.addfinalizer(catalog.compute.cache_clear)
    return data


@pytest.fixture
def compute(request: pytest.FixtureRequest) -> dict[str, Any]:
    """The deployment's compute: colabfold runs on `hpc1`. Change it, then `use` it."""
    return with_cluster(request)


def use(compute: dict[str, Any]) -> None:
    """Deploys `compute` as the deployment's."""
    Path(catalog._compute_file()).write_text(json.dumps(compute))  # type: ignore[arg-type]
    catalog.compute.cache_clear()


@pytest.fixture
def world(compute: dict[str, Any], settings: Any) -> World:
    settings.IKI_IMAGE_POLICY = POLICY
    return build_world()


@pytest.fixture(autouse=True)
def keep_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    from iki_server.realtime import views

    monkeypatch.setattr(views, "close_old_connections", lambda: None)


def login_node(
    world: World,
    cluster: str = "hpc1",
    name: str = "hpc1-login1",
    *,
    plugins: list[dict[str, Any]] | None = None,
) -> WorkerClient:
    _, value = workers.create_registration(
        label="hpc1 login nodes",
        tier="container",
        by=world.users["compute_admin"],
        slurm_cluster=cluster,
    )
    made = WorkerClient(value).post(
        "/register",
        {
            "name": name,
            "version": "0.1.0",
            # What the login node has doesn't matter: Slurm's nodes run the jobs.
            "capacity": {"cpu": 2, "memory": 4 * GI},
            "plugins": LOCAL if plugins is None else plugins,
        },
    )
    assert made.status_code == 201, made.content
    assert made.json()["slurm_cluster"] == cluster
    return WorkerClient(made.json()["token"])


def queue_run(world: World, name: str = "ACDE") -> dict[str, Any]:
    runner = api_as(world, "runner")
    abl = protein(runner, world.project, name=name)
    made = runner.post(
        f"/actions/{COLABFOLD}/runs",
        {"project": str(world.project.pk), "inputs": {"protein": abl["id"]}},
    )
    assert made.status_code == 201, made.content
    body: dict[str, Any] = made.json()
    return body


# --- Leasing -----------------------------------------------------------------------------------


def test_a_login_nodes_worker_leases_its_clusters_jobs_placed_where_the_template_says(
    world: World,
) -> None:
    run = queue_run(world)
    # Iki starts no machine for it: the cluster runs it.
    assert provisioner.template_for(Job.objects.get(run_id=run["id"])) is None
    node = login_node(world)
    leased = node.lease()
    assert leased.status_code == 200, leased.content
    payload = leased.json()
    assert not list(schemas.check("run-input", payload))
    assert payload["job"]["slurm"] == {
        "cluster": "hpc1",
        "template": "hpc-gpu",
        "partition": "gpu",
        "account": "rd-shared",
        "qos": "normal",
        "gpu_type": "a100",
        # Sized from the environment: 2 CPUs, 8Gi and a GPU, for the action's hour.
        "cpus": 2,
        "memory": 8 * GI,
        "gpus": 1,
        "walltime": 3600,
        "image": IMAGE,
        "image_policy": POLICY,
    }
    job = Job.objects.get(pk=payload["job"]["id"])
    assert job.template == "hpc-gpu"
    assert job.environment_build == IMAGE.rpartition("@")[2]
    # Its lease outlasts the login node restarting.
    assert job.lease_expires_at is not None
    assert job.lease_expires_at > timezone.now() + timedelta(minutes=9)
    placed = SlurmJob.objects.get(job=job)
    assert (placed.cluster, placed.partition, placed.account) == ("hpc1", "gpu", "rd-shared")
    assert ActionRun.objects.get(pk=run["id"]).status == RunStatus.RUNNING


@pytest.fixture
def customer_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = registry.plugins()["acme/prediction"]
    monkeypatch.setitem(
        plugin.environments,
        "colabfold",
        replace(plugin.environments["colabfold"], image=None, customer_image_required=True),
    )


def bind_customer_image(
    compute: dict[str, Any], image: str, *, pull_secret: str | None = None
) -> None:
    plugin = registry.plugins()["acme/prediction"]
    compute["image_bindings"] = [
        {
            "environment": "acme/prediction.colabfold",
            "plugin_version": f"=={plugin.version}",
            "image": image,
            "pull_secret": pull_secret,
        }
    ]
    use(compute)


def test_slurm_customer_image_is_pinned_and_controls_reuse_and_reproduction(
    world: World,
    compute: dict[str, Any],
    customer_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sha = "1" * 40
    monkeypatch.setattr(services, "plugin_sha", lambda _plugin: sha)
    original = "registry.example.com/lab/analysis@sha256:" + "a" * 64
    updated = "registry.example.com/lab/analysis@sha256:" + "b" * 64

    runner = api_as(world, "runner")
    entity = protein(runner, world.project, name="ACDE")
    payload = {"project": str(world.project.pk), "inputs": {"protein": entity["id"]}}

    bind_customer_image(compute, original)
    first = runner.post(f"/actions/{COLABFOLD}/runs", payload)
    assert first.status_code == 201, first.content
    run = first.json()
    assert ActionRun.objects.get(pk=run["id"]).environment_image == original

    # A deployment change after enqueue does not change what the job submits to Slurm.
    bind_customer_image(compute, updated)
    node = login_node(world, plugins=[{**item, "sha": sha} for item in LOCAL])
    leased = node.lease()
    assert leased.status_code == 200, leased.content
    job = leased.json()
    assert job["job"]["slurm"]["image"] == original
    assert Job.objects.get(pk=job["job"]["id"]).environment_build == original.rpartition("@")[2]
    assert done(node, job, output())["run_status"] == "succeeded"

    # Even with the same plugin revision, the new image doesn't reuse old results.
    different = runner.post(f"/actions/{COLABFOLD}/runs", payload)
    assert different.status_code == 201, different.content
    assert not different.json()["reused"]
    assert ActionRun.objects.get(pk=different.json()["id"]).environment_image == updated
    path = f"/runs/{run['hrid']}"
    assert not runner.get(path).json()["can"]["reproduce"]
    refused = runner.post(f"{path}/reproduce", {})
    assert refused.status_code == 409, refused.content
    assert "image binding" in refused.json()["detail"]

    alias = "mirror.example.com/tools@sha256:" + "a" * 64
    bind_customer_image(compute, alias)
    again = runner.post(f"/actions/{COLABFOLD}/runs", payload)
    assert again.status_code == 201, again.content
    assert again.json()["reused_from"]["id"] == run["id"]
    assert runner.get(path).json()["can"]["reproduce"]
    reproduced = runner.post(f"{path}/reproduce", {})
    assert reproduced.status_code == 201, reproduced.content
    assert ActionRun.objects.get(pk=reproduced.json()["id"]).environment_image == alias


def test_a_pinned_kubernetes_secret_cannot_follow_a_run_to_slurm(
    world: World, compute: dict[str, Any], customer_environment: None
) -> None:
    image = "registry.example.com/lab/analysis@sha256:" + "a" * 64
    binding = compute["bindings"]["acme/prediction.colabfold"]
    slurm_templates = binding["templates"]
    compute["templates"]["kube-gpu"] = hpc(name="kube-gpu", provisioner="k8s_job", slurm={})
    binding["templates"] = [["kube-gpu", 0]]
    bind_customer_image(compute, image, pull_secret="lab-registry")
    run = queue_run(world)
    assert queue.recover_expired() == 0

    binding["templates"] = slurm_templates
    bind_customer_image(compute, image)
    assert login_node(world).lease().status_code == 204
    assert queue.recover_expired() == 1
    ended = ActionRun.objects.get(pk=run["id"])
    assert ended.status == RunStatus.FAILED
    assert ended.error is not None
    assert ended.error["code"] == "unplaceable"
    assert "no longer binds" in ended.error["message"]


def test_a_cluster_holds_many_jobs_at_once_up_to_its_templates_limit(world: World) -> None:
    for i in range(3):
        queue_run(world, name="ACDEFG"[: i + 1])
    node = login_node(world)
    assert node.lease().status_code == 200
    assert node.lease().status_code == 200
    # `max_concurrent: 2`, across every login node of the cluster.
    assert login_node(world, name="hpc1-login2").lease().status_code == 204
    (waiting,) = [
        r for r in api_as(world, "runner").get("/runs").json()["items"] if r["status"] == "queued"
    ]
    assert waiting["waiting"] == (
        "Waiting for room on the hpc1 cluster: 2 of its jobs are running, as many as "
        "`hpc-gpu` allows."
    )


def test_only_its_clusters_workers_lease_a_job_a_slurm_template_takes_first(
    world: World, compute: dict[str, Any]
) -> None:
    queue_run(world)
    _, value = workers.create_registration(
        label="Pods", tier="container", by=world.users["compute_admin"]
    )
    pod = WorkerClient(
        WorkerClient(value)
        .post(
            "/register",
            {
                "name": "pod",
                "version": "0.1.0",
                "capacity": {"cpu": 64, "memory": 512 * GI, "gpu_count": 8, "gpu_memory": 80 * GI},
                "plugins": LOCAL,
            },
        )
        .json()["token"]
    )
    assert pod.lease().status_code == 204
    # Another cluster's worker leases none of hpc1's.
    compute["templates"]["other"] = hpc(name="other", slurm={**hpc()["slurm"], "cluster": "hpc2"})
    use(compute)
    assert login_node(world, cluster="hpc2", name="hpc2-login").lease().status_code == 204
    # When a Kubernetes template comes first, Iki starts a machine for it instead.
    compute["templates"]["gpu-t4"] = {
        **hpc(),
        "name": "gpu-t4",
        "provisioner": "k8s_job",
        "slurm": {},
        "price_per_cpu_hour": None,
        "price_per_gpu_hour": None,
        "price_per_memory_gib_hour": None,
        "price_per_hour": 0.5,
        "max_concurrent": None,
    }
    compute["bindings"]["acme/prediction.colabfold"]["templates"] = [["gpu-t4", 0], ["hpc-gpu", 1]]
    use(compute)
    assert login_node(world).lease().status_code == 204
    assert provisioner.template_for(Job.objects.get()) is not None
    assert pod.lease().status_code == 200


def test_a_job_reaching_the_internet_runs_only_where_compute_nodes_reach_the_proxy(
    world: World, compute: dict[str, Any]
) -> None:
    run = queue_run(world)
    ActionRun.objects.filter(pk=run["id"]).update(
        spec={**ActionRun.objects.get(pk=run["id"]).spec, "network": ["rest.uniprot.org"]}
    )
    node = login_node(world)
    assert node.lease().status_code == 204
    compute["templates"]["hpc-gpu"]["slurm"]["egress_proxy_url"] = "http://iki-proxy.hpc:3128"
    use(compute)
    payload = node.lease().json()
    # The proxy as the compute nodes reach it, with the attempt's own credentials.
    assert payload["job"]["proxy"].startswith("http://")
    assert payload["job"]["proxy"].endswith("@iki-proxy.hpc:3128")


def test_a_job_calling_outside_apis_needs_the_proxy_too(
    world: World, compute: dict[str, Any]
) -> None:
    """Outside APIs are called through the egress proxy (ADR 0130), which the cluster's
    compute nodes can't reach without the template's `egress_proxy_url`."""
    run = queue_run(world)
    spec = ActionRun.objects.get(pk=run["id"]).spec
    uses = [{"name": "fold", "api": "acme/prediction.fold", "inputs": {}, "params": []}]
    ActionRun.objects.filter(pk=run["id"]).update(spec={**spec, "apis": uses})
    assert slurm.first(Job.objects.select_related("run").get(run_id=run["id"])) is None
    assert login_node(world).lease().status_code == 204


def test_an_action_longer_than_the_partitions_limit_cant_run_there(
    world: World, compute: dict[str, Any]
) -> None:
    compute["templates"]["hpc-gpu"]["slurm"]["max_walltime"] = 1800
    use(compute)
    action = catalog.action(COLABFOLD)
    assert action is not None
    available = catalog.availability(action)
    assert not available.available
    assert available.reason == (
        "This deployment can't run it: its timeout (1h) is longer than `hpc-gpu`'s partition "
        "allows (30m). A compute admin can change the Slurm template in the deployment's "
        "compute.yaml."
    )


def test_a_project_is_charged_to_its_own_account_or_its_run_fails_with_why(
    world: World, compute: dict[str, Any]
) -> None:
    slurm.set_account("hpc1", world.project, "rd-proteins", by=world.users["compute_admin"])
    queue_run(world)
    node = login_node(world)
    assert node.lease().json()["job"]["slurm"]["account"] == "rd-proteins"
    # Without the project's own or the template's, it doesn't start.
    SlurmAccount.objects.all().delete()
    del compute["templates"]["hpc-gpu"]["slurm"]["account"]
    use(compute)
    run = queue_run(world, name="ACDEF")
    assert node.lease().status_code == 204
    failed = ActionRun.objects.get(pk=run["id"])
    assert failed.status == RunStatus.FAILED
    assert failed.error is not None
    assert "has no Slurm account on hpc1" in failed.error["message"]
    assert "Compute → Slurm" in failed.error["message"]


def test_a_cluster_with_no_worker_online_says_so(world: World) -> None:
    run = queue_run(world)
    body = api_as(world, "runner").get(f"/runs/{run['id']}").json()
    assert body["waiting"] == (
        "Waiting for the hpc1 cluster's worker: none is online. A Compute admin can check "
        "iki-worker on the cluster's login node."
    )


# --- What Slurm reports ------------------------------------------------------------------------


def test_the_worker_says_what_slurm_reports_and_the_run_page_shows_it(world: World) -> None:
    run = queue_run(world)
    node = login_node(world)
    payload = node.lease().json()
    assert node.report(payload, "heartbeat", state="staging").status_code == 200
    submitted = node.report(
        payload,
        "slurm",
        slurm_id="4242",
        state="PENDING",
        reason="Priority",
        submitted_at=timezone.now().isoformat(),
    )
    assert submitted.status_code == 200, submitted.content
    assert submitted.json()["slurm_id"] == "4242"
    # Waiting in the cluster's queue, its time doesn't count toward its timeout.
    assert node.report(payload, "heartbeat", state="pending").status_code == 200
    Job.objects.filter(pk=payload["job"]["id"]).update(
        started_at=timezone.now() - timedelta(hours=3)
    )
    assert queue.recover_expired() == 0
    # Its ID, once given, stays.
    other = problem(node.report(payload, "slurm", slurm_id="99"), 409, "refused")
    assert "submitted as Slurm job 4242" in other["detail"]
    bad = node.report(payload, "slurm", state="EXPLODED")
    assert bad.status_code == 422
    attempts = api_as(world, "viewer").get(f"/runs/{run['id']}/attempts").json()
    shown = attempts[0]["slurm"]
    assert attempts[0]["state"] == "pending"
    assert (shown["slurm_id"], shown["state"], shown["reason"]) == ("4242", "PENDING", "Priority")
    assert (shown["partition"], shown["account"]) == ("gpu", "rd-shared")


def test_only_a_clusters_jobs_wait_in_its_queue(world: World) -> None:
    from test_workers import queued, worker

    pod = worker(world)
    queued(world)
    job = pod.lease().json()
    refused = problem(pod.report(job, "heartbeat", state="pending"), 409, "wrong_state")
    assert "Only a Slurm cluster's jobs" in refused["detail"]
    assert problem(pod.report(job, "slurm", state="RUNNING"), 409, "refused")


def test_a_slurm_job_pays_for_what_it_was_given_while_it_ran(world: World) -> None:
    run = queue_run(world)
    node = login_node(world)
    payload = node.lease().json()
    started = timezone.now() - timedelta(minutes=30)
    node.report(payload, "heartbeat", state="running")
    reported = node.report(
        payload,
        "slurm",
        slurm_id="4243",
        state="RUNNING",
        nodes="gpu-007",
        submitted_at=(started - timedelta(hours=2)).isoformat(),
        started_at=started.isoformat(),
    )
    assert reported.status_code == 200, reported.content
    # While it runs, from when it started on its nodes, not from when it was submitted.
    so_far = api_as(world, "viewer").get(f"/runs/{run['id']}").json()["cost"]
    assert so_far["final"] is False
    # 2 CPUs, a GPU and 8 GiB: 0.04 + 1.10 + 0.008 an hour.
    assert so_far["amount"] == pytest.approx(0.574, abs=0.002)
    ended = node.report(
        payload,
        "slurm",
        state="FAILED",
        exit_code="1:0",
        ended_at=timezone.now().isoformat(),
        usage={
            "elapsed": 1800,
            "cpu_seconds": 3000.5,
            "max_rss": 6 * GI,
            "alloc": {"cpu": 4, "memory": 8 * GI, "gpu_count": 1},
        },
    )
    assert ended.status_code == 200, ended.content
    assert node.report(payload, "fail", reason="failed", message="exit 1").status_code == 200
    (record,) = CostRecord.objects.all()
    assert record.launch is None
    assert str(record.slurm_job_id) == payload["job"]["id"]
    assert record.seconds == Decimal("1800.000")
    # Slurm gave it 4 CPUs: it pays for those.
    assert record.price_per_hour == Decimal("1.188")
    assert record.amount == Decimal("0.5940")
    summary = costs.of_run(ActionRun.objects.get(pk=run["id"]))
    assert summary is not None
    assert summary["final"] is True


# --- Admins ------------------------------------------------------------------------------------


def test_registration_tokens_for_a_cluster(world: World) -> None:
    admin = api_as(world, "compute_admin")
    made = admin.post(
        "/compute/registration-tokens",
        {"label": "hpc1 login nodes", "tier": "container", "slurm_cluster": "hpc1"},
    )
    assert made.status_code == 201, made.content
    assert made.json()["slurm_cluster"] == "hpc1"
    unknown = admin.post(
        "/compute/registration-tokens",
        {"label": "x", "tier": "container", "slurm_cluster": "hpc9"},
    )
    assert "no Slurm templates for 'hpc9'" in problem(unknown, 409, "refused")["detail"]
    backend = admin.post(
        "/compute/registration-tokens", {"label": "x", "tier": "backend", "slurm_cluster": "hpc1"}
    )
    assert "container-tier" in problem(backend, 409, "refused")["detail"]
    node = login_node(world)
    shown = admin.get("/compute/workers").json()["items"]
    assert [w["slurm_cluster"] for w in shown] == ["hpc1"]
    assert node.call("GET", "/me").json()["slurm_cluster"] == "hpc1"


def test_compute_admins_see_clusters_and_set_projects_accounts(world: World) -> None:
    admin = api_as(world, "compute_admin")
    login_node(world)
    queue_run(world)
    (cluster,) = admin.get("/compute/slurm").json()
    assert cluster["name"] == "hpc1"
    assert cluster["workers_online"] == 1
    (template,) = cluster["templates"]
    assert (template["partition"], template["account"], template["max_concurrent"]) == (
        "gpu",
        "rd-shared",
        2,
    )
    assert template["price"]["per_gpu_hour"] == 1.10
    path = f"/compute/slurm/hpc1/accounts/{world.project.pk}"
    bad = admin.put(path, {"account": "rd proteins; rm"})
    assert bad.status_code == 422
    given = admin.put(path, {"account": "rd-proteins"})
    assert given.status_code == 200, given.content
    assert given.json()["account"] == "rd-proteins"
    assert admin.put(path, {"account": "rd-structure"}).status_code == 200
    (listed,) = admin.get("/compute/slurm/hpc1/accounts").json()["items"]
    assert listed["project"]["id"] == str(world.project.pk)
    assert admin.delete(path).status_code == 204
    events = AuditEvent.objects.filter(action="slurm_account.changed").order_by("id")
    assert [(e.data["before"], e.data["after"]) for e in events] == [
        (None, "rd-proteins"),
        ("rd-proteins", "rd-structure"),
        ("rd-structure", None),
    ]
    assert admin.get("/compute/slurm/hpc9/accounts").status_code == 404
    # Only Compute admins.
    assert api_as(world, "runner").get("/compute/slurm").status_code == 403
    assert api_as(world, "runner").put(path, {"account": "mine"}).status_code == 403


def test_a_worker_of_another_tier_cant_be_a_clusters(world: World) -> None:
    with pytest.raises(Exception, match="container-tier"):
        workers.create_registration(
            label="x", tier="vm", by=world.users["compute_admin"], slurm_cluster="hpc1"
        )
    assert not Worker.objects.exists()
