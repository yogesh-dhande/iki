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
from iki_spec.model import Rule

__all__ = ["world"]


@pytest.mark.django_db
def test_proposed_rules_count_distinct_records_including_both_sides_of_a_clash(
    world: World,
) -> None:
    old = registry.types()[BATCH]
    first = made(world, {"plate": "P1", "well": "A1", "purity": 95})
    second = made(world, {"plate": "P1", "well": "A2", "purity": 99})
    made(world, {"plate": "P1", "well": "A1", "stored": False})
    extra = Rule(
        "one_plate",
        "One stored batch per plate and status",
        old.plugin,
        unique=("plate", "status"),
        only_while=(("stored", ("true",)),),
        reads=("plate", "status", "stored"),
    )
    tightened = replace(old.rules["purity_in_specification"], check="record.purity >= 98.0")
    new = replace(old, rules={**old.rules, extra.name: extra, tightened.name: tightened})

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
    findings = [
        *preflight_rules.row_findings(old.fields, new.fields, old.rules, new.rules, rows),
        *preflight_rules.relational_findings(old.fields, new.fields, old.rules, new.rules, rows),
    ]
    by_name = {finding.subject: finding for finding in findings}
    assert set(by_name) == {extra.name, tightened.name}
    assert by_name[extra.name].count == 2
    assert sorted(by_name[extra.name].examples) == sorted([first["hrid"], second["hrid"]])
    assert by_name[tightened.name].count == 1
    assert by_name[tightened.name].examples == [first["hrid"]]


def test_a_constraint_counts_records_once_per_scope_and_reports_just_the_requested_type() -> None:
    old = registry.types()[BATCH]
    new_fields = {**old.fields, "name": replace(old.fields["name"], unique=True)}
    date_range = Rule(
        "made_ranges",
        "Production dates",
        old.plugin,
        no_overlap=("made_on", "expires_on"),
        per=("plate",),
        reads=("made_on", "expires_on", "plate"),
    )

    def row(hrid: str, start: str, end: str, project: str = "p1") -> Record:
        return Record(
            hrid,
            hrid,
            project,
            None,
            None,
            {
                "name": "lot",
                "made_on": start,
                "expires_on": end,
                "plate": "P",
            },
        )

    first = row("A", "2026-03-01", "2026-03-30")
    inside = row("B", "2026-03-03", "2026-03-04")
    last = row("C", "2026-03-30", "2026-04-01")
    other = row("D", "2026-03-30", "2026-04-01", "p2")
    rows = [first, inside, last, other]
    findings = preflight_rules.relational_findings(
        old.fields, new_fields, {}, {date_range.name: date_range}, rows, report_ids={"B", "C", "D"}
    )
    assert {f.subject: (f.count, f.examples) for f in findings} == {
        "name": (2, ["B", "C"]),
        "made_ranges": (2, ["B", "C"]),
    }


def test_a_unique_registry_fact_groups_entries_only_in_their_own_registry() -> None:
    fields = registry.types()["acme/protein.Protein"].fields
    proposed = {**fields, "ec_number": replace(fields["ec_number"], unique=True)}
    duplicate = {"ec_number": "1.1.1.1"}
    records = [
        Record("1", "REC-1", "p1", "r1", "first", duplicate),
        Record("2", "REC-2", "p1", "r1", "second", duplicate),
        Record("3", "REC-3", "p2", "r2", "third", duplicate),
        # Another project holds the same entry. It participates in the clash with
        # the second entry, but these two holders alone do not break the rule.
        Record("4", "REC-4", "p3", "r1", "first", duplicate),
    ]
    [finding] = preflight_rules.relational_findings(fields, proposed, {}, {}, records)
    assert (finding.kind, finding.count, finding.examples) == (
        "unique",
        3,
        sorted(records[i].example for i in (0, 1, 3)),
    )
    assert not preflight_rules.relational_findings(fields, proposed, {}, {}, records[::3])


def test_unread_references_are_explicit_instead_of_passing_a_null_check() -> None:
    batch = registry.types()[BATCH]
    check = batch.rules["protein_not_bsl3"]
    proposed = {check.name: check}
    rows = [Record("1", "BAT-1", "p1", None, None, {"protein": "A"})]
    assert preflight_rules.reference_ids(batch.fields, batch.fields, {}, proposed, rows) == {"A"}
    [unknown] = preflight_rules.row_findings(batch.fields, batch.fields, {}, proposed, rows)
    assert unknown.blocking and unknown.kind == "unchecked"
    [broken] = preflight_rules.row_findings(
        batch.fields,
        batch.fields,
        {},
        proposed,
        rows,
        referenced={"A": {"id": "A", "hrid": "PRT-1", "hazard": "bsl3"}},
    )
    assert broken.blocking and broken.kind == "rule"


def test_datetime_overlap_compares_instants_ignoring_empty_and_reversed_ranges() -> None:
    fields = registry.types()[BATCH].fields
    rule = registry.types()[BATCH].rules["one_at_a_time"]

    def row(key: str, lo: str, hi: str) -> Record:
        prefix = "2026-03-12T04:00:"
        return Record(
            key,
            key,
            "p",
            None,
            None,
            {"starts_at": prefix + lo + "Z", "ends_at": prefix + hi + "Z", "plate": "P"},
        )

    rows = [
        row("A", "00", "00.5"),
        row("B", "00.1", "00.6"),
        row("adjacent", "00.6", "01"),
        row("empty", "00.2", "00.2"),
        row("reversed", "00.5", "00.12"),
    ]
    [find] = preflight_rules.relational_findings(fields, fields, {}, {rule.name: rule}, rows)
    assert find.examples == ["A", "B"]


def test_unusable_projected_values_are_reported_instead_of_crashing_other_checks() -> None:
    batch = registry.types()[BATCH]
    rule = batch.rules["one_at_a_time"]
    rows = [Record("A", "A", "p", None, None, {"starts_at": [], "ends_at": 4, "plate": {"k": 1}})]
    [unknown] = preflight_rules.relational_findings(
        batch.fields, batch.fields, {}, {rule.name: rule}, rows
    )
    assert unknown.kind == "unchecked" and unknown.count == 1


def test_check_reads_a_field_that_changes_on_its_referenced_type() -> None:
    batch = registry.types()[BATCH]
    entry = registry.types()["acme/protein.Protein"]
    old_fields = entry.current_fields
    new_fields = {**old_fields, "hazard": replace(old_fields["hazard"], values=("none",))}
    assert preflight_rules.changed_reference_rules(
        batch.fields,
        batch.rules,
        lambda _name: (entry.label, old_fields),
        lambda _name: (entry.label, new_fields),
    ) == {"protein_not_bsl3", "proteins_not_bsl3"}


def test_compound_datetime_equality_uses_the_instant_in_the_database() -> None:
    batch = registry.types()[BATCH]
    same_time = Rule(
        "same_time",
        "Unique start per plate",
        batch.plugin,
        unique=("starts_at", "plate"),
        reads=("starts_at", "plate"),
    )
    rows = [
        Record("1", "A", "p", None, None, {"starts_at": "2026-03-01T01:00:00+01:00", "plate": "P"}),
        Record("2", "B", "p", None, None, {"starts_at": "2026-03-01T00:00:00Z", "plate": "P"}),
    ]
    [finding] = preflight_rules.relational_findings(
        batch.fields, batch.fields, {}, {same_time.name: same_time}, rows
    )
    assert finding.count == 2 and finding.examples == ["A", "B"]
