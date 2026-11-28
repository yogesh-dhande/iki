"""Included references (ADR 0105): an action's input can include the entities its references
name, such as a structure's chains. A run reads them as of launch, as it reads its inputs:
pinned at their revisions, checked readable and in its project, kept with the run, and
counted in what makes two runs identical."""

import json
from pathlib import Path
from typing import Any

import pytest
from api_client import Api, problem
from org_world import World
from test_api_entities import api_as, create, patch
from test_bundle_imports import PUBLIC, applied, bundle_of, upload
from test_registries import registrar
from test_requests import build_lab, request_with_private_sample, schedule
from test_run_projects import elsewhere
from test_run_results import RELAX, _stored, done, output, world
from test_run_reuse import pod as built_pod
from test_runs import launch, protein
from test_workers import keep_connection, worker

from iki_server.bundles import reading
from iki_server.entities.models import Entity, ProjectLink
from iki_server.entities.values import UNREADABLE_REFERENCE
from iki_server.org.models import OrgRole, OrgRoleAssignment, ProjectMembership, ProjectRole
from iki_server.runs import included, services
from iki_server.runs.models import ActionRun, RunInclusion
from iki_spec import schemas

pytestmark = pytest.mark.django_db

__all__ = ["keep_connection", "world"]

SHA = "a" * 40
PREP, WEIGH = "acme/protein.Prep", "acme/protein.weigh_prep"
PURITY, SAMPLE = "acme/protein.measure_purity", "acme/protein.Sample"


@pytest.fixture(autouse=True)
def storage(s3: Any) -> Any:
    return s3


def structure(world: World, *chains: dict[str, Any]) -> dict[str, Any]:
    values = {
        "structure": _stored(world, "abl.pdb"),
        "method": "xray",
        "chains": [c["id"] for c in chains],
    }
    made = create(api_as(world, "editor"), world.project, values, "acme/protein.ProteinStructure")
    assert made.status_code == 201, made.content
    body: dict[str, Any] = made.json()
    return body


def relax(world: World, of: dict[str, Any], **kw: Any) -> Any:
    return launch(api_as(world, "editor"), world.project, {"structure": of["id"]}, RELAX, **kw)


def test_a_run_reads_what_its_inputs_references_name_as_of_launch(world: World) -> None:
    editor = api_as(world, "editor")
    abl, src = protein(editor, world.project, "abl1"), protein(editor, world.project, "src")
    pst = structure(world, abl, src)

    made = relax(world, pst)

    assert made.status_code == 201, made.content
    run = made.json()
    assert [(i["field"], i["index"], i["hrid"], i["revision"]) for i in run["included"]] == [
        ("chains", 0, abl["hrid"], 1),
        ("chains", 1, src["hrid"], 1),
    ]
    assert {(i["input"], i["position"]) for i in run["included"]} == {("structure", 0)}
    # A change after launch isn't what the run reads.
    changed = patch(editor, abl["id"], {"values": {"name": "Renamed"}}, '"1"')
    assert changed.status_code == 200, changed.content
    job = worker(world).lease().json()
    assert not list(schemas.check("run-input", job))
    (given,) = job["inputs"]["structure"]
    chains = given["included"]["chains"]
    assert [(c["hrid"], c["revision"], c["values"]["name"]) for c in chains] == [
        (abl["hrid"], 1, None),
        (src["hrid"], 1, None),
    ]
    assert chains[1]["values"]["sequence"] == src["values"]["sequence"]
    assert chains[0]["type"] == "acme/protein.Protein"
    # Each record's lineage, so a command can tell a lab's subtype of a type it handles.
    assert given["types"] == ["acme/protein.ProteinStructure", "iki/core.Entity"]
    assert chains[0]["types"] == [
        "acme/protein.Protein",
        "acme/dna.Sequence",
        "iki/core.Sequence",
        "iki/core.Entity",
    ]
    assert chains[0]["entry_id"] == abl["entry"]["id"]


