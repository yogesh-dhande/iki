"""Load test: the job queue's throughput, with a deep backlog and a long history.

A run of ``acme/protein.compute_properties`` is launched through Iki, then copied in bulk, as
a restore loads a database: ``IKI_SCALE_QUEUE_JOBS`` queued (100,000 by default) across
``PROJECTS`` projects, behind ``IKI_SCALE_QUEUE_HISTORY`` finished ones (1,000,000).
Then workers, each in its own thread, lease jobs and complete them over ``/worker/v1`` of
the real server (production settings, ``SERVERS`` uvicorn processes), as iki-worker does,
as fast as each can: first one for each of the server's processes, which must answer
within ``BUDGET``; then all ``WORKERS``, for ``IKI_SCALE_QUEUE_LEASES`` jobs, which must
keep up ``RATE``. No job may be leased twice, and the projects must take turns (ADR 0043,
0108).

Then packs under quotas (ADR 0045, 0055): a batch of ``PACK`` runs of
``acme/protein.measure_purity``, a ``batchable`` action whose runs each read a sample, the
lot it's of and the lot's report, is copied ``IKI_SCALE_QUEUE_PACKS`` times (1,000: 100,000
runs) across ``PACKED_PROJECTS`` projects, each of which may run ``QUOTA`` jobs at once.
Every worker leases at once until none may lease more: each project then runs exactly its
quota, and the slowest lease took no longer than ``HOLD_SLOWEST``. Then one worker for each
of the server's processes leases and completes ``IKI_SCALE_QUEUE_PACK_LEASES`` packs (300),
each lease within ``PACK_BUDGET``. ``make check-scale-queue`` runs both."""

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
from test_scan_gates import _file
from test_workers import LOCAL, ROOMY, WorkerClient, keep_connection, registration

from iki_server.entities import services as entities
from iki_server.entities.models import Entity, ProjectLink
from iki_server.files import scan_state
from iki_server.org.models import Project, ProjectMembership, ProjectRole
from iki_server.runs.models import (
    ActionRun,
    Job,
    JobState,
    RunBatch,
    RunInclusion,
    RunInput,
    RunQuota,
    RunStatus,
)
from iki_server.runs.services import cancel, cancel_batch

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

PURITY = "acme/protein.measure_purity"
#: The runs a job packs: the batchable action's own most.
PACK = 100
PACKED_PROJECTS = 20
#: How many jobs each of those projects may run at once.
QUOTA = 2
PACKS = int(os.environ.get("IKI_SCALE_QUEUE_PACKS", "1000"))
PACK_LEASES = int(os.environ.get("IKI_SCALE_QUEUE_PACK_LEASES", "300"))
#: How long the slowest lease may take while every worker leases at once until each
#: project runs its quota. Were leases on a tier with quotas to wait for one another while
#: each reads its pack, the last would wait for all of them.
HOLD_SLOWEST = 10.0
#: What leasing a pack may take, at the median and the 99th percentile, with one worker for
#: each of the server's processes: a lease that reads what a hundred runs read.
PACK_BUDGET = {"p50": 0.5, "p99": 1.5}


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
    # Only the copies participate in the queue measurement.
    cancel(template.requested_by, template)
    _sql("ANALYZE iki_action_run")
    _sql("ANALYZE iki_run_input")
    _sql("ANALYZE iki_job")


