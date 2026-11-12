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
SECRET_NAME = "LAB_TOKEN"
SECRET = "s3cr3t-lab-token-0451"

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
        secrets = tmp_path_factory.mktemp("run-secrets")
        (secrets / f"protein.{SECRET_NAME}").write_text(SECRET + "\n", encoding="utf-8")
        env = server_env(
            role,
            tmp_path_factory.mktemp("static"),
            PYTHONPATH=plugins,
            IKI_COMPUTE_FILE=os.environ["IKI_COMPUTE_FILE"],
            IKI_RUN_SECRETS_DIR=str(secrets),
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


def test_a_worker_runs_a_batchs_runs_in_one_job(
    world: World, start: Callable[..., Running]
) -> None:
    from test_run_batches import batch

    launched = batch(world)
    code, said = start(registration(world), "--once").wait()
    assert code == 0, said
    weigh = runpy.run_path(str(PLUGINS / "protein/scripts/properties.py"))["weight"]
    for run in launched["runs"]:
        body = api_as(world).get(f"/runs/{run['id']}").json()
        assert body["status"] == "succeeded", log_of(launched["runs"][0])
        protein = api_as(world).get(f"/entities/{run['inputs'][0]['entity']}").json()
        assert protein["values"]["molecular_weight"] == pytest.approx(
            weigh(protein["values"]["sequence"])
        )
    held = Job.objects.get(run_id=launched["runs"][0]["id"])
    assert Job.objects.filter(packed_into=held).count() == 2
    assert (held.progress, held.progress_message) == (1.0, "Weighed 3 of 3")
    log = log_of(launched["runs"][0])
    assert all(run["inputs"][0]["hrid"] in log for run in launched["runs"])


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


def test_a_command_that_says_why_it_failed_fails_the_run_with_that(
    world: World, start: Callable[..., Running], tmp_path: Path
) -> None:
    run = queued(world)
    script = "print('::error Record 3 (pUC19) has no bases', flush=True)\nraise SystemExit(2)\n"
    code, said = start(registration(world), "--once", plugins=with_script(tmp_path, script)).wait()
    assert code == 0, said
    body = api_as(world).get(f"/runs/{run['id']}").json()
    assert body["status"] == "failed", body
    assert body["error"]["message"] == "Record 3 (pUC19) has no bases (exit status 2)"


def test_a_command_gets_its_secrets_in_its_environment_and_never_shows_them(
    world: World, start: Callable[..., Running], tmp_path: Path
) -> None:
    run = queued(world)
    # As if the action declared it: the server reads the run's spec when it leases.
    ActionRun.objects.filter(pk=run["id"]).update(
        spec={
            **ActionRun.objects.get(pk=run["id"]).spec,
            "secrets": [{"name": SECRET_NAME, "required": True, "description": ""}],
        }
    )
    script = (
        "import json, os\n"
        f"print('token:', os.environ.get('{SECRET_NAME}'), flush=True)\n"
        "job = json.load(open(os.environ['IKI_INPUT']))['job']\n"
        "print('in input.json:', 'secrets' in job, flush=True)\n"
        f"print('::error It was', os.environ['{SECRET_NAME}'], flush=True)\n"
        "raise SystemExit(3)\n"
    )
    code, said = start(registration(world), "--once", plugins=with_script(tmp_path, script)).wait()
    assert code == 0, said
    log = log_of(run)
    assert "token: ***" in log
    assert "in input.json: False" in log
    assert SECRET not in log
    assert SECRET not in said
    body = api_as(world).get(f"/runs/{run['id']}").json()
    assert body["error"]["message"] == "It was *** (exit status 3)"
    (attempt,) = api_as(world).get(f"/runs/{run['id']}/attempts").json()
    assert attempt["secrets"] == [SECRET_NAME]


#: Draws a random number from its seed, and reports both.
SEEDED = """
import json, os, random
seed = int(os.environ["IKI_SEED"])
draw = random.Random(seed).random()
metrics = {"seed": seed, "draw": draw}
with open(os.environ["IKI_OUTPUT"], "w") as out:
    json.dump({"apiVersion": "iki/v1", "kind": "RunOutput", "metrics": metrics}, out)
"""


def test_a_command_gets_its_seed_and_a_reproduction_gets_it_again(
    world: World, start: Callable[..., Running], tmp_path: Path
) -> None:
    run = queued(world)
    plugins = with_script(tmp_path, SEEDED)
    code, said = start(registration(world), "--once", plugins=plugins).wait()
    assert code == 0, said
    body = api_as(world).get(f"/runs/{run['id']}").json()
    assert body["status"] == "succeeded", body
    assert body["metrics"]["seed"] == body["seed"]

    again = api_as(world).post(f"/runs/{run['hrid']}/reproduce", {"reason": "Audit"})
    assert again.status_code == 201, again.content
    code, said = start(registration(world), "--once", plugins=plugins).wait()
    assert code == 0, said
    reproduced = api_as(world).get(f"/runs/{again.json()['id']}").json()
    assert reproduced["status"] == "succeeded", reproduced
    assert reproduced["reproduction"]["matches"] is True, reproduced["reproduction"]


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


def test_a_deployment_worker_runs_in_the_environment_built_for_it(
    world: World, start: Callable[..., Running], tmp_path: Path
) -> None:
    from iki_cli import workers as bundle
    from iki_spec.validate import load_plugin

    loaded = load_plugin(PLUGINS / "protein", dependencies=[PLUGINS / "dna"])
    assert loaded.plugin is not None
    item = bundle.WorkerPlugin(loaded.plugin, PLUGINS / "protein", None)
    manifest = bundle.write([item], tmp_path / "bundle", str(tmp_path / "envs"))
    script = tmp_path / "bundle" / bundle.SCRIPT
    # This machine's Python, rather than downloading the version the plugin asks for.
    script.write_text(script.read_text().replace("--python 3.12", f"--python {sys.executable}"))
    subprocess.run(["sh", str(script)], check=True, capture_output=True)  # noqa: S603, S607
    built = manifest["plugins"][0]["environments"]["protein-tools"]

    run = queued(world)
    code, said = start(
        registration(world), "--once", plugins=tmp_path / "bundle" / "plugins"
    ).wait()
    assert code == 0, said
    assert api_as(world).get(f"/runs/{run['id']}").json()["status"] == "succeeded"
    assert Job.objects.get(run_id=run["id"]).environment_build == built["digest"]
    # Its own interpreter, not the worker's.
    assert f" in {built['prefix']}\n" in log_of(run)


def test_a_worker_leases_only_runs_of_plugins_it_has(
    world: World, start: Callable[..., Running], tmp_path: Path
) -> None:
    run = queued(world)
    only_dna = tmp_path / "plugins"
    only_dna.mkdir()
    (only_dna / "dna").symlink_to(PLUGINS / "dna", target_is_directory=True)
    running = start(registration(world), plugins=only_dna)
    pod = eventually(lambda: Worker.objects.filter(name="e2e-worker").first())
    assert pod.plugins == [{"name": "dna", "sha": None, "environments": None}]
    time.sleep(2)  # Long enough to lease, were it allowed to.
    assert Job.objects.get(run_id=run["id"]).state == JobState.QUEUED
    running.process.send_signal(signal.SIGTERM)
    code, said = running.wait()
    assert code == 0, said


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


#: Writes a protein for each of 1,500 made-up records to a creates file (ADR 0056).
MANY = """
import json, os
from pathlib import Path
run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
file_id = run["params"]["file"]
letters = "ACDEFGHIKLMNPQRSTVWY"
with (Path(os.environ["IKI_OUT_DIR"]) / "creates.ndjson").open("w") as out:
    for n in range(1500):
        sequence = "M" + letters[n // 400] + letters[n // 20 % 20] + letters[n % 20] + "KV"
        create = {
            "type": "protein.Protein",
            "values": {"name": f"P{n}", "sequence": sequence},
            "origin": {"source": "FASTA file", "id": f"P{n}", "file": file_id},
        }
        out.write(json.dumps(create) + "\\n")
output = {"apiVersion": "iki/v1", "kind": "RunOutput", "creates_file": {"$file": "creates.ndjson"}}
Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")
"""


def test_a_command_gives_many_creates_in_a_file_and_they_are_imported(
    world: World, start: Callable[..., Running], tmp_path: Path
) -> None:
    from test_run_imports import IMPORT, stored

    plugins = tmp_path / "plugins"
    shutil.copytree(PLUGINS / "protein", plugins / "protein")
    (plugins / "protein" / "scripts" / "imports.py").write_text(MANY, encoding="utf-8")
    file_id = stored(world)
    made = launch(api_as(world, "editor"), world.project, {}, IMPORT, params={"file": file_id})
    assert made.status_code == 201, made.content
    run = made.json()
    code, said = start(registration(world), "--once", plugins=plugins).wait(120)
    assert code == 0, said
    assert "Uploaded the result files." in said
    # The task runner beside the server checks and writes them.
    body = eventually(
        lambda: (b := api_as(world).get(f"/runs/{run['id']}").json())["status"] != "running" and b,
        timeout=120,
    )
    assert body["status"] == "succeeded", body
    assert body["effect_counts"] == {"created": 1500, "updated": 0, "reused": 0}
    assert len(body["effects"]) == 100
    assert body["effects_next_cursor"] is not None
    job = Job.objects.get(run_id=run["id"])
    assert (job.state, job.progress) == (JobState.SUCCEEDED, 1.0)


def test_someone_signs_a_worker_in_on_their_computer_and_it_runs_their_runs(
    world: World, worker_bin: Path, server: Server, tmp_path: Path
) -> None:
    """``iki-worker login`` through the device flow, ``start`` with nothing set but where
    plugins are, and ``logout`` (ADR 0058)."""
    from importlib import import_module

    from django.apps import apps as django_apps
    from org_world import post
    from test_worker_sign_in import on_client

    # Its app, which a migration adds and each test's emptying of the database takes away.
    import_module("iki_server.oauth.migrations.0003_iki_worker").create(django_apps, None)
    python = Path(sys.executable).parent
    env = {
        "PATH": f"{python}{os.pathsep}{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path / "home"),
        "IKI_WORKER_NAME": "ada-laptop",
        "IKI_WORKER_CPU": "4",
        "IKI_WORKER_MEMORY": "8Gi",
    }
    login = subprocess.Popen(  # noqa: S603 - our own binary
        [str(worker_bin), "login", server.url, "--no-browser"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert login.stderr is not None
        asked = login.stderr.readline()
        assert asked.startswith(f"To sign this worker in, open {server.url}/oauth/device/"), asked
        code = asked.split()[-1]
        browser = world.browser("editor")
        entered = post(browser, "/oauth/device/", {"user_code": code})
        assert entered.status_code == 302, entered.content
        choice = {"granted": ["worker"], "access": "chosen", "projects": [str(world.project.pk)]}
        assert post(browser, entered["Location"], choice).status_code == 200
    except BaseException:
        login.kill()
        login.communicate()
        raise
    rest = Running(login).wait(60)
    assert rest[0] == 0, rest[1]
    assert "Signed in to" in rest[1] and "ada-laptop" in rest[1]
    state = tmp_path / "home" / ".config" / "iki" / "worker.json"
    assert state.stat().st_mode & 0o777 == 0o600
    signed_in = Worker.objects.get(name="ada-laptop")
    assert (signed_in.tier, signed_in.owner) == ("client", world.users["editor"])

    run = on_client(queued(world))
    started = subprocess.Popen(  # noqa: S603 - our own binary
        [str(worker_bin), "start", "--once"],
        env={**env, "IKI_PLUGINS_DIR": str(PLUGINS), "IKI_WORK_DIR": str(tmp_path / "work")},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    code_, said = Running(started).wait(120)
    assert code_ == 0, said
    assert "Signed in with the saved token." in said
    assert api_as(world).get(f"/runs/{run['id']}").json()["status"] == "succeeded"
    # It kept its token: a signed-in worker isn't for one process.
    signed_in.refresh_from_db()
    assert signed_in.revoked_at is None
    assert {p["name"] for p in signed_in.plugins} >= {"protein"}

    out = subprocess.run(  # noqa: S603 - our own binary
        [str(worker_bin), "logout"], env=env, capture_output=True, text=True, check=False
    )
    assert out.returncode == 0, out.stderr
    assert f"Signed out of {server.url}." in out.stderr
    assert not state.exists()
    signed_in.refresh_from_db()
    assert signed_in.revoked_at is not None