@pytest.mark.parametrize("through_reference", [False, True])
def test_command_receives_both_revision_pins_after_a_registry_move(
    world: World, through_reference: bool
) -> None:
    editor = api_as(world, "editor")
    original = protein(editor, world.project)
    replacement = protein(editor, elsewhere(world), "src")
    curator = registrar(world)
    entry = f"/entries/{original['hrid']}"
    curated = curator.patch(
        entry,
        {"values": {"synonyms": ["reference"]}, "reason": "Reference label"},
        headers={"If-Match": curator.get(entry)["ETag"]},
    )
    assert curated.status_code == 200, curated.content
    if through_reference:
        made = relax(world, structure(world, original))
    else:
        made = launch(editor, world.project, {"protein": original["id"]})
    assert made.status_code == 201, made.content

    # Moving the record changes its live entry ID and HRID. The worker must receive
    # the original registry identity and the independently curated entry revision.
    superseded = curator.post(
        f"{entry}/supersede",
        {"replacement": replacement["hrid"], "reason": "Correction"},
        headers={"If-Match": curator.get(entry)["ETag"]},
    )
    assert superseded.status_code == 200, superseded.content
    record = f"/entities/{original['id']}"
    moved = editor.post(
        f"{record}/move", {"reason": "Correction"}, headers={"If-Match": editor.get(record)["ETag"]}
    )
    assert moved.status_code == 200, moved.content
    assert moved.json()["entry"]["id"] == replacement["entry"]["id"]

    job = worker(world).lease().json()
    assert not list(schemas.check("run-input", job))
    if through_reference:
        (row,) = job["inputs"]["structure"][0]["included"]["chains"]
    else:
        (row,) = job["inputs"]["protein"]
    assert (row["id"], row["revision"]) == (original["id"], 1)
    assert (row["entry_id"], row["entry_revision"]) == (original["entry"]["id"], 2)
    assert row["hrid"] == original["hrid"]
    assert row["values"]["synonyms"] == ["reference"]


def test_run_history_preserves_each_inclusion_slot_for_its_reader(world: World) -> None:
    editor = api_as(world, "editor")
    ours = protein(editor, world.project, "src")
    other = protein(editor, elsewhere(world), "abl1")
    ProjectLink.objects.create(project=world.project, entity_id=other["id"])
    run = relax(world, structure(world, other, ours)).json()
    ProjectLink.objects.filter(project=world.project, entity_id=other["id"]).delete()
    path = f"/runs/{run['hrid']}"
    assert editor.get(path).json()["included"] == run["included"]
    response = api_as(world, "viewer").get(path)
    assert response.status_code == 200, response.content
    hidden, visible = response.json()["included"]
    assert hidden == {**run["included"][0], "entity": None, "hrid": None, "revision": None}
    assert visible == run["included"][1]
    assert (
        other["id"].encode() not in response.content
        and other["hrid"].encode() not in response.content
    )


def test_an_input_that_includes_nothing_has_no_included(world: World) -> None:
    editor = api_as(world, "editor")
    made = launch(editor, world.project, {"protein": protein(editor, world.project)["id"]})
    assert made.status_code == 201, made.content
    assert made.json()["included"] == []
    job = worker(world).lease().json()
    assert "included" not in job["inputs"]["protein"][0]


def test_a_reference_reads_the_projects_own_record_of_its_entry(world: World) -> None:
    editor = api_as(world, "editor")
    theirs = protein(editor, elsewhere(world), "src")
    # A reference holds a registry entry (ADR 0115) the project has: another project's
    # record, which the editor reads there, isn't one.
    values = {"structure": _stored(world, "abl.pdb"), "method": "xray", "chains": [theirs["id"]]}
    refused = problem(
        create(editor, world.project, values, "acme/protein.ProteinStructure"),
        422,
        "validation_failed",
    )
    assert "isn't in Kinase Atlas" in refused["errors"][0]["detail"]
    ours = protein(editor, world.project, "src")
    assert ours["entry"]["id"] == theirs["entry"]["id"] and ours["id"] != theirs["id"]
    # Named by either record, it's the entry; the run reads the project's own record.
    pst = structure(world, theirs)
    assert pst["values"]["chains"] == [theirs["entry"]["id"]]

    made = relax(world, pst)

    assert made.status_code == 201, made.content
    assert [i["hrid"] for i in made.json()["included"]] == [ours["hrid"]]
    (included,) = RunInclusion.objects.all()
    assert str(included.entity_id) == ours["id"]
    assert not ProjectLink.objects.filter(entity_id=theirs["id"], project=world.project).exists()


