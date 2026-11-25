"""Run the lab's translation workflow with its local actions and first-party record types."""

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from first_party_dependencies import PLUGINS, ROOT, build_order
from org_world import build_world
from test_api_entities import api_as, create
from test_plugin_migrations import database, django, iki, migrate, sync
from test_workers import worker
from test_workflows import get, run_tasks, start, steps

from iki_server.runs import catalog
from iki_server.runs.models import Lineage
from iki_spec.compute import Binding

__all__ = ["database"]

EXAMPLE = ROOT / "examples" / "plugins" / "protein_translation"
PLUGIN = "acme/protein_translation"


def test_translated_protein_keeps_both_steps_provenance(tmp_path: Path, database: str) -> None:
    build = tmp_path / "build"
    built = iki(
        "plugin", "build", *build_order(PLUGINS / "protein"), EXAMPLE, "--output", build, cwd=ROOT
    )
    assert built.returncode == 0, built.stdout + built.stderr
    migrate(build, database)
    sync(build, database)
    checked = django(
        build,
        database,
        "-c",
        "import django; django.setup(); "
        "from test_protein_workflow_example import exercise; exercise()",
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr


def exercise() -> None:
    world = build_world()
    editor = api_as(world, "editor")
    environment = f"{PLUGIN}.protein-tools"
    catalog.compute().bindings[environment] = Binding(
        environment=environment, templates=(("backend-small", 0),)
    )
    pod = worker(
        world, plugins=[{"name": PLUGIN, "sha": None, "environments": None, "bundle": None}]
    )
    dna = create(
        editor,
        world.project,
        {"bases": "ATGTGGTATTAA", "topology": "linear"},
        "iki/dna.DNASequence",
    ).json()
    response = start(
        editor,
        world,
        {"sequence": dna["id"]},
        f"{PLUGIN}.translate_and_characterize",
        params={"complete_cds": True},
    )
    assert response.status_code == 201, response.content
    workflow = response.json()
    assert steps(workflow) == {"translate": "running", "properties": "waiting"}

    for action in ("translate", "compute_properties"):
        job = pod.lease().json()
        assert job["run"]["action"] == f"{PLUGIN}.{action}"
        with TemporaryDirectory() as stage:
            inp, out = Path(stage) / "input.json", Path(stage) / "output.json"
            inp.write_text(json.dumps(job))
            subprocess.run(  # noqa: S603 - the example's local action command
                [sys.executable, *job["command"][1:]],
                cwd=EXAMPLE,
                env={**os.environ, "IKI_INPUT": str(inp), "IKI_OUTPUT": str(out)},
                check=True,
                timeout=30,
            )
            reported = pod.report(job, "complete", output=json.loads(out.read_text()))
        assert reported.status_code == 200, reported.content
        assert reported.json()["run_status"] == "succeeded", reported.content
        run_tasks()

    done = get(world, workflow)
    assert done["status"] == "succeeded"
    translation, properties = [step["run"]["id"] for step in done["steps"]]
    (effect,) = editor.get(f"/runs/{translation}").json()["effects"]
    entity = effect["entity"]
    (measured,) = editor.get(f"/runs/{properties}").json()["inputs"]
    assert (measured["entity"], measured["revision"]) == (entity, effect["revision"])
    assert Lineage.objects.filter(
        entity_id=entity, parent_id=dna["id"], run_id=translation
    ).exists()
    record = editor.get(f"/entities/{entity}").json()
    assert record["values"]["sequence"] == "MWY"
    origin = editor.get(f"/entities/{entity}/provenance").json()
    assert origin["sequence"]["run"]["id"] == translation
    assert origin["molecular_weight"]["run"]["id"] == properties
