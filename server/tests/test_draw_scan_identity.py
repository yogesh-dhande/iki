"""A physical draw needs independent evidence of the particular tube used."""

from collections.abc import Callable
from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest
from api_client import Api, problem
from org_world import build_world
from test_amounts import sample
from test_storage import TUBE, box, moved, tube

from iki_server.amounts.models import Movement
from iki_server.audit.models import AuditEvent
from iki_server.entities import registry
from iki_server.org.models import Project, ProjectMembership, ProjectRole
from iki_spec.model import EntityType

pytestmark = pytest.mark.django_db


def ledger(api: Api, path: str) -> tuple[Any, int, int]:
    return (
        api.get(path).json(),
        Movement.objects.count(),
        AuditEvent.objects.filter(action="entity.amount_moved").count(),
    )


def test_a_shared_vendor_label_cannot_debit_the_selected_tube() -> None:
    world = build_world()
    second_project = Project.objects.create(team=world.team, name="Partner stock")
    ProjectMembership.objects.create(
        project=second_project, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    api = Api(world.browser("editor"))
    quantity = {"value": 20, "unit": "uL"}
    first = tube(api, world.project, "VENDOR-30", amount=quantity)
    other = tube(api, second_project, "VENDOR-30", amount=quantity)
    path = f"/entities/{first['id']}/amount"
    body = {"amount": {"value": 1, "unit": "uL"}, "reason": "Use at bench"}

    before = ledger(api, path)
    for proof in (
        "VENDOR-30",
        {"code": other["hrid"], "mode": "iki_id"},
        f"/entities/{other['id']}/",
    ):
        failure = api.post(path + "/draw", {**body, "confirm": proof})
        errors = problem(failure, 422, "validation_failed")["errors"]
        assert errors[0]["pointer"] == "#/confirm"
        assert other["id"] not in failure.content.decode()
        assert ledger(api, path) == before

    label = api.post(
        "/storage/labels",
        {"records": [first["id"]], "size": "cryo", "format": "print", "preview": True},
    )
    assert label.status_code == 200, label.content
    printed = label.json()["labels"][0]["code"]
    assert printed == "IKI:" + UUID(first["id"]).hex.upper()
    for proof in (printed, {"code": first["hrid"], "mode": "iki_id"}):
        ok = api.post(path + "/draw", {**body, "confirm": proof})
        assert ok.status_code == 200, ok.content
        accepted = proof if isinstance(proof, str) else first["hrid"]
        assert ok.json()["movement"]["confirmed"] == accepted
        assert AuditEvent.objects.filter(
            action="entity.amount_moved", target_id=first["id"], data__confirmed=accepted
        ).exists()
    assert api.get(path).json()["value"] == "18"

    # Once physically separated, an ordinary label uniquely names its tube in this source box.
    source = box(api, world.project)
    moved(api, (first, source, "A1"))
    ok = api.post(path + "/draw", {**body, "confirm": "VENDOR-30"})
    assert ok.status_code == 200 and ok.json()["movement"]["confirmed"] == "VENDOR-30"


@pytest.mark.parametrize("storable", [True, False], ids=["tube", "nonstorable-sample"])
def test_an_optional_scan_can_only_be_recorded_if_verified(
    redeclare: Callable[[str, EntityType], None], storable: bool
) -> None:
    world = build_world()
    api = Api(world.browser("editor"))
    quantity = {"value": 20, "unit": "uL"}
    if storable:
        declared = registry.types()[TUBE]
        assert declared.storage is not None
        redeclare(TUBE, replace(declared, storage=replace(declared.storage, confirm=())))
        record = tube(api, world.project, "OPTIONAL-35", amount=quantity)
    else:
        record = sample(world, "S-1", quantity)
    path = f"/entities/{record['id']}/amount"
    body = {"amount": {"value": 1, "unit": "uL"}}
    omitted = api.post(path + "/draw", body)
    assert omitted.status_code == 200, omitted.content
    assert omitted.json()["movement"]["confirmed"] == ""

    before = ledger(api, path)
    for proof in ("UNKNOWN-35", {"mode": "iki_id", "code": "  "}):
        refused = api.post(path + "/draw", {**body, "confirm": proof})
        errors = problem(refused, 422, "validation_failed")["errors"]
        assert errors[0]["pointer"] == "#/confirm"
        assert ledger(api, path) == before
    if storable:
        verified = api.post(path + "/draw", {**body, "confirm": "OPTIONAL-35"})
        assert verified.status_code == 200, verified.content
        assert verified.json()["movement"]["confirmed"] == "OPTIONAL-35"
