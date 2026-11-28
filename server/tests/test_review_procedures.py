"""Each project chooses its release testing, stability, prep verification and omics results'
review (ADRs 0240 and 0266).

The recipes are checked against the plugins' own manifests. The installed plugins then
show that a project's choice reaches its runs, that a Research project needs no QA grant
to issue its certificate, and that a release case opened under one choice finishes under
it after the project changes.
"""

import datetime as dt
import importlib
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from api_client import Api
from django.db import transaction
from django.utils import timezone
from first_party_dependencies import FIRST_PARTY, PLUGINS, ROOT, build_order
from iki_test_plugins import run_output
from org_world import build_world
from scan_fixtures import clean_blob
from test_plugin_migrations import database, django, iki, migrate, sync
from test_signatures import ready, signed

from iki_server.api.entities import _flow_field
from iki_server.entities import services, states
from iki_server.entities.models import Entity
from iki_server.files.models import File
from iki_server.org.models import LabRoleGrant, Project, ProjectMembership, ProjectRole
from iki_server.org.services import Refused
from iki_server.runs import catalog, queue, results
from iki_server.runs import services as runs
from iki_server.runs.models import ActionRun, EffectKind, Job, RunEffect
from iki_server.setup import starters
from iki_server.signatures.services import verify
from iki_spec.compute import Binding
from iki_spec.validate import load_plugin

__all__ = ["database"]

PROCEDURES = (starters.LOT_RELEASE_TESTING, starters.STABILITY_STUDY, starters.PREP_VERIFICATION)
#: Omics results pin their flow when an import records them, and may be kept without
#: sign-off (ADR 0266).
RESULT_REVIEWS = (
    starters.PROTEOMICS_REVIEW,
    starters.BIOLOGICS_MS_REVIEW,
    starters.SEQUENCING_QUALITY_REVIEW,
    starters.SEQUENCING_ANALYSIS_REVIEW,
)
QC = "iki/qc_release."


@pytest.mark.parametrize("recipe", PROCEDURES + RESULT_REVIEWS, ids=lambda r: r.id)
def test_each_recipe_is_its_plugins_own(recipe: starters.Starter) -> None:
    """Its roles, setting, types and states are declared by the plugin that offers it, and
    each flow asks for exactly the roles that sign its route."""
    directory = PLUGINS / recipe.plugin.removeprefix("iki/")
    loaded = load_plugin(directory, dependencies=list(FIRST_PARTY[directory]))
    assert loaded.problems == [] and loaded.plugin is not None
    plugin = loaded.plugin
    assert plugin.starters == {recipe.id: recipe.version}
    assert set(plugin.settings[recipe.setting].values or ()) == set(recipe.flows)
    assert recipe.roles.keys() <= {r.qualified_name for r in plugin.roles.values()}
    assert recipe.pinned_by is not None and recipe.pinned_by in plugin.types
    for flow, type_name in recipe.flows.items():
        spec = plugin.types[type_name]
        [field] = [_flow_field(f) for f in spec.fields.values() if f.type == "state"]
        route = recipe.flow_states.get(flow) or [s["name"] for s in field["states"]]
        assert set(route) <= {s["name"] for s in field["states"]}
        signers = {
            role
            for state in field["states"]
            if state["name"] in route
            for signature in state["signatures"]
            for role in signature["roles"]
        }
        required = set(recipe.required_roles[flow])
        assert signers <= required, (flow, signers - required)
        assert set(recipe.independent_roles.get(flow, ())) <= required
        # Research is the simpler route: no analyst's submission, nothing to keep
        # independent. Release testing's QA approves its specifications in either flow.
        if flow == "research":
            assert flow not in recipe.independent_roles
            assert required < set(recipe.required_roles["regulated_qc"])
            assert not [r for r in required if r.endswith("analyst")]
        # Record only stays where records start: no state to sign, no one to name.
        if flow == "record_only":
            assert list(route) == [field["initial"]]
            assert required == set() and flow not in recipe.independent_roles