def test_a_reference_the_launcher_cant_read_is_refused(world: World) -> None:
    editor = api_as(world, "editor")
    abl = protein(editor, world.project)
    pst = structure(world, abl)
    # Purged since: it's gone, and only its tombstone is left.
    Entity.objects.filter(pk=abl["id"]).update(purged_at="2026-01-01T00:00:00Z")

    body = problem(relax(world, pst), 422, "validation_failed")

    assert body["errors"] == [
        {
            "pointer": "#/inputs/structure",
            "detail": f"{pst['hrid']}'s chains isn't an entity you can read.",
        }
    ]
    assert not ActionRun.objects.exists()


def test_a_change_to_what_was_included_runs_again(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(services, "plugin_sha", lambda plugin: SHA)
    editor = api_as(world, "editor")
    abl = protein(editor, world.project)
    pst = structure(world, abl)
    first = relax(world, pst).json()
    pod = built_pod(world)
    job = pod.lease().json()
    values = {**job["inputs"]["structure"][0]["values"]}
    made = {"type": "acme/protein.ProteinStructure", "values": values}
    assert done(pod, job, output(creates=[made]))["run_status"] == "succeeded"

    same = relax(world, pst).json()
    assert same["reused"] is True and same["reused_from"]["hrid"] == first["hrid"]
    assert [i["hrid"] for i in same["included"]] == [abl["hrid"]]

    changed = patch(editor, abl["id"], {"values": {"name": "Renamed"}}, '"1"')
    assert changed.status_code == 200, changed.content
    again = relax(world, pst).json()
    assert again["reused"] is False and again["status"] == "queued"
    assert [(i["hrid"], i["revision"]) for i in again["included"]] == [(abl["hrid"], 2)]


def test_reproducing_reads_what_the_run_read(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services, "plugin_sha", lambda plugin: SHA)
    editor = api_as(world, "editor")
    abl = protein(editor, world.project)
    pst = structure(world, abl)
    first = relax(world, pst).json()
    pod = built_pod(world)
    job = pod.lease().json()
    values = {**job["inputs"]["structure"][0]["values"]}
    made = {"type": "acme/protein.ProteinStructure", "values": values}
    assert done(pod, job, output(creates=[made]))["run_status"] == "succeeded"
    changed = patch(editor, abl["id"], {"values": {"name": "Renamed"}}, '"1"')
    assert changed.status_code == 200, changed.content

    again = editor.post(f"/runs/{first['hrid']}/reproduce", {"reason": "Audit"})

    assert again.status_code == 201, again.content
    assert [(i["hrid"], i["revision"]) for i in again.json()["included"]] == [(abl["hrid"], 1)]
    job = pod.lease().json()
    (given,) = job["inputs"]["structure"]
    assert given["included"]["chains"][0]["values"]["name"] is None
    # One the person can't read any more isn't read for them.
    ProjectLink.objects.filter(entity_id=abl["id"]).delete()
    assert editor.post(f"/runs/{first['hrid']}/reproduce", {"reason": "x"}).status_code == 403


@pytest.mark.parametrize("withdrawn", ["direct", "included"])
def test_a_lease_requires_the_pinned_records_in_the_run_project(
    world: World, withdrawn: str
) -> None:
    editor = api_as(world, "editor")
    chain = protein(editor, world.project)
    pst = structure(world, chain)
    run = relax(world, pst).json()
    record = pst if withdrawn == "direct" else chain
    ProjectLink.objects.filter(entity_id=record["id"], project=world.project).delete()

    assert worker(world).lease().status_code == 204
    ended = ActionRun.objects.get(pk=run["id"])
    assert ended.status == "rejected" and ended.error == {
        "code": "not_allowed",
        "message": "The run needs every pinned input, reference and file in its project.",
    }


def test_a_service_commands_reference_values_match_its_project(world: World) -> None:
    lab = build_lab(world)
    request, shared, private = request_with_private_sample(lab)
    run = schedule(lab, "tech", lab.core, request["id"])
    assert run.status_code == 201, run.content
    leased = worker(world).lease()
    assert leased.status_code == 200, leased.content
    values = leased.json()["inputs"]["request"][0]["values"]
    assert values["private_sample"] == "unreadable"
    assert values["comparison_samples"] == [shared["id"], "unreadable"]
    assert values["sample_notes"] == [{"sample": "unreadable", "note": "Comparison"}]
    assert private["id"] not in leased.content.decode()
    assert private["hrid"] not in leased.content.decode()


def test_pinned_reference_values_need_project_access_at_reuse_and_reproduction(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(services, "plugin_sha", lambda plugin: SHA)
    editor = api_as(world, "editor")
    sample = create(
        editor, elsewhere(world), {"label": "Calibration"}, "acme/protein.Sample"
    ).json()
    linked = editor.put(f"/entities/{sample['id']}/projects/{world.project.pk}", {})
    assert linked.status_code == 201, linked.content
    chain = protein(editor, world.project)
    edited = patch(editor, chain["id"], {"values": {"source": sample["id"]}}, '"1"')
    assert edited.status_code == 200, edited.content
    pst = structure(world, chain)
    run = relax(world, pst).json()
    pod = built_pod(world)
    job = pod.lease().json()
    (given,) = job["inputs"]["structure"]
    assert given["included"]["chains"][0]["values"]["source"] == sample["id"]
    made = {"type": "acme/protein.ProteinStructure", "values": given["values"]}
    assert done(pod, job, output(creates=[made]))["run_status"] == "succeeded"
    assert relax(world, pst).json()["reused"]

    pending = relax(world, pst, reuse=False).json()
    unlinked = editor.delete(f"/entities/{sample['id']}/projects/{world.project.pk}")
    assert unlinked.status_code == 204, unlinked.content
    refused = problem(editor.post(f"/runs/{run['id']}/reproduce", {}), 409, "refused")
    assert (
        refused["detail"] == "The run needs every pinned input, reference and file in its project."
    )
    assert pod.lease().status_code == 204
    assert ActionRun.objects.get(pk=pending["id"]).status == "rejected"

    # The same revisions have a different reuse key when this command sees a marker.
    masked = relax(world, pst).json()
    assert masked["reused"] is False
    assert ActionRun.objects.get(pk=masked["id"]).reference_mask == [
        "/structure/0/included/chains/0/values/source"
    ]
    job = pod.lease().json()
    assert (
        job["inputs"]["structure"][0]["included"]["chains"][0]["values"]["source"] == "unreadable"
    )
    assert done(pod, job, output(creates=[made]))["run_status"] == "succeeded"
    assert relax(world, pst).json()["reused_from"]["id"] == masked["id"]

    # A new grant lets a fresh run see the ID, and preserves the masked run's exact input.
    assert (
        editor.put(f"/entities/{sample['id']}/projects/{world.project.pk}", {}).status_code == 201
    )
    assert relax(world, pst).json()["reused_from"]["id"] == run["id"]
    assert editor.post(f"/runs/{masked['id']}/reproduce", {}).status_code == 201
    job = pod.lease().json()
    assert (
        job["inputs"]["structure"][0]["included"]["chains"][0]["values"]["source"] == "unreadable"
    )


def test_a_bundle_keeps_what_its_runs_read_through_references(
    world: World, tmp_path: Path, settings: Any
) -> None:
    settings.IKI_BUNDLE_TRUSTED_KEYS = {"test": (PUBLIC,)}
    OrgRoleAssignment.objects.create(user=world.users["admin"], role=OrgRole.ORG_ADMIN)
    editor = api_as(world, "editor")
    other = elsewhere(world)
    ProjectMembership.objects.create(
        project=other, user=world.users["admin"], role=ProjectRole.VIEWER
    )
    sample = create(editor, other, {"label": "Reference sample"}, "acme/protein.Sample").json()
    kept: dict[str, Any] = {}

    def elsewhere_() -> list[Any]:
        abl = protein(editor, world.project)
        changed = patch(editor, abl["id"], {"values": {"source": sample["id"]}}, '"1"')
        assert changed.status_code == 200, changed.content
        pst = structure(world, abl)
        run = relax(world, pst).json()
        pod = worker(world)
        job = pod.lease().json()
        values = {**job["inputs"]["structure"][0]["values"]}
        made = {"type": "acme/protein.ProteinStructure", "values": values}
        assert done(pod, job, output(creates=[made]))["run_status"] == "succeeded"
        kept.update(run=run, abl=abl)
        relaxed = ActionRun.objects.get(pk=run["id"]).effects.get().entity_id
        return [Entity.objects.get(pk=relaxed)]

    path = bundle_of(world, tmp_path, elsewhere_)
    (line,) = reading.read(path, {}, limit=1024**3).lines("runs.ndjson")
    abl = kept["abl"]
    assert line["reference_mask"] == ["/structure/0/included/chains/0/values/source"]
    assert line["included"] == [
        {
            "input": "structure",
            "position": 0,
            "field": "chains",
            "index": 0,
            "entity": abl["id"],
            "revision": 2,
        }
    ]
    api = api_as(world, "admin")
    browser = Api(world.browser("admin"))
    made = upload(browser, world.project, path)
    assert made["state"] == "planned" and made["plan"]["applicable"], made
    assert applied(browser, made)["state"] == "applied"
    inclusion = RunInclusion.objects.get(run_id=line["id"])
    assert (str(inclusion.entity_id), inclusion.revision) == (abl["id"], 2)
    restored = included.manifest(ActionRun.objects.get(pk=line["id"]))
    assert (
        restored.inputs["structure"][0]["included"]["chains"][0]["values"]["source"] == "unreadable"
    )
    shown = api.get(f"/runs/{line['id']}").json()
    assert [i["hrid"] for i in shown["included"]] == [abl["hrid"]]
    assert shown["imported"]["included_elsewhere"] == []


def test_each_run_of_a_batch_reads_its_own_entitys_references(world: World) -> None:
    editor = api_as(world, "editor")
    abl, src = protein(editor, world.project, "abl1"), protein(editor, world.project, "src")
    first, second = structure(world, abl), structure(world, src)

    made = editor.post(
        f"/actions/{RELAX}/runs/batch",
        {
            "project": str(world.project.pk),
            "each": "structure",
            "entities": [first["id"], second["id"]],
        },
    )

    assert made.status_code == 201, made.content
    runs = made.json()["runs"]
    read = [[(i["position"], i["hrid"]) for i in r["included"]] for r in runs]
    assert read == [[(0, abl["hrid"])], [(0, src["hrid"])]]


def test_a_workflow_reads_what_its_steps_include_in_its_project(world: World) -> None:
    editor = api_as(world, "editor")
    protein(editor, elsewhere(world), "src")
    ours = protein(editor, world.project, "src")
    pst = structure(world, ours)

    started = editor.post(
        "/workflows/acme/protein.relax_twice/runs",
        {"project": str(world.project.pk), "inputs": {"structure": pst["id"]}},
    )

    assert started.status_code == 201, started.content
    assert {str(i.entity_id) for i in RunInclusion.objects.all()} <= {ours["id"]}


def test_a_run_reads_the_lab_list_option_its_record_names(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lab list's option (ADR 0270) is its registry's, with no record in any project: a
    run reads it as the option, at its revision, without adding it to the run's project;
    a curator's later relabel isn't what the run reads; and the run shows it, is reused
    and is reproduced in its project as any other."""
    monkeypatch.setattr(services, "plugin_sha", lambda plugin: SHA)
    editor = api_as(world, "editor")
    project = str(world.project.pk)
    methods = editor.get("/lists/acme/protein.PurificationMethod/options", project=project)
    assert methods.status_code == 200, methods.content
    protein_a = next(o for o in methods.json() if o["code"] == "protein_a")
    made = editor.post(
        "/entities",
        {
            "type": "acme/protein.Prep",
            "project": project,
            "values": {"name": "Anti-HER2 prep 12", "method": protein_a["id"]},
        },
    )
    assert made.status_code == 201, made.content

    run = launch(editor, world.project, {"prep": made.json()["id"]}, "acme/protein.weigh_prep")

    assert run.status_code == 201, run.content
    assert [(i["field"], i["revision"]) for i in run.json()["included"]] == [("method", 1)]
    assert not ProjectLink.objects.filter(entity_id=protein_a["id"]).exists()
    curator = registrar(world)
    relabelled = curator.patch(
        "/lists/acme/protein.PurificationMethod/options/protein_a",
        {"label": "MabSelect SuRe", "reason": "The resin we use"},
        headers={"If-Match": f'"{protein_a["revision"]}"'},
        registry=str(world.project.registry_id),
    )
    assert relabelled.status_code == 200, relabelled.content
    pod = built_pod(world)
    job = pod.lease().json()
    assert not list(schemas.check("run-input", job))
    (given,) = job["inputs"]["prep"]
    (method,) = given["included"]["method"]
    assert method["id"] == protein_a["id"] == given["values"]["method"]
    assert method["type"] == "acme/protein.PurificationMethod"
    assert (method["revision"], method["values"]["code"]) == (1, "protein_a")
    assert method["values"]["label"] == "Protein A affinity"
    named = {"entity": given["id"], "values": {"name": "Anti-HER2 prep 12 (weighed)"}}
    assert done(pod, job, output(updates={"prep": [named]}))["run_status"] == "succeeded"
    shown = editor.get(f"/runs/{run.json()['id']}").json()
    assert [(i["hrid"], i["revision"]) for i in shown["included"]] == [(protein_a["hrid"], 1)]
    reproduced = editor.post(f"/runs/{run.json()['id']}/reproduce", {})
    assert reproduced.status_code == 201, reproduced.content
    again = pod.lease().json()
    assert again["inputs"]["prep"][0]["included"]["method"][0]["values"]["label"] == (
        "Protein A affinity"
    )


def runs_reading_everything(world: World) -> list[ActionRun]:
    """Runs in two projects whose manifests read all a manifest reads: chains, one of them
    since moved to another registry entry, and one each since unlinked from its project or
    purged; a service request's masked references and its two sides; a lab list's option;
    and files, one still being scanned and one since quarantined."""
    from test_scan_gates import _file, _infected, _legacy_lot

    from iki_server.entities import services as entities

    editor = api_as(world, "editor")
    runs = []

    def launched(made: Any) -> None:
        assert made.status_code == 201, made.content
        runs.append(ActionRun.objects.get(pk=made.json()["id"]))

    abl, src, lck = (protein(editor, world.project, name) for name in ("abl1", "src", "lck"))
    launched(relax(world, structure(world, abl, src)))
    # Moved, with its entry curated first: the run reads both pinned revisions.
    original = protein(editor, world.project, "kit")
    replacement = protein(editor, elsewhere(world), "met")
    curator = registrar(world)
    entry = f"/entries/{original['hrid']}"
    curated = curator.patch(
        entry,
        {"values": {"synonyms": ["reference"]}, "reason": "Reference label"},
        headers={"If-Match": curator.get(entry)["ETag"]},
    )
    assert curated.status_code == 200, curated.content
    launched(relax(world, structure(world, original, lck)))
    superseded = curator.post(
        f"{entry}/supersede",
        {"replacement": replacement["hrid"], "reason": "Correction"},
        headers={"If-Match": curator.get(entry)["ETag"]},
    )
    assert superseded.status_code == 200, superseded.content
    record = f"/entities/{original['id']}"
    moved = editor.post(
        f"{record}/move", {"reason": "Correction"}, headers={"If-Match": editor.get(record)["ETag"]}
    )
    assert moved.status_code == 200, moved.content
    withdrawn, purged = protein(editor, world.project, "ret"), protein(editor, world.project, "alk")
    launched(relax(world, structure(world, withdrawn)))
    launched(relax(world, structure(world, purged, src)))
    ProjectLink.objects.filter(entity_id=withdrawn["id"], project=world.project).delete()
    Entity.objects.filter(pk=purged["id"]).update(purged_at="2026-01-01T00:00:00Z")

    project = str(world.project.pk)
    methods = editor.get("/lists/acme/protein.PurificationMethod/options", project=project)
    assert methods.status_code == 200, methods.content
    protein_a = next(o for o in methods.json() if o["code"] == "protein_a")
    prep = create(
        editor, world.project, {"name": "Anti-HER2 prep 12", "method": protein_a["id"]}, PREP
    )
    assert prep.status_code == 201, prep.content
    launched(launch(editor, world.project, {"prep": prep.json()["id"]}, WEIGH))

    scanning, quarantined = _file(world, "scanning.pdf"), _file(world, "quarantined.pdf")
    for n, file in enumerate((scanning, quarantined)):
        values = {"label": f"Purity sample {n}", "lot": _legacy_lot(world, file).pk}
        sample = entities.create(world.users["editor"], SAMPLE, world.project, values).entity
        launched(launch(editor, world.project, {"sample": str(sample.pk)}, PURITY))
    _infected(quarantined)

    lab = build_lab(world)
    request, _, _ = request_with_private_sample(lab)
    launched(schedule(lab, "tech", lab.core, request["id"]))
    return runs


def _read(found: included.Manifest | included.Unavailable) -> Any:
    """What a manifest says, or why it's unavailable, comparably."""
    if isinstance(found, included.WaitingForScan):
        return ("waiting", str(found), sorted(found.pending), _read(found.found))
    if isinstance(found, included.Unavailable):
        return (type(found).__name__, str(found), found.ends_with)
    return (found.inputs, sorted(found.input_files), sorted(found.param_files))


def test_a_pack_reads_what_each_of_its_runs_would_read_alone(world: World) -> None:
    """A lease reads a pack's manifests at once (ADR 0055): each run's is the one it reads
    alone, whatever the others read, in its project or another."""
    runs = runs_reading_everything(world)

    together = included.manifests(runs)

    alone = {run.pk: _read(included.manifests([run])[run.pk]) for run in runs}
    assert {run.pk: _read(together[run.pk]) for run in runs} == alone
    for run in runs:
        found = together[run.pk]
        if isinstance(found, included.Unavailable):
            with pytest.raises(type(found), match=str(found)):
                included.manifest(run)
        else:
            assert _read(included.manifest(run)) == _read(found)
    # Each kind of reading is among them.
    outcomes = [alone[run.pk] for run in runs]
    read = json.dumps([o for o in outcomes if isinstance(o[0], dict)], default=str)
    for word in ("entry_revision", "request", UNREADABLE_REFERENCE):
        assert f'"{word}"' in read, word
    (chain, _) = alone[runs[1].pk][0]["structure"][0]["included"]["chains"]
    assert chain["entry_id"] != str(Entity.objects.get(pk=chain["id"]).entry_id)
    ended = [o[0] if o[0] == "waiting" else o[2] for o in outcomes if isinstance(o[0], str)]
    assert sorted(ended) == ["file_quarantined", "not_allowed", "not_allowed", "waiting"]
