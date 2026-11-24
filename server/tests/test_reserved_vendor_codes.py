"""Vendor-code fields reserve the same record-identity namespace on every entry path."""

import pytest
from api_client import Api
from org_world import build_world
from test_api_entities import create, patch
from test_storage import FREEZER, TUBE, errors

pytestmark = pytest.mark.django_db


def test_vendor_codes_reserve_the_iki_namespace_on_create_and_edit() -> None:
    world = build_world()
    api = Api(world.browser("editor"))
    made = create(api, world.project, {"barcode": "VENDOR-01"}, TUBE)
    assert made.status_code == 201, made.content
    record = made.json()
    ref = record["hrid"]
    etag = api.get(f"/entities/{ref}")["ETag"]
    for code in ("IKI:" + "A" * 32, "iKi:malformed", "  iki:old"):
        rejected = create(api, world.project, {"barcode": code}, TUBE)
        assert "IKI:" in errors(rejected)["#/values/barcode"]
        rejected = patch(api, ref, {"values": {"barcode": code}}, etag)
        assert "IKI:" in errors(rejected)["#/values/barcode"]
    current = api.get(f"/entities/{ref}").json()
    assert (current["revision"], current["values"]["barcode"]) == (record["revision"], "VENDOR-01")
    rejected = create(api, world.project, {"name": "Shelf", "asset_tag": "Iki:one"}, FREEZER)
    assert "IKI:" in errors(rejected)["#/values/asset_tag"]
    ordinary = create(api, world.project, {"name": "IKI: freezer shelf"}, FREEZER)
    assert ordinary.status_code == 201, ordinary.content