def fill_packs(template: list[ActionRun], projects: list[Project]) -> None:
    """``PACKS`` copies of ``template``'s batch, each a batch of its own in one of
    ``projects`` in turn, queued a millisecond apart: their runs, what each reads, and
    their jobs."""
    batch = template[0].batch
    assert batch is not None
    ids = "ARRAY[" + ", ".join(f"'{p.pk}'::uuid" for p in projects) + "]"
    project = f"({ids})[1 + n %% {len(projects)}]"
    runs = "ARRAY[" + ", ".join(f"'{r.pk}'::uuid" for r in template) + "]"
    copied = "md5('batch' || n)::uuid"

    def run_of(column: str) -> str:
        return f"md5('run' || n || {column}::text)::uuid"

    began = time.monotonic()
    _copy(
        RunBatch,
        {"id": copied, "hrid": "'BAT-' || lpad(n::text, 9, '0')", "project_id": project},
        f"t.id = '{batch.pk}'",
        1,
        PACKS,
    )
    _copy(
        ActionRun,
        {
            "id": run_of("t.id"),
            "hrid": "'RUN-' || lpad((n * 100000 + substring(t.hrid from 5)::int)::text, 12, '0')",
            "project_id": project,
            "batch_id": copied,
        },
        f"t.id = ANY({runs})",
        1,
        PACKS,
    )
    for model in (RunInput, RunInclusion):
        _copy(
            model, {"id": None, "run_id": run_of("t.run_id")}, f"t.run_id = ANY({runs})", 1, PACKS
        )
    _copy(
        Job,
        {
            "id": "md5('job' || n || t.id::text)::uuid",
            "run_id": run_of("t.run_id"),
            "project_id": project,
            "queued_at": f"t.queued_at - interval '1 millisecond' * ({PACKS} - n + 1)",
        },
        f"t.run_id = ANY({runs})",
        1,
        PACKS,
    )
    print(f"copied {PACKS:,} packs in {time.monotonic() - began:.0f}s", flush=True)  # noqa: T201
    # Only the copies participate in the measurement.
    cancel_batch(template[0].requested_by, batch)
    for table in ("iki_run_batch", "iki_action_run", "iki_run_input", "iki_run_inclusion"):
        _sql(f"ANALYZE {table}")
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
    # Each copy reads it in its own project, as a run must.
    ProjectLink.objects.bulk_create(
        ProjectLink(project=project, entity_id=target["id"]) for project in projects[1:]
    )
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
    _report(server, unloaded, loaded, of=f"jobs, from {JOBS:,} queued behind {HISTORY:,} finished")

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


def purity_batch(world: World) -> list[ActionRun]:
    """A batch of ``PACK`` runs of measure_purity, packed whole: each reads a sample, the lot
    it's of, and the lot's report, scanned clean."""
    editor = world.users["editor"]
    samples = []
    for n in range(PACK):
        report = _file(world, f"lot-{n:03d}-coa.pdf")
        scan_state.record_result(report.blob, state="clean", scanner="test", signature_version="1")
        lot = entities.create(
            editor, "acme/protein.Lot", world.project, {"label": f"L{n:03d}", "report": report.pk}
        ).entity
        samples.append(
            entities.create(
                editor,
                "acme/protein.Sample",
                world.project,
                {"label": f"Purity sample {n:03d}", "lot": lot.pk},
            ).entity
        )
    launched = api_as(world, "editor").post(
        f"/actions/{PURITY}/runs/batch",
        {
            "project": str(world.project.pk),
            "each": "sample",
            "entities": [str(e.pk) for e in samples],
        },
    )
    assert launched.status_code == 201, launched.content
    runs = list(ActionRun.objects.filter(batch__hrid=launched.json()["batch"]["hrid"]))
    for run in runs:
        run.spec = {**run.spec, "batchable": PACK}
        run.save(update_fields=["spec"])
    return runs


