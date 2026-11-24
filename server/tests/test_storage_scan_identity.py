"""Physical scan identity across readable records, shared labels and explicit evidence."""

from uuid import UUID, uuid4

import pytest
from api_client import Api
from django.db import connection
from django.test.utils import CaptureQueriesContext
from org_world import build_world
from test_api_entities import create
from test_requests import SAMPLE
from test_storage import FREEZER, box, made, moved, tube

from iki_server.entities.models import ProjectLink
from iki_server.org.models import Project, ProjectMembership, ProjectRole
from iki_server.storage import scan_identity, services

pytestmark = pytest.mark.django_db


def only_id(match: scan_identity.Match) -> UUID:
    assert match.status == "one" and match.record is not None
    return match.record.id


def test_a_label_is_confirmed_only_in_its_scope_and_choices_must_still_match() -> None:
    world = build_world()
    user = world.users["editor"]
    other = Project.objects.create(name="Other lab", team=world.team)
    ProjectMembership.objects.create(project=other, user=user, role=ProjectRole.EDITOR)
    api = Api(world.browser("editor"))
    a = tube(api, world.project, "shared")
    b = tube(api, other, "shared")
    c = tube(api, world.project, a["hrid"])
    container = box(api, world.project)
    moved(api, (a, container, "A2"))
    aid, bid, cid = (UUID(row["id"]) for row in (a, b, c))
    link = f"https://iki.example/entities/{aid}/"
    unknown_link = f"/entities/{uuid4()}/"
    hrid_link = f"/entities/{a['hrid']}/"
    printed = scan_identity.record_code(aid)
    assert printed == f"IKI:{aid.hex.upper()}"
    index = scan_identity.ScanIndex(
        user,
        ["shared", a["hrid"], printed, hrid_link, link, unknown_link],
        scope_ids=[aid, bid, cid],
    )
    match = index.resolve("shared")
    assert match.status == "ambiguous" and match.record is None
    assert {e.pk for e in match.candidates} == {aid, bid}
    assert only_id(index.resolve("shared", candidates=[aid])) == aid
    assert index.resolve("shared", candidates=[cid]).status == "none"
    from_slot = index.resolve("shared", occupant=aid)
    assert only_id(from_slot) == aid and from_slot.basis == "position"
    with pytest.raises(scan_identity.ScanRefused):
        index.resolve("shared", occupant=aid, choice=bid)
    chosen = index.resolve("shared", choice=bid)
    assert only_id(chosen) == bid and chosen.basis == "choice"
    with pytest.raises(scan_identity.ScanRefused):
        index.resolve("shared", choice=cid)
    with pytest.raises(scan_identity.ScanRefused):
        index.resolve("shared", candidates=[bid], choice=aid)
    assert index.resolve(a["hrid"]).status == "ambiguous"
    explicit = index.resolve(a["hrid"], mode="iki_id")
    assert only_id(explicit) == aid and explicit.basis == "iki_id"
    assert only_id(index.resolve(link)) == aid and index.resolve(link).basis == "record_link"
    for encoded in (printed, hrid_link):
        assert only_id(index.resolve(encoded)) == aid
    for malformed in ("IKI:", "iki:no-record", "IKI:" + "g" * 32, "/entities/unavailable/"):
        assert scan_identity.is_explicit(malformed)
        with pytest.raises(scan_identity.ScanRefused):
            index.resolve(malformed)
    with pytest.raises(scan_identity.ScanRefused):
        index.resolve(unknown_link)
    with pytest.raises(scan_identity.ScanRefused):
        index.resolve("shared", mode="iki_id")
    result = index.describe(match)
    shown = next(x for x in result["candidates"] if x["id"] == a["id"])
    assert shown["owner_project"] == {"id": str(world.project.pk), "name": world.project.name}
    assert shown["box"]["id"] == container["id"] and shown["position"] == "A2"

    hidden = scan_identity.ScanIndex(world.users["viewer"], ["shared", link], scope_ids=[aid, bid])
    assert aid in hidden.readable_ids and bid not in hidden.readable_ids
    for code in ("shared", "different", link, ""):
        with pytest.raises(scan_identity.ScanRefused) as refusal:
            hidden.resolve(code, protected=True)
        assert (
            str(refusal.value) == "This scan can’t identify a tube here. "
            "Use its printed Iki ID or ask an authorized reviewer."
        )
    with pytest.raises(scan_identity.ScanRefused):
        hidden.resolve("shared", choice=aid, has_concealed=True)
    at_position = hidden.resolve("shared", has_concealed=True, occupant=aid)
    assert only_id(at_position) == aid and at_position.basis == "position"
    assert hidden.resolve("", has_concealed=True).status == "none"
    assert only_id(hidden.resolve(link, has_concealed=True)) == aid
    assert only_id(hidden.resolve("shared")) == aid


