# ruff: noqa: F811 - shared Playwright fixtures are injected by name.
"""Saved fragment sizes are shown on the digest and plasmid records in real browsers."""

import re
from pathlib import Path
from typing import Any

import pytest
from org_world import World
from playwright.sync_api import Page, Route, expect
from test_browser import (  # noqa: F401 - fixtures
    _orm_beside_playwright,
    assert_clean,
    browser,
    open_as,
    playwright,
    server,
    world,
)

from iki_server.api.entities import field_out
from iki_server.entities import services as entities
from iki_server.entities.models import Entity
from iki_spec.validate import load_plugin

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.built_web_app]

PLUGINS = Path(__file__).resolve().parents[2] / "plugins"
ASSEMBLY = "iki/plasmid.Assembly"
PLASMID = "iki/plasmid.Plasmid"
SAMPLE = "acme/protein.Sample"

# Synthetic saved result rows. The browser only receives lengths and display metadata.
FRAGMENTS = [
    {"part": "Synthetic backbone", "length": 1500, "start": 1, "end": 1500},
    {"part": "Synthetic backbone", "length": 500, "start": 1501, "end": 2000},
    {"part": "Synthetic backbone", "length": 500, "start": 2001, "end": 2500},
    {"part": "Synthetic insert", "length": 750, "start": 1, "end": 750},
]
SECOND_FRAGMENTS = [
    {"part": "Synthetic backbone", "length": 2250, "start": 1, "end": 2250},
    {"part": "Synthetic backbone", "length": 250, "start": 2251, "end": 2500},
]


@pytest.fixture(scope="module")
def digest_types() -> dict[str, dict[str, Any]]:
    """Describe just the fields under test, using the installed API's field encoder."""
    loaded = load_plugin(PLUGINS / "plasmid", dependencies=[PLUGINS / "dna"])
    assert loaded.plugin is not None, loaded.problems
    return {
        kind: {
            "name": kind,
            "plugin": "iki/plasmid",
            "label": spec.label,
            "label_plural": spec.label_plural or "Plasmids",
            "description": None,
            "extends": None,
            "abstract": False,
            "sample": False,
            "detached": False,
            "ancestors": ["iki/core.Entity"],
            "roles": {},
            "subtypes": [],
            "hrid_prefix": spec.hrid_prefix,
            "identity": list(spec.identity),
            "fields": [field_out(name, spec.fields[name], "iki/plasmid") for name in fields],
            "values_schema": {},
            "viewers": [],
            "exports": [],
            "rules": [],
            "notifications": [],
        }
        for kind, fields in (
            (ASSEMBLY, ("name", "method", "backbone", "fragments")),
            (PLASMID, ("name", "sequence")),
        )
        for spec in (loaded.plugin.types[kind],)
    }


