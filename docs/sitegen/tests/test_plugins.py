"""The Plugins reference is read from every first-party plugin's manifests."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from sitegen import plugins
from sitegen.repo import ROOT


@pytest.fixture(scope="module")
def found() -> list[dict[str, Any]]:
    return plugins.plugins()


def by_name(found: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(plugin for plugin in found if plugin["name"] == name)


def test_every_first_party_plugin_is_read_the_core_plugins_first(
    found: list[dict[str, Any]],
) -> None:
    on_disk = sorted(path.parent.name for path in (ROOT / "plugins").glob("*/plugin.yaml"))
    assert [plugin["short"] for plugin in found] == ["core", *on_disk]
    core = found[0]
    assert core["name"] == "iki/core"
    assert {"Entity", "Sequence", "Collection", "Organism"} <= {t["name"] for t in core["types"]}


def test_a_type_keeps_its_fields_states_and_references_in_full(
    found: list[dict[str, Any]],
) -> None:
    equipment = by_name(found, "iki/equipment")
    instrument = next(t for t in equipment["types"] if t["name"] == "Instrument")
    assert instrument["hrid"] == "INS"
    assert instrument["path"] == "plugins/equipment/types/instrument.yaml"
    service = next(f for f in instrument["fields"] if f["key"] == "service")
    assert service["initial"] == "in_service"
    assert [s["name"] for s in service["states"]] == ["in_service", "out_of_service", "retired"]
    assert next(s for s in service["states"] if s["name"] == "retired")["locks"] is True
    requests = by_name(found, "iki/requests")
    deliverable = next(t for t in requests["types"] if t["name"] == "PlainDeliverable")
    request = next(f for f in deliverable["fields"] if f["key"] == "request")
    # A reference to the plugin's own type is named in full, as one to another plugin's is.
    assert request["to"] == ["iki/requests.PlainRequest"]
    connector = by_name(found, "iki/lims_connector_example")
    [extension] = [e for e in connector["extensions"] if e["extends"] == "iki/protein.Protein"]
    assert "lims_id" in {f["key"] for f in extension["fields"]}


def test_an_action_keeps_its_inputs_params_and_effects(found: list[dict[str, Any]]) -> None:
    sequencing = by_name(found, "iki/plasmid_sequencing")
    action = next(a for a in sequencing["actions"] if a["name"] == "verify_plasmid_reads")
    inputs = [i["type"] for i in action["inputs"]]
    assert inputs == ["iki/plasmid.Plasmid", "iki/sequencing.ReadSet"]
    assert {p["name"] for p in action["params"]} >= {"platform", "min_depth"}
    assert action["creates"] == ["iki/plasmid_sequencing.WholePlasmidAlignment"]


def test_each_anchor_is_once_on_its_page(found: list[dict[str, Any]]) -> None:
    for plugin in found:
        ids = [t["name"] for t in plugin["types"]] + [a["name"] for a in plugin["actions"]]
        for owner in plugin["types"]:
            ids += [f"{owner['name']}.{f['key']}" for f in owner["fields"]]
        repeated = [id for id, count in Counter(ids).items() if count > 1]
        assert repeated == [], plugin["name"]


def test_a_manifest_the_reader_refuses_stops_the_generation(tmp_path: Path) -> None:
    root = tmp_path / "acme"
    (root / "types").mkdir(parents=True)
    (root / "plugin.yaml").write_text("apiVersion: iki/v1\nkind: Plugin\nname: acme/x\n")
    (root / "types" / "a.yaml").write_text("kind: EntityType\nname: A\nname: B\n")
    with pytest.raises(plugins.UnreadableError, match="yaml-duplicate-key"):
        plugins.plugin(root)


def test_it_writes_what_the_site_reads(tmp_path: Path) -> None:
    written = plugins.write(tmp_path)
    data = json.loads(written.read_text(encoding="utf-8"))
    assert data["plugins"][0]["slug"] == "core"
    assert by_name(data["plugins"], "iki/lab_roles")["slug"] == "lab-roles"
