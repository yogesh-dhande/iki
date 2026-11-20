"""iki-worker on a real Slurm cluster (ADR 0133), in a container: slurmctld, slurmd and
slurmdbd, with Apptainer (deploy/dev/slurm). The worker runs on it as its login node's
service account, against the real server on this machine. It leases a run, submits it
with sbatch, follows it through squeue, runs it under Apptainer in its image, reads
sacct, and reports how it ended.

``make check-slurm`` builds the cluster's image and a Linux iki-worker, then runs these
with ``--slurm``. ``IKI_SLURM_E2E_IMAGE`` names the image, and ``IKI_SLURM_E2E_WORKER``
the binary.
"""

import hashlib
import http.client
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest
from conftest import S3_ACCESS_KEY, S3_SECRET_KEY
from iki_test_plugins import PLUGINS, ensure_built
from org_world import World, build_world
from real_server import Server, app_role, manage, run_tasks, serve, server_env
from test_api_entities import api_as
from test_runs import protein
from test_slurm import hpc

from iki_server.runs import catalog, workers
from iki_server.runs.models import ActionRun, CostRecord, Job, JobLog

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.slurm_e2e]

COLABFOLD = "acme/prediction.predict_with_colabfold"
#: The image its environment runs in, for these tests: a real one, which Apptainer pulls.
IMAGE = (
    "docker.io/library/python:3.13-alpine"
    "@sha256:1e1f6334529d1fb26a2ef619d7cffb30a7c7d7cc1cc7338ebee283a58d1aeaca"
)
SECRET_NAME = "LAB_TOKEN"
SECRET = "s3cr3t-cluster-token-0133"
#: Where the server is, as the cluster sees this machine.
HOST = "host.docker.internal"
PREFIX = f"slurm-e2e-{uuid.uuid4().hex}"
GI = 1024**3

#: Folds its protein, as far as these tests need: writes a PDB file of it, and says so.
FOLD = """
import json, os, sys, time
from pathlib import Path
run = json.loads(Path(os.environ["IKI_INPUT"]).read_text())
(protein,) = run["inputs"]["protein"]
print(f"Folding {protein['hrid']} under Python {sys.version.split()[0]}", flush=True)
print("host env:", "SLURM_JOB_ID" in os.environ, os.environ.get("HOME"), flush=True)
print("secret:", os.environ.get("LAB_TOKEN"), flush=True)
print("lease:", run["job"]["lease_token"], flush=True)
print("secrets file left:", (Path(os.environ["IKI_INPUT"]).parent / "secrets.json").exists())
time.sleep(float(os.environ.get("FOLD_SECONDS", "0")))
print("secret again:", os.environ.get("LAB_TOKEN"), flush=True)
pdb = "REMARK   1 FOLDED ON A SLURM CLUSTER\\nEND\\n"
(Path(os.environ["IKI_OUT_DIR"]) / "fold.pdb").write_text(pdb)
made = {"type": "acme/protein.ProteinStructure", "values": {
    "structure": {"$file": "fold.pdb", "media_type": "chemical/x-pdb"},
    "method": "predicted", "chains": [protein["id"]]}}
out = {"apiVersion": "iki/v1", "kind": "RunOutput", "creates": [made]}
Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(out))
print("Done.", flush=True)
"""


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"Set {name}: make check-slurm does.")
    return value


