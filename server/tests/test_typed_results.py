"""A specification's criteria, a release test and a stability measurement are a number
or a result that conforms to a requirement (ICH Q6A §3.2.1), and each says which."""

from typing import Any

import pytest
from first_party_dependencies import FIRST_PARTY, PLUGINS

from iki_spec import rules
from iki_spec.model import EntityType
from iki_spec.validate import load_plugin

QC = PLUGINS / "qc_release"
#: A criterion row as it's saved: every field, empty ones null.
ROW: dict[str, Any] = dict.fromkeys(
    [
        "attribute",
        "procedure",
        "procedure_revision",
        "method",
        "kind",
        "unit",
        "lower",
        "upper",
        "acceptance",
        "source",
    ]
)
NUMBER = ROW | {"attribute": "Assay", "method": "Titration", "kind": "numeric", "unit": "%"}
NUMBER |= {"lower": 99.0}
IDENTITY = ROW | {
    "attribute": "Identification A",
    "method": "USP <191>",
    "kind": "conforms",
    "acceptance": "Meets the requirements",
}


def spec(directory: Any, name: str) -> EntityType:
    plugin = load_plugin(directory, dependencies=list(FIRST_PARTY[directory])).plugin
    assert plugin is not None
    return plugin.types[name]


def outcomes(
    entity_type: EntityType, check: str, records: list[dict[str, Any]]
) -> list[bool | None]:
    variables = rules.rule_variables(
        rules.record_schema(entity_type.label, entity_type.fields, None)
    )
    rows = rules.evaluate(variables, [(check, rules.CONDITION)], [{"record": r} for r in records])
    return [row[0].value for row in rows]


def test_a_criterion_is_a_number_with_limits_or_a_requirement() -> None:
    version = spec(QC, "iki/qc_release.SpecificationVersion")
    check = version.rules["typed_criteria"].check
    assert check is not None
    cases = [
        ([NUMBER, IDENTITY], True),
        ([NUMBER | {"unit": None}], False),
        ([IDENTITY | {"upper": 1}], False),
        ([IDENTITY | {"unit": "%"}], False),
        ([NUMBER | {"acceptance": "Clear"}], False),
    ]
    assert outcomes(version, check, [{"criteria": c} for c, _ in cases]) == [ok for _, ok in cases]


def test_approval_needs_each_criterions_limit_or_requirement() -> None:
    version = spec(QC, "iki/qc_release.SpecificationVersion")
    approval = version.fields["approval"]
    guard = approval.states["approved"].guards["documented"]
    states = {n: f for n, f in version.fields.items() if f.type == "state"}
    variables = rules.guard_variables(
        rules.record_schema(version.label, version.fields, None), states, [], approval
    )
    cases = [
        ([NUMBER, IDENTITY], True),
        ([IDENTITY | {"acceptance": None}], False),
        ([NUMBER | {"lower": None}], False),
    ]
    contexts = [
        {"record": {"criteria": c}, "state": {"from": "draft", "to": "approved"}} for c, _ in cases
    ]
    rows = rules.evaluate(variables, [(guard.check, rules.CONDITION)], contexts)
    assert [row[0].value for row in rows] == [ok for _, ok in cases]


@pytest.mark.parametrize(
    ("values", "ok"),
    [
        ({"kind": "numeric", "unit": "%", "value": 99.5, "lower": 99}, True),
        ({"kind": "numeric", "unit": "%", "lower": 99}, False),
        ({"kind": "conforms", "result": "conforms", "acceptance": "Meets the requirements"}, True),
        ({"kind": "conforms", "acceptance": "Meets the requirements"}, False),
        ({"kind": "conforms", "result": "conforms", "acceptance": "Meets", "value": 1}, False),
    ],
)
def test_a_release_test_keeps_its_kind_of_result(values: dict[str, Any], ok: bool) -> None:
    test = spec(QC, "iki/qc_release.ReleaseTest")
    check = test.rules["recorded_observation"].check
    assert check is not None
    origin = dict.fromkeys(test.fields) | {
        "case": "c",
        "lot": "l",
        "sequence": 1,
        "attribute": "A",
        "method": "M",
        "outcome": "pass",
        "observed_by": "p",
    }
    assert outcomes(test, check, [origin | values]) == [ok]
