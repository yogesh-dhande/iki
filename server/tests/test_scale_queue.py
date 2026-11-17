"""Load test: the job queue's throughput, with a deep backlog and a long history.

A run of ``acme/protein.compute_properties`` is launched through Iki, then copied in bulk, as
a restore loads a database: ``IKI_SCALE_QUEUE_JOBS`` queued (100,000 by default) across
``PROJECTS`` projects, behind ``IKI_SCALE_QUEUE_HISTORY`` finished ones (1,000,000).
Then workers, each in its own thread, lease jobs and complete them over ``/worker/v1`` of
the real server (production settings, ``SERVERS`` uvicorn processes), as iki-worker does,
as fast as each can: first one for each of the server's processes, which must answer
within ``BUDGET``; then all ``WORKERS``, for ``IKI_SCALE_QUEUE_LEASES`` jobs, which must
keep up ``RATE``. No job may be leased twice, and the projects must take turns (ADR 0043,
0108). ``make check-scale-queue`` runs it.
"""

import json
import os
import statistics
import threading
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
import requests
from django.db import connection, models
from iki_test_plugins import ensure_built
from org_world import World
from real_server import Server, app_role, manage, serve, server_env
from test_api_entities import api_as
from test_run_results import output
from test_runs import launch, protein, world
from test_workers import LOCAL, ROOMY, WorkerClient, keep_connection, registration

from iki_server.org.models import Project, ProjectMembership, ProjectRole
from iki_server.runs.models import ActionRun, Job, JobState, RunInput, RunStatus

pytestmark = [pytest.mark.scale, pytest.mark.django_db(transaction=True)]

__all__ = ["keep_connection", "world"]  # the fixtures, used here too

JOBS = int(os.environ.get("IKI_SCALE_QUEUE_JOBS", "100000"))
HISTORY = int(os.environ.get("IKI_SCALE_QUEUE_HISTORY", "1000000"))
LEASES = int(os.environ.get("IKI_SCALE_QUEUE_LEASES", "5000"))
PROJECTS = 100
WORKERS = int(os.environ.get("IKI_SCALE_QUEUE_WORKERS", "32"))
#: The server's processes: a deployment scales pods instead, each one process.
SERVERS = 4
CHUNK = 1_000_000
#: How many jobs the first phase leases, with one worker for each of the server's processes.
UNLOADED = 1000
#: What a lease, and a completion, may take then: at the median and the 99th percentile.
#: However deep the queue, as long as the server isn't saturated.
BUDGET = {"p50": 0.1, "p99": 0.5}
#: With every worker asking at once, as fast as each can, the server is saturated: each
#: request waits its turn, as many as there are workers ahead of it (Little's law). So the
#: second phase holds the rate at least to this, on CI's runner (4 vCPUs, the database and
#: the workers on it too), and only bounds how long the slowest wait.
RATE = 40
LOADED_P99 = 2.0


