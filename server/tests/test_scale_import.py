"""Load test (ADR 0056): a run imports 10M entities from a creates file, and goes on from
where it was when it's interrupted halfway. ``make check-scale`` runs it;
``IKI_SCALE_RECORDS`` sets how many (10,000,000 by default)."""

import hashlib
import os
import time
from pathlib import Path
from typing import Any

import pytest
import requests
from org_world import World
from test_api_entities import api_as
from test_run_imports import IMPORT, stored
from test_run_results import output
from test_runs import launch, world
from test_workers import WorkerClient, keep_connection, worker

from iki_server.entities.models import Entity
from iki_server.runs import imports
from iki_server.runs.models import Job, JobState, RunEffect, RunImport

pytestmark = [pytest.mark.scale, pytest.mark.django_db(transaction=True)]

__all__ = ["keep_connection", "world"]  # the fixtures, used here too

RECORDS = int(os.environ.get("IKI_SCALE_RECORDS", "10000000"))
AMINO_ACIDS = "ACDEFGHIKLMNPQRSTVWY"


def sequence(n: int) -> str:
    """A protein sequence of its own for each n: M, then n in base 20."""
    letters = []
    while True:
        n, digit = divmod(n, 20)
        letters.append(AMINO_ACIDS[digit])
        if n == 0:
            break
    return "M" + "".join(reversed(letters)).rjust(8, "A")


def write(path: Path, file_id: str, count: int) -> None:
    with path.open("w", encoding="utf-8") as out:
        for n in range(count):
            seq = sequence(n)
            out.write(
                f'{{"type":"protein.Protein","values":{{"name":"P{n}","sequence":"{seq}"}},'
                f'"origin":{{"source":"FASTA file","id":"P{n}","file":"{file_id}"}}}}\n'
            )


def upload(pod: WorkerClient, job: dict[str, Any], path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as given:
        for block in iter(lambda: given.read(8 * 1024 * 1024), b""):
            sha.update(block)
    size = path.stat().st_size
    started = pod.report(
        job,
        "uploads",
        name="creates.ndjson",
        media_type="application/x-ndjson",
        size=size,
        sha256=sha.hexdigest(),
    )
    assert started.status_code == 201, started.content
    made: dict[str, Any] = started.json()
    with path.open("rb") as given:
        for part in made["parts"]:
            requests.put(part["url"], data=given.read(part["size"]), timeout=600).raise_for_status()
    for _ in range(600):
        made = pod.report(job, f"uploads/{made['id']}/complete").json()
        if made["state"] == "ready":
            break
        from test_files import run_tasks

        run_tasks()
    assert made["state"] == "ready", made
    return str(made["file"])


def test_a_run_imports_10m_entities_and_goes_on_after_an_interruption(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, s3: Any
) -> None:
    file_id = stored(world)
    run = launch(api_as(world, "editor"), world.project, {}, IMPORT, params={"file": file_id})
    assert run.status_code == 201, run.content
    pod = worker(world)
    job = pod.lease().json()
    path = tmp_path / "creates.ndjson"
    began = time.monotonic()
    write(path, file_id, RECORDS)
    creates = upload(pod, job, path)
    path.unlink()
    print(f"\nwrote and uploaded {RECORDS:,} lines in {time.monotonic() - began:.0f}s")  # noqa: T201 - its report

    answer = pod.report(job, "complete", output=output(creates_file=creates)).json()
    assert answer["run_status"] == "running", answer
    imported = RunImport.objects.get(run_id=run.json()["id"])

    # Checking stops halfway, as when its runner's pod is evicted…
    real = imports._check_chunk
    half = RECORDS // 2

    def evicted(found: RunImport, *args: Any) -> None:
        if found.staged >= half:
            raise RuntimeError("The pod was evicted.")
        real(found, *args)

    monkeypatch.setattr(imports, "STEP_FOR", 10**9)
    monkeypatch.setattr(imports, "_check_chunk", evicted)
    began = time.monotonic()
    with pytest.raises(RuntimeError):
        imports._advance(imported.pk)
    imported.refresh_from_db()
    assert half <= imported.staged < half + imports.CHUNK_LINES
    first = time.monotonic() - began

    # …and the next step goes on from there, then writes it all at once.
    monkeypatch.setattr(imports, "_check_chunk", real)
    began = time.monotonic()
    assert imports._advance(imported.pk) is True
    rest = time.monotonic() - began
    imported.refresh_from_db()
    print(  # noqa: T201 - the load test's report
        f"checked {half:,} in {first:.0f}s, then the rest and wrote it all in {rest:.0f}s "
        f"({RECORDS / (first + rest):,.0f} a second)"
    )
    assert (imported.created, imported.reused) == (RECORDS, 0)
    assert Job.objects.get(pk=imported.job_id).state == JobState.SUCCEEDED
    assert Entity.objects.filter(type="protein.Protein").count() == RECORDS
    assert RunEffect.objects.filter(run_id=imported.run_id).count() == RECORDS
    hrids = Entity.objects.filter(type="protein.Protein").order_by("hrid")
    assert hrids.values_list("hrid", flat=True).first() == "PRT-000001"
