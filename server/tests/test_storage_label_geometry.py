"""The native two-dimensional codes, quiet zones, identifiers and optional secondary bars fit."""

import re
import uuid

import pytest
from api_client import Api
from org_world import World
from test_storage import tube, world

from iki_server.audit.models import AuditEvent
from iki_server.entities.models import Entity
from iki_server.storage import labels

__all__ = ["world"]

LONG_HRID = "ITEM-" + "9" * 27


@pytest.mark.parametrize("dpi", labels.DPIS)
@pytest.mark.parametrize("size", labels.SIZES.values(), ids=lambda size: size.key)
@pytest.mark.parametrize("symbology", labels.SYMBOLOGIES)
def test_label_symbols_and_full_hrid_fit_the_stock(
    dpi: int, size: labels.Size, symbology: str
) -> None:
    record = Entity(id=uuid.UUID("ffffffff-ffff-7fff-bfff-ffffffffffff"), hrid=LONG_HRID)
    code = "IKI:" + record.pk.hex.upper()
    narrow = round(0.25 * dpi / 25.4)
    for vendor in (None, "LB-0042") if size.code128 else (None,):
        label = labels.Label(record, code, LONG_HRID, "Title", "Label line", vendor)
        output = labels.zpl([label], size=size, dpi=dpi, symbology=symbology, copies=2)
        width = round(size.width_mm * dpi / 25.4)
        height = round(size.height_mm * dpi / 25.4)
        assert f"^PW{width}\n^LL{height}" in output
        assert "^PQ2," in output
        barcode = re.search(r"\^FO(\d+),(\d+)\^B(?:XN,(\d+),200,24,24|QN,2,(\d+))", output)
        assert barcode, output
        x, y = int(barcode[1]), int(barcode[2])
        pitch = int(barcode[3] or barcode[4])
        if symbology == "datamatrix":
            modules, quiet = 24, 1
            assert f"^FD{code}^FS" in output
        else:
            modules, quiet = 29, 4
            assert f"^FDQM,A{code}^FS" in output
        assert pitch >= narrow and x >= quiet * pitch and y >= quiet * pitch
        assert x + (modules + quiet) * pitch <= width
        assert y + (modules + quiet) * pitch <= height
        if size.key == "cryo" and symbology == "datamatrix":
            assert pitch == {203: 3, 300: 5}[dpi]
        # Font A is 5 by 9 dots plus 1 dot character spacing, scaled in integer multiples.
        rows = re.findall(r"\^FO(\d+),(\d+)\^AAN,(\d+),(\d+)\^FH\\\^FD([^\^]*)\^FS", output)
        assert "".join(row[4] for row in rows) == LONG_HRID
        for left, top, h, w, text in rows:
            assert int(left) >= x + (modules + quiet) * pitch
            assert int(left) + len(text) * (int(w) // 5 * 6) <= width
            assert int(top) + int(h) <= height
        bar = re.search(r"\^FO(\d+),(\d+)\^BY(\d+),3,(\d+)\^BCN", output)
        if vendor is None:
            assert bar is None
        else:
            assert bar and int(bar[3]) == narrow
            assert int(bar[1]) >= 10 * narrow
            assert int(bar[1]) + (labels.code128_modules(vendor) - 10) * narrow <= width
            assert int(bar[2]) >= y + (quiet + modules) * pitch
            assert int(bar[2]) + int(bar[4]) <= height


@pytest.mark.django_db
def test_long_vendor_code_is_refused_before_preparing_any_record(world: World) -> None:

    api = Api(world.browser("editor"))
    short, long = (tube(api, world.project, barcode) for barcode in ("SHORT-1", "X" * 40))
    for how in ("print", "zpl"):
        for dpi in labels.DPIS:
            answer = api.post(
                "/storage/labels",
                {
                    "records": [short["id"], long["id"]],
                    "size": "box",
                    "format": how,
                    "dpi": dpi,
                    "include_vendor_code": True,
                },
            )
            assert answer.status_code == 422, answer.content
            assert "Code 128" in answer.content.decode() and long["hrid"] in answer.content.decode()
    assert not AuditEvent.objects.filter(action="entity.labelled").exists()


@pytest.mark.django_db
def test_existing_iki_shaped_vendor_codes_cannot_be_printed_as_vendor_symbols(world: World) -> None:
    from unittest.mock import patch

    from iki_server.entities import services

    api = Api(world.browser("editor"))
    record = tube(api, world.project, "LEGACY-SOURCE")
    pk = uuid.UUID(record["id"])
    data = {"records": [record["id"]], "size": "box", "format": "zpl"}
    # The stored barcode predates namespace validation; prepare the label as read by the editor.
    for legacy in ("IKI:" + "0" * 32, "/entities/TUB-999999/"):
        with patch.object(services, "current_values_of", return_value={pk: {"barcode": legacy}}):
            refused = api.post("/storage/labels", {**data, "include_vendor_code": True})
            assert refused.status_code == 422, refused.content
            assert "reserved" in refused.content.decode()
            allowed = api.post("/storage/labels", data)
            assert allowed.status_code == 200, allowed.content
            label = allowed.json()["labels"][0]
            assert label["code"] == "IKI:" + pk.hex.upper() and label["vendor_code"] is None
            assert "^BCN" not in allowed.json()["zpl"]


@pytest.mark.parametrize(
    ("raw", "data", "modules"),
    [
        ("LB-0042", "LB->50042", 121),
        ("001122334455", ">;001122334455", 121),
        ("12345X", ">;1234>65X", 110),
        (">^~", ">0><>=", 88),
    ],
)
def test_vendor_code128_has_explicit_literal_data_and_exact_width(
    raw: str, data: str, modules: int
) -> None:
    record = Entity(id=uuid.uuid4(), hrid="BOX-000001")
    label = labels.Label(record, "IKI:" + record.pk.hex.upper(), record.hrid, "", "", raw)
    output = labels.zpl([label], size=labels.SIZES["box"])
    assert f"^FD{data}^FS" in output and labels.code128_modules(raw) == modules


@pytest.mark.django_db
def test_shared_vendor_barcode_prints_two_distinct_record_identities(world: World) -> None:
    from iki_server.org.models import Project, ProjectMembership, ProjectRole
    from iki_server.storage import services

    user = world.users["editor"]
    api = Api(world.browser("editor"))
    other = Project.objects.create(name="Other receiving lab", team=world.team)
    ProjectMembership.objects.create(project=other, user=user, role=ProjectRole.EDITOR)
    a, b = (tube(api, project, "COMMON-42") for project in (world.project, other))
    response = api.post(
        "/storage/labels",
        {
            "records": [a["id"], b["id"]],
            "size": "standard",
            "format": "zpl",
            "include_vendor_code": True,
        },
    )
    assert response.status_code == 200, response.content
    prepared = response.json()
    assert len({label["code"] for label in prepared["labels"]}) == 2
    for label, source in zip(prepared["labels"], (a, b), strict=True):
        assert label["vendor_code"] == "COMMON-42"
        assert label["code"] in prepared["zpl"]
        assert [str(record.pk) for record in services.find(user, label["code"])] == [source["id"]]
