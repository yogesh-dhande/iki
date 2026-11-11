"""iki-worker against the real server (ADR 0043): the Rust binary registers, leases a job
over HTTP, runs the action's command, streams its log and progress, stops it when asked,
and reports how it ended.

The binary is built with cargo, unless ``IKI_WORKER_BIN`` names one. Without cargo these
tests skip, except in CI, where they fail.
"""

import os
import runpy
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import requests
from conftest import S3_ACCESS_KEY, S3_SECRET_KEY
from iki_test_plugins import ensure_built
from org_world import World, build_world
from real_server import Server, app_role, manage, run_tasks, serve, server_env
from test_api_entities import api_as, create
from test_files import api_as as browser_api
from test_files import send
from test_files import start as start_upload
from test_runs import launch, protein
from test_workers import queued, registration

from iki_server.runs import catalog
from iki_server.runs.models import ActionRun, Job, JobLog, JobState, Worker

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.worker_e2e]

RELAX = "protein.relax_structure"

ROOT = Path(__file__).parents[2]
PLUGINS = ROOT / "spec" / "tests" / "plugins"
#: Where this module's files go in the test store, for the server and the test alike.
PREFIX = f"worker-e2e-{uuid.uuid4().hex}"


def _require(what: bool, message: str) -> None:
    if what:
        return
    if os.environ.get("CI"):
        pytest.fail(message)
    pytest.skip(message)


@pytest.fixture(scope="module")
def worker_bin() -> Path:
    named = os.environ.get("IKI_WORKER_BIN")
    if named:
        return Path(named)
    cargo = shutil.which("cargo")
    _require(cargo is not None, "Install Rust (https://rustup.rs) to build iki-worker")
    assert cargo is not None
    built = subprocess.run(  # noqa: S603 - fixed argv
        [cargo, "build", "--locked", "--quiet", "-p", "iki-worker"],
        cwd=ROOT / "worker",
        capture_output=True,
        text=True,
        check=False,
        timeout=900,
    )
    assert built.returncode == 0, built.stderr
    return ROOT / "worker" / "target" / "debug" / "iki-worker"


@pytest.fixture(scope="module")
def server(
    django_db_setup: None,
    django_db_blocker: Any,
    tmp_path_factory: pytest.TempPathFactory,
    _s3_endpoint: str,
) -> Iterator[Server]:
    """The server, with file storage, and a task runner beside it (it checks uploads)."""
    with django_db_blocker.unblock(), app_role() as role:
        plugins = os.pathsep.join(
            p for p in (str(ensure_built()), os.environ.get("PYTHONPATH")) if p
        )
        env = server_env(
            role,
            tmp_path_factory.mktemp("static"),
            PYTHONPATH=plugins,
            IKI_FILES_BUCKET="iki",
            IKI_FILES_PREFIX=PREFIX,
            IKI_FILES_ENDPOINT_URL=_s3_endpoint,
            AWS_ACCESS_KEY_ID=S3_ACCESS_KEY,
            AWS_SECRET_ACCESS_KEY=S3_SECRET_KEY,
        )
        env["IKI_PUBLIC_URL"] = f"http://127.0.0.1:{env['IKI_PORT']}"
        for command in (["collectstatic", "--noinput"], ["setup_storage"]):
            done = manage(*command, env=env)
            assert done.returncode == 0, done.stdout + done.stderr
        with serve(env) as running, run_tasks(env):
            yield running


