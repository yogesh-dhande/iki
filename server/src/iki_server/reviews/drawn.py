"""The flows a reviewed type's records can follow, to draw (ADR 0306).

A reviewed type's review field has no states of its own: a record's are its pinned
procedure's flow (ADR 0301). So a type's flow lists, for its review field, each flow a
project could pin it to: those an installed starter offers for it (or for the type its
records follow), the project's current choice and the deployment's default, and a pin
asked for by name, such as a record's own. Each is compiled as the server enforces it,
its slots played by the roles that choice binds them to.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from iki_server.entities import registry
from iki_server.reviews import pins
from iki_server.reviews.models import Choice
from iki_spec.model import Field, ReviewSpec

if TYPE_CHECKING:
    from iki_server.org.models import Project

__all__ = ["Drawn", "DrawnFlow", "drawn"]


@dataclass
class DrawnFlow:
    procedure: str
    procedure_label: str
    flow: str
    label: str
    summary: str
    #: The review field as a record pinned to it has it; None for a type that only opens.
    field: Field | None
    starter: str | None = None
    suggested: bool = False
    chosen: bool = False
    default: bool = False
    pinned: bool = False


@dataclass
class Drawn:
    field: str | None
    follows: str | None
    flows: list[DrawnFlow] = dataclasses.field(default_factory=list)


def _governing(type_name: str, review: ReviewSpec) -> list[str]:
    """The types whose choice a record of ``type_name`` is pinned by: its own, or for a
    follower, each type its ``by`` reference may name."""
    if review.by is None:
        return [type_name]
    ref = registry.effective(type_name).fields.get(review.by)
    return [t for t in (ref.to if ref is not None else ()) if t in registry.types()]


def _current(project: Project | None, types: list[str]) -> Choice | None:
    """The nearest current choice for one of ``types`` or their ancestors, in ``project``
    alone (None: the deployment's default alone)."""
    for name in types:
        lineage = list(registry.effective(name).chain)
        rows = list(
            Choice.objects.filter(
                project=project, entity_type__in=lineage, superseded_at__isnull=True
            )
        )
        if rows:
            rows.sort(key=lambda c: lineage.index(c.entity_type))
            return rows[0]
    return None


@dataclass
class _Offer:
    procedure: str
    flow: str
    summary: str
    starter: str
    suggested: bool
    #: Who plays each slot as the starter binds it, before anyone chooses.
    binding: dict[str, str]


def _offered(types: list[str]) -> Iterator[_Offer]:
    """Each flow an installed starter offers for one of ``types`` or an ancestor."""
    from iki_server.setup import declared_starters

    lineages = {t: set(registry.effective(t).chain) for t in types}
    for qualified, (starter, procedure) in sorted(declared_starters.declared().items()):
        if not any(starter.governs in chain for chain in lineages.values()):
            continue
        bound = declared_starters.binding(starter, procedure)
        for flow in starter.flows:
            yield _Offer(
                procedure=starter.procedure,
                flow=flow,
                summary=starter.summaries.get(flow) or procedure.flows[flow].summary,
                starter=qualified,
                suggested=flow == starter.suggested,
                binding=bound,
            )


def drawn(type_name: str, project: Project | None, pin: pins.Pin | None = None) -> Drawn | None:
    """``type_name``'s review flows, or None for a type that isn't reviewed."""
    found = pins.review_of(type_name)
    if found is None:
        return None
    _, review = found
    governing = _governing(type_name, review)
    chosen = _current(project, governing) if project is not None else None
    default = _current(None, governing)
    flows: dict[tuple[str, str], DrawnFlow] = {}

    def add(procedure: str, flow: str, summary: str | None = None) -> DrawnFlow:
        key = (procedure, flow)
        if key not in flows:
            installed = pins.installed(procedure)
            flows[key] = DrawnFlow(
                procedure=procedure,
                procedure_label=installed.label,
                flow=flow,
                label=installed.flows[flow].label,
                summary=summary or installed.flows[flow].summary,
                field=None,
            )
        return flows[key]

    # Before a procedure is chosen, its slots are played as its starter binds them.
    offered: dict[str, dict[str, str]] = {}
    for offer in _offered(governing):
        made = add(offer.procedure, offer.flow, offer.summary)
        made.starter = made.starter or offer.starter
        made.suggested = made.suggested or offer.suggested
        offered.setdefault(offer.procedure, offer.binding)
    if default is not None:
        add(default.procedure_id, default.flow).default = True
    if chosen is not None:
        add(chosen.procedure_id, chosen.flow).chosen = True
    if pin is not None:
        add(pin.procedure, pin.flow).pinned = True
    for (procedure, flow), made in flows.items():
        if review.opens:
            continue
        # Drawn with the roles that play its slots here: the project's binding, else the
        # deployment's, else its starter's, else each slot's shared role (ADR 0304).
        project_id = project.pk if project else None
        bound = pins.chosen_binding(project_id, type_name, procedure)
        if bound is None:
            bound = offered.get(procedure) or pins.binding(project_id, type_name, procedure)
        made.field = pins.flow_field(type_name, pins.Pin(procedure, flow), bound)
    return Drawn(
        field=None if review.opens else review.field,
        follows=review.by,
        flows=list(flows.values()),
    )
