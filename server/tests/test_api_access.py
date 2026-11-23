"""The API's authorization matrix: every operation against every principal.

401 means "sign in first", 404 "you can't know this exists", 403 "you can
see it but not do this". A test fails if an operation in the OpenAPI
document isn't listed here, so no endpoint ships without its row.
"""

import contextlib
import typing
from collections.abc import Callable
from typing import Any

import pytest
from api_client import Api
from ninja import NinjaAPI
from org_world import PRINCIPALS, World, build_world
from pydantic import BaseModel

from iki_server.accounts import service_accounts
from iki_server.api.v1 import api
from iki_server.api.worker import api as worker_api

pytestmark = pytest.mark.django_db

UNAUTHENTICATED = {"anonymous": 401, "inactive": 401}
PROJECT_VISIBLE = ("org_admin", "viewer", "editor", "runner", "admin", "via_team", "mixed")

#: Builds the request: (path, body) or (path, body, headers).
Build = Callable[[World], tuple[Any, ...]]


def row(default: int, **overrides: int) -> dict[str, int]:
    return {**dict.fromkeys(PRINCIPALS, default), **UNAUTHENTICATED, **overrides}


def visible(default_for_visible: int, **overrides: int) -> dict[str, int]:
    """A project operation: 404 unless the caller can see the project."""
    return row(404, **{**dict.fromkeys(PROJECT_VISIBLE, default_for_visible), **overrides})


def fixed(path: str, body: Any = None) -> Build:
    return lambda w: (path, body)


def viewer(suffix: str = "", body: Any = None) -> Build:
    return lambda w: (f"/users/{w.users['viewer'].pk}{suffix}", body)


def robot(body: Any = None) -> Build:
    def build(w: World) -> tuple[str, Any]:
        account = service_accounts.create("Robot", "", by=w.users["org_admin"])
        return f"/service-accounts/{account.pk}", body

    return build


def team(suffix: str = "", body: Any = None) -> Build:
    return lambda w: (f"/teams/{w.team.pk}{suffix}", body)


def project(suffix: str = "", body: Any = None) -> Build:
    return lambda w: (f"/projects/{w.project.pk}{suffix}", body)


def viewers_notification(w: World) -> tuple[str, Any]:
    from iki_server.notifications.models import Notification

    note = Notification.objects.create(
        recipient=w.users["viewer"], kind="project.added", title="Added", project=w.project
    )
    return f"/notifications/{note.pk}", {"read": True}


def viewer_grant(w: World) -> tuple[str, Any]:
    membership = w.project.memberships.get(user=w.users["viewer"])
    return f"/projects/{w.project.pk}/members/{membership.pk}", None


def viewer_lab_roles(w: World) -> tuple[str, Any]:
    membership = w.project.memberships.get(user=w.users["viewer"])
    path = f"/projects/{w.project.pk}/members/{membership.pk}/lab-roles"
    return path, {"roles": ["acme/protein.qa"]}


def starter_choice(operation: str) -> Build:
    def build(w: World) -> tuple[str, Any]:
        from iki_server.org.models import Project, ProjectMembership, ProjectRole

        requester = Project.objects.create(team=w.team, name="Assay requester")
        ProjectMembership.objects.create(
            project=requester, user=w.users["admin"], role=ProjectRole.EDITOR
        )
        # The fixture deployment has no such starter. Only an authorized project Admin
        # reaches the catalog lookup.
        return f"/starters/dose-response/{operation}", {
            "project": str(w.project.pk),
            "requester_project": str(requester.pk),
            "version": "1.0.0",
            "flow": "research",
            "role_assignments": {},
        }

    return build


def _webhook(w: World, *, org: bool = False) -> Any:
    from iki_server.webhooks import services as webhooks

    hook, _ = webhooks.create(
        project=None if org else w.project,
        url="https://hooks.example.com/iki",
        description="",
        chosen=["team.created"] if org else ["project.updated"],
        by=w.users["org_admin" if org else "admin"],
    )
    return hook


def webhook(suffix: str = "", body: Any = None, *, org: bool = False) -> Build:
    return lambda w: (f"/webhooks/{_webhook(w, org=org).pk}{suffix}", body)


def delivery(suffix: str = "") -> Build:
    def build(w: World) -> tuple[str, Any]:
        hook = _webhook(w)
        sent = hook.deliveries.get()  # the ping
        return f"/webhooks/{hook.pk}/deliveries/{sent.pk}{suffix}", None

    return build


def _entity(w: World) -> Any:
    from iki_server.entities import services as entities

    return entities.create(
        w.users["editor"], "acme/dna.DNASequence", w.project, {"bases": "ACGT"}
    ).entity


def plasmid_export(w: World) -> tuple[Any, ...]:
    """A type that exports a file (ADR 0171): DNASequence's fixture exports none."""
    from iki_server.entities import services as entities

    found = entities.create(w.users["editor"], "acme/dna.Plasmid", w.project, {"bases": "ACGT"})
    return f"/entities/{found.entity.pk}/export?format=genbank", None


def project_board(w: World) -> tuple[Any, ...]:
    return "/components/acme/protein.board/entities", {"project": str(w.project.pk)}


def sample_state(w: World) -> tuple[Any, ...]:
    """A sample's status set (ADR 0125), which needs a lab role nobody has."""
    from iki_server.entities import services as entities

    found = entities.create(w.users["editor"], "acme/protein.Sample", w.project, {"label": "S"})
    return (
        f"/entities/{found.entity.pk}/states/status",
        {"state": "in_testing", "reason": ""},
        {"If-Match": '"1"'},
    )


def batch_hold(w: World) -> tuple[Any, ...]:
    """A batch's hold reviewed (ADR 0127), which needs the QA role nobody has."""
    from iki_server.entities import services as entities
    from iki_server.entities.models import RuleHold

    found = entities.create(
        w.users["editor"],
        "acme/protein.Batch",
        w.project,
        {"name": "L", "purity": 50.0},
        hold_reason="Measured twice.",
    )
    hold = RuleHold.objects.get(entity_id=found.entity.pk)
    return f"/entities/{found.entity.pk}/holds/{hold.pk}/review", {"reason": "Fine."}


def sample_amount(suffix: str, body: Any = None) -> Build:
    """A sample with an amount (ADR 0139); a transfer's body names another."""

    def build(w: World) -> tuple[Any, ...]:
        import uuid

        from iki_server.entities import services as entities

        def made() -> Any:
            label = f"S-{uuid.uuid4().hex[:8]}"
            amount = {"value": "100", "unit": "uL"}
            return entities.create(
                w.users["editor"], "acme/protein.Sample", w.project, {"label": label}, amount=amount
            ).entity

        found = made()
        given = dict(body or {})
        if given.get("to") == "another":
            given["to"] = str(made().pk)
        if "values" in given:
            given["values"] = {"label": f"A-{uuid.uuid4().hex[:8]}"}
        return f"/entities/{found.pk}{suffix}", given or None

    return build


def plugin_candidate(w: World) -> tuple[str, Any]:
    from iki_server.entities import registry
    from iki_spec import API_VERSION
    from iki_spec.model import plugin_to_dict

    plugin = registry.plugins()["acme/dna"]
    return f"/plugins/{plugin.name}/preflight", {
        "apiVersion": API_VERSION,
        "plugin": plugin_to_dict(plugin),
    }


def sample_take(w: World) -> tuple[Any, ...]:
    """Taking a received sample as its analyst (ADR 0122), which needs the role nobody has."""
    from iki_server.entities import services as entities

    found = entities.create(
        w.users["editor"], "acme/protein.Sample", w.project, {"label": "S-take"}
    )
    return f"/entities/{found.entity.pk}/take", {"field": "analyst"}, {"If-Match": '"1"'}


def sample_watch(label: str, body: Any = None) -> Build:
    """A sample, which tells its watchers when it's in review (ADR 0129)."""

    def build(w: World) -> tuple[Any, ...]:
        from iki_server.entities import services as entities

        found = entities.create(
            w.users["editor"], "acme/protein.Sample", w.project, {"label": label}
        )
        return f"/entities/{found.entity.pk}/watch", body

    return build


def samples_assign(w: World) -> tuple[Any, ...]:
    """Emptying a sample's analyst: each record anyone can't change is skipped, with why."""
    from iki_server.entities import services as entities

    found = entities.create(
        w.users["editor"], "acme/protein.Sample", w.project, {"label": "S-assign"}
    )
    body = {"entities": [str(found.entity.pk)], "field": "analyst", "person": None}
    return "/entities/assign", body


def _service(w: World) -> Any:
    """A core facility beside the project, taking its assay requests (ADR 0132)."""
    from iki_server.entities import requests
    from iki_server.org.models import Project

    core = Project.objects.create(team=w.team, name="Protein Core")
    requests.set_intake(
        core,
        types=["acme/protein.AssayRequest"],
        from_registry=True,
        sources=[],
        by=w.users["org_admin"],
    )
    return core


def _assay_request(w: World) -> Any:
    from iki_server.entities import services as entities

    return entities.create(
        w.users["editor"], "acme/protein.AssayRequest", w.project, {"title": "Purity"}
    ).entity


def request_submit(w: World) -> tuple[Any, ...]:
    """Sending a request to a service that takes it: the asking project's Editors send it."""
    core = _service(w)
    found = _assay_request(w)
    return f"/entities/{found.pk}/submit", {"service": str(core.pk)}, {"If-Match": '"1"'}


def request_sides(w: World) -> tuple[Any, ...]:
    from iki_server.entities import requests

    core = _service(w)
    found = _assay_request(w)
    requests.submit(w.users["editor"], found, core, expected_revision=1)
    return f"/entities/{found.pk}/request", None


def request_sent_to(w: World) -> tuple[Any, ...]:
    from iki_server.entities import requests

    requests.submit(w.users["editor"], _assay_request(w), _service(w), expected_revision=1)
    return f"/projects/{w.project.pk}/sent-to", None


