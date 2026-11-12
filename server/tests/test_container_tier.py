"""The container tier (ADR 0061): the provisioner starts a Kubernetes Job's pod for each
queued run, its worker registers with a token made for that pod alone and runs one job, and
what Kubernetes reports is reconciled with what Iki launched. Kubernetes here is a fake
that keeps what it's asked for; the dev cluster's end-to-end checks use a real one."""

import base64
import json
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from api_client import problem
from django.db.models import F
from django.utils import timezone
from org_world import World, build_world
from test_api_entities import api_as
from test_runs import TEMPLATE, protein
from test_workers import WorkerClient

from iki_server.audit.models import AuditEvent
from iki_server.runs import catalog, provisioner
from iki_server.runs.kubernetes import LABEL, KubernetesError
from iki_server.runs.models import ActionRun, Job, JobState, Launch, LaunchState, RunStatus, Worker

pytestmark = pytest.mark.django_db

COLABFOLD = "structure_prediction.predict_with_colabfold"
IMAGE = (
    "ghcr.io/acme/colabfold@sha256:2c26b46b68ffc68ff99b453c1d30413413422d706483bfa0f98a5e886266e7ae"
)
WORKER_IMAGE = "ghcr.io/yogesh-dhande/iki-worker:0.1.0@sha256:" + "b" * 64
GI = 1024**3