def _result_records(
    page: Page, world: World, types: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Give the browser saved results on the real, signed-in entity routes.

    The default browser server has only fixture plugins. Reuse its entities for permissions
    and ancillary requests, and supply the first-party type and values at the API boundary.
    """
    admin = world.users["admin"]
    created = entities.create(
        admin,
        "acme/dna.DNASequence",
        world.project,
        {"bases": "ACGT" * 625, "name": "Synthetic backbone"},
    )
    sequence = Entity.objects.get(pk=created.entity.pk)
    assert sequence.entry_id is not None
    sequence_id = str(sequence.entry_id)

    def saved(kind: str, title: str, values: dict[str, Any]) -> dict[str, Any]:
        if kind == PLASMID:
            entity = entities.create(
                admin, "acme/dna.Plasmid", world.project, {"bases": "ACGT" * 625, "name": title}
            ).entity
        else:
            entity = entities.create(admin, SAMPLE, world.project, {"label": title}).entity
        response = page.request.get(f"/api/v1/entities/{entity.pk}")
        assert response.ok, response.text()
        original: dict[str, Any] = response.json()
        return {
            **original,
            "title": title,
            "type": kind,
            "values": {"name": title, **values},
            "refs": {
                sequence_id: {
                    "hrid": sequence.hrid,
                    "title": "Synthetic backbone",
                    "type": "acme/dna.DNASequence",
                    "archived": False,
                    "purged": False,
                },
            },
        }

    plasmid = saved(PLASMID, "Synthetic plasmid", {"sequence": sequence_id})
    digest = saved(
        ASSEMBLY,
        "Synthetic digest",
        {"method": "restriction_ligation", "backbone": sequence_id, "fragments": FRAGMENTS},
    )
    other = saved(
        ASSEMBLY,
        "Second synthetic digest",
        {"method": "digest", "backbone": sequence_id, "fragments": SECOND_FRAGMENTS},
    )
    records = {ref: row for row in (plasmid, digest, other) for ref in (row["id"], row["hrid"])}
    searches: list[dict[str, Any]] = []

    def entity_types(route: Route) -> None:
        response = route.fetch()
        route.fulfill(response=response, json=[*response.json(), *types.values()])

    def by_ref(route: Route) -> None:
        route.fulfill(json=records[route.request.url.split("?")[0].rsplit("/", 1)[1]])

    def find_digests(route: Route) -> None:
        payload = route.request.post_data_json
        if not isinstance(payload, dict) or payload.get("type") != ASSEMBLY:
            route.continue_()
            return
        searches.append(payload)
        route.fulfill(json={"items": [digest, other], "next_cursor": None})

    page.route("**/api/v1/entity-types", entity_types)
    refs = "|".join(re.escape(ref) for ref in records)
    page.route(re.compile(rf"/api/v1/entities/(?:{refs})(?:\?.*)?$"), by_ref)
    page.route("**/api/v1/entities/search", find_digests)
    return plasmid, digest, other, searches


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_saved_digest_and_plasmid_gels_fit_a_phone(
    open_as: Any, world: World, digest_types: dict[str, dict[str, Any]], color_scheme: str
) -> None:
    page = open_as("viewer", "/projects/kinase-atlas/", color_scheme)
    page.set_viewport_size({"width": 390, "height": 844})
    plasmid, digest, other, searches = _result_records(page, world, digest_types)

    page.goto(f"/entities/{digest['hrid']}/")
    expect(page.get_by_role("heading", name="Virtual gel")).to_be_visible()
    gel = page.locator("iki-virtual-gel")
    expect(gel.get_by_role("img", name="Virtual gel band pattern")).to_be_visible()
    ladder = page.get_by_role("combobox", name="DNA ladder")
    expect(ladder).to_have_value("neb-1-kb")
    ladder.select_option("neb-100-bp")
    backbone_bands = gel.locator('.virtual-gel__band[title^="Synthetic backbone"]')
    expect(backbone_bands).to_have_count(2)
    band_position = "e => e.style.getPropertyValue('--band-position')"
    first_position = backbone_bands.first.evaluate(band_position)
    agarose = page.get_by_role("combobox", name="Agarose (%)")
    agarose.select_option("2")
    expect(gel.get_by_text("2% agarose", exact=True)).to_be_visible()
    assert backbone_bands.first.evaluate(band_position) != first_position
    assert backbone_bands.evaluate_all(
        "bands => bands.map(b => Number(b.style.getPropertyValue('--band-intensity')))"
    ) == pytest.approx([1.0, 2 / 3])
    gel.get_by_text("Band details", exact=True).press("Enter")
    details = gel.get_by_role("table", name="Gel band sizes and masses")
    expect(details).to_contain_text("Synthetic backbone")
    expect(details).to_contain_text("Synthetic insert")
    # New England Biolabs N3231 publishes a 1517 bp band for its 100 bp DNA Ladder.
    # https://www.neb.com/products/n3231-100-bp-dna-ladder
    expect(details.get_by_role("cell", name=re.compile(r"^1[,\s]?517(?: bp)?$"))).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth - innerWidth <= 1")
    assert_clean(page)

    page.goto(f"/entities/{plasmid['hrid']}/")
    digest_result = page.get_by_role("combobox", name="Digest result")
    expect(digest_result).to_be_visible()
    assert searches, "The plasmid page should read saved digest results"
    search = searches[-1]
    sequence = plasmid["values"]["sequence"]
    assert search["project"] == str(world.project.pk)
    assert search["archived"] is False
    assert search["where"] == {
        "all": [
            {"field": "fragments", "op": "is_set", "value": True},
            {
                "any": [
                    {"field": "backbone", "op": "eq", "value": sequence},
                    {"field": "inserts", "op": "has", "value": sequence},
                ]
            },
        ]
    }
    digest_result.select_option(other["id"])
    expect(gel.get_by_role("img", name="Virtual gel band pattern")).to_be_visible()
    gel.get_by_text("Band details", exact=True).press("Enter")
    expect(details.get_by_role("cell", name=re.compile(r"^2[,\s]?250(?: bp)?$"))).to_be_visible()
    expect(details).not_to_contain_text("Synthetic insert")
    assert page.evaluate("document.documentElement.scrollWidth - innerWidth <= 1")
    assert_clean(page)
    page.get_by_role("link", name="Open digest record").click()
    expect(page.get_by_role("heading", name="Second synthetic digest", level=1)).to_be_visible()
