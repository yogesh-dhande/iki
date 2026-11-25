"""Entity references in a command's stored output and in comparisons of two outputs."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from iki_server.accounts.models import User
from iki_server.entities import access, registry
from iki_server.entities.models import Entity
from iki_server.entities.values import UNREADABLE_REFERENCE, exact_reference_ids, shape
from iki_server.runs.models import ActionRun, RunOutput


class Unavailable(ValueError):  # noqa: N818 - the reference types are unavailable
    """The exact output needs a complete description of the references it carries."""


def dependencies(document: dict[str, Any]) -> set[UUID]:
    """IDs named by the worker protocol and by its typed create and update values."""
    refs: set[UUID] = set()

    def values(type_name: str | None, item: dict[str, Any]) -> None:
        fields = item.get("values") or {}
        if fields:
            if type_name is None or type_name not in registry.types():
                raise Unavailable
            refs.update(exact_reference_ids(shape(type_name).fields(removed=True), fields))

    try:
        for msg in document.get("messages") or []:
            if msg.get("entity"):
                refs.add(UUID(msg["entity"]))
        for item in document.get("creates") or []:
            refs.update(UUID(v) for v in item.get("derived_from") or [])
            values(item.get("type"), item)
        updates = [
            (UUID(item["entity"]), item)
            for group in (document.get("updates") or {}).values()
            for item in group
        ]
        types = dict(
            Entity.objects.filter(pk__in={pk for pk, _ in updates}).values_list("pk", "type")
        )
        for pk, item in updates:
            refs.add(pk)
            values(types.get(pk), item)
    except (ValueError, KeyError, AttributeError, TypeError) as exc:
        raise Unavailable from exc
    return refs


def _compares_entities(difference: dict[str, Any]) -> bool:
    return difference["pointer"].removeprefix("#/").partition("/")[0] in {"creates", "updates"}


def comparison_dependencies(runs: Sequence[ActionRun]) -> dict[UUID, set[UUID] | None]:
    """References backing stored comparison values, including values shortened for display."""
    compared = [
        r
        for r in runs
        if r.reproduction and any(_compares_entities(d) for d in r.reproduction["differences"])
    ]
    ids = {pk for r in compared for pk in (r.pk, r.reproduces_id) if pk is not None}
    refs: dict[UUID, set[UUID]] = {}
    for output in RunOutput.objects.filter(run_id__in=ids).final():
        try:
            refs[output.run_id] = dependencies(output.document)
        except Unavailable:
            continue
    return {
        r.pk: refs[r.pk] | refs[r.reproduces_id]
        if r.reproduces_id in refs and r.pk in refs
        else None
        for r in compared
    }


def comparisons(user: User, runs: Sequence[ActionRun]) -> dict[UUID, dict[str, Any]]:
    """The outcome of each comparison, with entity values for their current readers."""
    named = comparison_dependencies(runs)
    readable = access.readable_reference_ids(
        user, {pk for ids in named.values() if ids for pk in ids}
    )
    out = {}
    for run in runs:
        if not run.reproduction:
            continue
        shown = run.reproduction
        if run.pk in named:
            refs = named[run.pk]
            if refs is None or not refs <= readable:
                shown = {
                    **shown,
                    "differences": [
                        {**d, "original": UNREADABLE_REFERENCE, "reproduced": UNREADABLE_REFERENCE}
                        if _compares_entities(d)
                        else d
                        for d in shown["differences"]
                    ],
                }
        out[run.pk] = shown
    return out