def request_sent_from(w: World) -> tuple[Any, ...]:
    """The projects that sent requests to the world's project, as a service."""
    from iki_server.accounts.models import User
    from iki_server.entities import requests
    from iki_server.entities import services as entities
    from iki_server.org.models import Project, ProjectMembership, ProjectRole

    asking = Project.objects.create(team=w.team, name="Discovery")
    # One per world: the IDOR fuzzing builds a foreign one beside it.
    chemist = User.objects.create_user(f"chemist-{w.project.pk}@example.com", "Chemist")
    ProjectMembership.objects.create(project=asking, user=chemist, role=ProjectRole.EDITOR)
    requests.set_intake(
        w.project,
        types=["acme/protein.AssayRequest"],
        from_registry=True,
        sources=[],
        by=w.users["org_admin"],
    )
    found: Any = entities.create(chemist, "acme/protein.AssayRequest", asking, {"title": "Purity"})
    requests.submit(chemist, found.entity, w.project, expected_revision=1)
    return f"/projects/{w.project.pk}/sent-from", None


INTAKE = {"types": ["acme/protein.AssayRequest"], "from_registry": True, "projects": []}


def _signature(w: World) -> Any:
    """The editor's signature setting a lot In review (ADR 0126)."""
    from test_signatures import lot, ready, signed
    from test_states import ANALYST, roles

    ready(w)
    roles(w, editor=(ANALYST,))
    made = lot(w)
    return signed(w, "editor", made["id"], "in_review").signature


def signature(suffix: str = "", body: Any = None) -> Build:
    return lambda w: (f"/signatures/{_signature(w).pk}{suffix}", body)


def signed_lot(suffix: str = "") -> Build:
    return lambda w: (f"/entities/{_signature(w).entity_id}{suffix}", None)


def lot_signing(w: World) -> tuple[Any, ...]:
    """Signing for a lot's first state, which needs the Analyst role nobody has."""
    from test_signatures import lot, ready

    ready(w)
    made = lot(w)
    return (
        f"/entities/{made['id']}/signatures",
        {"field": "status", "state": "in_review", "acknowledge": True, "return_to": "/"},
        {"If-Match": '"1"'},
    )


def editors_signing(w: World) -> tuple[Any, ...]:
    from datetime import timedelta

    from django.utils import timezone
    from test_signatures import lot, provider

    from iki_server.signatures.models import Signing

    made = lot(w)
    found = Signing.objects.create(
        user=w.users["editor"],
        session="0" * 64,
        entity_id=made["id"],
        revision=1,
        field="status",
        state="in_review",
        index=0,
        return_to="/",
        provider=provider(),
        expires_at=timezone.now() + timedelta(minutes=10),
    )
    return f"/signings/{found.pk}", None


def dashboard(w: World) -> tuple[Any, ...]:
    from iki_server.entities import services as entities

    spec = {
        "version": 1,
        "panels": [{"id": "hi", "component": "iki.text", "props": {"text": "Hi"}}],
    }
    made = entities.create(
        w.users["editor"], "iki/core.App", w.project, {"name": "Hi", "spec": spec}
    )
    return f"/projects/{w.project.pk}/dashboard", {"app": str(made.entity.pk)}


def app_draft(w: World) -> tuple[Any, ...]:
    from iki_server.entities import app_drafts

    spec = {
        "version": 1,
        "panels": [{"id": "hi", "component": "iki.text", "props": {"text": "Hi"}}],
    }
    made = app_drafts.create(w.users["editor"], w.project, "Hi", spec)
    return f"/apps/drafts/{made.pk}", None


def entity(suffix: str = "", body: Any = None, *, if_match: bool = False) -> Build:
    def build(w: World) -> tuple[Any, ...]:
        found = _entity(w)
        headers = {"If-Match": f'"{found.revision}"'} if if_match else None
        return f"/entities/{found.pk}{suffix}", body, headers

    return build


def dynamic_collection(w: World) -> tuple[Any, ...]:
    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity

    made = entities.create(
        w.users["editor"], "iki/core.Collection", w.project, {"name": "Kin", "search": {"q": "kin"}}
    )
    found = Entity.objects.get(pk=made.entity.pk)
    return f"/collections/{found.pk}/freeze", {}, {"If-Match": f'"{found.revision}"'}


def linked_into_archived(w: World) -> tuple[str, Any]:
    """An entity of the project, also in the archived project (whose Editors can't change it)."""
    from iki_server.entities.models import ProjectLink

    found = _entity(w)
    ProjectLink.objects.create(entity_id=found.pk, project=w.archived)
    return f"/entities/{found.pk}/projects/{w.archived.pk}", None


def export_request(w: World) -> tuple[str, Any]:
    hrid = _entity(w).hrid
    return f"/projects/{w.project.pk}/bundles", {"entities": [hrid], "reason": "For production"}


def bundle_export(suffix: str = "") -> Build:
    def build(w: World) -> tuple[str, Any]:
        from iki_server.bundles import exports
        from iki_server.bundles.models import BundleExport

        made = exports.start(
            w.users["admin"], w.project, reason="For production", entities=[_entity(w).hrid]
        )
        BundleExport.objects.filter(pk=made.pk).update(state="ready", sha256="0" * 64, size=1)
        return f"/bundles/{made.pk}{suffix}", None

    return build


def bundle_import(suffix: str = "", body: Any = None) -> Build:
    """An import someone started: nobody in the world is both an Org Admin and the
    project's Admin, as importing needs, so nobody sees it."""

    def build(w: World) -> tuple[str, Any]:
        from iki_server.bundles import imports
        from iki_server.org import permissions
        from iki_server.org.models import OrgRole, OrgRoleAssignment

        admin = w.users["admin"]
        role = OrgRoleAssignment.objects.create(user=admin, role=OrgRole.ORG_ADMIN)
        permissions.forget(admin)
        made = imports.start(admin, w.project, name="b.tar.gz", size=10, sha256="0" * 64)
        role.delete()
        permissions.forget(admin)
        return f"/imports/{made.pk}{suffix}", body

    return build


IMPORT_BODY = {"name": "b.tar.gz", "size": 10, "sha256": "0" * 64}


def _protein(w: World) -> Any:
    from iki_server.entities import services as entities

    return entities.create(
        w.users["editor"], "acme/protein.Protein", w.project, {"sequence": "MKV"}
    ).entity


def _run(w: World) -> Any:
    from iki_server.runs import catalog
    from iki_server.runs import services as runs

    action = catalog.action("acme/protein.compute_properties")
    assert action is not None
    return runs.launch(w.users["editor"], action, w.project, {"protein": [_protein(w).pk]}, {})


def launch_run(w: World) -> tuple[str, Any]:
    body = {"project": str(w.project.pk), "inputs": {"protein": str(_protein(w).pk)}}
    return "/actions/acme/protein.compute_properties/runs", body


def launch_batch(w: World) -> tuple[str, Any]:
    body = {"project": str(w.project.pk), "each": "protein", "entities": [str(_protein(w).pk)]}
    return "/actions/acme/protein.compute_properties/runs/batch", body


def start_workflow(w: World) -> tuple[str, Any]:
    kit = str(_protein(w).pk)
    body = {"project": str(w.project.pk), "inputs": {"query": kit, "target": kit}}
    return "/workflows/acme/protein.weigh_and_align/runs", body


def workflow_run(suffix: str = "", body: Any = None) -> Build:
    def build(w: World) -> tuple[str, Any]:
        from iki_server.runs import workflows

        found = workflows.workflow("acme/protein.weigh_and_align")
        assert found is not None
        kit = _protein(w).pk
        made = workflows.start(
            w.users["editor"], found, w.project, {"query": [kit], "target": [kit]}, {}
        )
        return f"/workflow-runs/{made.pk}{suffix}", body

    return build


def cancelled_workflow_run() -> Build:
    """The editor's run of weigh_and_align, cancelled: so it may be re-run."""

    def build(w: World) -> tuple[str, Any]:
        from iki_server.runs import workflows
        from iki_server.runs.models import WorkflowRun

        path, _ = workflow_run()(w)
        made = WorkflowRun.objects.get(pk=path.rsplit("/", 1)[-1])
        workflows.cancel(w.users["editor"], made)
        return f"{path}/rerun", {}

    return build


def gated_step(verb: str, body: Any) -> Build:
    """The editor's run of review_then_weigh, its weighing waiting for a Runner's approval."""

    def build(w: World) -> tuple[str, Any]:
        from iki_server.runs import workflows
        from iki_server.runs.models import ActionRun, RunStatus

        found = workflows.workflow("acme/protein.review_then_weigh")
        assert found is not None
        kit = _protein(w).pk
        made = workflows.start(
            w.users["editor"], found, w.project, {"query": [kit], "target": [kit]}, {}
        )
        ActionRun.objects.filter(workflow=made).update(status=RunStatus.SUCCEEDED)
        workflows.advance(made.pk)
        return f"/workflow-runs/{made.pk}/steps/weigh/{verb}", body

    return build


def run(suffix: str = "", body: Any = None) -> Build:
    return lambda w: (f"/runs/{_run(w).pk}{suffix}", body)


def batch(suffix: str = "", body: Any = None) -> Build:
    def build(w: World) -> tuple[str, Any]:
        from iki_server.runs import catalog
        from iki_server.runs import services as runs

        action = catalog.action("acme/protein.compute_properties")
        assert action is not None
        (made,) = runs.launch_each(
            w.users["editor"], action, w.project, "protein", [_protein(w).pk], {}, {}
        )
        return f"/batches/{made.batch_id}{suffix}", body

    return build


