"""Check proposed rules on bounded samples of projected records (ADR 0181)."""

from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping
from typing import Any

from iki_server.entities import checking
from iki_server.plugins.preflight_types import Finding, Record
from iki_spec import rules as cel
from iki_spec import wasm
from iki_spec.model import Field, Rule


def _changed(
    old_fields: dict[str, Field],
    fields: dict[str, Field],
    old_rules: dict[str, Rule],
    rules: dict[str, Rule],
) -> Iterable[Rule]:
    for name, rule in rules.items():
        if rule != old_rules.get(name) or any(
            old_fields.get(n) != fields.get(n) for n in rule.reads
        ):
            yield rule


def _finding(kind: str, subject: str, message: str, rows: Iterable[Record]) -> Finding:
    hrids = {row.id: row.example for row in rows}
    return Finding(kind, subject, message, True, len(hrids), sorted(hrids.values())[:5])


def _refs(f: Field) -> bool:
    return f.type == "ref" or (f.type == "list" and f.items is not None and f.items.type == "ref")


def _ids(f: Field, value: Any) -> list[str]:  # noqa: ANN401 - a projected JSON value
    if value is None:
        return []
    return [str(v) for v in value] if f.type == "list" and isinstance(value, list) else [str(value)]


def changed_reference_rules(
    fields: Mapping[str, Field],
    rules: Mapping[str, Rule],
    before: cel.RefFields,
    after: cel.RefFields,
) -> set[str]:
    """CEL checks whose one-hop values may change even if their own type does not."""
    reads = {n for r in rules.values() if r.check for n in r.reads}
    changed = {
        name
        for name, field in fields.items()
        if name in reads
        and _refs(field)
        and any(before(t) != after(t) for t in (field.items.to if field.items else field.to))
    }
    return {key for key, r in rules.items() if r.check and changed.intersection(r.reads)}


def reference_ids(
    old_fields: dict[str, Field],
    new_fields: dict[str, Field],
    old_rules: dict[str, Rule],
    new_rules: dict[str, Rule],
    rows: list[Record],
) -> set[str]:
    """One-hop lookup keys needed by changed CEL rules, batched across sampled records."""
    names = {
        name
        for rule in _changed(old_fields, new_fields, old_rules, new_rules)
        if rule.kind == "check"
        for name in rule.reads
        if name in new_fields and _refs(new_fields[name])
    }
    return {
        target
        for row in rows
        for name in names
        for target in _ids(new_fields[name], row.values.get(name))
    }


def row_findings(
    old_fields: dict[str, Field],
    new_fields: dict[str, Field],
    old_rules: dict[str, Rule],
    new_rules: dict[str, Rule],
    rows: list[Record],
    *,
    referenced: Mapping[str, dict[str, Any]] | None = None,
    ref_fields: cel.RefFields | None = None,
) -> list[Finding]:
    """Evaluate CEL with the save-time interpreter and its null and conditional semantics."""
    out: list[Finding] = []
    checks = [rule for rule in _changed(old_fields, new_fields, old_rules, new_rules) if rule.check]
    if not rows or not checks:
        return out
    variables = cel.rule_variables(
        cel.record_schema("Record", new_fields, ref_fields or checking.ref_fields)
    )
    targets = referenced or {}
    times = checking.now_values()
    for rule in checks:
        assert rule.check is not None  # noqa: S101 - filtered above
        expressions = [(rule.check, cel.CONDITION)]
        if rule.when:
            expressions.append((rule.when, cel.CONDITION))
        broken: list[Record] = []
        unknown: list[Record] = []
        for start in range(0, len(rows), 64):
            checking_rows: list[Record] = []
            contexts = []
            for row in rows[start : start + 64]:
                record = _record(row, new_fields, targets, rule.reads)
                if record is None:
                    unknown.append(row)
                else:
                    checking_rows.append(row)
                    contexts.append({"record": record, **times})
            try:
                results = cel.evaluate(variables, expressions, contexts)
            except wasm.UnavailableError:
                unknown.extend(checking_rows)
                continue
            for row, outcomes in zip(checking_rows, results, strict=True):
                failure = checking.check_failure(outcomes[0], outcomes[1] if rule.when else None)
                if failure == "":
                    broken.append(row)
                elif failure is not None:
                    unknown.append(row)
        if broken:
            message = (
                "The proposed rule would hold these records for review."
                if rule.hold
                else "These records fail the proposed rule."
            )
            out.append(_finding("rule", rule.key, message, broken))
        if unknown:
            out.append(
                _finding(
                    "unchecked",
                    rule.key,
                    "The rule or a reference it reads could not be evaluated.",
                    unknown,
                )
            )
    return out


def _record(
    row: Record,
    fields: Mapping[str, Field],
    targets: Mapping[str, dict[str, Any]],
    reads: Iterable[str],
) -> dict[str, Any] | None:
    record: dict[str, Any] = {"id": row.id, "hrid": row.hrid}
    for name in reads:
        field = fields.get(name)
        if field is None:
            continue
        value = row.values.get(name)
        if _refs(field):
            ids = _ids(field, value)
            if any(k not in targets for k in ids):
                return None
            if field.type == "list":
                record[name] = [targets[k] for k in ids]
            else:
                record[name] = targets[ids[0]] if ids else None
        else:
            try:
                record[name] = checking.cel_value(field, value, from_json=True)
            except (AttributeError, TypeError, ValueError):
                # A field can change type. Its existing rows need a backfill
                # before a rule over the proposed shape can read them.
                return None
    return record