@pytest.fixture
def world(
    server: Server, _s3_endpoint: str, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> Iterator[World]:
    from iki_server.files import storage

    # The test adds files as the server would, to the same store.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", S3_ACCESS_KEY)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", S3_SECRET_KEY)
    settings.IKI_FILES_BUCKET = "iki"
    settings.IKI_FILES_PREFIX = PREFIX
    settings.IKI_FILES_ENDPOINT_URL = _s3_endpoint
    settings.IKI_FILES_PUBLIC_ENDPOINT_URL = _s3_endpoint
    storage.reset()
    catalog.compute.cache_clear()
    yield build_world()
    catalog.compute.cache_clear()
    storage.reset()


class Running:
    """An ``iki-worker start`` process, and what it says on stderr."""

    def __init__(self, process: "subprocess.Popen[str]") -> None:
        self.process = process

    def wait(self, timeout: float = 60) -> tuple[int, str]:
        try:
            _, err = self.process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            _, err = self.process.communicate()
            pytest.fail(f"iki-worker didn't stop within {timeout}s:\n{err}")
        return self.process.returncode, err


@pytest.fixture
def start(worker_bin: Path, server: Server, tmp_path: Path) -> Iterator[Callable[..., Running]]:
    started: list[Running] = []

    def start_worker(token: str, *args: str, plugins: Path = PLUGINS) -> Running:
        python = Path(sys.executable).parent
        env = {
            "PATH": f"{python}{os.pathsep}{os.environ.get('PATH', '')}",
            "IKI_URL": server.url,
            "IKI_REGISTRATION_TOKEN": token,
            "IKI_PLUGINS_DIR": str(plugins),
            "IKI_WORK_DIR": str(tmp_path / "work"),
            "IKI_WORKER_NAME": "e2e-worker",
            "IKI_WORKER_CPU": "4",
            "IKI_WORKER_MEMORY": "8Gi",
        }
        process = subprocess.Popen(  # noqa: S603 - our own binary
            [str(worker_bin), "start", *args],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        running = Running(process)
        started.append(running)
        return running

    yield start_worker
    for running in started:
        if running.process.poll() is None:
            running.process.kill()
            running.process.communicate()


def eventually(check: Callable[[], Any], timeout: float = 30) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        found = check()
        if found:
            return found
        if time.monotonic() > deadline:
            pytest.fail("Timed out waiting.")
        time.sleep(0.1)


def log_of(run: dict[str, Any]) -> str:
    chunks = JobLog.objects.filter(job__run_id=run["id"]).order_by("job__attempt", "offset")
    return "".join(chunks.values_list("text", flat=True))


def with_script(tmp_path: Path, script: str) -> Path:
    """The protein plugin, with its command replaced by `script`."""
    plugins = tmp_path / "plugins"
    shutil.copytree(PLUGINS / "protein", plugins / "protein")
    (plugins / "protein" / "scripts" / "properties.py").write_text(script, encoding="utf-8")
    return plugins


#: Prints, then waits to be stopped; says so on SIGTERM.
WAITS = """
import signal, sys, time
def stop(*_):
    print("stopping", flush=True)
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
print("started", flush=True)
time.sleep(600)
"""


def running(run: dict[str, Any]) -> bool:
    return "started" in log_of(run)


def test_a_worker_runs_a_job_and_its_results_are_written(
    world: World, start: Callable[..., Running]
) -> None:
    run = queued(world)
    code, said = start(registration(world), "--once").wait()
    assert code == 0, said
    assert "Reported the job's results." in said
    body = api_as(world).get(f"/runs/{run['id']}").json()
    assert body["status"] == "succeeded", body
    protein = api_as(world).get(f"/entities/{run['inputs'][0]['entity']}").json()
    sequence = protein["values"]["sequence"]
    expected = runpy.run_path(str(PLUGINS / "protein/scripts/properties.py"))["weight"](sequence)
    assert protein["values"]["molecular_weight"] == pytest.approx(expected)
    log = log_of(run)
    assert f"{len(sequence)} residues" in log
    assert "Done." in log
    job = Job.objects.get(run_id=run["id"])
    assert (job.progress, job.progress_message) == (1.0, "Weighed 1 of 1")
    # Its token went with it: it registered for this process alone.
    assert Worker.objects.get(name="e2e-worker").revoked_at is not None


def test_a_command_that_fails_fails_the_run_with_its_log(
    world: World, start: Callable[..., Running], tmp_path: Path
) -> None:
    run = queued(world)
    plugins = with_script(tmp_path, "import sys\nprint('no luck')\nsys.exit(3)\n")
    code, said = start(registration(world), "--once", plugins=plugins).wait()
    assert code == 0, said
    body = api_as(world).get(f"/runs/{run['id']}").json()
    assert body["status"] == "failed", body
    assert body["error"]["message"] == "The command exited with status 3."
    assert "no luck" in log_of(run)


def test_a_cancel_stops_the_command_at_once(
    world: World, start: Callable[..., Running], tmp_path: Path
) -> None:
    run = queued(world)
    worker = start(registration(world), "--once", plugins=with_script(tmp_path, WAITS))
    eventually(lambda: running(run))
    cancelled = time.monotonic()
    assert api_as(world, "editor").post(f"/runs/{run['id']}/cancel", {}).status_code == 200
    code, said = worker.wait()
    # The watch answers at once: no waiting for the next heartbeat, 20 seconds away.
    assert time.monotonic() - cancelled < 10, said
    assert code == 0, said
    assert api_as(world).get(f"/runs/{run['id']}").json()["status"] == "cancelled"
    assert "stopping" in log_of(run)


def test_a_command_past_its_timeout_is_stopped(
    world: World, start: Callable[..., Running], tmp_path: Path
) -> None:
    run = queued(world)
    ActionRun.objects.filter(pk=run["id"]).update(timeout=1)
    code, said = start(registration(world), "--once", plugins=with_script(tmp_path, WAITS)).wait()
    assert code == 0, said
    body = api_as(world).get(f"/runs/{run['id']}").json()
    assert body["status"] == "timed_out", body
    assert body["error"]["message"] == "Stopped: it ran past its timeout of 1 second."
    assert "stopping" in log_of(run)


def test_stopping_the_worker_interrupts_its_job(
    world: World, start: Callable[..., Running], tmp_path: Path
) -> None:
    run = queued(world)
    worker = start(registration(world), plugins=with_script(tmp_path, WAITS))
    eventually(lambda: running(run))
    worker.process.send_signal(signal.SIGTERM)
    code, said = worker.wait()
    assert code == 0, said
    job = Job.objects.filter(run_id=run["id"]).order_by("attempt").first()
    assert job is not None
    assert job.state == JobState.INTERRUPTED
    assert "stopping" in log_of(run)
    assert "Deregistered." in said


def test_a_worker_without_the_plugin_says_so(
    world: World, start: Callable[..., Running], tmp_path: Path
) -> None:
    run = queued(world)
    (tmp_path / "empty").mkdir()
    code, said = start(registration(world), "--once", plugins=tmp_path / "empty").wait()
    assert code == 0, said
    body = api_as(world).get(f"/runs/{run['id']}").json()
    assert body["status"] == "failed", body
    assert body["error"]["message"].startswith("This worker has no code for the plugin protein")
    assert "Couldn't remove" not in said


def test_input_files_are_staged_and_result_files_uploaded(
    world: World, start: Callable[..., Running]
) -> None:
    editor = api_as(world, "editor")
    pdb = b"ATOM      1  N   MET A   1      27.340  24.430   2.614  1.00  9.67           N\n"
    uploads = browser_api(world)
    started = start_upload(uploads, world, pdb, name="abl.pdb").json()
    send(started, data=pdb)
    uploads.post(f"/uploads/{started['id']}/complete")
    # The task runner beside the server checks it.
    added = eventually(
        lambda: (u := uploads.get(f"/uploads/{started['id']}").json())["state"] == "ready" and u
    )
    abl = protein(editor, world.project)
    values = {"structure": added["file"]["id"], "method": "xray", "chains": [abl["id"]]}
    structure = create(editor, world.project, values, "protein.ProteinStructure")
    assert structure.status_code == 201, structure.content
    made = launch(editor, world.project, {"structure": structure.json()["id"]}, RELAX)
    assert made.status_code == 201, made.content
    run = made.json()
    code, said = start(registration(world), "--once").wait(120)
    assert code == 0, said
    assert "Staged the input files." in said
    assert "Uploaded the result files." in said
    body = api_as(world).get(f"/runs/{run['id']}").json()
    assert body["status"] == "succeeded", body
    assert f"Read abl.pdb: {len(pdb)} bytes" in log_of(run)
    relaxed = api_as(world).get(f"/entities/{body['effects'][0]['entity']}").json()
    file_id = relaxed["values"]["structure"]
    assert file_id != added["file"]["id"]
    file = api_as(world).get(f"/files/{file_id}").json()
    assert (file["name"], file["media_type"]) == ("relaxed.pdb", "chemical/x-pdb")
    link = api_as(world).get(f"/files/{file_id}/content")
    assert link.status_code == 302
    fetched = requests.get(link["Location"], timeout=30)
    assert fetched.content == b"REMARK   1 RELAXED BY IKI EXAMPLE\n" + pdb