def approval(suffix: str = "", body: Any = None) -> Build:
    """A request the runner's agent made: theirs alone to decide."""

    def build(w: World) -> tuple[str, Any]:
        from datetime import timedelta

        from django.utils import timezone

        from iki_server.runs.models import RunApproval

        found = RunApproval.objects.create(
            project=w.project,
            requested_by=w.users["runner"],
            client="Notebook",
            expires_at=timezone.now() + timedelta(days=7),
        )
        return f"/run-approvals/{found.pk}{suffix}", body

    return build


def _registration(w: World) -> Any:
    from iki_server.runs import workers

    token, _ = workers.create_registration(
        label="Pods", tier="backend", by=w.users["compute_admin"]
    )
    return token


def _self_hosted(w: World) -> Any:
    from iki_server.runs import workers

    token, _ = workers.create_registration(
        label="Lab 3",
        tier="self_hosted",
        by=w.users["compute_admin"],
        scope=workers.make_scope([w.team.pk], []),
    )
    worker, _ = workers.register(
        token, name="hplc-pc-1", version="0.1.0", capacity={"cpu": 2}, plugins=[]
    )
    return worker


def _budget(w: World) -> Any:
    from iki_server.runs.models import Budget

    return Budget.objects.create(
        project=w.project, amount=100, currency="USD", created_by=w.users["compute_admin"]
    )


def _setup(w: World, state: str = "awaiting_approval") -> Any:
    """A setup build someone other than the Compute admin asked for."""
    from iki_server.runs.models import SetupBuild

    return SetupBuild.objects.create(
        hrid="SETUP-000099",
        environment="acme/prediction.alphafold-gpu",
        plugin_version="1.0.0",
        image="ghcr.io/acme/alphafold@sha256:" + "a" * 64,
        place="iki-runs",
        template="gpu-a100",
        version=1,
        spec={"script": "setup/download_dbs.sh", "volumes": {}, "network": []},
        state=state,
        requested_by=w.users["org_admin"],
        timeout=3600,
    )


def _quota(w: World) -> Any:
    from iki_server.runs.models import RunQuota

    return RunQuota.objects.create(
        project=w.project, max_running=4, created_by=w.users["compute_admin"]
    )


def _worker(w: World) -> Any:
    from iki_server.runs import workers

    worker, _ = workers.register(
        _registration(w),
        name="pod-1",
        version="0.1.0",
        capacity={"cpu": 2, "memory": 2**31},
        plugins=[],
    )
    return worker


# --- Storage (ADR 0180) -------------------------------------------------------------------------


def _box(w: World, *, project: Any = None, rows: int = 2) -> Any:
    from iki_server.entities import services as entities

    return entities.create(
        w.users["editor"],
        "acme/protein.Box",
        project or w.project,
        {"name": "Box", "rows": rows, "columns": 2},
    ).entity


def _tube(w: World, *, project: Any = None, into: Any = None, out: bool = False) -> Any:
    import uuid

    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity
    from iki_server.storage import services as storage

    found = entities.create(
        w.users["editor"],
        "acme/protein.Tube",
        project or w.project,
        {"barcode": f"T-{uuid.uuid4().hex[:8]}"},
        amount={"value": "10", "unit": "uL"},
    ).entity
    found = Entity.objects.get(pk=found.pk)
    if into is not None:
        storage.move(w.users["editor"], [storage.Wanted(found, into, None)], reason="Put away")
    if out:
        storage.check_out(w.users["editor"], [found], to=w.users["editor"], reason="Thawing")
    return found


def stored(suffix: str = "", body: Any = None) -> Build:
    """A tube in a box of the project."""

    def build(w: World) -> tuple[Any, ...]:
        box = _box(w)
        tube = _tube(w, into=box)
        return suffix.format(tube=tube.pk, box=box.pk), body

    return build


def storage_moves(w: World) -> tuple[Any, ...]:
    box, tube = _box(w), _tube(w)
    items = [{"record": str(tube.pk), "to": str(box.pk), "position": "A1"}]
    return "/storage/moves", {"items": items, "reason": "Put away"}


def storage_check_out(w: World) -> tuple[Any, ...]:
    tube = _tube(w, into=_box(w))
    return "/storage/check-out", {"records": [str(tube.pk)], "reason": "Thawing"}


def storage_check_in(w: World) -> tuple[Any, ...]:
    tube = _tube(w, into=_box(w), out=True)
    return "/storage/check-in", {"items": [{"record": str(tube.pk)}], "reason": "Done"}


def storage_hand_over(w: World) -> tuple[Any, ...]:
    tube = _tube(w, into=_box(w), out=True)
    body = {"record": str(tube.pk), "to": str(w.users["admin"].pk), "reason": "Bench 4"}
    return "/storage/hand-over", body


def storage_labels(w: World) -> tuple[Any, ...]:
    return "/storage/labels", {"records": [str(_tube(w).pk)], "size": "cryo", "format": "zpl"}


def _received(w: World) -> Any:
    """An assay request sending a tube to a core, whose analyst has started receiving it."""
    from iki_server.accounts.models import User
    from iki_server.entities import requests
    from iki_server.entities import services as entities
    from iki_server.entities.models import Entity
    from iki_server.org.models import LabRoleGrant, ProjectMembership, ProjectRole
    from iki_server.storage import receipts

    core = _service(w)
    tube = _tube(w)
    found = entities.create(
        w.users["editor"],
        "acme/protein.AssayRequest",
        w.project,
        {"title": "Purity", "tubes": [str(tube.pk)]},
    ).entity
    found = Entity.objects.get(pk=found.pk)
    requests.submit(w.users["editor"], found, core, expected_revision=1)
    tech = User.objects.create_user(f"tech-{w.project.pk}@example.com", "Tech")
    grant = ProjectMembership.objects.create(project=core, user=tech, role=ProjectRole.EDITOR)
    LabRoleGrant.objects.create(membership=grant, role="acme/protein.analyst")
    return found, receipts.start(tech, found)


def receipt(suffix: str = "", body: Any = None) -> Build:
    def build(w: World) -> tuple[Any, ...]:
        found, started = _received(w)
        line = started.lines.get()
        path = suffix.format(request=found.pk, receipt=started.pk, line=line.pk)
        return path, body

    return build


def reconciliation(suffix: str = "", body: Any = None) -> Build:
    def build(w: World) -> tuple[Any, ...]:
        from iki_server.storage import reconciliations

        box = _box(w)
        tube = _tube(w, into=box)
        started = reconciliations.start(w.users["editor"], box)
        line = started.lines.get(entity=tube)
        payload = (
            {**body, "plan_revision": started.plan_revision}
            if suffix.endswith(("/complete", "/lines/{line}"))
            else body
        )
        return suffix.format(reconciliation=started.pk, line=line.pk), payload

    return build


#: Entities are read by anyone with a role in one of their projects. Org Admins see that
#: projects exist, but read their data only with a role, like everyone else.
ENTITY_READERS = ("viewer", "editor", "runner", "admin", "via_team", "mixed")
ENTITY_EDITORS = ("editor", "runner", "admin", "via_team", "mixed")


def saved(suffix: str = "", body: Any = None) -> Build:
    """A search the editor saved and shared with the project."""

    def build(w: World) -> tuple[str, Any]:
        from iki_server.searches import services

        found = services.create(
            w.users["editor"], w.project, name="Shared", query={"q": "kin"}, shared=True
        )
        return f"/saved-searches/{found.pk}{suffix}", body

    return build


def readers(status: int, **overrides: int) -> dict[str, int]:
    return row(404, **{**dict.fromkeys(ENTITY_READERS, status), **overrides})


def editors(status: int) -> dict[str, int]:
    return readers(403, **dict.fromkeys(ENTITY_EDITORS, status))


def registry(suffix: str = "", body: Any = None) -> Build:
    def build(w: World) -> tuple[Any, ...]:
        from iki_server.org.models import Registry

        return f"/registries/{Registry.default().pk}{suffix}", body

    return build


def entry(suffix: str = "", body: Any = None, *, if_match: bool = False) -> Build:
    """A registry entry (ADR 0115): the editor's DNA sequence's, which no one is a registrar
    of. Its ID alone grants nothing: only those who read a record of it see it."""

    def build(w: World) -> tuple[Any, ...]:
        from iki_server.entities.models import Entity

        record = _entity(w)
        found = Entity.objects.get(pk=record.entry_id)
        headers = {"If-Match": f'"{found.revision}"'} if if_match else None
        given = body(w, found) if callable(body) else body
        return f"/entries/{found.pk}{suffix}", given, headers

    return build


def readers_of_entries(status: int, **overrides: int) -> dict[str, int]:
    """Who reads the entry: those who read a record of it; the rest get 404."""
    return readers(status, **overrides)


def _workbook(w: World) -> Any:
    """A file the editor added to the project, to import."""
    from iki_server.files import services as files
    from iki_server.files.services import EMPTY_SHA256

    upload = files.start(
        w.users["editor"],
        w.project,
        name="manifest.csv",
        media_type="text/csv",
        size=0,
        sha256=EMPTY_SHA256,
    )
    upload.refresh_from_db()
    return upload.file


def workbook_import(w: World) -> tuple[str, Any]:
    return f"/projects/{w.project.pk}/spreadsheet-imports", {
        "file": str(_workbook(w).pk),
        "type": "acme/protein.Sample",
    }


def spreadsheet_import(suffix: str = "", body: Any = None) -> Build:
    """The editor's import of a manifest, its columns mapped: only they see it until it's
    committed."""

    def build(w: World) -> tuple[str, Any]:
        from iki_server.spreadsheets.models import SpreadsheetImport

        made = SpreadsheetImport.objects.create(
            project=w.project,
            type="acme/protein.Sample",
            file=_workbook(w),
            requested_by=w.users["editor"],
            state="mapping",
            kind="csv",
            sheets=[
                {
                    "name": "manifest",
                    "rows": 2,
                    "columns": 1,
                    "hidden": False,
                    "sample": [["Label"], ["S-1"]],
                    "cells": [["Label"], ["S-1"]],
                }
            ],
            sheet="manifest",
            columns=[{"index": 0, "field": "label"}],
        )
        return f"/spreadsheet-imports/{made.pk}{suffix}", body

    return build


