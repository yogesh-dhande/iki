"""Compare the decisions stored for a reviewed import with its locked replan (ADR 0096)."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from itertools import islice
from typing import Any
from uuid import UUID

from iki_server.bundles.models import BundleImport, ImportEntity, ImportPerson


@dataclass
class ReviewedPlan:
    summary: dict[str, Any]
    entities: dict[UUID, Mapping[str, object]]
    people: dict[UUID, Mapping[str, object]]

    @classmethod
    def read(cls, made: BundleImport) -> ReviewedPlan:
        entities = ImportEntity.objects.filter(bundle_import=made).values(
            "entity_id",
            "type",
            "hrid",
            "hrid_here",
            "title",
            "action",
            "revisions",
            "adds",
            "why",
            "entry_id",
            "registers",
        )
        people = ImportPerson.objects.filter(bundle_import=made).values(
            "source_id",
            "kind",
            "name",
            "email",
            "user_id",
        )
        return cls(
            made.plan,
            {r["entity_id"]: r for r in entities.iterator()},
            {p["source_id"]: p for p in people.iterator()},
        )

    def difference(self, new: ReviewedPlan) -> str:
        """Name the first changes. All decisions must agree for an empty result."""
        if self == new:
            return ""
        changes = list(islice(self._changes(new), 6))
        if len(changes) > 5:
            changes[5] = "Other decisions changed too. Review the new plan."
        if not changes:
            # Types, counts, outside references and applicability are part of the plan too.
            return "The import summary or its requirements changed."
        return " ".join(changes)

    def _changes(self, new: ReviewedPlan) -> Iterator[str]:
        for key in self.entities.keys() | new.entities.keys():
            before, after = self.entities.get(key), new.entities.get(key)
            if before == after:
                continue
            if before is None or after is None:
                yield f"The list of records changed at {key}."
                continue
            hrid = after["hrid"]
            if before["hrid_here"] != after["hrid_here"]:
                if not after["hrid_here"]:
                    yield f"{hrid} would now be renumbered."
                elif after["hrid_here"] == hrid:
                    yield f"{hrid} would now keep its source HRID."
                else:
                    yield f"{hrid} would now use {after['hrid_here']}."
            elif before["action"] != after["action"]:
                yield f"{hrid}'s action changed from {before['action']} to {after['action']}."
            elif (
                before["entry_id"] != after["entry_id"] or before["registers"] != after["registers"]
            ):
                yield f"{hrid}'s registry entry changed."
            else:
                yield f"The planned revisions or details for {hrid} changed."
        for key in self.people.keys() | new.people.keys():
            before_person, after_person = self.people.get(key), new.people.get(key)
            if before_person == after_person:
                continue
            if not after_person:
                yield f"The bundle would no longer import the person {key}."
            elif after_person["user_id"] is None:
                yield f"{after_person['email']} would now need a placeholder."
            else:
                yield f"{after_person['email']} would now match a local account."
        old_members = self.summary.get("members", {})
        new_members = new.summary.get("members", {})
        for group in old_members.keys() | new_members.keys():
            old = old_members.get(group, {})
            current = new_members.get(group, {})
            for key in old.keys() | current.keys():
                if old.get(key) != current.get(key):
                    member = current.get(key) or old[key]
                    label = member["label"]
                    if group == "outside" and key in current:
                        status = "available" if current[key]["here"] else "missing"
                        yield f"The outside reference {label} is now {status}."
                    elif group == "files":
                        yield f"How the import stores or reuses {label} changed."
                    elif group == "runs":
                        yield f"How the import handles run {label} and its HRID changed."
                    else:
                        yield f"The registry decision for {label} changed."

        if "members" not in self.summary:
            yield "This earlier plan needs a fresh review of its members."
