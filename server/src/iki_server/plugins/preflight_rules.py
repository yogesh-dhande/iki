"""Check proposed rules on bounded samples of projected records (ADR 0181)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from iki_server.entities import checking
from iki_server.plugins.preflight_projection import fill_default
from iki_server.plugins.preflight_types import Finding, Record
from iki_spec import rules as cel
from iki_spec import wasm
from iki_spec.model import Field, Rule


def _value_changes(old: Field, new: Field) -> bool:
    """Whether a migration or a new CEL representation can change an existing value."""
    if (old.type, old.removed, old.registry, old.unit, old.to) != (
        new.type,
        new.removed,
        new.registry,
        new.unit,
        new.to,
    ) or fill_default(old, new) is not None:
        return True
    if new.type == "state":
        return any(
            name in old.live_states and s.removed and s.moved_to in new.live_states
            for name, s in new.states.items()
        )
    if new.type == "list" and old.items and new.items:
        return _value_changes(old.items, new.items)
    return new.type == "row" and bool(projected_changes(old.fields, new.fields))


def projected_changes(before: Mapping[str, Field], after: Mapping[str, Field]) -> set[str]:
    """Field values a candidate can change, including the destination of a state move."""
    changed = before.keys() ^ after.keys()
    for name in before.keys() & after.keys():
        old, new = before[name], after[name]
        if old != new and _value_changes(old, new):
            changed.add(name)
        if new.removed and new.moved_to and not old.removed:
            changed.add(new.moved_to.field)
    return changed


def checked_cel_rules(
    old_fields: Mapping[str, Field],
    new_fields: Mapping[str, Field],
    old_rules: Mapping[str, Rule],
    new_rules: Mapping[str, Rule],
) -> dict[str, Rule]:
    changed = projected_changes(old_fields, new_fields)
    selected = {}
    for key, rule in new_rules.items():
        old = old_rules.get(key)
        if rule.check and (
            old is None
            or (old.check, old.when, bool(old.hold)) != (rule.check, rule.when, bool(rule.hold))
            or changed.intersection(rule.reads)
        ):
            selected[key] = rule
    return selected


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

    def target_changes(name: str) -> bool:
        old, new = before(name), after(name)
        if old is None or new is None:
            return old != new
        return bool(projected_changes(old[1], new[1]))

    changed = {
        name
        for name, field in fields.items()
        if name in reads
        and _refs(field)
        and any(target_changes(t) for t in (field.items.to if field.items else field.to))
    }
    return {key for key, r in rules.items() if r.check and changed.intersection(r.reads)}


def reference_ids(
    fields: Mapping[str, Field],
    rules: Mapping[str, Rule],
    rows: list[Record],
) -> set[str]:
    """One-hop lookup keys needed by changed CEL rules, batched across sampled records."""
    names = {
        name
        for rule in rules.values()
        for name in rule.reads
        if name in fields and _refs(fields[name])
    }
    return {
        target
        for row in rows
        for name in names
        for target in _ids(fields[name], row.values.get(name))
    }


def row_findings(
    fields: Mapping[str, Field],
    rules: Mapping[str, Rule],
    rows: list[Record],
    *,
    referenced: Mapping[str, dict[str, Any]] | None = None,
    ref_fields: cel.RefFields | None = None,
) -> list[Finding]:
    """Evaluate CEL with the save-time interpreter and its null and conditional semantics."""
    out: list[Finding] = []
    if not rows or not rules:
        return out
    variables = cel.rule_variables(
        cel.record_schema("Record", fields, ref_fields or checking.ref_fields)
    )
    targets = referenced or {}
    times = checking.now_values()
    for rule in rules.values():
        assert rule.check is not None  # noqa: S101 - checked_cel_rules selected these
        expressions = [(rule.check, cel.CONDITION)]
        if rule.when:
            expressions.append((rule.when, cel.CONDITION))
        broken: list[Record] = []
        unknown: list[Record] = []
        for start in range(0, len(rows), 64):
            checking_rows: list[Record] = []
            contexts = []
            for row in rows[start : start + 64]:
                record = _record(row, fields, targets, rule.reads)
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