def spreadsheet_mapping(w: World) -> tuple[str, Any]:
    from iki_server.spreadsheets.models import SavedMapping

    saved = SavedMapping.objects.create(
        project=w.project,
        type="acme/protein.Sample",
        name="Plate reader",
        columns=[{"header": "Label", "field": "label"}],
        headers=["Label"],
        created_by=w.users["editor"],
        updated_by=w.users["editor"],
    )
    return f"/spreadsheet-mappings/{saved.pk}", None


PDB = b"HEADER    TEST\nEND\n"


def _upload(w: World, *, sent: bool = False) -> Any:
    import hashlib

    import requests

    from iki_server.files import services as files

    upload = files.start(
        w.users["editor"],
        w.project,
        name="1abc.pdb",
        media_type="chemical/x-pdb",
        size=len(PDB),
        sha256=hashlib.sha256(PDB).hexdigest(),
    )
    if sent:
        (part,) = files.parts(upload)
        requests.put(part["url"], data=PDB, timeout=30).raise_for_status()
    return upload


def upload(suffix: str = "", body: Any = None, *, sent: bool = False) -> Build:
    return lambda w: (f"/uploads/{_upload(w, sent=sent).pk}{suffix}", body)


def _lab_object() -> str:
    """A CSV in a versioned lab bucket that files may be registered from (see the matrix)."""
    import uuid

    from iki_server.files import storage

    s3 = storage.client()
    with contextlib.suppress(s3.exceptions.BucketAlreadyOwnedByYou):
        s3.create_bucket(Bucket="lab-data")
    s3.put_bucket_versioning(Bucket="lab-data", VersioningConfiguration={"Status": "Enabled"})
    key = f"runs/{uuid.uuid4().hex}.csv"
    s3.put_object(Bucket="lab-data", Key=key, Body=b"well,od600\n")
    return f"s3://lab-data/{key}"


def registration() -> Build:
    def build(w: World) -> tuple[Any, ...]:
        from iki_server.files import services as files

        made = files.register(w.users["editor"], w.project, uri=_lab_object())
        return f"/registrations/{made.pk}", None

    return build


def file(suffix: str = "") -> Build:
    """An empty file, which is ready as soon as it's added."""

    def build(w: World) -> tuple[Any, ...]:
        from iki_server.files import services as files
        from iki_server.files.services import EMPTY_SHA256

        added = files.start(
            w.users["editor"],
            w.project,
            name="e.txt",
            media_type="text/plain",
            size=0,
            sha256=EMPTY_SHA256,
        )
        added.refresh_from_db()
        return f"/files/{added.file_id}{suffix}", None

    return build


OUTSIDE_API = "acme/protein.fold"


def outside_api_approved(w: World) -> tuple[str, Any]:
    """FoldCloud approved for the project (ADR 0130), for revoking it."""
    from iki_server.entities import registry
    from iki_server.runs import outside

    api = registry.plugins()["acme/protein"].apis["fold"]
    outside.approve(w.users["org_admin"], w.project, api, note="")
    return f"/outside-apis/{OUTSIDE_API}/approvals/{w.project.pk}", None


ORG_ADMIN_ONLY = row(403, org_admin=200)
COMPUTE_ONLY = row(403, compute_admin=200)
TEAM_VISIBLE = row(404, org_admin=200, team_member=200)
TEAM_MANAGE = row(404, org_admin=200, team_member=403)
PROJECT_MEMBERS = visible(403, org_admin=200, admin=200)

