"""Which procedure a reviewed record follows, and its review field as that procedure has it
(ADRs 0301, 0302, 0304).

Creating a record of a reviewed type stamps ``review_procedure`` and ``review_flow``:
from the project's choice for the type or an ancestor, else the deployment's default,
else, for ``pinned: {by: field}``, from the record that field names. A request is pinned
as it's sent instead, from its service's choice (ADR 0305). Neither is a refusal,
:class:`ProcedureNotChosen`: a record is never made under a procedure no one chose. The
stamp never changes.

A record's review field is its type's, with the states, guards, roles and signatures of
its pinned flow compiled from the procedure version installed here, the procedure's
slots bound to the lab roles its project chose for them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from functools import cache, lru_cache
from typing import TYPE_CHECKING, Any

from django.db.models import Q

from iki_server.entities import registry
from iki_server.org.services import Refused
from iki_server.reviews import sync
from iki_server.reviews.models import Choice, Procedure
from iki_spec import procedures
from iki_spec.model import (
    PINNED_FIELDS,
    Field,
    ReviewProcedure,
    ReviewSpec,
    names_people,
)

if TYPE_CHECKING:
    from iki_server.entities.models import Entity
    from iki_server.org.models import Project

__all__ = [
    "Pin",
    "ProcedureNotChosen",
    "at_send",
    "binding",
    "chosen",
    "field_of",
    "flow_field",
    "installed",
    "pin_of",
    "pin_sent",
    "review_of",
    "stamp",
]


class ProcedureNotChosen(Refused):
    """No one has chosen the procedure a new record of the type would follow."""

    code = "procedure_not_chosen"


@dataclass(frozen=True)
class Pin:
    procedure: str
    flow: str


def review_of(type_name: str) -> tuple[str, ReviewSpec] | None:
    """The type in ``type_name``'s lineage that declares its review, and the review."""
    if type_name not in registry.types() or type_name == "iki/core.Entity":
        return None
    e = registry.effective(type_name)
    if e.review is None:
        return None
    return e.sources["review"], e.review


@cache
def installed(qualified: str) -> ReviewProcedure:
    """An installed procedure version, as it was installed: a version never changes."""
    return sync.from_definition(Procedure.objects.get(qualified=qualified).definition)


def _lineage(type_name: str) -> tuple[str, ...]:
    return registry.effective(type_name).chain


def chosen(project: Project, type_name: str) -> Choice | None:
    """The current choice for ``type_name``'s new records in ``project``: the project's for
    it or its nearest ancestor with one, else the deployment's, likewise."""
    lineage = _lineage(type_name)
    rows = list(
        Choice.objects.filter(
            Q(project=project) | Q(project__isnull=True),
            entity_type__in=lineage,
            superseded_at__isnull=True,
        ).select_related("procedure")
    )
    rows.sort(key=lambda c: (c.project_id is None, lineage.index(c.entity_type)))
    return rows[0] if rows else None


def _not_chosen(project: Project, type_name: str) -> ProcedureNotChosen:
    label = registry.types()[type_name].label
    return ProcedureNotChosen(
        f"{label} has no review procedure in {project.name}. A project Admin chooses one "
        "in Project settings → Starters."
    )


def _entity_id(value: Any) -> Any:  # noqa: ANN401 - a reference as the form gives it
    return getattr(value, "pk", value)


def stamp(project: Project, type_name: str, given: Mapping[str, Any]) -> dict[str, Any]:
    """What a new record of ``type_name`` in ``project`` is pinned to, as its values:
    ``review_procedure``, ``review_flow`` and its review field, in its flow's first state.
    Empty for a type that isn't reviewed. Raises :class:`ProcedureNotChosen`."""
    from iki_server.entities.models import Entity

    found = review_of(type_name)
    if found is None or at_send(type_name):
        return {}
    _, review = found
    if review.by is not None:
        opener_id = _entity_id(given.get(review.by))
        opener = Entity.objects.filter(pk=opener_id).first() if opener_id else None
        pin = pin_of(opener) if opener is not None else None
        if pin is None:
            label = registry.types()[type_name].label
            raise ProcedureNotChosen(
                f"{label} follows the review procedure of its {review.by}, which has none."
            )
    else:
        choice = chosen(project, type_name)
        if choice is None:
            raise _not_chosen(project, type_name)
        pin = Pin(choice.procedure_id, choice.flow)
    out: dict[str, Any] = {"review_procedure": pin.procedure, "review_flow": pin.flow}
    if not review.opens:
        out[review.field] = installed(pin.procedure).flows[pin.flow].open
    return out


def at_send(type_name: str) -> bool:
    """Whether ``type_name``'s records are pinned as they're sent to a service, from its
    choice, rather than as they're created: a request's (ADR 0305)."""
    found = review_of(type_name)
    return (
        found is not None and found[1].opens and registry.effective(type_name).request is not None
    )