def relational_findings(
    old_fields: dict[str, Field],
    new_fields: dict[str, Field],
    old_rules: dict[str, Rule],
    new_rules: dict[str, Rule],
    rows: list[Record],
    *,
    report_ids: set[str] | None = None,
    entry_fields: Collection[str] = (),
) -> list[Finding]:
    """Compare across a declaring table's subtypes, reporting clashes touching this type."""
    out: list[Finding] = []
    for name, field in new_fields.items():
        if field.removed or not field.unique or old_fields.get(name) == field:
            continue
        on_entry = name in entry_fields or field.registry is not None
        groups: dict[tuple[str, str], list[Record]] = defaultdict(list)
        for row in rows:
            scope = row.registry if on_entry else row.project
            value = row.values.get(name)
            if scope is not None and value is not None:
                groups[(scope, json.dumps(value, sort_keys=True))].append(row)
        duplicates = [
            r
            for group in groups.values()
            if len({(r.entry or r.id) if on_entry else r.id for r in group}) > 1
            for r in group
            if report_ids is None or r.id in report_ids
        ]
        if duplicates:
            scope_label = "registry" if on_entry else "project"
            out.append(
                _finding(
                    "unique",
                    name,
                    f"These records share a value within a {scope_label}.",
                    duplicates,
                )
            )
    for rule in _changed(old_fields, new_fields, old_rules, new_rules):
        if rule.kind == "check":
            continue
        unknown: list[Record] = []
        participants = _constraint(rule, new_fields, rows, unknown)
        matches = [row for row in participants if report_ids is None or row.id in report_ids]
        if matches:
            message = (
                "These records share the same combination of values in a project."
                if rule.unique
                else "These records have overlapping ranges in a project."
            )
            out.append(_finding(rule.kind, rule.key, message, matches))
        unread = [row for row in unknown if report_ids is None or row.id in report_ids]
        if unread:
            out.append(
                _finding(
                    "unchecked", rule.key, "The proposed rule cannot compare these values.", unread
                )
            )
    return out


def _constraint(
    rule: Rule, fields: dict[str, Field], rows: list[Record], unknown: list[Record]
) -> Iterable[Record]:
    """Group like the rule trigger's iki_rule_values, ignoring null keys and inactive rows."""
    groups: dict[tuple[Any, ...], list[Record]] = defaultdict(list)
    for row in rows:
        if row.project is None:
            continue
        if any(_text(row.values.get(n)) not in allowed for n, allowed in rule.only_while):
            continue
        names = rule.unique or rule.per
        values = [row.values.get(n) for n in names]
        if None in values:
            continue
        if any(isinstance(v, (dict, list)) for v in values):
            unknown.append(row)
            continue
        try:
            key = tuple(
                _bound(fields[n].type, v) if fields[n].type in ("date", "datetime") else v
                for n, v in zip(names, values, strict=True)
            )
        except ValueError:
            unknown.append(row)
            continue
        groups[(row.project, *key)].append(row)
    for group in groups.values():
        if len(group) < 2:
            continue
        if rule.unique:
            yield from group
        else:
            start, end = rule.no_overlap
            yield from _overlaps(group, start, end, fields[start].type, unknown)


def _text(value: Any) -> str | None:  # noqa: ANN401 - a stored enum, state or bool
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _bound(kind: str, value: Any) -> Any:  # noqa: ANN401 - one range endpoint
    if kind == "date" and isinstance(value, str):
        return dt.date.fromisoformat(value)
    if kind == "datetime" and isinstance(value, str):
        instant = dt.datetime.fromisoformat(value)
        if instant.tzinfo is not None:
            return instant
    if kind in ("int", "float", "quantity") and type(value) in (int, float):
        return value
    raise ValueError("Value does not fit the range endpoint.")


def _overlaps(
    rows: list[Record], start: str, end: str, kind: str, unknown: list[Record]
) -> Iterable[Record]:
    """A sweep by start, keeping the furthest end, finds all participants in O(n log n).

    The database includes both ends of a date range and excludes the upper endpoint of
    a number or datetime range. Empty and inverted ranges overlap nothing.
    """
    inclusive = kind == "date"
    spans = []
    for row in rows:
        lo, hi = row.values.get(start), row.values.get(end)
        if lo is None or hi is None:
            continue
        try:
            lo, hi = _bound(kind, lo), _bound(kind, hi)
        except ValueError:
            unknown.append(row)
            continue
        if lo < hi or (inclusive and lo == hi):
            spans.append((lo, hi, row))
    furthest = None
    previous: Record | None = None
    for lo, hi, row in sorted(spans, key=lambda span: span[0]):
        if previous is not None and (lo < furthest or (inclusive and lo == furthest)):
            yield previous
            yield row
        if previous is None or hi > furthest:
            previous, furthest = row, hi