#: (method, OpenAPI path, how to build the request, expected status per principal)
CASES: list[tuple[str, str, Build, dict[str, int]]] = [
    ("GET", "/me", fixed("/me"), row(200)),
    ("GET", "/users", fixed("/users"), ORG_ADMIN_ONLY),
    ("GET", "/users", fixed("/users?email=editor@example.com"), row(200)),
    ("GET", "/users/{user_id}", viewer(), row(200)),
    ("PATCH", "/users/{user_id}", viewer(body={"active": False}), ORG_ADMIN_ONLY),
    ("DELETE", "/users/{user_id}/sessions", viewer("/sessions"), row(403, org_admin=204)),
    ("GET", "/users/{user_id}/org-roles", viewer("/org-roles"), ORG_ADMIN_ONLY),
    (
        "PUT",
        "/users/{user_id}/org-roles",
        viewer("/org-roles", {"roles": ["auditor"]}),
        ORG_ADMIN_ONLY,
    ),
    ("GET", "/service-accounts", fixed("/service-accounts"), ORG_ADMIN_ONLY),
    (
        "POST",
        "/service-accounts",
        fixed("/service-accounts", {"name": "LIMS sync"}),
        row(403, org_admin=201),
    ),
    ("GET", "/service-accounts/{account_id}", robot(), ORG_ADMIN_ONLY),
    (
        "PATCH",
        "/service-accounts/{account_id}",
        robot({"description": "Plates"}),
        ORG_ADMIN_ONLY,
    ),
    ("GET", "/teams", fixed("/teams"), row(200)),
    ("POST", "/teams", fixed("/teams", {"name": "Genomics"}), row(403, org_admin=201)),
    ("GET", "/teams/{team_id}", team(), TEAM_VISIBLE),
    ("PATCH", "/teams/{team_id}", team(body={"description": "Proteins"}), TEAM_MANAGE),
    ("GET", "/teams/{team_id}/members", team("/members"), TEAM_VISIBLE),
    (
        "PUT",
        "/teams/{team_id}/members/{user_id}",
        lambda w: (f"/teams/{w.team.pk}/members/{w.users['outsider'].pk}", None),
        TEAM_MANAGE,
    ),
    (
        "DELETE",
        "/teams/{team_id}/members/{user_id}",
        lambda w: (f"/teams/{w.team.pk}/members/{w.users['team_member'].pk}", None),
        row(404, org_admin=204, team_member=403),
    ),
    ("GET", "/projects", fixed("/projects"), row(200)),
    (
        "POST",
        "/projects",
        lambda w: ("/projects", {"team": str(w.team.pk), "name": "New Work"}),
        # Org Admins can see the team but only its members create projects in it.
        row(404, team_member=201, org_admin=403),
    ),
    ("GET", "/projects/{project_id}", project(), visible(200)),
    (
        "PATCH",
        "/projects/{project_id}",
        project(body={"description": "Kinases"}),
        visible(403, admin=200),
    ),
    ("GET", "/project-roles", fixed("/project-roles"), row(200)),
    ("GET", "/projects/{project_id}/members", project("/members"), visible(200)),
    ("GET", "/projects/{project_id}/costs", project("/costs"), visible(200)),
    (
        "GET",
        "/projects/{project_id}/grantable-teams",
        project("/grantable-teams"),
        visible(403, org_admin=200, admin=200),
    ),
    (
        "PUT",
        "/projects/{project_id}/members/users/{user_id}",
        lambda w: (
            f"/projects/{w.project.pk}/members/users/{w.users['outsider'].pk}",
            {"role": "viewer"},
        ),
        PROJECT_MEMBERS,
    ),
    (
        "PUT",
        "/projects/{project_id}/members/teams/{team_id}",
        lambda w: (f"/projects/{w.project.pk}/members/teams/{w.team.pk}", {"role": "viewer"}),
        PROJECT_MEMBERS,
    ),
    (
        "DELETE",
        "/projects/{project_id}/members/{membership_id}",
        viewer_grant,
        visible(403, org_admin=204, admin=204),
    ),
    # Lab roles (ADR 0116): given with a membership, as members are.
    ("GET", "/lab-roles", fixed("/lab-roles"), row(200)),
    (
        "PUT",
        "/projects/{project_id}/members/{membership_id}/lab-roles",
        viewer_lab_roles,
        PROJECT_MEMBERS,
    ),
    # Searching everything (ADR 0176): anyone signed in, finding only what they can read.
    ("GET", "/search", fixed("/search?q=kin"), row(200)),
    # Entities: read with a role in any of their projects, changed by the owner's Editors.
    ("POST", "/plugins/{name}/preflight", plugin_candidate, ORG_ADMIN_ONLY),
    ("GET", "/starters", fixed("/starters"), row(200)),
    (
        "GET",
        "/starters",
        lambda w: (f"/starters?project={w.project.pk}", None),
        visible(200, org_admin=403),
    ),
    (
        "POST",
        "/starters/{starter}/preview",
        starter_choice("preview"),
        visible(403, admin=404),
    ),
    (
        "POST",
        "/starters/{starter}/apply",
        starter_choice("apply"),
        visible(403, admin=404),
    ),
    ("GET", "/entity-types", fixed("/entity-types"), row(200)),
    ("GET", "/entity-types/{name}", fixed("/entity-types/acme/dna.DNASequence"), row(200)),
    (
        "GET",
        "/entity-types/{name}/flow",
        fixed("/entity-types/acme/dna.DNASequence/flow"),
        row(200),
    ),
    ("GET", "/entities", fixed("/entities"), row(200)),
    ("POST", "/entities/search", lambda w: ("/entities/search", {"q": "kin"}), row(200)),
    ("GET", "/entities/search.csv", fixed("/entities/search.csv?q=kin"), row(200)),
    # Over what each caller reads: anyone signed in, and in a project, those who can see it.
    ("GET", "/entities/counts", fixed("/entities/counts"), row(200)),
    (
        "GET",
        "/entities/counts",
        lambda w: (f"/entities/counts?project={w.project.pk}", None),
        visible(200),
    ),
    (
        "GET",
        "/entities/lookup",
        fixed("/entities/lookup?type=acme/protein.Protein&field=lims_id&value=LIMS-1"),
        row(200),
    ),
    # Saved searches: a project's readers save their own; the owner changes one; Admins
    # can remove shared ones.
    (
        "GET",
        "/saved-searches",
        lambda w: (f"/saved-searches?project={w.project.pk}", None),
        readers(200, org_admin=403),
    ),
    (
        "POST",
        "/saved-searches",
        lambda w: (
            "/saved-searches",
            {"project": str(w.project.pk), "name": "Mine", "query": {"q": "kin"}},
        ),
        readers(201, org_admin=403),
    ),
    # Searches of every project you can read (ADR 0176): anyone's own.
    ("GET", "/saved-searches", fixed("/saved-searches"), row(200)),
    (
        "POST",
        "/saved-searches",
        fixed("/saved-searches", {"name": "Everywhere", "query": {"q": "kin"}}),
        row(201),
    ),
    ("GET", "/saved-searches/{saved_id}", saved(), readers(200)),
    (
        "PATCH",
        "/saved-searches/{saved_id}",
        saved(body={"name": "Renamed"}),
        readers(403, editor=200),
    ),
    ("DELETE", "/saved-searches/{saved_id}", saved(), readers(403, editor=204, admin=204)),
    (
        "POST",
        "/entities",
        lambda w: (
            "/entities",
            {
                "type": "acme/dna.DNASequence",
                "project": str(w.project.pk),
                "values": {"bases": "GC"},
            },
        ),
        visible(403, **dict.fromkeys(ENTITY_EDITORS, 201)),
    ),
    ("GET", "/entities/{ref}", entity(), readers(200)),
    (
        "PATCH",
        "/entities/{ref}",
        entity(body={"values": {"name": "pUC19"}}, if_match=True),
        editors(200),
    ),
    # Only those with one of a state's lab roles set it: nobody here has one.
    ("POST", "/entities/{ref}/states/{field}", sample_state, readers(403)),
    # Amounts (ADR 0139): read by readers, changed by editors.
    ("GET", "/entities/{ref}/amount", sample_amount("/amount"), readers(200)),
    ("GET", "/entities/{ref}/amount/movements", sample_amount("/amount/movements"), readers(200)),
    (
        "POST",
        "/entities/{ref}/amount/draw",
        sample_amount("/amount/draw", {"amount": {"value": 1, "unit": "uL"}}),
        editors(200),
    ),
    (
        "POST",
        "/entities/{ref}/amount/transfer",
        sample_amount("/amount/transfer", {"to": "another", "amount": {"value": 1, "unit": "uL"}}),
        editors(200),
    ),
    (
        "POST",
        "/entities/{ref}/amount/adjust",
        sample_amount(
            "/amount/adjust", {"amount": {"value": 5, "unit": "uL"}, "reason": "Recounted"}
        ),
        editors(200),
    ),
    (
        "POST",
        "/entities/{ref}/aliquots",
        sample_amount(
            "/aliquots", {"count": 1, "each": {"value": 1, "unit": "uL"}, "values": {"label": "A"}}
        ),
        editors(201),
    ),
    # Storage (ADR 0180): read by whoever reads the record; a container's contents changed
    # by its project's Editors.
    ("GET", "/storage/places", fixed("/storage/places"), row(200)),
    ("GET", "/storage/containers/{ref}", stored("/storage/containers/{box}"), readers(200)),
    ("GET", "/entities/{ref}/location", stored("/entities/{tube}/location"), readers(200)),
    (
        "GET",
        "/entities/{ref}/location/events",
        stored("/entities/{tube}/location/events"),
        readers(200),
    ),
    ("POST", "/storage/moves", storage_moves, readers(422, **dict.fromkeys(ENTITY_EDITORS, 200))),
    (
        "POST",
        "/storage/check-out",
        storage_check_out,
        readers(422, **dict.fromkeys(ENTITY_EDITORS, 200)),
    ),
    (
        "POST",
        "/storage/check-in",
        storage_check_in,
        readers(422, **dict.fromkeys(ENTITY_EDITORS, 200)),
    ),
    (
        "POST",
        "/storage/hand-over",
        storage_hand_over,
        readers(422, **dict.fromkeys(ENTITY_EDITORS, 200)),
    ),
    ("GET", "/storage/scan", fixed("/storage/scan?code=T-1"), row(200)),
    ("GET", "/storage/checked-out", fixed("/storage/checked-out"), row(200)),
    ("GET", "/storage/labels", fixed("/storage/labels"), row(200)),
    ("POST", "/storage/labels", storage_labels, readers(200)),
    # A request's receipts: read by its readers; received by the core's analysts alone,
    # whom this world has none of.
    ("GET", "/entities/{ref}/receipts", receipt("/entities/{request}/receipts"), readers(200)),
    ("POST", "/entities/{ref}/receipts", receipt("/entities/{request}/receipts"), readers(403)),
    ("GET", "/receipts/{receipt_id}", receipt("/receipts/{receipt}"), readers(200)),
    (
        "PATCH",
        "/receipts/{receipt_id}",
        receipt("/receipts/{receipt}", {"handed_over_by": "Courier"}),
        readers(403),
    ),
    (
        "POST",
        "/receipts/{receipt_id}/scan",
        receipt("/receipts/{receipt}/scan", {"code": "T-1"}),
        readers(403),
    ),
    (
        "PATCH",
        "/receipts/{receipt_id}/lines/{line_id}",
        receipt("/receipts/{receipt}/lines/{line}", {"outcome": "matched"}),
        readers(403),
    ),
    (
        "DELETE",
        "/receipts/{receipt_id}/lines/{line_id}",
        receipt("/receipts/{receipt}/lines/{line}"),
        readers(403),
    ),
    (
        "POST",
        "/receipts/{receipt_id}/complete",
        receipt("/receipts/{receipt}/complete", {"missing_rest": True}),
        readers(403),
    ),
    ("POST", "/receipts/{receipt_id}/cancel", receipt("/receipts/{receipt}/cancel"), readers(403)),
    # A container's reconciliations: read by its readers, made by its project's Editors.
    (
        "GET",
        "/storage/containers/{ref}/reconciliations",
        stored("/storage/containers/{box}/reconciliations"),
        readers(200),
    ),
    (
        "POST",
        "/storage/containers/{ref}/reconciliations",
        stored("/storage/containers/{box}/reconciliations"),
        editors(201),
    ),
    (
        "GET",
        "/reconciliations/{reconciliation_id}",
        reconciliation("/reconciliations/{reconciliation}"),
        readers(200),
    ),
    (
        "POST",
        "/reconciliations/{reconciliation_id}/scans",
        reconciliation(
            "/reconciliations/{reconciliation}/scans", {"scans": [{"position": "A2", "code": None}]}
        ),
        editors(200),
    ),
    (
        "PATCH",
        "/reconciliations/{reconciliation_id}/lines/{line_id}",
        reconciliation("/reconciliations/{reconciliation}/lines/{line}", {"resolution": None}),
        editors(200),
    ),
    (
        "POST",
        "/reconciliations/{reconciliation_id}/complete",
        reconciliation("/reconciliations/{reconciliation}/complete", {"missing_rest": True}),
        editors(200),
    ),
    (
        "POST",
        "/reconciliations/{reconciliation_id}/cancel",
        reconciliation("/reconciliations/{reconciliation}/cancel"),
        editors(200),
    ),
    # People in records (ADR 0122). Taking open work needs its lab role too: nobody has it.
    ("POST", "/entities/{ref}/take", sample_take, readers(403)),
    # A hold is reviewed by its rule's lab roles (ADR 0127): nobody here has one.
    ("POST", "/entities/{ref}/holds/{hold_id}/review", batch_hold, readers(403)),
    # Reassigning many skips what you can't change, rather than refusing them all; what you
    # can't read is 404.
    ("POST", "/entities/assign", samples_assign, readers(200)),
    # Requests between projects (ADR 0132): what a service takes is its Admins' to choose
    # and its members' to read; a request is sent by its project's Editors.
    ("GET", "/projects/{project_id}/intake", project("/intake"), visible(200, org_admin=403)),
    ("PUT", "/projects/{project_id}/intake", project("/intake", INTAKE), visible(403, admin=200)),
    ("GET", "/projects/{project_id}/services", project("/services"), visible(200, org_admin=403)),
    ("GET", "/projects/{project_id}/sent-to", request_sent_to, visible(200, org_admin=403)),
    ("GET", "/projects/{project_id}/sent-from", request_sent_from, visible(200, org_admin=403)),
    ("POST", "/entities/{ref}/submit", request_submit, editors(200)),
    ("GET", "/entities/{ref}/request", request_sides, readers(200)),
    # Whom its records can name is its data: an Org Admin sees the project, not its people.
    ("GET", "/projects/{project_id}/people", project("/people"), readers(200, org_admin=403)),
    # Each person's work is their own: there's always some, if only none.
    ("GET", "/me/work", fixed("/me/work"), row(200)),
    ("GET", "/me/work/open", fixed("/me/work/open"), row(200)),
    # Electronic signatures (ADR 0126): read by the record's readers; given only by those
    # with the lab role, and withdrawn only by their signer, before they're carried.
    ("POST", "/entities/{ref}/signatures", lot_signing, readers(403)),
    ("GET", "/entities/{ref}/signatures", signed_lot("/signatures"), readers(200)),
    ("GET", "/signatures/{signature_id}", signature(), readers(200)),
    ("GET", "/signatures/{signature_id}/verification", signature("/verification"), readers(200)),
    (
        "POST",
        "/signatures/{signature_id}/withdrawal",
        signature("/withdrawal", {"reason": ""}),
        readers(403, editor=409),
    ),
    ("GET", "/signings/{signing_id}", editors_signing, row(404, editor=200)),
    ("GET", "/me/signing", fixed("/me/signing"), row(200)),
    ("GET", "/signing-keys", fixed("/signing-keys"), row(200)),
    ("GET", "/signatures/certification", fixed("/signatures/certification"), row(200)),
    (
        "POST",
        "/signatures/certification",
        fixed("/signatures/certification", {"submitted_on": "2026-11-01", "reference": "QA-1"}),
        ORG_ADMIN_ONLY,
    ),
    ("POST", "/users/{user_id}/signing/unlock", viewer("/signing/unlock"), row(403, org_admin=409)),
    ("GET", "/entities/{ref}/revisions", entity("/revisions"), readers(200)),
    ("GET", "/entities/{ref}/revisions/{number}", entity("/revisions/1"), readers(200)),
    ("GET", "/entities/{ref}/provenance", entity("/provenance"), readers(200)),
    # A project's latest changes: its records' history, so its members' alone (ADR 0176).
    ("GET", "/projects/{project_id}/changes", project("/changes"), visible(200, org_admin=403)),
    # Its audit trail (ADR 0176): who it can be narrowed to, and all of it as CSV.
    ("GET", "/entities/{ref}/contributors", entity("/contributors"), readers(200)),
    ("GET", "/entities/{ref}/revisions.csv", entity("/revisions.csv"), readers(200)),
    ("GET", "/entities/{ref}/export", plasmid_export, readers(200)),
    ("GET", "/entities/{ref}/origins", entity("/origins"), readers(200)),
    ("GET", "/entities/{ref}/making", entity("/making"), readers(200)),
    ("GET", "/entities/{ref}/bundles", entity("/bundles"), readers(200)),
    # Across the graph (ADR 0123): as far as each reads.
    ("GET", "/entities/{ref}/upstream", entity("/upstream"), readers(200)),
    ("GET", "/entities/{ref}/downstream", entity("/downstream"), readers(200)),
    ("GET", "/entities/{ref}/upstream.csv", entity("/upstream.csv"), readers(200)),
    ("GET", "/entities/{ref}/downstream.csv", entity("/downstream.csv"), readers(200)),
    ("GET", "/entities/{ref}/graph", entity("/graph"), readers(200)),
    ("GET", "/entities/{ref}/projects", entity("/projects"), readers(200)),
    ("GET", "/entities/{ref}/apps", entity("/apps"), readers(200)),
    (
        "PUT",
        "/entities/{ref}/projects/{project_id}",
        lambda w: (f"/entities/{_entity(w).pk}/projects/{w.project.pk}", None),
        editors(200),
    ),
    (
        "DELETE",
        "/entities/{ref}/projects/{project_id}",
        linked_into_archived,
        editors(204),
    ),
    # Collections from searches: made and frozen by the owner project's Editors.
    (
        "POST",
        "/collections",
        lambda w: (
            "/collections",
            {
                "project": str(w.project.pk),
                "name": "Kin",
                "search": {"q": "kin"},
                "keep": "members",
            },
        ),
        visible(403, **dict.fromkeys(ENTITY_EDITORS, 201)),
    ),
    ("POST", "/collections/{ref}/freeze", dynamic_collection, editors(200)),
    # How to write an app, and checking one, are for everyone signed in: a check saves
    # nothing and reads only what the caller could.
    ("GET", "/apps/guide", fixed("/apps/guide"), row(200)),
    ("POST", "/apps/check", fixed("/apps/check", {"spec": {}}), row(200)),
    ("POST", "/helm/read", fixed("/helm/read", {"helm": ["RNA1{R(A)P}$$$$V2.0"]}), row(200)),
    # A draft: made by the project's Editors, opened only by the person it's for.
    (
        "POST",
        "/apps/drafts",
        lambda w: (
            "/apps/drafts",
            {
                "project": str(w.project.pk),
                "name": "Hi",
                "spec": {
                    "version": 1,
                    "panels": [{"id": "hi", "component": "iki.text", "props": {"text": "Hi"}}],
                },
            },
        ),
        visible(403, **dict.fromkeys(ENTITY_EDITORS, 201)),
    ),
    ("GET", "/apps/drafts/{draft_id}", app_draft, row(404, editor=200)),
    # Actions are listed to everyone signed in. Running on the backend tier takes Editor;
    # runs are read with their project, and cancelled by whoever may run them.
    ("GET", "/actions", fixed("/actions"), row(200)),
    ("GET", "/actions/{name}", fixed("/actions/acme/protein.compute_properties"), row(200)),
    (
        "POST",
        "/actions/{name}/runs",
        launch_run,
        visible(403, **dict.fromkeys(ENTITY_EDITORS, 201)),
    ),
    (
        "POST",
        "/actions/{name}/runs/batch",
        launch_batch,
        visible(403, **dict.fromkeys(ENTITY_EDITORS, 201)),
    ),
    ("GET", "/runs", fixed("/runs"), row(200)),
    ("GET", "/runs/{ref}", run(), readers(200)),
    ("POST", "/runs/{ref}/cancel", run("/cancel", {}), editors(200)),
    # A queued run has no results to compare with yet: those who may run it are told so.
    ("POST", "/runs/{ref}/reproduce", run("/reproduce", {}), editors(409)),
    ("GET", "/runs/{ref}/effects", run("/effects"), readers(200)),
    ("GET", "/runs/{ref}/attempts", run("/attempts"), readers(200)),
    ("GET", "/runs/{ref}/artifacts", run("/artifacts"), readers(200)),
    # It has none: readers learn that; others, that there's no such run.
    (
        "GET",
        "/runs/{ref}/artifacts/{artifact_id}/content",
        run("/artifacts/00000000-0000-7000-8000-000000000000/content"),
        readers(404),
    ),
    ("GET", "/batches", fixed("/batches"), row(200)),
    ("GET", "/batches/{ref}", batch(), readers(200)),
    ("POST", "/batches/{ref}/cancel", batch("/cancel", {}), editors(200)),
    ("GET", "/workflows", fixed("/workflows"), row(200)),
    ("GET", "/workflows/{name}", fixed("/workflows/acme/protein.weigh_and_align"), row(200)),
    (
        "POST",
        "/workflows/{name}/runs",
        start_workflow,
        visible(403, **dict.fromkeys(ENTITY_EDITORS, 201)),
    ),
    ("GET", "/workflow-runs", fixed("/workflow-runs"), row(200)),
    ("GET", "/workflow-runs/{ref}", workflow_run(), readers(200)),
    ("POST", "/workflow-runs/{ref}/cancel", workflow_run("/cancel", {}), editors(200)),
    ("POST", "/workflow-runs/{ref}/rerun", cancelled_workflow_run(), editors(201)),
    # A Runner or above approves, other than whoever started it (never with a token:
    # test_workflows).
    (
        "POST",
        "/workflow-runs/{ref}/steps/{step}/approve",
        gated_step("approve", {}),
        readers(403, runner=200, admin=200, editor=409),
    ),
    (
        "POST",
        "/workflow-runs/{ref}/steps/{step}/decline",
        gated_step("decline", {"note": "Wrong pair"}),
        readers(403, runner=200, admin=200, editor=409),
    ),
    # Only the person an agent asked for decides (and never with a token: test_run_approvals).
    ("GET", "/run-approvals/{approval_id}", approval(), readers(200)),
    (
        "POST",
        "/run-approvals/{approval_id}/approve",
        approval("/approve"),
        readers(403, runner=200),
    ),
    (
        "POST",
        "/run-approvals/{approval_id}/decline",
        approval("/decline", {}),
        readers(403, runner=200),
    ),
    ("GET", "/runs/{ref}/attempts/{attempt}/log", run("/attempts/1/log"), readers(200)),
    ("GET", "/runs/{ref}/attempts/{attempt}/log.txt", run("/attempts/1/log.txt"), readers(200)),
    # Workers and the tokens they register with are the Compute admin's.
    ("GET", "/compute/registration-tokens", fixed("/compute/registration-tokens"), COMPUTE_ONLY),
    (
        "POST",
        "/compute/registration-tokens",
        fixed("/compute/registration-tokens", {"label": "Pods", "tier": "backend"}),
        row(403, compute_admin=201),
    ),
    (
        "POST",
        "/compute/registration-tokens/{token_id}/revoke",
        lambda w: (f"/compute/registration-tokens/{_registration(w).pk}/revoke", {}),
        COMPUTE_ONLY,
    ),
    ("GET", "/compute/overview", fixed("/compute/overview"), COMPUTE_ONLY),
    ("GET", "/compute/workers", fixed("/compute/workers"), COMPUTE_ONLY),
    # Environments' volumes and the setup builds that fill them (ADR 0075).
    ("GET", "/compute/volumes", fixed("/compute/volumes"), COMPUTE_ONLY),
    ("GET", "/compute/setups", fixed("/compute/setups"), COMPUTE_ONLY),
    # No template here gives alphafold-gpu's volumes from a snapshot: nowhere to build.
    (
        "POST",
        "/compute/setups",
        fixed("/compute/setups", {"environment": "acme/prediction.alphafold-gpu"}),
        row(403, compute_admin=409),
    ),
    (
        "GET",
        "/compute/setups/{ref}",
        lambda w: (f"/compute/setups/{_setup(w).hrid}", None),
        COMPUTE_ONLY,
    ),
    (
        "GET",
        "/compute/setups/{ref}/log",
        lambda w: (f"/compute/setups/{_setup(w).hrid}/log", None),
        COMPUTE_ONLY,
    ),
    (
        "POST",
        "/compute/setups/{ref}/approve",
        lambda w: (f"/compute/setups/{_setup(w).hrid}/approve", {}),
        COMPUTE_ONLY,
    ),
    (
        "POST",
        "/compute/setups/{ref}/cancel",
        lambda w: (f"/compute/setups/{_setup(w).hrid}/cancel", {}),
        COMPUTE_ONLY,
    ),
    (
        "POST",
        "/compute/setups/{ref}/retire",
        lambda w: (f"/compute/setups/{_setup(w, 'succeeded').hrid}/retire", {}),
        COMPUTE_ONLY,
    ),
    ("GET", "/compute/quotas", fixed("/compute/quotas"), COMPUTE_ONLY),
    (
        "GET",
        "/compute/quota-scopes",
        fixed("/compute/quota-scopes?kind=project"),
        COMPUTE_ONLY,
    ),
    # What runs cost: for those who answer for it (ADR 0070).
    (
        "GET",
        "/compute/costs",
        fixed("/compute/costs"),
        row(403, compute_admin=200, auditor=200, org_admin=200),
    ),
    (
        "GET",
        "/compute/budgets",
        fixed("/compute/budgets"),
        row(403, compute_admin=200, auditor=200, org_admin=200),
    ),
    # No compute template here has a price: there's nothing to budget, but only Compute
    # Admins are told so.
    (
        "POST",
        "/compute/budgets",
        fixed("/compute/budgets", {"amount": "10"}),
        row(403, compute_admin=409),
    ),
    (
        "PATCH",
        "/compute/budgets/{budget_id}",
        lambda w: (f"/compute/budgets/{_budget(w).pk}", {"amount": "200"}),
        COMPUTE_ONLY,
    ),
    (
        "DELETE",
        "/compute/budgets/{budget_id}",
        lambda w: (f"/compute/budgets/{_budget(w).pk}", None),
        row(403, compute_admin=204),
    ),
    (
        "GET",
        "/compute/costs/records.csv",
        fixed("/compute/costs/records.csv"),
        row(403, compute_admin=200, auditor=200, org_admin=200),
    ),
    (
        "POST",
        "/compute/quotas",
        lambda w: ("/compute/quotas", {"project": str(w.project.pk), "max_running": 2}),
        row(403, compute_admin=201),
    ),
    (
        "PATCH",
        "/compute/quotas/{quota_id}",
        lambda w: (f"/compute/quotas/{_quota(w).pk}", {"max_running": 8}),
        COMPUTE_ONLY,
    ),
    (
        "DELETE",
        "/compute/quotas/{quota_id}",
        lambda w: (f"/compute/quotas/{_quota(w).pk}", None),
        row(403, compute_admin=204),
    ),
    # Slurm clusters (ADR 0133): the deployment's compute has one, hpc1.
    ("GET", "/compute/slurm", fixed("/compute/slurm"), COMPUTE_ONLY),
    (
        "GET",
        "/compute/slurm/{cluster}/accounts",
        fixed("/compute/slurm/hpc1/accounts"),
        COMPUTE_ONLY,
    ),
    (
        "PUT",
        "/compute/slurm/{cluster}/accounts/{project_id}",
        lambda w: (f"/compute/slurm/hpc1/accounts/{w.project.pk}", {"account": "rd-proteins"}),
        COMPUTE_ONLY,
    ),
    (
        "DELETE",
        "/compute/slurm/{cluster}/accounts/{project_id}",
        lambda w: (f"/compute/slurm/hpc1/accounts/{w.project.pk}", None),
        row(403, compute_admin=204),
    ),
    (
        "PATCH",
        "/compute/workers/{worker_id}",
        lambda w: (f"/compute/workers/{_self_hosted(w).pk}", {"labels": ["lab-3"]}),
        COMPUTE_ONLY,
    ),
    (
        "POST",
        "/compute/workers/{worker_id}/revoke",
        lambda w: (f"/compute/workers/{_worker(w).pk}/revoke", None),
        COMPUTE_ONLY,
    ),
    # Uploads are their maker's; files are read through the project they were added to.
    (
        "POST",
        "/uploads",
        lambda w: (
            "/uploads",
            {
                "project": str(w.project.pk),
                "name": "a.txt",
                "media_type": "text/plain",
                "size": 1,
                "sha256": "ca978112ca1bbdcafac231b39a23dc4da786eff8147c4e72b9807785afee48bb",
            },
        ),
        visible(403, **dict.fromkeys(ENTITY_EDITORS, 201)),
    ),
    ("GET", "/uploads", fixed("/uploads"), row(200)),
    ("GET", "/uploads/{upload_id}", upload(), row(404, editor=200)),
    ("POST", "/uploads/{upload_id}/parts", upload("/parts", {}), row(404, editor=200)),
    (
        "POST",
        "/uploads/{upload_id}/complete",
        upload("/complete", sent=True),
        row(404, editor=202),
    ),
    ("DELETE", "/uploads/{upload_id}", upload(), row(404, editor=204)),
    (
        "POST",
        "/registrations",
        lambda w: ("/registrations", {"project": str(w.project.pk), "uri": _lab_object()}),
        visible(403, **dict.fromkeys(ENTITY_EDITORS, 202)),
    ),
    ("GET", "/registrations", fixed("/registrations"), row(200)),
    ("GET", "/registrations/locations", fixed("/registrations/locations"), row(200)),
    ("GET", "/registrations/{registration_id}", registration(), row(404, editor=200)),
    ("GET", "/files", fixed("/files"), row(200)),
    ("GET", "/files/count", fixed("/files/count"), row(200)),
    ("GET", "/files/formats", fixed("/files/formats"), row(200)),
    ("GET", "/files/{file_id}", file(), readers(200)),
    ("GET", "/files/{file_id}/records", file("/records"), readers(200)),
    ("GET", "/files/{file_id}/runs", file("/runs"), readers(200)),
    ("GET", "/files/{file_id}/content", file("/content"), readers(302)),
    ("GET", "/files/{file_id}/content-url", file("/content-url"), readers(200)),
    # Notifications are each person's own: nobody else's are found.
    ("GET", "/notifications", fixed("/notifications"), row(200)),
    ("GET", "/notifications/summary", fixed("/notifications/summary"), row(200)),
    ("PATCH", "/notifications/{notification_id}", viewers_notification, row(404, viewer=200)),
    ("POST", "/notifications/read", fixed("/notifications/read", {}), row(200)),
    ("GET", "/me/notification-settings", fixed("/me/notification-settings"), row(200)),
    (
        "PATCH",
        "/me/notification-settings",
        fixed("/me/notification-settings", {"kinds": {"project.added": "in_app"}}),
        row(200),
    ),
    # Watching a record (ADR 0129) takes reading it; what you can't read is 404.
    ("GET", "/entities/{ref}/watch", sample_watch("S-watch"), readers(200)),
    ("PUT", "/entities/{ref}/watch", sample_watch("S-watched", {"mode": "watching"}), readers(200)),
    # Webhooks: a project's are its Admins'; the org's are Org Admins', who can also
    # see where every project's webhook sends and turn it off.
    ("GET", "/webhook-events", fixed("/webhook-events"), row(200)),
    ("GET", "/webhook-policy", fixed("/webhook-policy"), row(200)),
    (
        "PATCH",
        "/webhook-policy",
        fixed("/webhook-policy", {"project_webhooks": True}),
        ORG_ADMIN_ONLY,
    ),
    # Triggers: read with the project; turned on and off by its Admins (in the web app
    # only: test_triggers).
    ("GET", "/triggers", fixed("/triggers"), row(200)),
    ("GET", "/components", fixed("/components"), row(200)),
    # What fills a component's place: what its caller reads (ADR 0117).
    ("POST", "/components/{name}/entities", project_board, readers(200, org_admin=403)),
    ("GET", "/projects/{project_id}/triggers", project("/triggers"), readers(200, org_admin=403)),
    # A project's dashboard: read with the project; chosen by its Admins.
    (
        "GET",
        "/projects/{project_id}/dashboard",
        project("/dashboard"),
        readers(200, org_admin=403),
    ),
    ("PUT", "/projects/{project_id}/dashboard", dashboard, visible(403, admin=200)),
    ("DELETE", "/projects/{project_id}/dashboard", project("/dashboard"), visible(403, admin=204)),
    (
        "GET",
        "/projects/{project_id}/dashboard/choices",
        project("/dashboard/choices"),
        visible(403, admin=200),
    ),
    (
        "POST",
        "/projects/{project_id}/triggers/{name}/enable",
        project("/triggers/acme/protein.weigh_new_proteins/enable", {}),
        visible(403, admin=200),
    ),
    (
        "POST",
        "/projects/{project_id}/triggers/{name}/disable",
        project("/triggers/acme/protein.weigh_new_proteins/disable", {}),
        visible(403, admin=200),
    ),
    (
        "GET",
        "/projects/{project_id}/trigger-firings",
        project("/trigger-firings"),
        readers(200, org_admin=403),
    ),
    ("GET", "/projects/{project_id}/bundles", project("/bundles"), visible(403, admin=200)),
    ("POST", "/projects/{project_id}/bundles", export_request, visible(403, admin=202)),
    ("GET", "/bundles/{export_id}", bundle_export(), row(404, admin=200)),
    ("GET", "/bundles/{export_id}/content", bundle_export("/content"), row(404, admin=302)),
    ("GET", "/projects/{project_id}/imports", project("/imports"), visible(403)),
    ("POST", "/projects/{project_id}/imports", project("/imports", IMPORT_BODY), visible(403)),
    ("GET", "/imports/{import_id}", bundle_import(), row(404)),
    ("DELETE", "/imports/{import_id}", bundle_import(), row(404)),
    ("POST", "/imports/{import_id}/parts", bundle_import("/parts", {}), row(404)),
    ("POST", "/imports/{import_id}/complete", bundle_import("/complete"), row(404)),
    ("GET", "/imports/{import_id}/entities", bundle_import("/entities"), row(404)),
    ("GET", "/imports/{import_id}/people", bundle_import("/people"), row(404)),
    (
        "POST",
        "/imports/{import_id}/apply",
        bundle_import(
            "/apply", {"reason": "Moving the work", "planned_at": "2026-11-15T10:00:05Z"}
        ),
        row(404),
    ),
    ("GET", "/projects/{project_id}/webhooks", project("/webhooks"), visible(403, admin=200)),
    (
        "POST",
        "/projects/{project_id}/webhooks",
        project("/webhooks", {"url": "https://hooks.example.com/a", "events": ["project.updated"]}),
        visible(403, admin=201),
    ),
    ("GET", "/webhooks", fixed("/webhooks"), ORG_ADMIN_ONLY),
    (
        "POST",
        "/webhooks",
        fixed("/webhooks", {"url": "https://hooks.example.com/b", "events": ["team.created"]}),
        row(403, org_admin=201),
    ),
    ("GET", "/project-webhooks", fixed("/project-webhooks"), ORG_ADMIN_ONLY),
    ("GET", "/webhooks/{webhook_id}", webhook(), row(404, admin=200)),
    ("GET", "/webhooks/{webhook_id}", webhook(org=True), row(404, org_admin=200)),
    (
        "PATCH",
        "/webhooks/{webhook_id}",
        webhook(body={"description": "LIMS"}),
        row(404, admin=200),
    ),
    (
        "PATCH",
        "/webhooks/{webhook_id}",
        webhook(body={"active": False}),
        row(404, admin=200, org_admin=200),
    ),
    ("DELETE", "/webhooks/{webhook_id}", webhook(), row(404, admin=204)),
    (
        "POST",
        "/webhooks/{webhook_id}/rotate-secret",
        webhook("/rotate-secret"),
        row(404, admin=200),
    ),
    ("POST", "/webhooks/{webhook_id}/ping", webhook("/ping"), row(404, admin=202)),
    (
        "GET",
        "/webhooks/{webhook_id}/deliveries",
        webhook("/deliveries"),
        row(404, admin=200),
    ),
    (
        "GET",
        "/webhooks/{webhook_id}/deliveries/{delivery_id}",
        delivery(),
        row(404, admin=200),
    ),
    (
        "POST",
        "/webhooks/{webhook_id}/deliveries/{delivery_id}/redeliver",
        delivery("/redeliver"),
        row(404, admin=202),
    ),
    # --- Registries (ADR 0115) ---
    ("GET", "/registries", fixed("/registries"), row(200)),
    (
        "POST",
        "/registries",
        fixed("/registries", {"name": "Client A", "all_teams": True}),
        row(403, org_admin=201),
    ),
    ("GET", "/registries/{registry_id}", registry(), row(200)),
    (
        "PATCH",
        "/registries/{registry_id}",
        registry(body={"description": "The organization's."}),
        row(403, org_admin=200),
    ),
    ("GET", "/registries/{registry_id}/registrars", registry("/registrars"), row(200)),
    (
        "PUT",
        "/registries/{registry_id}/registrars/{user_id}",
        lambda w: (registry(f"/registrars/{w.users['viewer'].pk}")(w)[0], None),
        row(403, org_admin=201),
    ),
    (
        "DELETE",
        "/registries/{registry_id}/registrars/{user_id}",
        lambda w: (registry(f"/registrars/{w.users['viewer'].pk}")(w)[0], None),
        row(403, org_admin=204),
    ),
    # Only its registrars list a registry's entries, and nobody here is one.
    ("GET", "/registries/{registry_id}/entries", registry("/entries"), row(403)),
    ("GET", "/entries/{ref}", entry(), readers_of_entries(200)),
    (
        "PATCH",
        "/entries/{ref}",
        entry(body={"values": {"accession": "L09137"}, "reason": "NCBI"}, if_match=True),
        readers_of_entries(403),
    ),
    (
        "POST",
        "/entries/{ref}/supersede",
        entry("/supersede", body=lambda w, e: {"replacement": e.hrid, "reason": "Mistyped"}),
        readers_of_entries(403),
    ),
    ("GET", "/entries/{ref}/revisions", entry("/revisions"), readers_of_entries(200)),
    (
        "GET",
        "/entries/{ref}/revisions/{number}",
        entry("/revisions/1"),
        readers_of_entries(200),
    ),
    (
        "POST",
        "/entries/{ref}/records",
        entry("/records", body=lambda w, e: {"project": str(w.project.pk)}),
        # The project has its record already: it's returned as it is.
        readers_of_entries(403, **dict.fromkeys(ENTITY_EDITORS, 200)),
    ),
    (
        "POST",
        "/entities/{ref}/move",
        entity("/move", {"reason": "Superseded"}),
        # Its entry wasn't superseded, so there's nowhere to move it.
        editors(409),
    ),
    ("GET", "/outside-apis", fixed("/outside-apis"), ORG_ADMIN_ONLY),
    (
        "GET",
        "/projects/{project_id}/outside-apis",
        project("/outside-apis"),
        visible(200),
    ),
    (
        "POST",
        "/outside-apis/{name}/approvals",
        lambda w: (f"/outside-apis/{OUTSIDE_API}/approvals", {"project": str(w.project.pk)}),
        visible(403, org_admin=201),
    ),
    (
        "DELETE",
        "/outside-apis/{name}/approvals/{project_id}",
        outside_api_approved,
        visible(403, org_admin=204),
    ),
    ("GET", "/runs/{ref}/api-calls", run("/api-calls"), readers(200)),
    # Spreadsheet imports (ADR 0178): the requester's until they're committed.
    # An Org Admin sees the project, not its data.
    (
        "POST",
        "/projects/{project_id}/spreadsheet-imports",
        workbook_import,
        {**editors(201), "org_admin": 403},
    ),
    (
        "GET",
        "/projects/{project_id}/spreadsheet-imports",
        project("/spreadsheet-imports"),
        visible(200, org_admin=403),
    ),
    ("GET", "/spreadsheet-imports/{import_id}", spreadsheet_import(), row(404, editor=200)),
    (
        "PATCH",
        "/spreadsheet-imports/{import_id}",
        spreadsheet_import(body={"header_row": 1}),
        row(404, editor=200),
    ),
    ("DELETE", "/spreadsheet-imports/{import_id}", spreadsheet_import(), row(404, editor=204)),
    (
        "POST",
        "/spreadsheet-imports/{import_id}/preview",
        spreadsheet_import("/preview"),
        row(404, editor=202),
    ),
    (
        "GET",
        "/spreadsheet-imports/{import_id}/rows",
        spreadsheet_import("/rows"),
        row(404, editor=200),
    ),
    (
        "POST",
        "/spreadsheet-imports/{import_id}/commit",
        spreadsheet_import("/commit", {}),
        row(404, editor=409),
    ),
    (
        "POST",
        "/spreadsheet-imports/{import_id}/mappings",
        spreadsheet_import("/mappings", {"name": "Plate reader", "source_revision": 1}),
        row(404, editor=201),
    ),
    (
        "GET",
        "/projects/{project_id}/spreadsheet-mappings",
        project("/spreadsheet-mappings"),
        visible(200, org_admin=403),
    ),
    ("DELETE", "/spreadsheet-mappings/{mapping_id}", spreadsheet_mapping, editors(204)),
]


