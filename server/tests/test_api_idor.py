"""Authorization fuzzing: every operation, with another project's objects swapped in.

The matrix (test_api_access) sends each operation's request as each principal, with
objects all in one project. A real attack mixes them: a project's Admin sends a path
under their own project, or a body, naming an object in a project they can't see. This
builds a second, foreign world with the same requests' objects, then, as the first
world's Admin, swaps each of the request's identifiers for its foreign counterpart, one
at a time and all at once. Each must answer exactly as it does when the identifier
names nothing at all, a key made up for the purpose: never success, never 403 (which
says the object exists), never a server error. And no response may carry an identifier
of the foreign project's objects that the request didn't send.
"""

import contextlib
import itertools
import json
import re
import uuid
from collections.abc import Iterator
from functools import cache
from typing import Any

import pytest
from api_client import Api
from django.apps import apps
from django.db import IntegrityError, models, transaction
from org_world import World, build_world
from test_api_access import CASES, Build

from iki_server.core.ids import uuid7
from iki_server.org.models import Project
from iki_server.org.services import Refused
from iki_server.searches.services import Refused as SearchRefused

pytestmark = pytest.mark.django_db

ADMIN = "admin"


@cache
def _lookups() -> dict[type[models.Model], list[str]]:
    """Each model's ways to its project, as ORM lookups: ``project``, ``entity__project``."""
    found: dict[type[models.Model], list[str]] = {}
    for model in apps.get_models():
        paths: list[str] = []
        frontier: list[tuple[type[models.Model], str]] = [(model, "")]
        for _ in range(3):
            following: list[tuple[type[models.Model], str]] = []
            for current, prefix in frontier:
                for field in current._meta.get_fields():
                    if not (field.is_relation and (field.many_to_one or field.one_to_one)):
                        continue
                    if not field.concrete or field.related_model is None:
                        continue
                    lookup = f"{prefix}{field.name}"
                    if field.related_model is Project:
                        paths.append(lookup)
                    elif field.related_model is not model:
                        following.append((field.related_model, f"{lookup}__"))
            frontier = following
        if paths:
            found[model] = paths
    return found


def _belongs_to(value: Any, project: Project) -> bool:
    """Whether ``value`` is the key of something in ``project``, or the project itself.
    Every key is a UUID."""
    try:
        key = uuid.UUID(str(value))
    except ValueError:
        return False
    return key == project.pk or any(
        model._default_manager.filter(pk=key, **{path: project.pk}).exists()
        for model, paths in _lookups().items()
        if isinstance(model._meta.pk, models.UUIDField)
        for path in paths
    )


def _keys_of(project: Project) -> set[str]:
    """Every key of everything in ``project``, long enough to find in a response."""
    keys: set[str] = {str(project.pk)}
    for model, paths in _lookups().items():
        for path in paths:
            for pk in model._default_manager.filter(**{path: project.pk}).values_list(
                "pk", flat=True
            ):
                if len(str(pk)) >= 8:
                    keys.add(str(pk))
    return keys


def _swaps(ours: Any, theirs: Any, project: Project) -> Iterator[tuple[str, Any, Any]]:
    """Where two requests' values differ and the foreign one is the foreign project's:
    (where, ours, theirs). Paths compare by segment; bodies by value, recursively."""
    if isinstance(ours, dict) and isinstance(theirs, dict):
        for key in ours.keys() & theirs.keys():
            for where, a, b in _swaps(ours[key], theirs[key], project):
                yield f"{key}.{where}" if where else key, a, b
    elif isinstance(ours, list) and isinstance(theirs, list) and len(ours) == len(theirs):
        for i, (a, b) in enumerate(zip(ours, theirs, strict=True)):
            for where, x, y in _swaps(a, b, project):
                yield f"{i}.{where}" if where else str(i), x, y
    elif ours != theirs and _belongs_to(theirs, project):
        yield "", ours, theirs


def _parts(path: str) -> list[str]:
    """A path's segments and its query's values: where identifiers go."""
    return re.split(r"[/?&=]", path)


def _replace(value: Any, ours: str, theirs: str) -> Any:
    """``value`` with every ``ours`` replaced by ``theirs``, in paths and bodies alike."""
    return json.loads(json.dumps(value).replace(ours, theirs))


def _problem(response: Any, masks: dict[str, str]) -> tuple[int, Any]:
    """What a response says, with the identifiers it was sent masked: an object out of
    reach must read exactly as one that doesn't exist."""
    text = (response.getvalue() if response.streaming else response.content).decode()
    for value, mask in masks.items():
        text = text.replace(value, mask)
    try:
        said: Any = json.loads(text)
    except ValueError:
        said = text
    if isinstance(said, dict):
        said.pop("request_id", None)  # Every request's own.
    return response.status_code, said


def _cases() -> list[tuple[str, str, Build]]:
    """Operations the Admin may perform with what they're given: the ones to attack."""
    return [(m, t, b) for m, t, b, expected in CASES if expected[ADMIN] < 400]