@pytest.fixture
def world(settings: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[World]:
    settings.IKI_RUNS_NAMESPACE = "iki-runs"
    settings.IKI_RUNS_NAMESPACES = ["gpu-runs"]
    settings.IKI_RUNS_WORKER_IMAGE = WORKER_IMAGE
    settings.IKI_RUNS_SERVER_URL = "https://iki.example.com"
    compute = json.loads(Path(catalog._compute_file()).read_text())  # type: ignore[arg-type]
    compute["templates"]["gpu-t4"] = {
        **TEMPLATE,
        "name": "gpu-t4",
        "provisioner": "k8s_job",
        "lifecycle": "ephemeral",
        "capacity": {
            "cpu": 4,
            "memory": 16 * GI,
            "gpu_count": 1,
            "gpu_memory": 16 * GI,
            "scratch_disk": 50 * GI,
        },
        "max_concurrent": 3,
        "kubernetes": {
            "node_selector": {"iki.ikisci.org/pool": "gpu"},
            "tolerations": [
                {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}
            ],
            "runtime_class": "nvidia",
        },
    }
    compute["bindings"]["structure_prediction.colabfold"] = {
        "environment": "structure_prediction.colabfold",
        "templates": [["gpu-t4", 0]],
    }
    file = tmp_path / "with-containers.json"
    file.write_text(json.dumps(compute))
    monkeypatch.setenv("IKI_COMPUTE_FILE", str(file))
    catalog.compute.cache_clear()
    yield build_world()
    catalog.compute.cache_clear()


@pytest.fixture(autouse=True)
def keep_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    from iki_server.realtime import views

    monkeypatch.setattr(views, "close_old_connections", lambda: None)


class FakeCluster:
    """Keeps the Jobs, Secrets and pods the provisioner makes, as Kubernetes would."""

    def __init__(self) -> None:
        self.made: dict[tuple[str, str], dict[str, Any]] = {}
        self.secrets: dict[tuple[str, str], dict[str, Any]] = {}
        self.pod_status: dict[tuple[str, str], dict[str, Any]] = {}
        self.refuse: str | None = None
        self.deleted: list[str] = []

    def create_job(self, namespace: str, job: dict[str, Any], secret: dict[str, Any]) -> None:
        if self.refuse:
            raise KubernetesError(403, self.refuse)
        name = job["metadata"]["name"]
        self.secrets[(namespace, secret["metadata"]["name"])] = secret
        self.made[(namespace, name)] = {**job, "status": {}}

    def jobs(self, namespace: str) -> list[dict[str, Any]]:
        return [job for (ns, _), job in self.made.items() if ns == namespace]

    def pods(self, namespace: str) -> list[dict[str, Any]]:
        return [
            {
                "metadata": {
                    "name": f"{name}-x1y2z",
                    "labels": job["spec"]["template"]["metadata"]["labels"],
                    "creationTimestamp": status.get("created"),
                },
                "status": status.get("status", {}),
            }
            for (ns, name), job in self.made.items()
            if ns == namespace and (status := self.pod_status.get((ns, name), {})) is not None
        ]

    def delete_job(self, namespace: str, name: str) -> None:
        self.made.pop((namespace, name), None)
        self.secrets.pop((namespace, name), None)
        self.deleted.append(name)

    # What happens to the pods.

    def only(self) -> tuple[str, dict[str, Any]]:
        ((_, name), job), *rest = self.made.items()
        assert not rest, list(self.made)
        return name, job

    def token(self, name: str, namespace: str = "iki-runs") -> str:
        return base64.b64decode(self.secrets[(namespace, name)]["data"]["token"]).decode()

    def waiting(self, name: str, reason: str, message: str, since: datetime) -> None:
        self.pod_status[("iki-runs", name)] = {
            "created": since.isoformat(),
            "status": {
                "containerStatuses": [
                    {"name": "run", "state": {"waiting": {"reason": reason, "message": message}}}
                ]
            },
        }

    def unschedulable(self, name: str, message: str) -> None:
        self.pod_status[("iki-runs", name)] = {
            "status": {
                "conditions": [
                    {
                        "type": "PodScheduled",
                        "status": "False",
                        "reason": "Unschedulable",
                        "message": message,
                    }
                ]
            }
        }

    def finish(self, name: str, *, ok: bool = True) -> None:
        condition = {"type": "Complete" if ok else "Failed", "status": "True"}
        if not ok:
            condition["message"] = "Pod failed: its worker exited 1."
        self.made[("iki-runs", name)]["status"] = {"conditions": [condition]}


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


def register(token: str, name: str = "iki-run-pod") -> Any:
    return WorkerClient(token).post(
        "/register",
        {
            "name": name,
            # Whatever the pod says it has, it gets the template's.
            "version": "0.1.0",
            "capacity": {"cpu": 64, "memory": 512 * GI, "gpu_count": 8},
            "plugins": [{"name": "dna", "sha": None, "environments": None}],
        },
    )


def later(minutes: float) -> datetime:
    return timezone.now() + timedelta(minutes=minutes)


# --- Starting pods ---------------------------------------------------------------------------


def test_a_queued_run_gets_a_pod_that_runs_its_image_with_iki_worker(world: World) -> None:
    run = queue_run(world)
    cluster = FakeCluster()
    assert provisioner.tick(cluster) == (1, 0)
    name, job = cluster.only()
    launch = Launch.objects.get()
    assert (launch.name, launch.template, launch.environment, launch.image) == (
        name,
        "gpu-t4",
        "structure_prediction.colabfold",
        IMAGE,
    )
    assert launch.state == LaunchState.STARTING
    assert job["metadata"]["labels"][LABEL] == str(launch.pk)
    assert job["spec"]["backoffLimit"] == 0
    pod = job["spec"]["template"]["spec"]
    (init,) = pod["initContainers"]
    (main,) = pod["containers"]
    # Iki's signed worker copies itself in; the run's own image runs it.
    assert init["image"] == WORKER_IMAGE
    assert init["command"] == ["iki-worker", "install", "/iki/bin"]
    assert main["image"] == IMAGE
    assert main["command"] == ["/iki/bin/iki-worker", "start", "--once"]
    assert {"name": "iki-bin", "mountPath": "/iki/bin", "readOnly": True} in main["volumeMounts"]
    # Exactly the template's size, on its nodes.
    wanted = {
        "cpu": "4000m",
        "memory": str(16 * GI),
        "ephemeral-storage": str(50 * GI),
        "nvidia.com/gpu": "1",
    }
    assert main["resources"] == {"requests": wanted, "limits": wanted}
    assert pod["nodeSelector"] == {"iki.ikisci.org/pool": "gpu"}
    assert pod["tolerations"][0]["key"] == "nvidia.com/gpu"
    assert pod["runtimeClassName"] == "nvidia"
    # PodSecurity "restricted", and nothing of Kubernetes' API.
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    for container in (init, main):
        assert container["securityContext"] == {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        }
    env = {e["name"]: e.get("value", e.get("valueFrom")) for e in main["env"]}
    assert env["IKI_URL"] == "https://iki.example.com"
    assert env["IKI_WORKER_ENVIRONMENT"] == "structure_prediction.colabfold"
    assert env["IKI_WORKER_IMAGE"] == IMAGE
    assert env["IKI_REGISTRATION_TOKEN"] == {"secretKeyRef": {"name": name, "key": "token"}}
    assert "IKI_ALLOW_HTTP" not in env
    # The token is in the Secret alone, and only its hash in the database.
    token = cluster.token(name)
    assert token.startswith("iki_wlch_")
    assert token not in json.dumps(job)
    assert launch.token_hash != token
    event = AuditEvent.objects.get(action="worker.launched")
    assert event.data["image"] == IMAGE
    assert token not in json.dumps(event.data)
    # A pod is already starting for it.
    assert provisioner.tick(cluster) == (0, 0)
    assert ActionRun.objects.get(pk=run["id"]).status == RunStatus.QUEUED


def test_no_more_pods_start_than_the_template_allows(world: World) -> None:
    for i in range(5):
        queue_run(world, name="ACDEFGHIK"[: i + 1])
    cluster = FakeCluster()
    assert provisioner.tick(cluster) == (3, 0)
    assert provisioner.tick(cluster) == (0, 0)
    assert Launch.objects.count() == 3


def test_a_template_in_another_namespace_runs_there_if_allowed(world: World, settings: Any) -> None:
    compute = catalog.compute()
    compute.templates["gpu-t4"].kubernetes["namespace"] = "gpu-runs"
    queue_run(world)
    cluster = FakeCluster()
    assert provisioner.tick(cluster) == (1, 0)
    assert next(iter(cluster.made))[0] == "gpu-runs"
    settings.IKI_RUNS_NAMESPACES = []
    queue_run(world, name="ACDEF")
    assert provisioner.tick(cluster) == (0, 0)


def test_when_kubernetes_refuses_the_pod_it_backs_off(world: World) -> None:
    queue_run(world)
    cluster = FakeCluster()
    cluster.refuse = 'jobs.batch is forbidden: User "system:serviceaccount:iki:iki-provisioner"'
    assert provisioner.tick(cluster) == (0, 0)
    launch = Launch.objects.get()
    assert (launch.state, launch.ended_reason) == (LaunchState.ENDED, "failed")
    assert "forbidden" in launch.message
    cluster.refuse = None
    # It waits before trying again, and longer each time.
    assert provisioner.tick(cluster) == (0, 0)
    assert provisioner.tick(cluster, later(1)) == (1, 0)


# --- The pod's worker ------------------------------------------------------------------------


def test_the_pods_worker_runs_one_job_of_its_environment_with_its_image(world: World) -> None:
    queue_run(world)
    cluster = FakeCluster()
    provisioner.tick(cluster)
    name, _ = cluster.only()
    token = cluster.token(name)
    made = register(token, name=f"{name}-x1y2z")
    assert made.status_code == 201, made.content
    assert made.json()["tier"] == "container"
    # The token is spent.
    assert register(token).status_code == 401
    worker = Worker.objects.get(pk=made.json()["id"])
    assert worker.capacity == {
        "cpu": 4,
        "memory": 16 * GI,
        "gpu_count": 1,
        "gpu_memory": 16 * GI,
        "scratch_disk": 50 * GI,
    }
    digest = IMAGE.rpartition("@")[2]
    assert worker.plugins == [
        {"name": "structure_prediction", "sha": "", "environments": {"colabfold": digest}}
    ]
    launch = Launch.objects.get()
    assert launch.state == LaunchState.RUNNING
    # Another environment's runs aren't for it, even on its tier.
    pod = WorkerClient(made.json()["token"])
    leased = pod.lease()
    assert leased.status_code == 200, leased.content
    job = Job.objects.get(pk=leased.json()["job"]["id"])
    assert (job.template, job.environment_build) == ("gpu-t4", digest)
    # One job, then it's done.
    queue_run(world, name="ACDEF")
    assert pod.lease().status_code == 204
    # Stopping ends its launch; the pod's Job finishes and Kubernetes removes it.
    assert pod.post("/deregister").status_code == 204
    launch.refresh_from_db()
    assert (launch.state, launch.ended_reason) == (LaunchState.ENDED, "finished")


def test_launch_tokens_work_once_and_not_for_long(world: World) -> None:
    queue_run(world)
    cluster = FakeCluster()
    provisioner.tick(cluster)
    name, _ = cluster.only()
    Launch.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    problem(register(cluster.token(name)), 401, "not_authenticated")
    # The pod that never registered is ended, and another started for the run.
    assert provisioner.tick(cluster, later(1)) == (1, 1)
    ended = Launch.objects.get(name=name)
    assert ended.ended_reason == "never_started"
    assert name in cluster.deleted


# --- Reconciling -----------------------------------------------------------------------------


def test_a_pod_that_cant_pull_its_image_is_ended_and_its_runs_fail_in_the_end(
    world: World,
) -> None:
    run = queue_run(world)
    cluster = FakeCluster()
    for attempt in range(provisioner.GIVE_UP_AFTER):
        started, _ = provisioner.tick(cluster)
        assert started == 1, attempt
        name, _ = cluster.only()
        pulling = 'Back-off pulling image "colabfold"'
        # A registry hiccup gets a little while to clear.
        cluster.waiting(name, "ImagePullBackOff", pulling, timezone.now())
        assert provisioner.tick(cluster) == (0, 0)
        stuck = timezone.now() - provisioner.IMAGE_GRACE - timedelta(seconds=1)
        cluster.waiting(name, "ImagePullBackOff", pulling, stuck)
        # Ended, and no other pod tried straight away.
        assert provisioner.tick(cluster) == (0, 1)
        assert name in cluster.deleted
        Launch.objects.update(ended_at=F("ended_at") - timedelta(minutes=5))
    failed = ActionRun.objects.get(pk=run["id"])
    assert failed.status == RunStatus.FAILED
    assert failed.error is not None
    assert failed.error["code"] == "machine_unavailable"
    assert IMAGE in failed.error["message"]
    assert "couldn't be pulled" in failed.error["message"]
    reasons = set(Launch.objects.values_list("ended_reason", flat=True))
    assert reasons == {"image_unavailable"}


def test_a_running_pod_that_disappears_interrupts_its_job(world: World) -> None:
    run = queue_run(world)
    cluster = FakeCluster()
    provisioner.tick(cluster)
    name, _ = cluster.only()
    made = register(cluster.token(name))
    pod = WorkerClient(made.json()["token"])
    assert pod.lease().status_code == 200
    # Someone deleted the Job, or its node went away with it.
    cluster.made.clear()
    _, ended = provisioner.tick(cluster, later(2))
    assert ended == 1
    launch = Launch.objects.get(name=name)
    assert launch.ended_reason == "gone"
    assert Worker.objects.get(pk=made.json()["id"]).revoked_at is not None
    attempt = Job.objects.get(run_id=run["id"], attempt=1)
    assert attempt.state == JobState.INTERRUPTED
    assert pod.lease().status_code == 401


def test_a_finished_job_ends_its_launch_and_a_stray_one_is_deleted(world: World) -> None:
    queue_run(world)
    cluster = FakeCluster()
    provisioner.tick(cluster)
    name, job = cluster.only()
    cluster.finish(name, ok=False)
    provisioner.tick(cluster)
    launch = Launch.objects.get(name=name)
    assert (launch.ended_reason, launch.message) == ("failed", "Pod failed: its worker exited 1.")
    # A Job Iki didn't launch, or whose launch ended, goes.
    stray = json.loads(json.dumps(job))
    stray["metadata"]["name"] = "iki-run-stray"
    stray["metadata"]["labels"][LABEL] = "0190d2a6-0000-7000-8000-000000000000"
    stray["status"] = {}
    cluster.made[("iki-runs", "iki-run-stray")] = stray
    provisioner.reconcile(cluster, later(1))
    assert "iki-run-stray" in cluster.deleted


def test_a_run_says_what_its_pod_is_waiting_for(world: World) -> None:
    run = queue_run(world)
    cluster = FakeCluster()
    provisioner.tick(cluster)
    name, _ = cluster.only()
    cluster.unschedulable(name, "0/3 nodes are available: 3 Insufficient nvidia.com/gpu.")
    provisioner.tick(cluster)
    got = api_as(world, "runner").get(f"/runs/{run['id']}").json()
    assert got["waiting"] == (
        "Waiting for a machine: 0/3 nodes are available: 3 Insufficient nvidia.com/gpu."
    )
