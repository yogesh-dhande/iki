"""Rules some core types' values follow beyond what a manifest can say.

A manifest checks each field on its own. Core types can need more, checked
on every create and edit, so they hold for the web, the API, MCP and runs alike:

- ``iki/core.Collection`` lists its ``members`` or has a ``search``, not both. Its
  search must work for the person saving it, in a project they can read.
- ``iki/core.View`` (ADR 0086, ADR 0110) shows, for each of its entities, a revision
  that entity has, in a viewer its type has, with what that viewer keeps.
- ``iki/core.App`` (ADR 0090) has a spec whose panels, props and expressions work for
  the person saving it (see :mod:`iki_server.entities.app_specs`).
- ``iki/core.Complex`` (ADR 0161) lists each part once, with its copies, each a registry
  entry; and each of its bonds joins two residues there are, of copies it has, a disulfide
  two cysteines in proteins' sequences, each residue in one bond at most.

Subtypes follow their parents' rules.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any

from iki_server.accounts.models import User
from iki_server.entities import access, app_specs, registry, search
from iki_server.entities.models import Entity, Revision
from iki_server.org import permissions
from iki_server.org.models import Project
from iki_server.org.permissions import Action
from iki_spec.model import EntityType

Errors = dict[str, list[str]]


@dataclass(frozen=True)
class Rule:
    #: The fields it reads. On an edit it sees their new values, or their current ones.
    fields: tuple[str, ...]
    #: The person saving it, the values it reads, and the project it's in.
    check: Callable[[User, Mapping[str, Any], Project], Errors]


def _collection(user: User, values: Mapping[str, Any], project: Project) -> Errors:
    data = values.get("search")
    if data is None:
        return {}
    if values.get("members"):
        return {"search": ["A collection lists its members or has a search, not both."]}
    errors = search_errors(user, data)
    return {"search": errors} if errors else {}


def search_errors(user: User, data: Mapping[str, Any]) -> list[str]:
    """What's wrong with a collection's search, run as ``user``: nothing if it works."""
    try:
        found = search.stored(data)
        if found.project is not None:
            project = Project.objects.filter(pk=found.project).first()
            if project is None or not permissions.can(user, Action.PROJECT_READ, project):
                raise search.SearchError([("#/project", "No such project, or you can’t see it.")])
        search.queryset(user, found)
    except search.SearchError as error:
        return [_pointed(p, d) for p, d in error.errors]
    return []


def _pointed(pointer: str, detail: str) -> str:
    where = pointer.removeprefix("#/").replace("/", ".")
    return f"`{where}`: {detail}" if where else detail


#: What each viewer keeps in a version 1 view's state (of one entity), besides its version.
VIEW_STATE = {"sequence": {"feature"}, "structure": {"camera"}, "alignment": {"position"}}
#: What each viewer keeps of each entity in a version 2 view's ``state.targets``.
TARGET_STATE = {
    "sequence": {"label", "feature"},
    "structure": {"label", "colour", "hidden", "transform", "superposed", "selection"},
    "alignment": {"label", "position"},
}


def _view(user: User, values: Mapping[str, Any], project: Project) -> Errors:
    targets: list[Any] = list(values.get("targets") or [])
    found = {e.pk: e for e in access.visible(user).filter(pk__in=targets)}
    # A target of a type with an identity is its registry entry (ADR 0115): the view pins
    # the view's project's record of it, which the references' check says it has.
    for record in Entity.objects.filter(
        entry_id__in=[t for t in targets if t not in found], owner_project=project
    ):
        assert record.entry_id is not None  # noqa: S101 - filtered so
        found[record.entry_id] = record
    if not targets or any(t not in found for t in targets):
        return {}  # The references' own check says so, naming nothing they can't read.
    shown = [found[t] for t in targets]
    revisions = values.get("target_revisions")
    viewers = values.get("viewers")
    errors: Errors = {}
    for name, given in (("target_revisions", revisions), ("viewers", viewers)):
        if given is not None and len(given) != len(targets):
            errors[name] = [
                f"has {len(given)} for {len(targets)} entities: give one for each, in the "
                "same order as `targets`."
            ]
    if errors:
        return errors
    if revisions is not None:
        had = set(
            Revision.objects.filter(
                entity_id__in=[e.pk for e in shown], number__in=set(revisions)
            ).values_list("entity_id", "number")
        )
        missing = [
            f"{e.hrid} has no revision {n}."
            for e, n in zip(shown, revisions, strict=True)
            if (e.pk, n) not in had
        ]
        if missing:
            errors["target_revisions"] = missing
    if viewers is not None:
        problems = _viewers(shown, viewers)
        if problems:
            errors["viewers"] = problems
        state = values.get("state")
        if isinstance(state, Mapping):
            problems = _view_state(shown, viewers, state)
            if problems:
                errors["state"] = problems
    return errors


def _viewers(shown: list[Any], viewers: list[str]) -> list[str]:
    problems = []
    seen: set[tuple[Any, str]] = set()
    for entity, viewer in zip(shown, viewers, strict=True):
        entity_type = registry.types().get(entity.type)
        has = {v.viewer for v in registry.viewers(entity_type)} if entity_type else set()
        if viewer not in has:
            problems.append(f"{entity.hrid}’s type has no {viewer} viewer.")
        if (entity.pk, viewer) in seen:
            problems.append(f"It shows {entity.hrid} in the {viewer} viewer twice.")
        seen.add((entity.pk, viewer))
    return problems


def _view_state(shown: list[Any], viewers: list[str], state: Mapping[str, Any]) -> list[str]:
    """What's wrong with a view's state for its entities and viewers, beyond its schema."""
    if state.get("version") == 1:
        if len(shown) > 1:
            return ["A view of several entities keeps their state as version 2, in `targets`."]
        others = sorted(set(state) - {"version"} - VIEW_STATE[viewers[0]])
        listed = ", ".join(f"`{o}`" for o in others)
        return [f"The {viewers[0]} viewer doesn’t keep {listed}."] if others else []
    problems = []
    if "camera" in state and "structure" not in viewers:
        problems.append("Only the structure viewer keeps `camera`, and it shows none of them.")
    kept = state.get("targets")
    if not isinstance(kept, list):
        return problems  # The schema says so.
    if len(kept) != len(shown):
        return [
            *problems,
            f"`targets` has {len(kept)} for {len(shown)} entities: give one for each, in the "
            "same order.",
        ]
    for index, (entity, viewer, item) in enumerate(zip(shown, viewers, kept, strict=True)):
        if not isinstance(item, Mapping):
            continue
        others = sorted(set(item) - TARGET_STATE[viewer])
        if others:
            listed = ", ".join(f"`{o}`" for o in others)
            problems.append(f"The {viewer} viewer doesn’t keep {listed} for {entity.hrid}.")
        onto = (item.get("superposed") or {}).get("onto")
        if isinstance(onto, int) and (
            onto == index or onto >= len(shown) or viewers[onto] != "structure"
        ):
            problems.append(
                f"{entity.hrid} is superposed on entity {onto + 1}, which isn’t another "
                "entity in the structure viewer."
            )
        spans = [s for s in item.get("selection") or [] if isinstance(s, Mapping)]
        # A residue is its number and insertion code: 35A comes after 35 and before 35B.
        if any(
            (s.get("start", 0), s.get("start_insertion", ""))
            > (s.get("end", 0), s.get("end_insertion", ""))
            for s in spans
        ):
            problems.append(f"{entity.hrid}’s selection has a range that ends before it starts.")
    return problems


def _app(user: User, values: Mapping[str, Any], project: Project) -> Errors:
    spec = values.get("spec")
    if not isinstance(spec, Mapping):
        return {}  # The field's schema says so.
    found = app_specs.problems(user, spec)
    return {"spec": found} if found else {}


#: What a disulfide joins, at each end: a cysteine, in a protein's sequence.
CYSTEINE = "C"
PROTEIN = "protein_sequence"
#: What a sequence of each format is, for saying why a disulfide can't end on it.
POLYMERS = {"dna_sequence": "DNA's", "rna_sequence": "RNA's"}


def _complex(user: User, values: Mapping[str, Any], project: Project) -> Errors:
    components: list[Mapping[str, Any]] = list(values.get("components") or [])
    bonds: list[Mapping[str, Any]] = list(values.get("bonds") or [])
    ids = {row["part"] for row in components if row.get("part") is not None}
    ids |= {row[end] for row in bonds for end in ("part", "partner") if row.get(end) is not None}
    found: dict[Any, Entity] = {e.pk: e for e in Entity.objects.filter(pk__in=ids)}
    errors: Errors = {}
    copies: dict[Any, int] = {}
    for index, row in enumerate(components):
        part = found.get(row.get("part"))
        if part is None:
            continue  # The reference's own check says so.
        if part.pk in copies:
            errors[f"components/{index}/part"] = [
                f"lists {part.hrid} again: give each part once, with how many copies of it "
                "there are."
            ]
            continue
        copies[part.pk] = int(row.get("copies") or 1)
        if part.registry_id is None:
            errors[f"components/{index}/part"] = [
                f"{part.hrid} is a project's own record, not a registered molecule: a "
                "complex is made of registry entries, such as proteins and compounds."
            ]
    residues = _residues([found[i] for i in copies if i in found])
    taken: dict[tuple[Any, int, int], int] = {}
    for index, row in enumerate(bonds):
        problems: dict[str, list[str]] = {}
        ends = []
        for end, copy, residue in (
            ("part", "copy", "residue"),
            ("partner", "partner_copy", "partner_residue"),
        ):
            part = found.get(row.get(end))
            if part is None:
                continue
            if part.pk not in copies:
                problems[end] = [f"{part.hrid} isn't one of its parts."]
                continue
            which, at = int(row.get(copy) or 1), int(row.get(residue) or 0)
            if which > copies[part.pk]:
                n = copies[part.pk]
                problems[copy] = [
                    f"{part.hrid} has {n} cop{'y' if n == 1 else 'ies'} in it, so there's no "
                    f"copy {which}."
                ]
                continue
            sequence, form = residues.get(part.pk, (None, None))
            if sequence is not None and at > len(sequence):
                problems[residue] = [
                    f"{part.hrid} has {len(sequence)} residues, so there's no residue {at}."
                ]
                continue
            if row.get("kind") == "disulfide":
                # A disulfide joins two cysteines' sulfurs, so each end must be a residue
                # that's known to be one: a cysteine in a protein's sequence. A base is no
                # amino acid, and a part with no sequence (a compound, a heme, another
                # complex) has no residue to check.
                why = None
                if sequence is None:
                    why = (
                        f"{part.hrid} has no protein sequence to place it on, and a disulfide "
                        "joins two cysteines (C) in proteins' sequences."
                    )
                elif form != PROTEIN:
                    why = (
                        f"{part.hrid}'s sequence is {POLYMERS.get(form or '', 'another kind')}, "
                        "not a protein's, and a disulfide joins two cysteines (C) in proteins' "
                        "sequences."
                    )
                elif sequence[at - 1] != CYSTEINE:
                    why = (
                        f"{part.hrid}'s residue {at} is {sequence[at - 1]}, and a disulfide "
                        "joins two cysteines (C)."
                    )
                if why is not None:
                    problems[residue] = [why]
                    continue
            ends.append((part, which, at, residue))
        if len(ends) == 2 and ends[0][:3] == ends[1][:3]:
            problems["partner_residue"] = ["is the residue it starts from: a bond joins two."]
        elif len(ends) == 2:
            for part, which, at, residue in ends:
                other = taken.get((part.pk, which, at))
                if other is not None:
                    problems[residue] = [
                        f"{part.hrid}'s residue {at} (copy {which}) is bonded already, in bond "
                        f"{other + 1}."
                    ]
                else:
                    taken[(part.pk, which, at)] = index
        for name, messages in problems.items():
            errors[f"bonds/{index}/{name}"] = messages
    return errors


def _residues(parts: list[Entity]) -> dict[Any, tuple[str, str]]:
    """Each part's residues, upper case and without a terminal stop, and their sequence
    format (a protein's, DNA's or RNA's): read from the field its type's sequence viewer
    shows, or else its own field of a sequence format; none for a part without one, such
    as a compound or another complex."""
    from iki_server.entities.services import current_values_of
    from iki_server.entities.values import shape
    from iki_spec.identity import SEQUENCE_FORMATS

    fields: dict[Any, tuple[str, str]] = {}
    for part in parts:
        entity_type = registry.types().get(part.type)
        if entity_type is None:
            continue
        own = shape(part.type).fields()
        shown = next((v.sequence for v in registry.viewers(entity_type) if v.sequence), None)
        if shown is None or shown not in own:
            shown = next((n for n, f in own.items() if f.format in SEQUENCE_FORMATS), None)
        if shown is not None and own[shown].format in SEQUENCE_FORMATS:
            fields[part.pk] = (shown, str(own[shown].format))
    if not fields:
        return {}
    values = current_values_of([p for p in parts if p.pk in fields])
    out: dict[Any, tuple[str, str]] = {}
    for pk, (name, form) in fields.items():
        text = (values.get(pk) or {}).get(name)
        if isinstance(text, str):
            out[pk] = (text.upper().rstrip("*"), form)
    return out


RULES: dict[str, Rule] = {
    "iki/core.App": Rule(("spec",), _app),
    "iki/core.Collection": Rule(("members", "search"), _collection),
    "iki/core.Complex": Rule(("components", "bonds"), _complex),
    "iki/core.View": Rule(("targets", "target_revisions", "viewers", "state"), _view),
}


def rules_for(entity_type: EntityType) -> list[Rule]:
    """The rules for a type: its own and its parents'."""
    chain = [entity_type, *registry.ancestors(entity_type)]
    return [RULES[t.qualified_name] for t in chain if t.qualified_name in RULES]


def check(
    user: User,
    entity_type: EntityType,
    project: Project,
    values: Callable[[str], Any],
    changed: Collection[str] | None = None,
) -> Errors:
    """Every rule's problems. ``values`` gives a field's value as it will be saved.

    On an edit, ``changed`` names the fields it changes, and only rules that
    read one of them run: renaming a collection doesn't re-check its search.
    """
    errors: Errors = {}
    for rule in rules_for(entity_type):
        if changed is not None and not set(rule.fields) & set(changed):
            continue
        given = {f: values(f) for f in rule.fields}
        for name, messages in rule.check(user, given, project).items():
            errors.setdefault(name, []).extend(messages)
    return errors