@pytest.mark.parametrize(
    ("method", "template", "build"),
    _cases(),
    ids=[f"{m} {t}" for m, t, _ in _cases()],
)
def test_another_projects_objects_are_never_reached(
    method: str, template: str, build: Build, request: pytest.FixtureRequest
) -> None:
    if template.startswith(
        (
            "/uploads",
            "/files",
            "/registrations",
            "/bundles",
            "/imports",
            "/spreadsheet-imports",
            "/entity-exports",
        )
    ) or (template.endswith(("/bundles", "/imports", "/spreadsheet-imports"))):
        request.getfixturevalue("s3")
        settings = request.getfixturevalue("settings")
        settings.IKI_FILES_REGISTER_LOCATIONS = ["s3://lab-data/runs/"]
    world = build_world()
    foreign: World = build_world(prefix="foreign-")
    path, body, *headers = build(world)
    their_path, their_body, *_ = build(foreign)
    secret = _keys_of(foreign.project)

    swaps = {
        ours: theirs
        for _, ours, theirs in itertools.chain(
            _swaps(_parts(path), _parts(their_path), foreign.project),
            _swaps(body, their_body, foreign.project),
        )
    }
    if not swaps:
        pytest.skip("names nothing of a project's")
    # Each identifier alone, then all of them: a check on one mustn't vouch for another.
    attempts = [{ours: theirs} for ours, theirs in swaps.items()]
    if len(swaps) > 1:
        attempts.append(swaps)
    client = Api(world.browser(ADMIN))

    def send(replacing: dict[str, str]) -> tuple[Any, str]:
        sent_path, sent_body = path, body
        for ours, theirs in replacing.items():
            sent_path = _replace(sent_path, ours, theirs)
            sent_body = _replace(sent_body, ours, theirs)
        response = client.call(
            method, sent_path, sent_body, headers=headers[0] if headers else None
        )
        return response, json.dumps([sent_path, sent_body])

    for attempt in attempts:
        made_up = {ours: str(uuid7()) for ours in attempt}
        response, sent = send(attempt)
        missing, _ = send(made_up)
        where = f"{method} {template} with {sorted(attempt)} swapped"
        assert response.status_code < 500, (where, response.content[:300])
        assert response.status_code not in (200, 201, 202, 204, 403), (
            where,
            response.status_code,
            response.content[:300],
        )
        masks = {theirs: f"<{i}>" for i, theirs in enumerate(attempt.values())}
        mask_missing = {made_up[ours]: f"<{i}>" for i, ours in enumerate(attempt)}
        assert _problem(response, masks) == _problem(missing, mask_missing), where
        leaked = {key for key in secret if key in response.content.decode() and key not in sent}
        assert not leaked, (where, leaked)


#: People in the first world who aren't the organization's admins or auditors, who may
#: see every project. None of them may see the foreign one.
NOT_ORG_WIDE = ("admin", "editor", "runner", "viewer", "via_team", "team_member", "outsider")


def test_lists_and_searches_never_show_another_projects_objects(
    request: pytest.FixtureRequest,
) -> None:
    """The other half: what a list or search returns. The foreign project gets one of
    everything every operation's request makes; then everyone in the first world calls
    every list and search, plain and filtered to the foreign project."""
    request.getfixturevalue("s3")
    settings = request.getfixturevalue("settings")
    settings.IKI_FILES_REGISTER_LOCATIONS = ["s3://lab-data/runs/"]
    world = build_world()
    foreign: World = build_world(prefix="foreign-")
    for _, _, build, _ in CASES:
        # Several requests make the same thing (a service account called Robot, a saved
        # search called Shared), which can exist only once: one is enough.
        with contextlib.suppress(IntegrityError, Refused, SearchRefused), transaction.atomic():
            build(foreign)
    secret = _keys_of(foreign.project)
    assert len(secret) > 50, "the foreign project should hold one of nearly everything"
    lists = [
        (method, build)
        for method, template, build, _ in CASES
        if "{" not in template and (method == "GET" or template.endswith("/search"))
    ]
    made_up = str(uuid7())
    for principal in NOT_ORG_WIDE:
        client = Api(world.browser(principal))
        for method, build in lists:
            path, body, *headers = build(world)
            joiner = "&" if "?" in path else "?"
            for sent, other in (
                (path, None),
                (f"{path}{joiner}project={foreign.project.pk}", f"{path}{joiner}project={made_up}"),
            ):
                response = client.call(method, sent, body, headers=headers[0] if headers else None)
                where = f"{principal}: {method} {sent}"
                body_of = response.getvalue() if response.streaming else response.content
                assert response.status_code < 500, (where, body_of[:300])
                text = body_of.decode()
                leaked = {key for key in secret if key in text and key not in sent}
                assert not leaked, (where, leaked)
                if other is not None:
                    missing = client.call(
                        method, other, body, headers=headers[0] if headers else None
                    )
                    assert _problem(response, {str(foreign.project.pk): "<p>"}) == _problem(
                        missing, {made_up: "<p>"}
                    ), where


#: Live updates stream from views outside the OpenAPI document, one per thing followed.
STREAMS = ("/runs/{ref}", "/batches/{ref}", "/workflow-runs/{ref}", "/compute/setups/{ref}")


@pytest.mark.parametrize("template", STREAMS)
def test_a_stream_follows_only_what_its_caller_may_read(
    template: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whoever can't read a run, batch, workflow run or setup can't follow it either: the
    stream refuses them as reading it does (the matrix), and where that's 404, answers as
    for one that doesn't exist."""
    from iki_server.realtime import views

    # Streams return their connection between queries; in a test's transaction that
    # would close it.
    monkeypatch.setattr(views, "close_old_connections", lambda: None)
    (build, expected) = next((b, e) for m, t, b, e in CASES if m == "GET" and t == template)
    world = build_world()
    path, _, *_ = build(world)
    ref = path.rsplit("/", 1)[1]
    made_up = str(uuid7())
    refused = {p: status for p, status in expected.items() if status >= 400}
    assert refused, "someone should be unable to read it"
    for principal, status in refused.items():
        client = Api(world.browser(principal))
        response = client.call("GET", f"{path}/events")
        assert response.status_code == status, (principal, response.content[:300])
        if status == 404:
            missing = client.call("GET", f"{path.rsplit('/', 1)[0]}/{made_up}/events")
            assert _problem(response, {ref: "<ref>"}) == _problem(missing, {made_up: "<ref>"}), (
                principal
            )