def test_every_operation_is_in_the_matrix() -> None:
    schema = api.get_openapi_schema()
    operations = {
        (method.upper(), path.removeprefix("/api/v1"))
        for path, methods in schema["paths"].items()
        for method in methods
    }
    assert operations == {(case[0], case[1]) for case in CASES}


def _schemas_by_name(ninja: NinjaAPI) -> dict[str, set[type[BaseModel]]]:
    found: dict[str, set[type[BaseModel]]] = {}
    seen: set[Any] = set()

    def walk(annotation: Any) -> None:
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            if annotation in seen:
                return
            seen.add(annotation)
            # Ninja wraps bodies, params and responses in classes of its own, never named.
            if annotation.__module__.startswith("iki_"):
                found.setdefault(annotation.__name__, set()).add(annotation)
            for field in annotation.model_fields.values():
                walk(field.annotation)
        for argument in typing.get_args(annotation):
            walk(argument)

    for _, router in ninja._routers:
        for view in router.path_operations.values():
            for operation in view.operations:
                for model in [*(operation.response_models or {}).values(), *operation.models]:
                    walk(model)
    return found


@pytest.mark.parametrize("ninja", [api, worker_api], ids=["api", "worker"])
def test_no_two_schemas_share_a_name(ninja: NinjaAPI) -> None:
    """The OpenAPI document names schemas by class name and keeps one of each: two
    classes with the same name would give clients the wrong shape for one of them."""
    shared = {
        name: sorted(f"{c.__module__}.{c.__qualname__}" for c in classes)
        for name, classes in _schemas_by_name(ninja).items()
        if len(classes) > 1
    }
    assert shared == {}