def test_the_installed_procedures_follow_each_projects_choice(
    tmp_path: Path, database: str
) -> None:
    build = tmp_path / "build"
    plugins = build_order(
        PLUGINS / "qc_release",
        PLUGINS / "stability",
        PLUGINS / "proteomics",
        PLUGINS / "sequencing_analysis",
        PLUGINS / "biologics_ms",
    )
    built = iki("plugin", "build", *plugins, "--output", build, cwd=ROOT)
    assert built.returncode == 0, built.stdout + built.stderr
    # The deployment's default is the stricter flow. A project chooses Research over it.
    (build / "iki_plugin_iki__qc_release" / "settings.json").write_text(
        '{"review_flow":"regulated_qc"}', encoding="utf-8"
    )
    migrate(build, database)
    sync(build, database)
    checked = django(
        build,
        database,
        "-c",
        "import django; django.setup(); "
        "from test_review_procedures import exercise_procedures; exercise_procedures()",
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr


def exercise_procedures() -> None:
    sys.path.insert(0, str(PLUGINS / "qc_release" / "scripts"))
    actions = importlib.import_module("qc_release")
    world = build_world()
    ready(world)
    project, editor, admin = world.project, world.users["editor"], world.users["admin"]
    assert {r.id for r in PROCEDURES + RESULT_REVIEWS} <= starters.installed_starters().keys()
    member = {
        name: project.memberships.get(user=world.users[name]).pk
        for name in ("editor", "runner", "admin")
    }

    def choose(recipe: starters.Starter, flow: str, roles: dict[str, list[str]]) -> None:
        assignments = {role: [member[who] for who in people] for role, people in roles.items()}
        args = (project, recipe.id, recipe.version, flow, assignments)
        plan = starters.preview(*args, by=admin)
        assert starters.apply(*args, by=admin, plan=plan["plan"])["changed"]

    def setting(plugin: str, name: str, where: Project = project) -> Any:
        action = next(a for a in catalog.actions().values() if a.plugin == plugin)
        return runs.plugin_settings(action, where)[name]

    # A second project chose nothing: the deployment's defaults apply there.
    elsewhere = Project.objects.create(team=world.team, name="Stability Lab")
    ProjectMembership.objects.create(project=elsewhere, user=editor, role=ProjectRole.EDITOR)
    assert setting("iki/qc_release", "review_flow", elsewhere) == "regulated_qc"
    assert setting("iki/stability", "review_flow", elsewhere) == "research"
    assert setting("iki/lots", "verification_flow", elsewhere) == "research"

    # This project's Admin chooses Research for all three. Each asks for its reviewer, and
    # neither an analyst nor stability or Lot QA. Release Testing QA approves specifications.
    choose(
        starters.LOT_RELEASE_TESTING,
        "research",
        {QC + "reviewer": ["runner"], QC + "qa": ["admin"]},
    )
    choose(
        starters.STABILITY_STUDY,
        "research",
        {"iki/stability.coordinator": ["editor"], "iki/stability.reviewer": ["runner"]},
    )
    choose(starters.PREP_VERIFICATION, "research", {"iki/lots.verification_reviewer": ["runner"]})
    granted = set(
        LabRoleGrant.objects.filter(membership__project=project).values_list("role", flat=True)
    )
    assert granted == {
        QC + "reviewer",
        QC + "qa",
        "iki/stability.coordinator",
        "iki/stability.reviewer",
        "iki/lots.verification_reviewer",
    }
    assert setting("iki/qc_release", "review_flow") == "research"
    assert setting("iki/stability", "review_flow") == "research"
    assert setting("iki/lots", "verification_flow") == "research"

    # Proteomics records its search results without sign-off: no one is named, nothing
    # granted. Mass spectrometry chooses Regulated QC, with three different signers.
    choose(starters.PROTEOMICS_REVIEW, "record_only", {})
    assert not LabRoleGrant.objects.filter(
        membership__project=project, role__startswith="iki/proteomics."
    ).exists()
    ms = "iki/biologics_ms."
    choose(
        starters.BIOLOGICS_MS_REVIEW,
        "regulated_qc",
        {ms + "analyst": ["editor"], ms + "reviewer": ["runner"], ms + "qa": ["admin"]},
    )
    assert setting("iki/proteomics", "review_flow") == "record_only"
    assert setting("iki/biologics_ms", "review_flow") == "regulated_qc"
    assert setting("iki/proteomics", "review_flow", elsewhere) == "research"
    assert setting("iki/biologics_ms", "review_flow", elsewhere) == "research"
    # Sequencing: FASTQ quality summaries signed off by one reviewer; analysis results
    # recorded without sign-off.
    choose(
        starters.SEQUENCING_QUALITY_REVIEW,
        "research",
        {"iki/sequencing.reviewer": ["runner"]},
    )
    choose(starters.SEQUENCING_ANALYSIS_REVIEW, "record_only", {})
    assert setting("iki/sequencing", "review_flow") == "research"
    assert setting("iki/sequencing_analysis", "review_flow") == "record_only"
    assert setting("iki/sequencing_analysis", "review_flow", elsewhere) == "research"
    assert not LabRoleGrant.objects.filter(
        membership__project=project, role__startswith="iki/sequencing_analysis."
    ).exists()
    # Regulated QC still needs its three independent signers.
    with pytest.raises(Refused, match="three different people"):
        starters.preview(
            project,
            starters.BIOLOGICS_MS_REVIEW.id,
            starters.BIOLOGICS_MS_REVIEW.version,
            "regulated_qc",
            {ms + r: [member["editor"]] for r in ("analyst", "reviewer", "qa")},
            by=admin,
        )

    environment = QC + "qc-tools"
    catalog.compute().bindings[environment] = Binding(
        environment=environment, templates=(("backend-small", 0),)
    )

    def make(kind: str, **values: Any) -> Entity:
        return Entity.objects.get(pk=services.create(editor, kind, project, values).entity.pk)

    def sign(person: str, entity: Entity, state: str, *, field: str = "review") -> None:
        result = signed(world, person, str(entity.pk), state, field=field).signature
        assert result is not None and all(check.ok for check in verify(result))
        entity.refresh_from_db()
        assert states.values_of(entity)[field] == state

    def execute(name: str, entities: Mapping[str, Sequence[UUID]], **params: Any) -> ActionRun:
        action = catalog.action(QC + name)
        assert action is not None
        run = runs.launch(editor, action, project, entities, params, reuse_results=False)
        job = Job.objects.get(run=run)
        job.lease_token, job.lease_expires_at = uuid4(), timezone.now() + queue.LEASE_TTL
        output = run_output(actions.ACTIONS[name](queue.payload(job, run)))
        with transaction.atomic():
            results.commit(ActionRun.objects.select_for_update().get(pk=run.pk), output)
        return run

    def made(run: ActionRun, kind: str) -> Entity:
        return RunEffect.objects.get(
            run=run, kind=EffectKind.CREATED, entity__type=QC + kind
        ).entity

    def flow_of(entity: Entity) -> str:
        return str(services.current_values_of([entity])[entity.pk]["review_flow"])

    # PubChem CID 5234 identifies sodium chloride. The specification is the historical USP
    # excerpt the plugin's tests use; the lots and observations are synthetic.
    material = make(
        "iki/small_molecule.Compound",
        name="Sodium chloride",
        smiles="[Na+].[Cl-]",
        inchikey="FAPWRFPIFSIZLT-UHFFFAOYSA-M",
    )
    assert material.entry_id is not None
    schedule = make(QC + "SpecificationSchedule", material=str(material.entry_id))
    specification = json.loads(
        (PLUGINS / "qc_release/tests/data/sodium-chloride-specification.json").read_text()
    )
    version = make(QC + "SpecificationVersion", schedule=str(schedule.pk), **specification)
    sign("admin", version, "approved", field="approval")
    execute("publish_version", {"schedule": [schedule.pk], "version": [version.pk]})
    today = dt.datetime.now(dt.UTC).date().isoformat()
    lot = make("iki/lots.GenericLot", of=str(material.entry_id), lot_number="Research NaCl lot")
    case = made(
        execute("open_release", {"lot": [lot.pk], "schedule": [schedule.pk]}, testing_on=today),
        "ReleaseCase",
    )
    assert flow_of(case) == "research"
    assay, drying = specification["criteria"]

    def observe(criterion: dict[str, Any], value: float, **more: Sequence[UUID]) -> ActionRun:
        params = {key: criterion[key] for key in ("attribute", "method", "unit")}
        return execute("record_test", {"case": [case.pk], **more}, **params, value=value)

    first = observe(assay, 98.7)
    original, investigation = made(first, "ReleaseTest"), made(first, "ResearchOOSInvestigation")
    evidence = b"Synthetic evidence: the titrant was standardized against the wrong lot."
    attachment = File.objects.create(
        blob=clean_blob(sha256="d" * 64, size=len(evidence), storage_uri="s3://iki/procedures"),
        project=project,
        name="Synthetic investigation.txt",
        media_type="text/plain",
    )
    services.update(
        editor,
        investigation,
        {
            "resolution": "invalidated",
            "findings": "Synthetic example: the titrant was standardized incorrectly.",
            "evidence": str(attachment.pk),
        },
        expected_revision=investigation.revision,
    )
    result = signed(
        world, "runner", str(investigation.pk), "signed_off", field="review", reason="Assigned"
    ).signature
    assert result is not None
    repeat = made(
        observe(assay, 99.8, repeat_of=[original.pk], investigation=[investigation.pk]),
        "ReleaseTest",
    )
    moisture = made(observe(drying, 0.3), "ReleaseTest")

    # The project now chooses Regulated QC, with three different signers. The open case
    # keeps Research: its certificate is a research certificate, not refused for the switch.
    choose(
        starters.LOT_RELEASE_TESTING,
        "regulated_qc",
        {QC + "analyst": ["editor"], QC + "reviewer": ["runner"], QC + "qa": ["admin"]},
    )
    assert setting("iki/qc_release", "review_flow") == "regulated_qc"
    certificate = made(
        execute(
            "prepare_certificate",
            {
                "case": [case.pk],
                "tests": [original.pk, repeat.pk, moisture.pk],
                "investigations": [investigation.pk],
            },
        ),
        "ResearchCertificate",
    )
    assert flow_of(certificate) == "research"
    sign("runner", certificate, "signed_off")
    # Its certificate of analysis is issued under the research procedure too.
    coa = made(
        execute("issue_certificate", {"certificate": [certificate.pk]}), "CertificateOfAnalysis"
    )
    assert flow_of(coa) == "research"
    sign("runner", coa, "issued")
    issued = Api(world.browser("viewer")).get(
        f"/entities/{coa.pk}/export", format="signed-report-json"
    )
    assert issued.status_code == 200, issued.content
    assert len(issued.json()["signatures"]) == 1

    # A case opened now is regulated.
    second = make("iki/lots.GenericLot", of=str(material.entry_id), lot_number="QC NaCl lot")
    regulated = made(
        execute("open_release", {"lot": [second.pk], "schedule": [schedule.pk]}, testing_on=today),
        "ReleaseCase",
    )
    assert flow_of(regulated) == "regulated_qc"
    assert flow_of(case) == "research"
    # The other project's runs still read the deployment's default.
    assert setting("iki/qc_release", "review_flow", elsewhere) == "regulated_qc"
