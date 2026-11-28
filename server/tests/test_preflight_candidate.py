"""Preflight reads a compiled manifest as data, including when the upload is malformed."""

import json
from dataclasses import replace
from typing import Any

import pytest

from iki_server.entities import registry
from iki_spec.model import plugin_to_dict


def read(data: dict[str, Any]) -> Any:
    from iki_server.plugins import candidate

    return candidate.read("acme/protein", data)


def test_reads_compiled_candidate_and_checks_what_cel_reads() -> None:
    current = registry.plugins()["acme/protein"]
    data = plugin_to_dict(replace(current, version="3.1.0"))
    rule = data["types"]["acme/protein.Batch"]["rules"]["pass_in_specification"]
    rule["check"] = "record.purity > 98.0"
    rule["reads"] = ["supplied_by_client"]
    checked = read(data)
    assert checked.version == "3.1.0"
    assert set(checked.types["acme/protein.Batch"].rules["pass_in_specification"].reads) == {
        "purity"
    }


@pytest.mark.parametrize("name", ["iki/core", "acme/protein", "acme/prediction", "acme/dna"])
def test_valid_compiled_manifest_preserves_the_candidate_digest(name: str) -> None:
    from iki_server.plugins import candidate

    data = plugin_to_dict(registry.plugins()[name])
    checked = plugin_to_dict(candidate.read(name, data))
    assert json.dumps(checked, sort_keys=True) == json.dumps(data, sort_keys=True)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (["name"], "acme/other"),
        (["formats", "acme/protein.model_pdb", "reader"], "unknown-reader"),
        (["formats", "acme/protein.model_pdb", "media_types"], ["text/csv"]),
        (["formats", "acme/protein.model_pdb", "id"], "acme/other.model_pdb"),
        (["types", "acme/protein.Batch", "plugin"], "acme/other"),
        (["types", "acme/protein.Batch", "extends"], "acme/protein.Batch"),
        (["types", "acme/protein.Batch", "extends"], "acme/protein.Missing"),
        (["types", "acme/protein.Batch", "fields", "proteins", "items"], None),
        (["types", "acme/protein.Batch", "fields", "name", "name"], "another"),
        (["types", "acme/protein.Batch", "fields", "name", "required"], "false"),
        (["types", "acme/protein.Batch", "fields", "name", "type"], "missing"),
        (["types", "acme/protein.Batch", "fields", "name", "pattern"], "["),
        (
            ["types", "acme/protein.Protein", "fields", "hydropathy", "schema"],
            {"items": {"$ref": "file:/bad"}},
        ),
        (
            ["types", "acme/protein.Batch", "fields", "name", "schema"],
            {"$ref": "https://example.com"},
        ),
        (["types", "acme/protein.Batch", "rules", "one_per_well", "unique"], ["missing", "plate"]),
        (["types", "acme/protein.Batch", "rules", "one_at_a_time", "no_overlap"], ["starts_at"]),
        (["types", "acme/protein.Batch", "rules", "one_at_a_time", "plugin"], "acme/other"),
        (["types", "acme/protein.Batch", "rules", "expires_after_made", "check"], "record.missing"),
    ],
)
def test_invalid_compiled_candidate_is_refused(path: list[str], value: Any) -> None:
    data = plugin_to_dict(registry.plugins()["acme/protein"])
    node = data
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = value
    with pytest.raises(ValueError, match=r"."):
        read(data)