@dataclass
class Cluster:
    """The cluster's container, which is also its login node."""

    name: str
    workers: list["subprocess.Popen[str]"] = field(default_factory=list)

    def run(self, *argv: str, user: str = "iki", check: bool = True) -> str:
        done = subprocess.run(  # noqa: S603 - docker, with our arguments
            ["docker", "exec", "-u", user, self.name, *argv],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        if check:
            assert done.returncode == 0, done.stdout + done.stderr
        return done.stdout

    def sacct(self, slurm_id: str) -> dict[str, str]:
        fields = ["JobID", "JobName", "Comment", "Account", "Partition", "State", "User"]
        line = self.run(
            "sacct", "-X", "-n", "-P", "-j", slurm_id, "--format=" + ",".join(fields)
        ).strip()
        return dict(zip(fields, line.split("|"), strict=True))


@pytest.fixture(scope="module")
def cluster() -> Iterator[Cluster]:
    image = _required("IKI_SLURM_E2E_IMAGE")
    binary = Path(_required("IKI_SLURM_E2E_WORKER")).resolve()
    name = f"iki-slurm-e2e-{uuid.uuid4().hex[:8]}"
    started = subprocess.run(  # noqa: S603 - docker, with our arguments
        [  # noqa: S607
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--hostname",
            "slurm",
            "--platform",
            "linux/amd64",
            "--privileged",
            f"--add-host={HOST}:host-gateway",
            "-v",
            f"{binary}:/opt/iki/bin/iki-worker:ro",
            # The plugins' code: a test server's are built from local directories, whose
            # code only machines Iki starts fetch, so the login node has it, as
            # test_worker_e2e's workers do.
            "-v",
            f"{PLUGINS}:/opt/iki/plugins:ro",
            image,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert started.returncode == 0, started.stderr
    try:
        deadline = time.monotonic() + 240
        while True:
            logs = subprocess.run(  # noqa: S603
                ["docker", "logs", name],  # noqa: S607
                capture_output=True,
                text=True,
                check=False,
            )
            if "slurm ready" in logs.stdout:
                break
            running = subprocess.run(  # noqa: S603
                ["docker", "inspect", "-f", "{{.State.Running}}", name],  # noqa: S607
                capture_output=True,
                text=True,
                check=False,
            )
            if running.stdout.strip() != "true" or time.monotonic() > deadline:
                pytest.fail(f"The Slurm cluster didn't start:\n{logs.stdout}{logs.stderr}")
            time.sleep(1)
        yield Cluster(name)
    finally:
        # Slurm's own logs, for a failure's diagnosis (CI prints them).
        saved = Path(__file__).parents[2] / ".pytest_cache" / "slurm-e2e" / "logs"
        saved.mkdir(parents=True, exist_ok=True)
        subprocess.run(  # noqa: S603
            ["docker", "cp", f"{name}:/var/log/slurm/.", str(saved)],  # noqa: S607
            capture_output=True,
            check=False,
        )
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)  # noqa: S603,S607


def _with_image(plugins: Path) -> None:
    """The colabfold environment, in a real image."""
    environment = plugins / "structure_prediction" / "environments" / "colabfold.yaml"
    text = environment.read_text(encoding="utf-8")
    environment.write_text(re.sub(r"(?m)^image: .*$", f"image: {IMAGE}", text), encoding="utf-8")


def _for_cluster(url: str) -> str:
    """`url` on this machine, as the cluster's container reaches it."""
    parts = urlsplit(url)
    return urlunsplit(parts._replace(netloc=f"{HOST}:{parts.port}"))


@pytest.fixture(scope="module")
def compute_file(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """The deployment's compute, with colabfold on the `hpc1` cluster's `batch` partition."""
    data = json.loads(Path(os.environ["IKI_COMPUTE_FILE"]).read_text())
    data["templates"]["hpc-batch"] = hpc(
        name="hpc-batch",
        max_concurrent=4,
        slurm={
            "cluster": "hpc1",
            "partition": "batch",
            "account": "rd-shared",
            "max_walltime": 7200,
        },
    )
    data["bindings"]["acme/prediction.colabfold"] = {
        "environment": "acme/prediction.colabfold",
        "templates": [["hpc-batch", 0]],
    }
    path = tmp_path_factory.mktemp("compute") / "compute.json"
    path.write_text(json.dumps(data))
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("IKI_COMPUTE_FILE", str(path))
        catalog.compute.cache_clear()
        yield path
    catalog.compute.cache_clear()


@pytest.fixture(scope="module")
def server(
    django_db_setup: None,
    django_db_blocker: Any,
    tmp_path_factory: pytest.TempPathFactory,
    compute_file: Path,
    _s3_endpoint: str,
    _s3_bucket: str,
) -> Iterator[Server]:
    """The server, reachable from the cluster's container, with file storage."""
    variant = "slurm-" + hashlib.sha256(IMAGE.encode()).hexdigest()[:8]
    with django_db_blocker.unblock(), app_role() as role:
        plugins = os.pathsep.join(
            p for p in (str(ensure_built(variant, _with_image)), os.environ.get("PYTHONPATH")) if p
        )
        secrets = tmp_path_factory.mktemp("run-secrets")
        (secrets / f"acme__prediction.{SECRET_NAME}").write_text(SECRET + "\n", encoding="utf-8")
        env = server_env(
            role,
            tmp_path_factory.mktemp("static"),
            PYTHONPATH=plugins,
            IKI_HOST="0.0.0.0",  # noqa: S104 - the cluster's container connects from outside
            IKI_ALLOWED_HOSTS=f"127.0.0.1,{HOST}",
            IKI_COMPUTE_FILE=str(compute_file),
            IKI_RUN_SECRETS_DIR=str(secrets),
            IKI_FILES_BUCKET=_s3_bucket,
            IKI_FILES_PREFIX=PREFIX,
            IKI_FILES_ENDPOINT_URL=_s3_endpoint,
            IKI_FILES_PUBLIC_ENDPOINT_URL=_for_cluster(_s3_endpoint),
            AWS_ACCESS_KEY_ID=S3_ACCESS_KEY,
            AWS_SECRET_ACCESS_KEY=S3_SECRET_KEY,
        )
        env["IKI_PUBLIC_URL"] = f"http://{HOST}:{env['IKI_PORT']}"
        for command in (["collectstatic", "--noinput"], ["setup_storage"]):
            done = manage(*command, env=env)
            assert done.returncode == 0, done.stdout + done.stderr
        with serve(env) as running, run_tasks(env):
            yield running


@pytest.fixture
def world(
    server: Server, _s3_endpoint: str, _s3_bucket: str, settings: Any, monkeypatch: Any
) -> Iterator[World]:
    from iki_server.files import storage

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", S3_ACCESS_KEY)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", S3_SECRET_KEY)
    settings.IKI_FILES_BUCKET = _s3_bucket
    settings.IKI_FILES_PREFIX = PREFIX
    settings.IKI_FILES_ENDPOINT_URL = _s3_endpoint
    settings.IKI_FILES_PUBLIC_ENDPOINT_URL = _s3_endpoint
    storage.reset()
    catalog.compute.cache_clear()
    yield build_world()
    catalog.compute.cache_clear()
    storage.reset()


class LoginNode:
    """iki-worker on the cluster's login node, registered for `hpc1`, with the server at
    `url` (by default, the server itself)."""

    def __init__(
        self, cluster: Cluster, server: Server, token: str, url: str | None = None
    ) -> None:
        self.cluster = cluster
        self.url = url or f"http://{HOST}:{urlsplit(server.url).port}"
        self.env = {
            "IKI_URL": self.url,
            "IKI_ALLOW_HTTP": "1",
            "IKI_SLURM_WORK_DIR": "/scratch/iki",
            "IKI_PLUGINS_DIR": "/opt/iki/plugins",
            "IKI_SLURM_POLL_SECONDS": "1",
        }
        self.process: subprocess.Popen[str] | None = None
        # What it says, kept as it goes, for a failure's diagnosis.
        handle, path = tempfile.mkstemp(prefix="iki-slurm-worker-", suffix=".log")
        os.close(handle)
        self.said_path = Path(path)
        flags = [f"--env={k}={v}" for k, v in self.env.items()]
        done = subprocess.run(  # noqa: S603 - docker, with our arguments
            [  # noqa: S607
                "docker",
                "exec",
                "-u",
                "iki",
                *flags,
                cluster.name,
                "/opt/iki/bin/iki-worker",
                "register",
                "--token",
                token,
                "--name",
                "hpc1-login1",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert done.returncode == 0, done.stderr

    def start(self) -> None:
        flags = [f"--env={k}={v}" for k, v in self.env.items()]
        # The process keeps its own copy of the file.
        with self.said_path.open("a", encoding="utf-8") as said:
            self.process = subprocess.Popen(  # noqa: S603 - docker, with our arguments
                [  # noqa: S607
                    "docker",
                    "exec",
                    "-u",
                    "iki",
                    *flags,
                    self.cluster.name,
                    "sh",
                    "-c",
                    "echo $$ > /tmp/iki-worker.pid; exec /opt/iki/bin/iki-worker start",
                ],
                stdout=subprocess.DEVNULL,
                stderr=said,
                text=True,
            )
        self.cluster.workers.append(self.process)

    def said(self) -> str:
        return self.said_path.read_text(encoding="utf-8")

    def diagnosis(self, run: dict[str, Any]) -> str:
        """What the worker, Slurm and the server say, when a test times out."""
        job = Job.objects.filter(run_id=run["id"]).values("state", "worker_id", "template").first()
        shell = (
            "squeue -a -o '%i %j %u %a %T %R'; sinfo -N; "
            "tail -n 30 /var/log/slurm/slurmctld.log /var/log/slurm/slurmd.log; "
            "ls -la /scratch/iki/jobs/*/ 2>&1; tail -n 30 /scratch/iki/jobs/*/log 2>&1"
        )
        slurm = self.cluster.run("sh", "-c", shell, user="root", check=False)
        return (
            f"job: {job}\nlease: {leasing(run)}\nlog:\n{log_of(run)}\n"
            f"worker:\n{self.said()[-6000:]}\nslurm:\n{slurm[-6000:]}"
        )

    def stop(self, timeout: float = 60) -> str:
        """Stops it as systemd would (SIGTERM), and returns what it said."""
        assert self.process is not None
        self.cluster.run("sh", "-c", 'kill -TERM "$(cat /tmp/iki-worker.pid)"')
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
            pytest.fail(f"iki-worker didn't stop within {timeout}s:\n{self.said()}")
        said = self.said()
        assert self.process.returncode == 0, said
        return said


@pytest.fixture
def login_node(
    cluster: Cluster, server: Server, world: World
) -> Iterator[Callable[..., LoginNode]]:
    made: list[LoginNode] = []

    def register(url: str | None = None) -> LoginNode:
        _, token = workers.create_registration(
            label="hpc1 login nodes",
            tier="container",
            by=world.users["compute_admin"],
            slurm_cluster="hpc1",
        )
        node = LoginNode(cluster, server, token, url)
        made.append(node)
        return node

    yield register
    for node in made:
        if node.process is not None and node.process.poll() is None:
            node.stop()
    # Each test leaves the cluster as it found it.
    cluster.run("sh", "-c", "scancel -u iki; rm -rf /scratch/iki/jobs/*")


def leasing(run: dict[str, Any]) -> dict[str, Any]:
    """Which of a cluster worker's lease conditions the run's job meets, as this process
    sees them (its plugins are the unchanged build, so the bundle check may differ from
    the server's), and what they're decided from."""
    from iki_server.runs import checkpoints, queue, slurm
    from iki_server.runs.models import Worker

    job = Job.objects.select_related("run").get(run_id=run["id"])
    worker = Worker.objects.filter(slurm_cluster="hpc1").order_by("-pk").first()
    if worker is None:
        return {"worker": None}
    one = Job.objects.filter(pk=job.pk)
    leasable = slurm.leasable(worker)
    return {
        "job": {
            "environment": job.environment,
            "tier": job.tier,
            "resources": job.resources,
            "lifecycle": job.lifecycle,
            "labels": job.labels,
            "plugin_sha": job.run.plugin_sha,
            "timeout": job.run.timeout,
            "network": job.run.spec.get("network", "(none)"),
        },
        "worker": {"tier": worker.tier, "plugins": worker.plugins, "labels": worker.labels},
        "first": getattr(slurm.first(job), "name", None),
        "runnable": one.filter(queue._runnable(worker)).exists(),
        "leasable": leasable is not None and one.filter(leasable).exists(),
        "resumable": one.filter(checkpoints.ready_to_resume()).exists(),
    }


def launch(world: World, fold_seconds: float = 0, secret: bool = False) -> dict[str, Any]:
    """Launches colabfold on a protein, then sizes it for the test cluster, whose node has
    no GPUs, and gives it the test's command."""
    runner = api_as(world, "runner")
    made = runner.post(
        f"/actions/{COLABFOLD}/runs",
        {
            "project": str(world.project.pk),
            "inputs": {"protein": protein(runner, world.project)["id"]},
        },
    )
    assert made.status_code == 201, made.content
    run: dict[str, Any] = made.json()
    Job.objects.filter(run_id=run["id"]).update(resources={"cpu": 1, "memory": GI // 4})
    spec = ActionRun.objects.get(pk=run["id"]).spec
    spec["command"] = ["sh", "-c", f'FOLD_SECONDS={fold_seconds} exec python -c "$0"', FOLD]
    if secret:
        spec["secrets"] = [{"name": SECRET_NAME, "required": True, "description": ""}]
    ActionRun.objects.filter(pk=run["id"]).update(spec=spec)
    return run


def eventually(
    check: Callable[[], Any],
    timeout: float = 300,
    what: str = "it",
    explain: Callable[[], str] = str,
) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        found = check()
        if found:
            return found
        if time.monotonic() > deadline:
            pytest.fail(f"Timed out waiting for {what}.\n{explain()}")
        time.sleep(0.5)


def status(world: World, run: dict[str, Any]) -> str:
    return str(api_as(world).get(f"/runs/{run['id']}").json()["status"])


def attempt(world: World, run: dict[str, Any]) -> dict[str, Any]:
    (found,) = api_as(world).get(f"/runs/{run['id']}/attempts").json()
    return dict(found)


def log_of(run: dict[str, Any]) -> str:
    chunks = JobLog.objects.filter(job__run_id=run["id"]).order_by("job__attempt", "offset")
    return "".join(chunks.values_list("text", flat=True))


def ended(world: World, run: dict[str, Any], node: LoginNode) -> str:
    def finished() -> str | None:
        now = status(world, run)
        return now if now not in {"queued", "running"} else None

    return str(
        eventually(finished, what=f"run {run['id']} to end", explain=lambda: node.diagnosis(run))
    )


def started(run: dict[str, Any], node: LoginNode) -> None:
    eventually(
        lambda: "Folding" in log_of(run),
        what="the job to start on its node",
        explain=lambda: node.diagnosis(run),
    )


def test_a_run_goes_through_slurm_in_its_image_and_its_results_come_back(
    world: World, cluster: Cluster, login_node: Callable[[], LoginNode]
) -> None:
    # The project's runs are charged to an account of its own on the cluster.
    admin = api_as(world, "compute_admin")
    put = admin.put(f"/compute/slurm/hpc1/accounts/{world.project.pk}", {"account": "rd-protein"})
    assert put.status_code == 200, put.content
    run = launch(world, secret=True)
    node = login_node()
    node.start()
    assert ended(world, run, node) == "succeeded", node.diagnosis(run)
    said = node.stop()
    assert SECRET not in said
    log = log_of(run)
    assert "Submitted Slurm job" in log
    assert "Folding" in log and "under Python 3.13" in log
    # In its image, with none of the node's environment, and its secret, masked.
    assert "host env: False /scratch/iki/jobs/" in log
    assert "secret: ***" in log
    assert "secrets file left: False" in log
    assert SECRET not in log
    # The attempt's own credentials too.
    job = Job.objects.get(run_id=run["id"])
    assert "lease: ***" in log
    assert str(job.lease_token) not in log
    # Its result file went up from the login node.
    body = api_as(world).get(f"/runs/{run['id']}").json()
    folded = api_as(world).get(f"/entities/{body['effects'][0]['entity']}").json()
    file = api_as(world).get(f"/files/{folded['values']['structure']}").json()
    assert (file["name"], file["media_type"]) == ("fold.pdb", "chemical/x-pdb")
    # What Slurm says of it is on its attempt, from squeue and sacct.
    slurm = attempt(world, run)["slurm"]
    assert slurm["cluster"] == "hpc1"
    assert (slurm["partition"], slurm["account"]) == ("batch", "rd-protein")
    assert slurm["state"] == "COMPLETED"
    assert slurm["exit_code"] == "0:0"
    assert slurm["nodes"] == "slurm"
    assert slurm["requested"]["cpus"] == 1
    assert slurm["usage"]["elapsed"] >= 0
    assert slurm["usage"]["alloc"]["cpu"] >= 1
    assert slurm["submitted_at"] and slurm["started_at"] and slurm["ended_at"]
    assert slurm["image_signer"] == ""
    accounted = cluster.sacct(slurm["slurm_id"])
    assert accounted["Comment"] == f"iki:{job.pk}"
    assert accounted["Account"] == "rd-protein"
    assert accounted["User"] == "iki"
    # It costs what Slurm gave it, while it ran.
    assert CostRecord.objects.filter(job=job, slurm_job__isnull=False).exists()
    # Its directory went with it; its image stays for the next run.
    assert cluster.run("ls", "/scratch/iki/jobs").strip() == ""
    assert cluster.run("sh", "-c", "ls /scratch/iki/images/*.sif").strip()


def test_a_cancelled_run_is_scancelled(
    world: World, cluster: Cluster, login_node: Callable[[], LoginNode]
) -> None:
    run = launch(world, fold_seconds=600)
    node = login_node()
    node.start()
    started(run, node)
    cancelled = api_as(world, "runner").post(f"/runs/{run['id']}/cancel", {})
    assert cancelled.status_code == 200, cancelled.content
    assert ended(world, run, node) == "cancelled"
    slurm = attempt(world, run)["slurm"]
    assert cluster.sacct(slurm["slurm_id"])["State"].startswith("CANCELLED")
    node.stop()


def test_a_command_that_fails_fails_the_run(
    world: World, cluster: Cluster, login_node: Callable[[], LoginNode]
) -> None:
    run = launch(world)
    spec = ActionRun.objects.get(pk=run["id"]).spec
    spec["command"] = ["sh", "-c", "echo 'no luck'; exit 3"]
    ActionRun.objects.filter(pk=run["id"]).update(spec=spec)
    node = login_node()
    node.start()
    assert ended(world, run, node) == "failed"
    body = api_as(world).get(f"/runs/{run['id']}").json()
    assert body["error"]["message"] == "The command exited with status 3."
    assert "no luck" in log_of(run)
    assert attempt(world, run)["slurm"]["state"] == "FAILED"
    node.stop()


def test_a_login_node_restarting_leaves_its_jobs_running_and_picks_them_up(
    world: World, cluster: Cluster, login_node: Callable[[], LoginNode]
) -> None:
    run = launch(world, fold_seconds=20)
    node = login_node()
    node.start()
    started(run, node)
    said = node.stop()
    assert "the cluster's jobs run on" in said
    slurm_id = attempt(world, run)["slurm"]["slurm_id"]
    assert cluster.run("squeue", "-h", "-j", slurm_id, "-o", "%T").strip() == "RUNNING"
    assert status(world, run) == "running"
    node.start()
    assert ended(world, run, node) == "succeeded", node.diagnosis(run)
    said = node.stop()
    assert "Picked up a job on the cluster again." in said
    # One Slurm job, one attempt.
    assert len(api_as(world).get(f"/runs/{run['id']}/attempts").json()) == 1


def test_a_login_node_restarting_keeps_why_its_job_said_it_failed(
    world: World, cluster: Cluster, login_node: Callable[[], LoginNode]
) -> None:
    """The command says why it fails, the worker restarts, and the command then fails:
    the run says why, though the line was read before the restart and isn't read again."""
    run = launch(world)
    spec = ActionRun.objects.get(pk=run["id"]).spec
    why = "Record 3 (pUC19) has no bases"
    spec["command"] = ["sh", "-c", f"echo '::error {why}'; sleep 20; exit 3"]
    ActionRun.objects.filter(pk=run["id"]).update(spec=spec)
    node = login_node()
    node.start()
    eventually(
        lambda: why in log_of(run),
        what="the job to say why it fails",
        explain=lambda: node.diagnosis(run),
    )
    node.stop()
    node.start()
    assert ended(world, run, node) == "failed", node.diagnosis(run)
    said = node.stop()
    assert "Picked up a job on the cluster again." in said
    body = api_as(world).get(f"/runs/{run['id']}").json()
    assert body["error"]["message"] == f"{why} (exit status 3)"
    assert log_of(run).count(why) == 1


def test_a_cancel_reaches_a_cluster_job_while_its_heartbeats_fail(
    world: World, cluster: Cluster, server: Server, login_node: Callable[..., LoginNode]
) -> None:
    """Cancels reach a cluster's jobs by a watch of them all, besides their heartbeats
    (ADR 0167): with every heartbeat failing, a cancelled run's Slurm job is scancelled."""
    flaky = FlakyServer(server)
    try:
        run = launch(world, fold_seconds=600)
        node = login_node(flaky.url)
        node.start()
        started(run, node)
        flaky.failing = 10**9
        cancelled = api_as(world, "runner").post(f"/runs/{run['id']}/cancel", {})
        assert cancelled.status_code == 200, cancelled.content
        assert ended(world, run, node) == "cancelled", node.diagnosis(run)
        node.stop()
    finally:
        flaky.close()
    slurm = attempt(world, run)["slurm"]
    assert cluster.sacct(slurm["slurm_id"])["State"].startswith("CANCELLED")


def test_a_login_node_restarting_keeps_its_jobs_secrets_hidden_and_its_log_whole(
    world: World, cluster: Cluster, login_node: Callable[[], LoginNode]
) -> None:
    run = launch(world, fold_seconds=20, secret=True)
    node = login_node()
    node.start()
    eventually(
        lambda: "secrets file left" in log_of(run) and "started on" in log_of(run),
        what="the job to print its secret",
        explain=lambda: node.diagnosis(run),
    )
    node.stop()
    job = Job.objects.get(run_id=run["id"])
    # The job's log file on the cluster holds what it printed with its secrets hidden.
    on_disk = cluster.run("cat", f"/scratch/iki/jobs/{job.pk}/log")
    assert "secret: ***" in on_disk, on_disk
    assert SECRET not in on_disk and str(job.lease_token) not in on_disk
    node.start()
    assert ended(world, run, node) == "succeeded", node.diagnosis(run)
    node.stop()
    log = log_of(run)
    assert SECRET not in log and str(job.lease_token) not in log
    # Once each: the log carries on where it was, rather than starting again.
    printed = ("Folding", "secret: ***", "lease: ***", "secret again: ***", "Done.", "started on")
    for line in printed:
        assert log.count(line) == 1, (line, log)


class FlakyServer:
    """The server, through a proxy on this machine that answers 503 to the first
    `failing` job heartbeats once armed, as a server restarting or overloaded would."""

    def __init__(self, server: Server) -> None:
        target = urlsplit(server.url)
        host, port = target.hostname or "127.0.0.1", target.port
        self.failing = 0
        self.failed = 0
        flaky = self

        class Forward(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _forward(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path.endswith("/heartbeat") and flaky.failing > 0:
                    flaky.failing -= 1
                    flaky.failed += 1
                    self._answer(503, {"Content-Type": "application/json"}, b'{"detail": "Busy."}')
                    return
                upstream = http.client.HTTPConnection(host, port, timeout=120)
                headers = {k: v for k, v in self.headers.items() if k.lower() != "connection"}
                upstream.request(self.command, self.path, body=body, headers=headers)
                reply = upstream.getresponse()
                data = reply.read()
                skip = {"connection", "transfer-encoding", "content-length", "keep-alive"}
                kept = {k: v for k, v in reply.getheaders() if k.lower() not in skip}
                upstream.close()
                self._answer(reply.status, kept, data)

            def _answer(self, status: int, headers: dict[str, str], data: bytes) -> None:
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_DELETE = _forward  # noqa: N815

            def log_message(self, *args: Any) -> None:
                pass

        self.http = ThreadingHTTPServer(("0.0.0.0", 0), Forward)  # noqa: S104 - the cluster's container
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.url = f"http://{HOST}:{self.http.server_address[1]}"

    def close(self) -> None:
        self.http.shutdown()
        self.http.server_close()


def test_a_login_node_keeps_its_job_through_a_server_that_cant_answer_as_it_starts_again(
    world: World, cluster: Cluster, server: Server, login_node: Callable[..., LoginNode]
) -> None:
    # The worker reaches the server through the proxy from the start: it's registered, and
    # signs in, there.
    flaky = FlakyServer(server)
    try:
        run = launch(world, fold_seconds=20)
        node = login_node(flaky.url)
        node.start()
        started(run, node)
        node.stop()
        flaky.failing = 3
        node.start()
        assert ended(world, run, node) == "succeeded", node.diagnosis(run)
        said = node.stop()
    finally:
        flaky.close()
    assert flaky.failed == 3
    assert "Couldn't renew a job's lease after starting again; trying again." in said
    assert "Picked up a job on the cluster again." in said
    # The same Slurm job and attempt, which ran to the end.
    assert len(api_as(world).get(f"/runs/{run['id']}/attempts").json()) == 1
    assert attempt(world, run)["slurm"]["state"] == "COMPLETED"
