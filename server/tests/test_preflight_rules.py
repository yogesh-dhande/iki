"""A proposed rule is checked against existing records without installing its guards."""

from dataclasses import replace

import pytest
from org_world import World
from test_rules import BATCH, made, world

from iki_server.entities import registry, services
from iki_server.entities.models import Entity
from iki_server.entities.values import shape, to_json
from iki_server.plugins import preflight_rules
from iki_server.plugins.preflight_types import Record
from iki_spec.model import Field, Rule, State, StateMove

__all__ = ["world"]


@pytest.mark.django_db
def test_proposed_cel_rule_counts_failing_records(
    world: World,
) -> None:
    old = registry.types()[BATCH]
    first = made(world, {"plate": "P1", "well": "A1", "purity": 95})
    made(world, {"plate": "P1", "well": "A2", "purity": 99})
    tightened = replace(old.rules["purity_in_specification"], check="record.purity >= 98.0")
    new = replace(old, rules={**old.rules, tightened.name: tightened})

    entities = list(Entity.objects.filter(type=BATCH))
    values = services.current_values_of(entities)
    fields = shape(BATCH).fields()
    rows = [
        Record(
            str(e.pk),
            e.hrid,
            str(e.owner_project_id),
            None,
            None,
            {n: to_json(fields[n], v) for n, v in values[e.pk].items()},
        )
        for e in entities
    ]
    selected = preflight_rules.checked_cel_rules(old.fields, new.fields, old.rules, new.rules)
    [finding] = preflight_rules.row_findings(new.fields, selected, rows)
    assert finding.subject == tightened.name and finding.count == 1
    assert finding.examples == [first["hrid"]]


def test_unread_references_are_explicit_instead_of_passing_a_null_check() -> None:
    batch = registry.types()[BATCH]
    check = batch.rules["protein_not_bsl3"]
    proposed = {check.name: check}
    rows = [Record("1", "BAT-1", "p1", None, None, {"protein": "A"})]
    assert preflight_rules.reference_ids(batch.fields, proposed, rows) == {"A"}
    [unknown] = preflight_rules.row_findings(batch.fields, proposed, rows)
    assert unknown.blocking and unknown.kind == "unchecked"
    [broken] = preflight_rules.row_findings(
        batch.fields,
        proposed,
        rows,
        referenced={"A": {"id": "A", "hrid": "PRT-1", "hazard": "bsl3"}},
    )
    assert broken.blocking and broken.kind == "rule"


def test_check_reads_a_field_that_changes_on_its_referenced_type() -> None:
    batch = registry.types()[BATCH]
    entry = registry.types()["acme/protein.Protein"]
    old_fields = entry.current_fields
    new_fields = {**old_fields, "hazard": replace(old_fields["hazard"], values=("none",))}

    def rechecked(fields: dict[str, Field]) -> set[str]:
        return preflight_rules.changed_reference_rules(
            batch.fields,
            batch.rules,
            lambda _name: (entry.label, old_fields),
            lambda _name: (entry.label, fields),
        )

    assert not rechecked(new_fields)
    fills = replace(old_fields["hazard"], required=True, has_default=True, default="none")
    assert rechecked({**new_fields, "hazard": fills}) == {"protein_not_bsl3", "proteins_not_bsl3"}


def test_cel_rechecks_only_fields_with_changed_values() -> None:
    purity = Field("purity", "float", min=0)
    checked = Rule(
        "positive", "Positive", "acme/protein", check="record.purity > 0", reads=("purity",)
    )
    old_rules = {checked.name: checked}
    fields = {"purity": purity}
    for same_values in (
        replace(purity, label="Pure"),
        replace(purity, min=1),
        replace(purity, unique=True),
    ):
        assert not preflight_rules.checked_cel_rules(
            fields, {"purity": same_values}, old_rules, old_rules
        )
    fills = replace(purity, required=True, has_default=True, default=10)
    assert (
        preflight_rules.checked_cel_rules(fields, {"purity": fills}, old_rules, old_rules)
        == old_rules
    )
    state = Field(
        "phase",
        "state",
        states={"open": State("open", "Open"), "closed": State("closed", "Closed")},
        initial="open",
    )
    status = replace(state, name="status")
    retirement = replace(
        status, removed=True, moved_to=StateMove(field="phase", states=(("open", "closed"),))
    )
    rule = replace(checked, check='record.phase == "open"', reads=("phase",))
    rules = {rule.name: rule}
    assert (
        preflight_rules.checked_cel_rules(
            {"phase": state, "status": status}, {"phase": state, "status": retirement}, rules, rules
        )
        == rules
    )
