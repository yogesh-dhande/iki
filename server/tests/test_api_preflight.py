"""Schema impact for the admin page and CLI, against current records (ADR 0181)."""

from typing import Any

import pytest
from api_client import Api, problem
from org_world import World, build_world
from test_oauth import bearer, signed_in_token

from iki_server.entities import registry, services
from iki_server.entities.models import Entity
from iki_spec import API_VERSION
from iki_spec.model import plugin_to_dict

PATH = "/plugins/acme/protein/preflight"
pytestmark = pytest.mark.django_db


def candidate() -> dict[str, Any]:
    plugin = plugin_to_dict(registry.plugins()["acme/protein"])
    plugin["version"] = "4.0.0"
    plugin["types"]["acme/protein.Sample"]["fields"]["purity"]["required"] = True
    return {"apiVersion": API_VERSION, "plugin": plugin}


def test_admin_checks_real_records() -> None:
    world = build_world()
    made = services.create(
        world.users["editor"], "acme/protein.Sample", world.project, {"label": "Specimen 17"}
    )
    result = Api(world.browser("org_admin")).post(PATH, candidate())
    assert result.status_code == 200, result.content[:500]
    report = result.json()
    assert report["blocking"] and report["version"] == "4.0.0"
    assert report["configuration_revision"] and report["candidate_revision"]
    row = next(row for row in report["types"] if row["type"] == "acme/protein.Sample")
    assert row["checked"] == 1 and not row["sampled"]
    finding = next(f for f in row["findings"] if f["subject"] == "purity")
    assert isinstance(made.entity, Entity)
    assert finding["count"] == 1 and made.entity.hrid in finding["examples"]


@pytest.fixture
def world() -> World:
    return build_world()


def test_preview_checks_role_and_csrf_before_reading_records(world: World) -> None:
    body = candidate()
    problem(Api().post(PATH, body), 401, "not_authenticated")
    problem(Api(world.browser("auditor")).post(PATH, body), 403, "forbidden")
    problem(Api(world.browser("org_admin")).post(PATH, body, csrf=False), 403, "csrf_failed")


def test_admin_token_needs_admin_scope(world: World) -> None:
    _, tokens = signed_in_token(world, "org_admin", scope="read")
    problem(bearer(tokens["access_token"], "POST", PATH, candidate()), 403, "insufficient_scope")


@pytest.mark.parametrize(
    "patch",
    [
        {"apiVersion": "iki/future"},
        {"plugin": {"name": "acme/protein", "types": "incorrect"}},
        {"git": "https://example.org/plugin.git"},
    ],
)
def test_preview_rejects_invalid_artifacts(world: World, patch: dict[str, Any]) -> None:
    result = Api(world.browser("org_admin")).post(PATH, {**candidate(), **patch})
    assert result.status_code == 422, result.content
    assert result["Content-Type"] == "application/problem+json"


def test_plugin_name_is_bound_to_requested_path(world: World) -> None:
    result = Api(world.browser("org_admin")).post("/plugins/acme/dna/preflight", candidate())
    assert result.status_code == 422
