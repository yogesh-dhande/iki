"""Splitting a packed job's output into the output of each of its runs (ADR 0055).

A ``batchable`` action's command is given several runs' entities at once, in one
input. Each part of what it writes says which entity it's for, so each goes to
that entity's run:

- an update, by its ``entity``
- a new entity, by its ``derived_from`` (all of one run's)
- a message, by its ``entity``; one without goes to every run
- metrics go to every run

Then each run's part is checked and written as any run's output is, on its own:
one run's results that don't fit, or can't be written, don't hold back the rest.
Output that can't be split fails every run, with where and why.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from iki_server.runs.models import ActionRun, RunInput
from iki_spec import schemas

__all__ = ["Unsplittable", "split"]


@dataclass
class Unsplittable:
    """Output that doesn't say which run each part is for: ``pointer`` and ``detail``."""

    errors: list[dict[str, str]]


def _pointer(*parts: object) -> str:
    return "#/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def split(
    document: Mapping[str, Any], runs: Sequence[ActionRun]
) -> list[dict[str, Any]] | Unsplittable:
    """Each run's part of `document`, in the order of `runs`, or why it can't be split."""
    issues = schemas.check("run-output", document)
    if issues:
        return Unsplittable([{"pointer": _pointer(*i.path), "detail": i.message} for i in issues])
    position = {r.pk: n for n, r in enumerate(runs)}
    owner: dict[str, int] = {
        str(row.entity_id): position[row.run_id]
        for row in RunInput.objects.filter(run__in=list(position)).only("run_id", "entity_id")
    }

    def run_of(entity: str) -> int | None:
        try:
            return owner.get(str(UUID(entity)))
        except ValueError:
            return None

    head = {"apiVersion": document["apiVersion"], "kind": document["kind"]}
    parts: list[dict[str, Any]] = [dict(head) for _ in runs]
    errors: list[dict[str, str]] = []

    for name, items in (document.get("updates") or {}).items():
        for index, item in enumerate(items):
            run = run_of(item["entity"])
            if run is None:
                errors.append(
                    {
                        "pointer": _pointer("updates", name, index, "entity"),
                        "detail": "It isn't one of the entities the job was given.",
                    }
                )
                continue
            parts[run].setdefault("updates", {}).setdefault(name, []).append(item)

    if "creates_file" in document:
        errors.append(
            {
                "pointer": _pointer("creates_file"),
                "detail": "A job that runs several runs at once can't give its creates in a "
                "file: give them in `creates`, each derived from its run's entity.",
            }
        )

    for index, item in enumerate(document.get("creates") or []):
        parents = {run_of(e) for e in item.get("derived_from") or []}
        if not parents:
            errors.append(
                {
                    "pointer": _pointer("creates", index, "derived_from"),
                    "detail": f"Say which entity it was made from: the job ran {len(runs)} runs "
                    "at once, and each new entity belongs to one.",
                }
            )
        elif None in parents:
            errors.append(
                {
                    "pointer": _pointer("creates", index, "derived_from"),
                    "detail": "It names an entity the job wasn't given.",
                }
            )
        elif len(parents) > 1:
            errors.append(
                {
                    "pointer": _pointer("creates", index, "derived_from"),
                    "detail": "It names entities of different runs: each new entity belongs "
                    "to one run.",
                }
            )
        else:
            (run,) = parents
            assert run is not None  # noqa: S101 - excluded above
            parts[run].setdefault("creates", []).append(item)

    for index, item in enumerate(document.get("messages") or []):
        if "entity" not in item:
            for part in parts:
                part.setdefault("messages", []).append(item)
            continue
        run = run_of(item["entity"])
        if run is None:
            errors.append(
                {
                    "pointer": _pointer("messages", index, "entity"),
                    "detail": "It isn't one of the entities the job was given.",
                }
            )
            continue
        parts[run].setdefault("messages", []).append(item)

    if "metrics" in document:
        for part in parts:
            part["metrics"] = dict(document["metrics"])
    if errors:
        return Unsplittable(errors)
    return parts