def _sql(statement: str, params: list[Any] | None = None) -> list[tuple[Any, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(statement, params)
        return cursor.fetchall() if cursor.description else []


def _columns(model: type[models.Model], own: dict[str, str | None]) -> dict[str, str]:
    """Each of the model's columns, and what a copy puts in it: its own, or the template's.
    None leaves a column to its default, as a serial key's is."""
    found = {}
    for f in model._meta.local_concrete_fields:
        if getattr(f, "generated", False):
            continue
        value = own.get(str(f.column), f't."{f.column}"')
        if value is not None:
            found[str(f.column)] = value
    return found


def _copy(
    model: type[models.Model], own: dict[str, str | None], where: str, lo: int, hi: int
) -> None:
    columns = _columns(model, own)
    names = ", ".join(f'"{c}"' for c in columns)
    _sql(
        f"INSERT INTO {model._meta.db_table} ({names}) "  # noqa: S608
        f"SELECT {', '.join(columns.values())} FROM {model._meta.db_table} t, "
        f"generate_series(%s::bigint, %s::bigint) n WHERE {where}",
        [lo, hi],
    )


def fill(template: ActionRun, projects: list[Project]) -> None:
    """Copies of ``template``'s run, its input and its job: first the history, finished,
    then the queue, each copy in one of ``projects`` in turn, queued a millisecond apart."""
    ids = "ARRAY[" + ", ".join(f"'{p.pk}'::uuid" for p in projects) + "]"
    project = f"({ids})[1 + n %% {len(projects)}]"
    run_id = "md5('run' || n)::uuid"
    total = HISTORY + JOBS
    began = time.monotonic()
    for lo in range(1, total + 1, CHUNK):
        hi = min(lo + CHUNK - 1, total)
        # The first HISTORY copies are finished: they're what a queue that's been running
        # for a while has behind it.
        done = f"n <= {HISTORY}"
        _copy(
            ActionRun,
            {
                "id": run_id,
                "hrid": "'RUN-' || lpad(n::text, 9, '0')",
                "project_id": project,
                "status": f"CASE WHEN {done} THEN '{RunStatus.SUCCEEDED}' ELSE t.status END",
                "created_at": f"t.created_at - interval '1 millisecond' * ({total} - n + 1)",
            },
            f"t.id = '{template.pk}'",
            lo,
            hi,
        )
        _copy(
            RunInput,
            {"id": None, "run_id": run_id},
            f"t.run_id = '{template.pk}'",
            lo,
            hi,
        )
        _copy(
            Job,
            {
                "id": "md5('job' || n)::uuid",
                "run_id": run_id,
                "project_id": project,
                "state": f"CASE WHEN {done} THEN '{JobState.SUCCEEDED}' ELSE t.state END",
                "queued_at": f"t.queued_at - interval '1 millisecond' * ({total} - n + 1)",
                "finished_at": f"CASE WHEN {done} THEN t.queued_at ELSE NULL END",
                "started_at": f"CASE WHEN {done} THEN t.queued_at ELSE NULL END",
            },
            f"t.run_id = '{template.pk}'",
            lo,
            hi,
        )
        print(f"copied {hi:,} in {time.monotonic() - began:.0f}s", flush=True)  # noqa: T201
    # The template's own run waits behind the copies; leasing it would only be a 101st
    # project's turn.
    Job.objects.filter(run=template).delete()
    RunInput.objects.filter(run=template).delete()
    template.delete()
    _sql("ANALYZE iki_action_run")
    _sql("ANALYZE iki_run_input")
    _sql("ANALYZE iki_job")


@pytest.fixture(scope="module")
def server(
    django_db_setup: None, django_db_blocker: Any, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Server]:
    with django_db_blocker.unblock(), app_role() as role:
        plugins = os.pathsep.join(
            p for p in (str(ensure_built()), os.environ.get("PYTHONPATH")) if p
        )
        env = server_env(
            role,
            tmp_path_factory.mktemp("static"),
            PYTHONPATH=plugins,
            IKI_COMPUTE_FILE=os.environ["IKI_COMPUTE_FILE"],
            IKI_WEB_WORKERS=str(SERVERS),
        )
        env["IKI_PUBLIC_URL"] = f"http://127.0.0.1:{env['IKI_PORT']}"
        done = manage("collectstatic", "--noinput", env=env)
        assert done.returncode == 0, done.stdout + done.stderr
        # Its processes start one after another, each loading the whole app.
        with serve(env, startup=180) as running:
            yield running


def _quantile(times: list[float], q: int) -> float:
    return statistics.quantiles(times, n=100)[q - 1] if len(times) > 1 else times[0]


def test_workers_lease_and_complete_jobs_quickly_from_a_deep_queue(
    world: World, server: Server
) -> None:
    editor = api_as(world, "editor")
    projects = [world.project]
    for n in range(1, PROJECTS):
        made = Project.objects.create(team=world.project.team, name=f"Screen {n}")
        ProjectMembership.objects.create(
            project=made, user=world.users["editor"], role=ProjectRole.EDITOR
        )
        projects.append(made)
    target = protein(editor, world.project)
    launched = launch(editor, world.project, {"protein": target["id"]})
    assert launched.status_code == 201, launched.content
    fill(ActionRun.objects.get(pk=launched.json()["id"]), projects)
    assert Job.objects.filter(state=JobState.QUEUED).count() == JOBS

    token = registration(world)
    pods = []
    for n in range(WORKERS):
        made = WorkerClient(token).post(
            "/register",
            {"name": f"pod-{n}", "version": "0.1.0", "capacity": ROOMY, "plugins": LOCAL},
        )
        assert made.status_code == 201, made.content
        pod = requests.Session()
        pod.headers["Authorization"] = f"Bearer {made.json()['token']}"
        pods.append(pod)

    api = f"{server.url}/worker/v1"
    leased: list[tuple[str, str]] = []
    # As many workers as the server has processes, then every worker at once.
    unloaded = _drive(api, pods[:SERVERS], UNLOADED, leased)
    loaded = _drive(api, pods, LEASES, leased)
    _report(server, unloaded, loaded)

    # Each job once, and every project in turn: with as many running in each, none of a
    # hundred projects got more than a round's worth ahead of another.
    assert len({job for job, _ in leased}) == len(leased) == UNLOADED + LEASES
    turns = Counter(project for _, project in leased)
    assert len(turns) == PROJECTS
    assert max(turns.values()) - min(turns.values()) <= WORKERS, turns.most_common(3)
    assert Job.objects.filter(state=JobState.SUCCEEDED).count() == HISTORY + len(leased)
    for what in ("lease", "complete"):
        for q, budget in BUDGET.items():
            took = unloaded.times[what][q]
            assert took <= budget, f"{what} took {took * 1000:.0f} ms at {q}, unloaded"
        took = loaded.times[what]["p99"]
        assert took <= LOADED_P99, f"{what} took {took * 1000:.0f} ms at p99, loaded"
    assert loaded.rate >= RATE, f"{loaded.rate:.0f} jobs a second, loaded"


@dataclass
class Phase:
    workers: int
    leased: int
    elapsed: float
    times: dict[str, dict[str, float]]

    @property
    def rate(self) -> float:
        return self.leased / self.elapsed


def _drive(
    api: str, pods: list[requests.Session], count: int, leased: list[tuple[str, str]]
) -> Phase:
    """``pods`` lease and complete ``count`` jobs between them, each as fast as it can."""
    took: dict[str, list[float]] = {"lease": [], "complete": []}
    errors: list[str] = []
    lock = threading.Lock()
    left = [count]

    def work(pod: requests.Session) -> None:
        while True:
            with lock:
                if left[0] <= 0 or errors:
                    return
                left[0] -= 1
            began = time.perf_counter()
            answer = pod.post(f"{api}/lease?wait=0", timeout=60)
            leasing = time.perf_counter() - began
            if answer.status_code != 200:
                with lock:
                    errors.append(f"lease: {answer.status_code} {answer.content[:300]!r}")
                return
            job = answer.json()
            began = time.perf_counter()
            done = pod.post(
                f"{api}/jobs/{job['job']['id']}/complete",
                json={"lease_token": job["job"]["lease_token"], "output": output()},
                timeout=60,
            )
            completing = time.perf_counter() - began
            with lock:
                took["lease"].append(leasing)
                took["complete"].append(completing)
                leased.append((job["job"]["id"], job["run"]["project"]))
                if done.status_code != 200:
                    errors.append(f"complete: {done.status_code} {done.content[:300]!r}")

    threads = [threading.Thread(target=work, args=(pod,)) for pod in pods]
    began = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.monotonic() - began
    assert not errors, errors[:5]
    times = {
        what: {"p50": statistics.median(t), "p99": _quantile(t, 99)} for what, t in took.items()
    }
    return Phase(len(pods), count, elapsed, times)


def _report(server: Server, *phases: Phase) -> None:
    for phase in phases:
        print(  # noqa: T201
            f"\n{phase.leased:,} jobs leased and completed by {phase.workers} workers in "
            f"{phase.elapsed:.1f}s ({phase.rate:.0f} a second), from {JOBS:,} queued behind "
            f"{HISTORY:,} finished: "
            + ", ".join(
                f"{what} {t['p50'] * 1000:.0f} ms median, {t['p99'] * 1000:.0f} ms p99"
                for what, t in phase.times.items()
            ),
            flush=True,
        )
    # What the server says each took, from its request log: the rest of what the workers
    # saw was spent waiting to be served.
    served: dict[str, list[float]] = {"lease": [], "complete": []}
    for line in server.text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("logger") != "iki.request":
            continue
        path = str(entry.get("path", ""))
        for what, took in served.items():
            if path.endswith(f"/{what}"):
                took.append(float(entry["duration_ms"]) / 1000)
    for what, took in served.items():
        if len(took) > 1:
            print(  # noqa: T201
                f"{what} at the server, both phases: {statistics.median(took) * 1000:.0f} ms "
                f"median, {_quantile(took, 99) * 1000:.0f} ms p99, of {len(took):,}",
                flush=True,
            )