def test_a_rack_of_labels_is_resolved_in_queries_per_storage_field_not_per_cell() -> None:
    world = build_world()
    user = world.users["editor"]
    api = Api(world.browser("editor"))
    found = tube(api, world.project, "rack/10")

    def queries(codes: list[str]) -> int:
        with CaptureQueriesContext(connection) as captured:
            index = scan_identity.ScanIndex(user, codes)
            for code in codes:
                assert index.resolve(code).status == ("one" if code == "rack/10" else "none")
        return len(captured)

    single = queries(["rack/10"])
    assert queries(["rack/10", *(f"not-registered-{n}" for n in range(100))]) <= single
    assert found["id"]


def test_a_shared_record_does_not_reveal_an_unreadable_owner_or_box() -> None:
    world = build_world()
    other = build_world("other-")
    api = Api(other.browser("editor"))
    record = tube(api, other.project, "shared-vial")
    private_box = box(api, other.project)
    moved(api, (record, private_box, "B2"))
    shared_link = ProjectLink.objects.create(entity_id=record["id"], project=world.project)
    index = scan_identity.ScanIndex(world.users["viewer"], ["shared-vial"])
    (item,) = index.describe(index.resolve("shared-vial"))["candidates"]
    assert item["id"] == record["id"]
    assert (item["owner_project"], item["box"], item["position"]) == (None, None, None)
    ProjectLink.objects.create(entity_id=private_box["id"], project=world.project)
    (item,) = index.describe(index.resolve("shared-vial"))["candidates"]
    assert item["owner_project"] is None
    assert item["box"]["id"] == private_box["id"] and item["position"] == "B2"
    match = index.resolve("shared-vial")
    shared_link.delete()
    with pytest.raises(scan_identity.ScanRefused):
        index.describe(match)