def pin_sent(entity: Entity, service: Project) -> Pin:
    """Pins a request as it's sent to ``service``, from the service's choice for its type:
    the service owns how its work is reviewed. Raises :class:`ProcedureNotChosen`."""
    from iki_server.entities.values import shape

    choice = chosen(service, entity.type)
    if choice is None:
        t = registry.types()[entity.type]
        raise ProcedureNotChosen(
            f"{service.name} hasn't chosen how the deliverables of its "
            f"{(t.label_plural or t.label).lower()} are reviewed, so it can't take this one "
            "yet. Its Admin chooses in Project settings → Starters."
        )
    pin = Pin(choice.procedure_id, choice.flow)
    part = shape(entity.type).part_of("review_procedure")
    part.model._default_manager.filter(pk=entity.pk, review_procedure__isnull=True).update(
        review_procedure=pin.procedure, review_flow=pin.flow
    )
    return pin


def pin_of(entity: Entity) -> Pin | None:
    """The procedure and flow ``entity`` was pinned to, or None for one that isn't
    reviewed (or a registry entry)."""
    from iki_server.entities.values import shape

    if entity.registry_id is not None or review_of(entity.type) is None:
        return None
    part = shape(entity.type).part_of("review_procedure")
    row = part.model._default_manager.filter(pk=entity.pk).values(*PINNED_FIELDS).first()
    if not row or not row["review_procedure"]:
        return None
    return Pin(row["review_procedure"], row["review_flow"])


def binding(project_id: Any, type_name: str, qualified: str) -> dict[str, str]:  # noqa: ANN401 - a UUID
    """Who plays each of ``qualified``'s slots on ``type_name``'s records in the project:
    the binding its latest choice of that procedure made (the project's, else the
    deployment's), else each slot's ``iki/lab_roles`` role (ADR 0304)."""
    found = chosen_binding(project_id, type_name, qualified)
    if found is not None:
        return found
    from iki_server.org import lab_roles

    return procedures.default_binding(installed(qualified), lab_roles.declared())


def chosen_binding(project_id: Any, type_name: str, qualified: str) -> dict[str, str] | None:  # noqa: ANN401 - a UUID
    """The binding the latest choice of ``qualified`` for ``type_name`` made, in the
    project or else the deployment, or None if it was never chosen there."""
    lineage = list(_lineage(type_name))
    found = review_of(type_name)
    if found is not None and found[1].by is not None:
        # A follower is played as the record it follows is (ADR 0305).
        ref = registry.effective(type_name).fields.get(found[1].by)
        for target in ref.to if ref is not None else ():
            lineage += [t for t in _lineage(target) if t not in lineage]
    rows = list(
        Choice.objects.filter(
            Q(project_id=project_id) | Q(project__isnull=True),
            entity_type__in=lineage,
            procedure_id=qualified,
        ).values("project_id", "entity_type", "binding", "chosen_at")
    )
    if rows:
        rows.sort(
            key=lambda c: (
                c["project_id"] is None,
                lineage.index(c["entity_type"]),
                -c["chosen_at"].timestamp(),
            )
        )
        return dict(rows[0]["binding"])
    return None


def _people(type_name: str) -> procedures.PeopleKind:
    """For a person path of ``type_name``'s records: a list of people (True), one
    (False), or not a person (None)."""
    fields = registry.effective(type_name).fields

    def kind(path: str) -> bool | None:
        head, _, tail = path.partition(".")
        f: Field | None = fields.get(head)
        if f is not None and tail:
            if f.type != "ref" or len(f.to) != 1 or f.to[0] not in registry.types():
                return None
            f = registry.effective(f.to[0]).fields.get(tail)
        if f is None or not names_people(f):
            return None
        return f.type == "list"

    return kind


@lru_cache(maxsize=1024)
def _compiled(
    type_name: str, qualified: str, flow: str, bound: tuple[tuple[str, str], ...]
) -> Field:
    found = review_of(type_name)
    assert found is not None  # noqa: S101 - only reviewed types are pinned
    _, review = found
    placeholder = registry.effective(type_name).fields[review.field]
    compiled = procedures.review_field(
        review, installed(qualified), flow, binding=dict(bound), people=_people(type_name)
    )
    return replace(placeholder, states=compiled.states, initial=compiled.initial)


def flow_field(type_name: str, pin: Pin, bound: Mapping[str, str] | None = None) -> Field:
    """``type_name``'s review field as a record pinned to ``pin`` has it, its slots played
    by ``bound`` (no one, if not given: as guards' variables are typed)."""
    return _compiled(type_name, pin.procedure, pin.flow, tuple(sorted((bound or {}).items())))


def field_of(entity: Entity, pin: Pin | None = None) -> Field | None:
    """``entity``'s review field, with the states of the flow it's pinned to, or None for
    a record that isn't reviewed or has no review field of its own."""
    found = review_of(entity.type)
    if found is None or found[1].opens:
        return None
    pin = pin if pin is not None else pin_of(entity)
    if pin is None:
        return None
    return flow_field(
        entity.type, pin, binding(entity.owner_project_id, entity.type, pin.procedure)
    )
