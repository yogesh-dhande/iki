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
        w.users["editor"], "dna.DNASequence", w.project, {"bases": "ACGT"}
    ).entity


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
        w.users["editor"], "core.Collection", w.project, {"name": "Kin", "search": {"q": "kin"}}
    )
    found = Entity.objects.get(pk=made.entity.pk)
    return f"/collections/{found.pk}/freeze", {}, {"If-Match": f'"{found.revision}"'}


def linked_into_archived(w: World) -> tuple[str, Any]:
    """An entity of the project, also in the archived project (whose Editors can't change it)."""
    from iki_server.entities.models import ProjectLink

    found = _entity(w)
    ProjectLink.objects.create(entity_id=found.pk, project=w.archived)
    return f"/entities/{found.pk}/projects/{w.archived.pk}", None


def _protein(w: World) -> Any:
    from iki_server.entities import services as entities

    return entities.create(
        w.users["editor"], "protein.Protein", w.project, {"sequence": "MKV"}
    ).entity


def _run(w: World) -> Any:
    from iki_server.runs import catalog
    from iki_server.runs import services as runs

    action = catalog.action("protein.compute_properties")
    assert action is not None
    return runs.launch(w.users["editor"], action, w.project, {"protein": [_protein(w).pk]}, {})


def launch_run(w: World) -> tuple[str, Any]:
    body = {"project": str(w.project.pk), "inputs": {"protein": str(_protein(w).pk)}}
    return "/actions/protein.compute_properties/runs", body


def launch_batch(w: World) -> tuple[str, Any]:
    body = {"project": str(w.project.pk), "each": "protein", "entities": [str(_protein(w).pk)]}
    return "/actions/protein.compute_properties/runs/batch", body


def run(suffix: str = "", body: Any = None) -> Build:
    return lambda w: (f"/runs/{_run(w).pk}{suffix}", body)


def _registration(w: World) -> Any:
    from iki_server.runs import workers

    token, _ = workers.create_registration(
        label="Pods", tier="backend", by=w.users["compute_admin"]
    )
    return token


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
    # Entities: read with a role in any of their projects, changed by the owner's Editors.
    ("GET", "/entity-types", fixed("/entity-types"), row(200)),
    ("GET", "/entity-types/{name}", fixed("/entity-types/dna.DNASequence"), row(200)),
    ("GET", "/entities", fixed("/entities"), row(200)),
    ("POST", "/entities/search", lambda w: ("/entities/search", {"q": "kin"}), row(200)),
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
            {"type": "dna.DNASequence", "project": str(w.project.pk), "values": {"bases": "GC"}},
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
    ("GET", "/entities/{ref}/revisions", entity("/revisions"), readers(200)),
    ("GET", "/entities/{ref}/revisions/{number}", entity("/revisions/1"), readers(200)),
    ("GET", "/entities/{ref}/provenance", entity("/provenance"), readers(200)),
    ("GET", "/entities/{ref}/lineage", entity("/lineage"), readers(200)),
    ("GET", "/entities/{ref}/projects", entity("/projects"), readers(200)),
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
    # Actions are listed to everyone signed in. Running on the backend tier takes Editor;
    # runs are read with their project, and cancelled by whoever may run them.
    ("GET", "/actions", fixed("/actions"), row(200)),
    ("GET", "/actions/{name}", fixed("/actions/protein.compute_properties"), row(200)),
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
    ("GET", "/runs/{ref}/attempts", run("/attempts"), readers(200)),
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
    ("GET", "/compute/workers", fixed("/compute/workers"), COMPUTE_ONLY),
    ("GET", "/compute/quotas", fixed("/compute/quotas"), COMPUTE_ONLY),
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
    ("GET", "/files/{file_id}", file(), readers(200)),
    ("GET", "/files/{file_id}/content", file("/content"), readers(302)),
    # Notifications are each person's own: nobody else's are found.
    ("GET", "/notifications", fixed("/notifications"), row(200)),
    ("GET", "/notifications/summary", fixed("/notifications/summary"), row(200)),
    ("PATCH", "/notifications/{notification_id}", viewers_notification, row(404, viewer=200)),
    ("POST", "/notifications/read", fixed("/notifications/read", {}), row(200)),
    ("GET", "/me/notification-email", fixed("/me/notification-email"), row(200)),
    (
        "PATCH",
        "/me/notification-email",
        fixed("/me/notification-email", {"email": {"project.added": False}}),
        row(200),
    ),
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
    if template.startswith(("/uploads", "/files", "/registrations")):
        request.getfixturevalue("s3")
        settings = request.getfixturevalue("settings")
        settings.IKI_FILES_REGISTER_LOCATIONS = ["s3://lab-data/runs/"]
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