def test_a_shared_registration_hrid_does_not_identify_one_project_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = build_world()
    other = Project.objects.create(name="Second project", team=world.team)
    ProjectMembership.objects.create(
        project=other, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    api = Api(world.browser("editor"))
    a, b = (create(api, project, {"bases": "GATTACA"}).json() for project in (world.project, other))
    assert a["hrid"] == b["hrid"] and a["id"] != b["id"]
    # A plugin can declare a registering type storable. Both project records share the
    # registry entry's HRID while the UUID printed on each record label differs.
    monkeypatch.setattr(services, "storable_types", lambda: ["acme/dna.DNASequence"])
    index = scan_identity.ScanIndex(world.users["editor"], [a["hrid"]])
    assert index.resolve(a["hrid"], mode="iki_id").status == "ambiguous"


def test_global_search_keeps_nonstorage_records_and_uses_the_printed_namespace() -> None:
    world = build_world()
    api = Api(world.browser("editor"))
    user = world.users["editor"]
    record = tube(api, world.project, "lot/code")
    printed = scan_identity.record_code(UUID(record["id"]))
    assert [str(e.pk) for e in services.find(user, printed)] == [record["id"]]
    assert services.find(user, "IKI:malformed") == []
    assert services.find(user, scan_identity.record_code(uuid4())) == []
    sequence = create(api, world.project, {"bases": "GATTACA"}).json()
    assert [str(e.pk) for e in services.find(user, sequence["hrid"])] == [sequence["id"]]
    sample = made(api, world.project, SAMPLE, {"label": "Sample"})
    # Uppercasing this vendor label would turn it into another record's ASCII HRID.
    vendor = "\N{LATIN SMALL LETTER LONG S}" + sample["hrid"][1:]
    labelled = tube(api, world.project, vendor)
    assert [str(e.pk) for e in services.find(user, vendor)] == [labelled["id"]]
    index = scan_identity.ScanIndex(user, [vendor], include_nonstorable=True)
    with pytest.raises(scan_identity.ScanRefused):
        index.resolve(vendor, mode="iki_id")


def test_uuid_looking_vendor_labels_are_resolved_in_the_destination_chooser() -> None:
    world = build_world()
    api = Api(world.browser("editor"))
    target = box(api, world.project, "Destination box")
    bare = target["id"]
    compact = UUID(bare).hex
    hyphenated_label, hex_label = (
        made(api, world.project, FREEZER, {"name": name, "asset_tag": code})
        for name, code in (("Freezer 1", bare), ("Freezer 2", compact))
    )

    def destination(code: str, choice: str | None = None) -> set[str]:
        reply = api.post(
            "/storage/scan/resolve", {"scope": "destination", "code": code, "choice": choice}
        )
        assert reply.status_code == 200, reply.content
        body = reply.json()
        ids = {item["id"] for item in body["candidates"]}
        assert body["status"] == {0: "none", 1: "one", 2: "ambiguous"}[len(ids)]
        return ids

    assert destination(bare) == {bare, hyphenated_label["id"]}
    assert destination(bare, hyphenated_label["id"]) == {hyphenated_label["id"]}
    assert destination(bare, bare) == {bare}
    assert destination(compact) == {hex_label["id"]}
    assert destination(f"/entities/{compact}/") == {bare}
    assert destination(scan_identity.record_code(UUID(bare))) == {bare}
    assert destination(str(uuid4())) == set()


def test_receipt_evidence_records_what_matched_even_after_an_explicit_choice() -> None:
    world = build_world()
    user = world.users["editor"]
    api = Api(world.browser("editor"))
    first = tube(api, world.project, "TEMP")
    path = f"/entities/{first['id']}"
    hrid = first["hrid"]
    revision = api.get(path)["ETag"]
    tagged = api.patch(path, {"values": {"barcode": hrid}}, headers={"If-Match": revision})
    assert tagged.status_code == 200, tagged.content
    other = Project.objects.create(name="Other lab", team=world.team)
    ProjectMembership.objects.create(project=other, user=user, role=ProjectRole.EDITOR)
    second = tube(api, other, hrid)
    first_id, second_id = UUID(first["id"]), UUID(second["id"])
    printed, link = scan_identity.record_code(first_id), f"/entities/{first_id}/"
    index = scan_identity.ScanIndex(user, [printed, link, hrid, second["hrid"], "missing"])
    for code, expected in (
        (printed, "record_code"),
        (link, "record_link"),
        (second["hrid"], "hrid"),
    ):
        assert index.evidence_basis(code, "label", index.resolve(code)) == expected
    assert index.evidence_basis(hrid, "label", index.resolve(hrid)) == "ambiguous"
    assert (
        index.evidence_basis(hrid, "label", index.resolve(hrid, choice=first_id))
        == "hrid_and_vendor"
    )
    assert index.evidence_basis(hrid, "label", index.resolve(hrid, choice=second_id)) == "vendor"
    assert index.evidence_basis(hrid, "iki_id", index.resolve(hrid, mode="iki_id")) == "iki_id"
    assert index.evidence_basis("missing", "label", index.resolve("missing")) == "unmatched"