def test_every_operation_documents_its_summary() -> None:
    for methods in api.get_openapi_schema()["paths"].values():
        for operation in methods.values():
            assert operation.get("summary"), operation["operationId"]


@pytest.mark.parametrize("principal", PRINCIPALS)
@pytest.mark.parametrize(
    ("method", "template", "build", "expected"),
    CASES,
    ids=[f"{i:02d} {c[0]} {c[1]}" for i, c in enumerate(CASES)],
)
def test_operation_matrix(
    principal: str,
    method: str,
    template: str,
    build: Build,
    expected: dict[str, int],
    request: pytest.FixtureRequest,
) -> None:
    if template.startswith(
        ("/uploads", "/files", "/registrations", "/bundles", "/imports", "/spreadsheet")
    ) or (template.endswith(("/bundles", "/imports", "/spreadsheet-imports"))):
        request.getfixturevalue("s3")
        settings = request.getfixturevalue("settings")
        settings.IKI_FILES_REGISTER_LOCATIONS = ["s3://lab-data/runs/"]
    if template.startswith("/compute/slurm"):
        from test_slurm import with_cluster

        with_cluster(request)
    world = build_world()
    path, body, *headers = build(world)
    client = Api(world.browser(principal))
    response = client.call(method, path, body, headers=headers[0] if headers else None)
    assert response.status_code == expected[principal], (
        response.status_code,
        response.content[:300],
    )
    if response.status_code >= 400:
        assert response["Content-Type"] == "application/problem+json"