def test_workers_lease_packs_within_their_projects_quotas_quickly(
    world: World, server: Server
) -> None:
    projects = [world.project]
    for n in range(1, PACKED_PROJECTS):
        made = Project.objects.create(team=world.project.team, name=f"Purity screen {n}")
        ProjectMembership.objects.create(
            project=made, user=world.users["editor"], role=ProjectRole.EDITOR
        )
        projects.append(made)
    template = purity_batch(world)
    # Each copy reads its samples and their lots in its own project, as a run must.
    read = [
        pk
        for run in template
        for pk in [
            *run.inputs.values_list("entity_id", flat=True),
            *run.inclusions.values_list("entity_id", flat=True),
        ]
    ]
    assert Entity.objects.filter(pk__in=read).count() == 2 * PACK
    ProjectLink.objects.bulk_create(
        ProjectLink(project=project, entity_id=pk) for project in projects[1:] for pk in read
    )
    fill_packs(template, projects)
    for project in projects:
        RunQuota.objects.create(project=project, max_running=QUOTA, created_by=world.users["admin"])
    assert Job.objects.filter(state=JobState.QUEUED).count() == PACKS * PACK

    token = registration(world)
    pods = []
    for n in range(WORKERS):
        made = WorkerClient(token).post(
            "/register",
            {"name": f"pack-{n}", "version": "0.1.0", "capacity": ROOMY, "plugins": LOCAL},
        )
        assert made.status_code == 201, made.content
        pod = requests.Session()
        pod.headers["Authorization"] = f"Bearer {made.json()['token']}"
        pods.append(pod)
    api = f"{server.url}/worker/v1"

    # Every worker at once, until none may lease more: each project runs as many as its
    # quota lets it, and not one more, however their leases raced.
    since = len(server.text.splitlines())
    held = _hold(api, pods)
    running = Counter(h.project for h in held)
    assert running == {str(p.pk): QUOTA for p in projects}, running.most_common(3)
    assert {h.runs for h in held} == {PACK}
    assert Job.objects.filter(state=JobState.LEASED, packed_into__isnull=True).count() == len(held)
    for h in held:
        done = h.pod.post(
            f"{api}/jobs/{h.job}/complete",
            json={"lease_token": h.token, "output": output()},
            timeout=60,
        )
        assert done.status_code == 200, done.content

    # Then as many workers as the server has processes, each leasing a pack and completing
    # it as fast as it can.
    leased: list[tuple[str, str]] = []
    unloaded = _drive(api, pods[:SERVERS], PACK_LEASES, leased)
    took = sorted(h.took for h in held)
    print(  # noqa: T201
        f"\n{len(held)} packs leased by {WORKERS} workers at once, until {QUOTA} ran in each "
        f"of {PACKED_PROJECTS} projects: lease {statistics.median(took) * 1000:.0f} ms median, "
        f"{took[-1] * 1000:.0f} ms the slowest",
        flush=True,
    )
    _report(server, unloaded, of=f"packs of {PACK} runs, from {PACKS:,} queued", since=since)
    assert len({job for job, _ in leased}) == len(leased) == PACK_LEASES
    finished = Job.objects.filter(state=JobState.SUCCEEDED).count()
    assert finished == (len(held) + PACK_LEASES) * PACK
    assert took[-1] <= HOLD_SLOWEST, f"the slowest lease took {took[-1] * 1000:.0f} ms"
    for q, budget in PACK_BUDGET.items():
        each = unloaded.times["lease"][q]
        assert each <= budget, f"a pack's lease took {each * 1000:.0f} ms at {q}"


@dataclass
class Held:
    """A job a worker leased: which, by which worker, in which project, and how many runs
    it packs."""

    pod: requests.Session
    job: str
    token: str
    project: str
    runs: int
    took: float


def _hold(api: str, pods: list[requests.Session]) -> list[Held]:
    """``pods`` lease, each as fast as it can, until none is given any more."""
    held: list[Held] = []
    errors: list[str] = []
    lock = threading.Lock()

    def work(pod: requests.Session) -> None:
        while True:
            began = time.perf_counter()
            answer = pod.post(f"{api}/lease?wait=0", timeout=60)
            took = time.perf_counter() - began
            if answer.status_code == 204:
                return
            if answer.status_code != 200:
                with lock:
                    errors.append(f"lease: {answer.status_code} {answer.content[:300]!r}")
                return
            job = answer.json()
            runs = len(job.get("pack") or [job["run"]])
            with lock:
                held.append(
                    Held(
                        pod,
                        job["job"]["id"],
                        job["job"]["lease_token"],
                        job["run"]["project"],
                        runs,
                        took,
                    )
                )

    threads = [threading.Thread(target=work, args=(pod,)) for pod in pods]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, errors[:5]
    return held


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


def _report(server: Server, *phases: Phase, of: str, since: int = 0) -> None:
    """What each of ``phases`` took, leasing and completing ``of``; and what the server says
    its requests took, from line ``since`` of its log on."""
    for phase in phases:
        print(  # noqa: T201
            f"\n{phase.leased:,} {of}, leased and completed by {phase.workers} workers in "
            f"{phase.elapsed:.1f}s ({phase.rate:.0f} a second): "
            + ", ".join(
                f"{what} {t['p50'] * 1000:.0f} ms median, {t['p99'] * 1000:.0f} ms p99"
                for what, t in phase.times.items()
            ),
            flush=True,
        )
    # What the server says each took, from its request log: the rest of what the workers
    # saw was spent waiting to be served.
    served: dict[str, list[float]] = {"lease": [], "complete": []}
    for line in server.text.splitlines()[since:]:
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
                f"{what} at the server: {statistics.median(took) * 1000:.0f} ms "
                f"median, {_quantile(took, 99) * 1000:.0f} ms p99, of {len(took):,}",
                flush=True,
            )
