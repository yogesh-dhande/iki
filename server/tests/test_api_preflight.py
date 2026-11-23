"""Schema impact for the admin page and CLI, against current records (ADR 0181)."""

from typing import Any

import pytest
from api_client import Api, problem
from org_world import World, build_world
from test_oauth import bearer, signed_in_token

from iki_server.entities import registry, services
from iki_server.entities.models import Entity
from iki_server.plugins import preflight
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
    assert row["checked"] == row["total"] == 1
    assert not row["sampled"] and row["sampled_checks"] == []
    finding = next(f for f in row["findings"] if f["subject"] == "purity")
    assert isinstance(made.entity, Entity)
    assert finding["count"] == 1 and not finding["sampled"]
    assert made.entity.hrid in finding["examples"]


def test_api_preserves_exact_counts_alongside_sampled_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = build_world()
    type_name = "acme/protein.Batch"
    for name in ("L-1", "L-2"):
        services.create(
            world.users["editor"], type_name, world.project, {"name": name, "purity": 95.0}
        )
    monkeypatch.setattr(preflight, "SAMPLE_LIMIT", 1)
    manifest = plugin_to_dict(registry.plugins()["acme/protein"])
    proposed = manifest["types"][type_name]
    proposed["fields"]["well"]["required"] = True
    rule_name = "purity_in_specification"
    proposed["rules"][rule_name]["check"] = "record.purity >= 98.0"

    response = Api(world.browser("org_admin")).post(
        PATH, {"apiVersion": API_VERSION, "plugin": manifest}
    )
    assert response.status_code == 200, response.content[:500]
    row = next(t for t in response.json()["types"] if t["type"] == type_name)
    assert (row["total"], row["checked"], row["sampled"]) == (2, 1, True)
    assert any(rule_name in name for name in row["sampled_checks"])
    findings = {f["subject"]: (f["count"], f["sampled"]) for f in row["findings"]}
    assert findings["well"] == (2, False)
    assert findings[rule_name] == (1, True)


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
