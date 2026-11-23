"""Compare a proposed plugin to the schema that stored the live records (ADR 0181)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import cached_property
from typing import Any

from django.db import DatabaseError, connection
from psycopg.errors import QueryCanceled

from iki_server.core.snapshots import snapshot
from iki_server.plugins import baseline, candidate, preflight_records, preflight_rules
from iki_server.plugins.preflight_records import Table, layout, records
from iki_server.plugins.preflight_sql import Scope
from iki_server.plugins.preflight_sql_fields import (
    field_checks,
    projection_inputs,
    sampled_fields,
    sampled_findings,
)
from iki_server.plugins.preflight_sql_relations import exact_relations
from iki_server.plugins.preflight_types import Finding, Report, TypeReport
from iki_server.plugins.preflight_values import amount_findings, amount_unit
from iki_spec import naming
from iki_spec.model import BUILTIN_TYPES, EntityType, Extension, Field, Plugin, Rule, plugin_to_dict

# Fetch one extra indexed key to distinguish a complete small type from a partial check.
SAMPLE_LIMIT = 500


class PreflightError(ValueError):
    """The proposed declarative schema cannot be compared with this deployment."""


@contextmanager
def read_snapshot() -> Iterator[None]:
    try:
        with snapshot(), connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = '15s'")
            yield
    except DatabaseError as error:
        if isinstance(error.__cause__, QueryCanceled):
            raise PreflightError(
                "The database timed out during preflight. Retry the check."
            ) from error
        raise


def preview_manifest(name: str, data: dict[str, Any]) -> Report:
    with read_snapshot():
        state = baseline.current()
        if state is None:
            raise PreflightError(
                "Run sync_plugins to record the deployment's current schema first."
            )
        revision, current = state
        plugin = candidate.read(name, data, installed=current)
        return compare(current, {**current, plugin.name: plugin}, revision, plugin)


@dataclass
class _Shape:
    tables: list[Table]
    rules: dict[str, Rule]
    # Rules that compare different records have the scope of their declaring table.
    rule_tables: dict[str, str]
    retired: dict[str, Field]
    amount_unit: str | None

    @cached_property
    def fields(self) -> dict[str, Field]:
        return {name: f for table in self.tables for name, f in table.fields.items()}


class _Schema:
    def __init__(self, plugins: Mapping[str, Plugin]) -> None:
        self.plugins = plugins
        self.types = {
            **BUILTIN_TYPES,
            **{k: t for p in plugins.values() for k, t in p.types.items()},
        }
        self.extensions = [e for p in plugins.values() for e in p.extensions]
        self._shapes: dict[str, _Shape] = {}

    def lineage(self, name: str) -> list[EntityType]:
        try:
            return preflight_records.lineage(name, self.plugins)
        except ValueError as error:
            raise PreflightError(str(error)) from error

    def shape(self, name: str) -> _Shape:
        if name not in self._shapes:
            chain = self.lineage(name)
            rules: dict[str, Rule] = {}
            scopes: dict[str, str] = {}
            retired: dict[str, Field] = {}
            names = {t.qualified_name for t in chain}
            declarations: list[EntityType | Extension] = [
                *reversed(chain),
                *(e for e in self.extensions if e.extends_type in names),
            ]
            for item in declarations:
                for rule in item.rules.values():
                    rules[rule.key] = rule
                    scopes[rule.key] = item.table
                retired.update(
                    {n: f for n, f in item.fields.items() if f.removed and f.type == "state"}
                )
            self._shapes[name] = _Shape(
                layout(name, self.plugins, chain=chain), rules, scopes, retired, amount_unit(chain)
            )
        return self._shapes[name]

    def ref_fields(self, name: str) -> tuple[str, Mapping[str, Field]] | None:
        if name not in self.types:
            return None
        if name == naming.ENTITY:
            return self.types[name].label, {}
        tables = self.shape(name).tables
        if any(t.identity for t in self.lineage(name)):
            tables = [t for t in tables if t.entry]
        return self.types[name].label, {n: f for t in tables for n, f in t.fields.items()}


def _touched(old: Plugin | None, new: Plugin) -> set[str]:
    """Types the candidate defines differently or extends differently, including removals."""
    old_types = old.types if old else {}
    names = {n for n in old_types.keys() | new.types.keys() if old_types.get(n) != new.types.get(n)}
    before = {e.extends_type: e for e in old.extensions} if old else {}
    after = {e.extends_type: e for e in new.extensions}
    return names | {n for n in before.keys() | after.keys() if before.get(n) != after.get(n)}


def _check_type(name: str, old: _Schema, new: _Schema, recheck: set[str]) -> TypeReport:
    before, after = old.shape(name), new.shape(name)
    proposed = {**after.fields, **after.retired}
    scope = Scope([name], before.tables)
    total = scope.population()
    findings = field_checks(scope, before.fields, proposed)
    findings.extend(amount_findings(before.amount_unit, after.amount_unit, name))
    # Reference targets may be changing in another type in this promotion.
    old_rules = {k: v for k, v in before.rules.items() if k not in recheck}
    rules = preflight_rules.checked_cel_rules(before.fields, proposed, old_rules, after.rules)
    fields = sampled_fields(before.fields, proposed)
    if not (fields or rules):
        return TypeReport(name, total, total, False, [], findings)

    reads = fields | {n for rule in rules.values() for n in rule.reads}
    rows, capped = records(
        name, old.plugins, SAMPLE_LIMIT, fields=projection_inputs(proposed, reads)
    )
    evaluated, projected = sampled_findings(before.fields, proposed, rows, reads=reads)
    ids = preflight_rules.reference_ids(after.fields, rules, projected)
    evaluated.extend(
        preflight_rules.row_findings(
            after.fields,
            rules,
            projected,
            referenced=preflight_records.referenced(ids, old.plugins, new.plugins) if ids else {},
            ref_fields=new.ref_fields,
        )
    )
    findings.extend(replace(f, sampled=capped) for f in evaluated)
    labels = [*(f"field {n}" for n in sorted(fields)), *(f"rule {k}" for k in sorted(rules))]
    return TypeReport(name, total, len(rows), capped, labels if capped else [], findings)


def compare(
    current: Mapping[str, Plugin], proposed: Mapping[str, Plugin], revision: str, plugin: Plugin
) -> Report:
    """Read only from the old schema. The caller supplies one read-only transaction."""
    digest = hashlib.sha256(
        json.dumps(
            plugin_to_dict(plugin), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()
    touched = _touched(current.get(plugin.name), plugin)
    report = Report(plugin.name, plugin.version, revision, digest, [])
    if not touched:
        return report
    old, new = _Schema(current), _Schema(proposed)
    scopes: dict[str, tuple[_Shape, _Shape]] = {}
    checked: dict[str, TypeReport] = {}
    for name, t in sorted(old.types.items()):
        if t.abstract or name == naming.ENTITY:
            continue
        target = new.types.get(name)
        if target is None or target.abstract:
            if name in touched:
                count, examples = Scope([name], []).count_examples()
                findings = []
                if count:
                    findings.append(
                        Finding(
                            "type",
                            name,
                            "Review the migration removing this concrete type.",
                            True,
                            count,
                            examples,
                        )
                    )
                report.types.append(TypeReport(name, count, count, False, [], findings))
            continue
        before, after = old.shape(name), new.shape(name)
        scopes[name] = before, after
        recheck = preflight_rules.changed_reference_rules(
            after.fields, after.rules, old.ref_fields, new.ref_fields
        )
        lineage = (t.qualified_name for t in [*old.lineage(name), *new.lineage(name)])
        if not recheck and (
            not touched.intersection(lineage)
            or (
                before.tables == after.tables
                and before.rules == after.rules
                and before.amount_unit == after.amount_unit
            )
        ):
            continue
        checked[name] = _check_type(name, old, new, recheck)
        report.types.append(checked[name])
    for name, findings in exact_relations(scopes, affected=set(checked)).items():
        if name not in checked:
            # A migration in a sibling can create a conflict with an unchanged type.
            total = Scope([name], []).population()
            checked[name] = TypeReport(name, total, total, False, [], [])
            report.types.append(checked[name])
        checked[name].findings.extend(findings)
    report.types.sort(key=lambda t: t.type)
    return report
