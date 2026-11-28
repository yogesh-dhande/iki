"""One write pipeline (ADR 0280): every change to an entity's values or revisions is
checked and written here, so a save's rules hold whichever operation makes it (ADR 0035,
ADR 0127).

An operation (creating a record, editing one, setting its state, archiving, moving it
to its entry's replacement, a registrar's curation, a lab list's options) authorizes the
change, locks what it changes and plans it: a :class:`Plan`, the entity, what it writes,
why and by whom. Then:

- :func:`evaluate` checks the plan as every save is checked, before anything is written:
  the results of the checks reading what it changes are cleared (ADR 0080), its computed
  values are worked out from what it will hold (ADR 0297), and the core rules and the
  plugins' rules reading what it changes are checked (ADR 0127). One that refuses refuses
  it; one that holds asks for a reason, and the record is held for it.
- :func:`commit` writes it: its values and each table's history row, its revision (told
  to webhooks and triggers), the files it names, its assignments, the rules it keeps and
  the holds it's saved under, the notifications its type declares, and the rechecks it
  fires. A change of no value (archiving, superseding, a move) is a revision all the same.

:func:`commit_alike` writes the same values to many records at once, a few queries for
them all: what a registry entry's change clears on the records holding it.

Nothing else writes values, revisions or history: an architecture test
(``tests/test_write_pipeline.py``) holds every other module to it, but a bundle's restore
and a bulk import's set-based inserts, each of which says why.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from django.db.models import F, Model
from django.db.models.functions import Now

from iki_server.audit import log
from iki_server.audit.log import agent_data, current_agent, current_oauth_client, current_via
from iki_server.entities import assignments, checking, computing, holds, requests, rules
from iki_server.entities.models import Entity, ProjectLink, Revision, RevisionKind
from iki_server.entities.values import (
    FOREIGN_KEYS,
    Invalid,
    Part,
    Shape,
    row_columns,
    shape,
    to_history,
)
from iki_server.files.models import FileUse
from iki_server.notifications import declared
from iki_server.org.services import Refused
from iki_spec.model import Field, names_people, stored_in_list_table

if TYPE_CHECKING:
    from iki_server.accounts.models import User
    from iki_server.entities.services import Rows
    from iki_server.org.models import Project
    from iki_spec.model import Trigger

__all__ = [
    "RECHECK_NOTE",
    "Checked",
    "Committed",
    "Plan",
    "commit",
    "commit_alike",
    "evaluate",
    "is_quiet",
    "quietly",
    "set_value",
    "use_files",
    "write_list",
]

#: A recheck's note on a record once what its check read changed (ADR 0080).
RECHECK_NOTE = "Needs re-verification"


@dataclass
class Plan:
    """A change to one entity, as its operation planned it once it locked it."""

    user: User | None
    form: Shape
    kind: str
    #: The record or registry entry, as its type's model; for a new one, its unsaved
    #: instance, with its own table's values set. None while a new record's values are
    #: checked, before it's made.
    entity: Any = None
    #: Where its values live now (:class:`services.Rows`).
    rows: Rows | None = None
    #: What it writes, as the database stores it.
    values: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    #: How it's made, when it isn't the request's (``run``, ``system``).
    via: str | None = None
    #: The record's project, whose rules check it.
    project: Project | None = None
    #: Values it changes that are written apart from it, as the save reads them: the
    #: registry facts it fills in (ADR 0115), the states its edit returns (ADR 0125).
    also: dict[str, Any] = field(default_factory=dict)
    #: Fields that change though it doesn't write them, so the checks reading them are
    #: cleared: the facts it fills in, its entry's values a move to another gave it.
    edited: frozenset[str] = frozenset()
    #: Why it's saved though it breaks rules that hold records for review (ADR 0127).
    hold_reason: str = ""
    #: The label of the state it sets when that state locks the record (ADR 0128): it isn't
    #: set while a rule would hold it.
    locking: str = ""
    #: Whether the results of the checks reading what it changes are cleared (ADR 0080).
    rechecks: bool = True
    #: The fields its revision says it changed, when they aren't what it writes.
    changed_fields: Sequence[str] | None = None
    #: For a registry entry's revision, the project it came through (ADR 0115).
    through: Project | None = None
    #: For a record's first revision, its new entry's, which registering it made.
    registered: Revision | None = None
    #: Its identity's values as given, where they're spelled other than its entry's.
    submitted: dict[str, Any] | None = None
    #: The entry it moved from (ADR 0115), or that a registrar registered it in place of.
    moved_from: Any = None
    #: For a state change, (field, from, to), and the signatures that set it (ADR 0126).
    state: tuple[str, str, str] | None = None
    signatures: tuple[Any, ...] = ()
    #: A bulk change it's part of, which its assignments' audit records name (ADR 0122).
    group: str = ""
    #: The registry entry revision its holds name (ADR 0127).
    entry_revision: int | None = None
    #: A request's sides it writes as (ADR 0132), or None for a type with none.
    sides: frozenset[str] | None = None

    @property
    def type_name(self) -> str:
        return self.form.entity_type.qualified_name

    @property
    def new(self) -> bool:
        return self.kind == RevisionKind.CREATED

    def current(self, name: str) -> Any:  # noqa: ANN401 - a model value
        """A field's value as the change leaves it."""
        if name in self.values:
            return self.values[name]
        if name in self.also:
            return self.also[name]
        return None if self.new else self.stored.get(name)

    @property
    def stored(self) -> Rows:
        assert self.rows is not None  # noqa: S101 - planned with its rows
        return self.rows


@dataclass
class Checked:
    """What :func:`evaluate` found of a plan: the rules it breaks, and those it's held by."""

    #: The rules it breaks that hold it, saved for its hold reason.
    holding: list[checking.Broken] = field(default_factory=list)
    found: list[checking.Broken] = field(default_factory=list)
    #: The checks whose results it clears, each with the fields its action sets.
    rechecks: list[tuple[Trigger, tuple[str, ...]]] = field(default_factory=list)
    #: What its rules read as changed, or None for a new record's, which every rule reads.
    changed: frozenset[str] | None = frozenset()


@dataclass
class Committed:
    revision: Revision | None
    #: A recheck that couldn't start notes why in a revision after the plan's.
    noted: Revision | None = None


# --- Evaluating -------------------------------------------------------------------------------


def evaluate(plan: Plan, *, core: bool = True) -> Checked:
    """Checks ``plan`` before anything of it is written: clears the results of the checks
    reading what it changes, works its computed values out and checks its rules. Raises
    :class:`Invalid` for a rule it breaks that refuses, :class:`checking.Held` for one
    that holds when it has no hold reason, or :class:`Refused` for a locking state a rule
    would hold. ``core``: whether the core types' rules are checked too, which a bulk
    import checks a chunk at a time (ADR 0056)."""
    if plan.new:
        return _evaluate_new(plan, core=core)
    from iki_server.runs import triggers

    type_name = plan.type_name
    rechecks = []
    edited = set(plan.values) | plan.edited
    if plan.rechecks and edited:
        rechecks = triggers.rechecks(type_name, edited)
    for spec, outputs in rechecks:
        for name in outputs:
            value = RECHECK_NOTE if name == spec.recheck else None
            if plan.stored.get(name) != value or name in plan.values:
                plan.values[name] = value
    unworked: dict[str, list[str]] = {}
    if plan.values:
        # Its computed values, from what it will hold, before its rules; one its field
        # refuses is said with what its rules say.
        try:
            plan.values.update(computing.changes(type_name, plan.current, plan.stored.get))
        except Invalid as error:
            unworked = dict(error.errors)
    changing = frozenset(plan.values) | frozenset(plan.also)
    if not changing:
        return Checked(rechecks=rechecks)
    if core and plan.project is not None and plan.user is not None:
        errors = rules.check(
            plan.user, plan.form.entity_type, plan.project, plan.current, changed=changing
        )
        if errors:
            raise Invalid(errors)
    entity = plan.entity
    found = checking.broken(
        type_name, [checking.Saving(plan.current, changing, id=entity.pk, hrid=entity.hrid)]
    )[0]
    if plan.project is not None:
        found += checking.apart(
            plan.project.pk,
            plan.form,
            {n: plan.current(n) for n in plan.form.fields() if not plan.form.part_of(n).entry},
            entity=entity.pk,
            changed=changing,
        )
    holding = _held(plan, found, unworked)
    return Checked(holding=holding, found=found, rechecks=rechecks, changed=changing)


def _evaluate_new(plan: Plan, *, core: bool) -> Checked:
    """A new record's plan, every rule of its type read."""
    unworked: dict[str, list[str]] = {}
    try:
        computing.fill(plan.type_name, [plan.values])
    except Invalid as error:
        unworked = dict(error.errors)
    if core and plan.project is not None and plan.user is not None:
        errors = rules.check(plan.user, plan.form.entity_type, plan.project, plan.values.get)
        if errors:
            raise Invalid(errors)
    found = checking.broken(plan.type_name, [checking.Saving(plan.values.get)])[0]
    if plan.project is not None:
        own = {n: v for n, v in plan.values.items() if not plan.form.part_of(n).entry}
        found += checking.apart(plan.project.pk, plan.form, own)
    return Checked(holding=_held(plan, found, unworked), found=found, changed=None)


def _held(
    plan: Plan, found: list[checking.Broken], unworked: Mapping[str, list[str]]
) -> list[checking.Broken]:
    if plan.locking and any(b.held for b in found):
        held = next(b for b in found if b.held)
        raise Refused(
            f"It would break “{held.rule.label}”, which holds records for review, so it isn't "
            f"set to {plan.locking}: {held.message}"
        )
    return checking.raise_broken(found, hold_reason=plan.hold_reason, errors=unworked)


# --- Committing -------------------------------------------------------------------------------


def commit(plan: Plan, checked: Checked | None) -> Committed:
    """Writes ``plan``, as :func:`evaluate` checked it (``checked``), or None for a plan
    an enclosing change's evaluation checks with its own: a state its edit returns, or a
    registry entry's values, which its records' rules are checked for (ADR 0115). An edit
    with nothing of its own to write makes no revision, but keeps its rules and holds."""
    entity = plan.entity
    record = getattr(entity, "registry_id", None) is None
    fields = plan.form.fields()
    revision: Revision | None = None
    was = {} if plan.new or not plan.values else {n: plan.stored.get(n) for n in plan.values}
    if plan.values or plan.kind != RevisionKind.EDITED:
        touched: list[Part] = []
        writing = requests.writing(plan.sides) if plan.sides is not None else nullcontext()
        with writing:
            if plan.new:
                touched = _insert(plan)
            elif plan.values:
                touched = _write(plan.form, plan.stored, plan.values, entity.pk)
        changed = plan.changed_fields
        if changed is None:
            written = [n for n, v in plan.values.items() if not plan.new or v is not None]
            changed = sorted(written)
        revision = _revise(plan, list(changed))
        if touched:
            _write_history(touched, plan.stored, revision.number)
        use_files(fields, entity.pk, plan.values, revision.number)
        if plan.new and record:
            # A new record is in the project it's made in, before anyone's told of it.
            assert plan.project is not None  # noqa: S101 - a record is made in one
            ProjectLink.objects.create(
                entity_id=entity.pk, project=plan.project, linked_by=plan.user
            )
        if record:
            _tell(plan, revision, was)
    if checked is not None and record:
        _keep(plan, checked, revision)
    noted = _recheck(plan, checked, revision) if checked is not None and revision else None
    return Committed(revision, noted)


def _tell(plan: Plan, revision: Revision, was: Mapping[str, Any]) -> None:
    """Its assignments (ADR 0122) and the notifications its type declares, for a record."""
    entity = plan.entity
    if plan.new:
        own = {n: v for n, v in plan.values.items() if not plan.form.part_of(n).entry}
        assignments.changed(entity, revision, assignments.diff(plan.type_name, {}, own), plan.user)
        declared.created(entity, revision, plan.user)
    elif plan.state is not None:
        name, old, new = plan.state
        assignments.became(entity, revision, name, old, new, plan.user)
        declared.changed(entity, revision, {name: (old or None, new)}, plan.user)
    elif plan.kind == RevisionKind.EDITED:
        fields = plan.form.fields()
        before = {n: v for n, v in was.items() if names_people(fields[n])}
        assignments.changed(
            entity,
            revision,
            assignments.diff(plan.type_name, before, plan.values),
            plan.user,
            group=plan.group,
        )
        declared.changed(
            entity, revision, {n: (was.get(n), v) for n, v in plan.values.items()}, plan.user
        )


def _keep(plan: Plan, checked: Checked, revision: Revision | None) -> None:
    """The rules it was reported for that it keeps now are cleared, and it's held by those
    that hold it (ADR 0127)."""
    if checked.changed is not None:
        checking.kept(plan.entity, checked.changed, checked.found)
    if checked.holding:
        number = revision.number if revision is not None else plan.entity.revision
        holds.hold(
            Entity.objects.select_related("owner_project").get(pk=plan.entity.pk),
            checked.holding,
            user=plan.user,
            reason=plan.hold_reason,
            revision=number,
            entry_revision=plan.entry_revision,
        )


def _recheck(plan: Plan, checked: Checked, revision: Revision) -> Revision | None:
    """Runs each check whose results it cleared again, as whoever made it (ADR 0080). One
    that can't start notes why on the record, in a revision after."""
    from iki_server.entities import services
    from iki_server.runs import triggers

    noted = None
    for spec, _ in checked.rechecks:
        assert plan.user is not None  # noqa: S101 - an edit of a record is someone's
        problem = triggers.recheck_edit(plan.user, plan.entity, revision, spec)
        if problem:
            with log.using(log.Context(via="trigger")):
                saved = services.update(
                    plan.user,
                    plan.entity,
                    {spec.recheck: f"{RECHECK_NOTE}. {problem}"[:10000]},
                    _recheck_note=spec.recheck,
                    expected_revision=(noted or revision).number,
                    reason=revision.reason,
                )
            noted = saved.revision or noted
    return noted


def commit_alike(
    records: Sequence[Entity],
    values: Mapping[str, Any],
    *,
    user: User | None,
    reason: str,
    changed: Mapping[Any, Sequence[str]],
) -> list[Revision]:
    """Writes the same ``values`` to each of ``records``, locked, of one type, as an
    edit, a few queries for them all: each its revision, by ``user`` for ``reason``,
    saying it ``changed`` what it did of them, and each table's history row. A list's
    only cleared. Like a bulk import's (ADR 0056), they tell no webhook and fire no
    trigger one by one, but a calculation reading through a reference to one is flagged.
    ``user`` None: by no one, through the system."""
    from iki_server.entities import services
    from iki_server.runs import triggers

    if not records:
        return []
    form = shape(records[0].type)
    ids = [r.pk for r in records]
    touched = _write_each(form, ids, values)
    revisions = Revision.objects.bulk_create(
        [
            Revision(
                entity_id=r.pk,
                number=r.revision + 1,
                kind=RevisionKind.EDITED,
                changed_fields=sorted(changed[r.pk]),
                user=user,
                via=current_via() if user is not None else "system",
                client=current_oauth_client() if user is not None else "",
                agent=current_agent() if user is not None else None,
                reason=reason.strip(),
            )
            for r in records
        ]
    )
    Entity.objects.filter(pk__in=ids).update(revision=F("revision") + 1, updated_at=Now())
    for record in records:
        record.revision += 1
    now = services.current_values_of(list(records))
    for part in touched:
        part.history._default_manager.bulk_create(
            [
                part.history(
                    entity_id=r.pk,
                    revision=r.revision,
                    **{
                        n: None if f.removed else to_history(f, now[r.pk].get(n))
                        for n, f in part.fields.items()
                    },
                )
                for r in records
            ]
        )
    if not _quiet.get():
        for record, revision in zip(records, revisions, strict=True):
            triggers.source_changed(record, revision)
    return revisions


# --- Writing ----------------------------------------------------------------------------------


def _insert(plan: Plan) -> list[Part]:
    """Saves a new entity's instance, then its extensions' rows and its lists: those of its
    record's tables, or for a registry entry, its entry's. Returns the tables it wrote."""
    instance = plan.entity
    entry = getattr(instance, "registry_id", None) is not None
    instance.save()
    values = plan.values
    mine = [p for p in plan.form.parts if p.entry == entry]
    touched = [p for p in mine if not p.extension]
    for part in mine:
        if part.extension and any(values.get(n) is not None for n in part.fields):
            row = plan.stored.ensure(part)
            for name, f in part.fields.items():
                if name in values and not stored_in_list_table(f):
                    set_value(row, f, values[name])
            row.save()
            touched.append(part)
        for name, f in part.fields.items():
            if stored_in_list_table(f) and values.get(name):
                write_list(part, f, instance.pk, values[name])
    return touched


def _write(form: Shape, rows: Rows, new: Mapping[str, Any], owner_id: Any) -> list[Part]:  # noqa: ANN401 - a UUID
    """Writes ``new`` values to the tables of ``rows`` that hold them; returns those it did."""
    touched: list[Part] = []
    saves: list[Model] = []
    for part in form.parts:
        mine = {n: v for n, v in new.items() if n in part.fields}
        if not mine:
            continue
        touched.append(part)
        target = rows.ensure(part)
        owner = rows.owner(part)
        key = owner.pk if owner is not None else owner_id
        for name, value in mine.items():
            f = part.fields[name]
            if stored_in_list_table(f):
                part.list_model(name)._default_manager.filter(entity_id=key).delete()
                if value:
                    write_list(part, f, key, value)
            else:
                set_value(target, f, value)
        if all(target is not s for s in saves):
            saves.append(target)
    for target in saves:
        target.save()
    return touched


def _write_each(form: Shape, ids: Sequence[Any], new: Mapping[str, Any]) -> list[Part]:
    """Writes the same ``new`` values to each of the records ``ids``, as :func:`_write`
    does to one, a query or two a table: a list's only cleared. Returns the tables it
    wrote."""
    touched: list[Part] = []
    for part in form.parts:
        mine = {n: v for n, v in new.items() if n in part.fields}
        if not mine or part.entry:
            continue
        touched.append(part)
        columns: dict[str, Any] = {}
        for name, value in mine.items():
            f = part.fields[name]
            if stored_in_list_table(f):
                assert not value  # noqa: S101 - only cleared
                part.list_model(name)._default_manager.filter(entity_id__in=ids).delete()
            else:
                columns[f"{name}_id" if f.type in FOREIGN_KEYS else name] = value
        if not columns:
            continue
        manager = part.model._default_manager
        if not part.extension:
            manager.filter(pk__in=ids).update(**columns)
            continue
        have = set(manager.filter(entity_id__in=ids).values_list("entity_id", flat=True))
        manager.filter(entity_id__in=have).update(**columns)
        manager.bulk_create([part.model(entity_id=i, **columns) for i in ids if i not in have])
    return touched


def set_value(instance: Model, f: Field, value: Any) -> None:  # noqa: ANN401 - a model value
    """Sets a field of an unsaved row, as the database stores it."""
    if f.type in FOREIGN_KEYS:
        setattr(instance, f"{f.name}_id", value)
    else:
        setattr(instance, f.name, value)


def write_list(part: Part, f: Field, entity_id: Any, items: list[Any]) -> None:  # noqa: ANN401
    """Writes a list's items, or its rows (ADR 0137), to its table: a new entity's, or one
    whose list was cleared."""
    model = part.list_model(f.name)
    if f.is_rows:
        columns = row_columns(f)
        model._default_manager.bulk_create(
            model(entity_id=entity_id, position=index, **{c: row[n] for n, c in columns.items()})
            for index, row in enumerate(items)
        )
        return
    model._default_manager.bulk_create(
        model(entity_id=entity_id, position=index, item_id=item) for index, item in enumerate(items)
    )


def _write_history(touched: list[Part], rows: Rows, number: int) -> None:
    """A history row for each table the revision touched, with all of that table's values."""
    for part in touched:
        owner = rows.owner(part)
        assert owner is not None  # noqa: S101
        data = {
            name: None if f.removed else to_history(f, rows.value(part, f))
            for name, f in part.fields.items()
        }
        part.history._default_manager.create(entity_id=owner.pk, revision=number, **data)


def use_files(
    fields: Mapping[str, Field],
    entity_id: uuid.UUID,
    values: Mapping[str, Any],
    revision: int,
) -> None:
    """Records the files these values refer to, so the entity's readers can read them: a
    revision's, or a bundle's restored record's."""
    found: set[Any] = set()
    for name, value in values.items():
        f = fields.get(name)
        if f is None or value is None:
            continue
        if f.type == "file":
            found.add(value)
        elif f.type == "list" and f.items is not None and f.items.type == "file":
            found.update(value)
        elif f.is_rows:
            assert f.items is not None  # noqa: S101 - lists have items
            files = [n for n, cell in f.items.fields.items() if cell.type == "file"]
            found.update(row[n] for row in value for n in files if row.get(n) is not None)
    if found:
        FileUse.objects.bulk_create(
            [FileUse(file_id=i, entity_id=entity_id, revision=revision) for i in sorted(found)],
            ignore_conflicts=True,
        )


def _revise(plan: Plan, changed: list[str]) -> Revision:
    """The entity's next revision."""
    from iki_server.entities import states

    entity = plan.entity
    current = Entity.objects.only("revision", "has_been_locked").get(pk=entity.pk)
    if plan.kind == RevisionKind.EDITED and not plan.reason.strip() and current.has_been_locked:
        raise states.CorrectionReason(
            {"reason": ["Give a reason for editing this record after its lock was lifted."]}
        )
    number = current.revision + 1
    revision = Revision.objects.create(
        entity_id=entity.pk,
        number=number,
        kind=plan.kind,
        changed_fields=changed,
        user=plan.user,
        via=plan.via or current_via(),
        client=current_oauth_client(),
        agent=current_agent(),
        reason=plan.reason.strip(),
        project=plan.through,
        moved_from_id=plan.moved_from,
        signatures=list(plan.signatures),
        submitted=plan.submitted,
        **(
            dict(zip(("state_field", "state_from", "state_to"), plan.state, strict=True))
            if plan.state
            else {}
        ),
    )
    Entity.objects.filter(pk=entity.pk).update(revision=number, updated_at=Now())
    if getattr(entity, "registry_id", None) is not None:
        _announce_entry(entity.pk, revision, plan.user)
    else:
        _announce(entity.pk, revision, plan.user, registered=plan.registered)
    return revision


# --- Telling ----------------------------------------------------------------------------------

_EVENTS = {
    RevisionKind.CREATED: "entity.created",
    RevisionKind.EDITED: "entity.updated",
    RevisionKind.ARCHIVED: "entity.archived",
    RevisionKind.RESTORED: "entity.restored",
    RevisionKind.MOVED: "entity.updated",
    RevisionKind.STATE_CHANGED: "entity.state_changed",
}

#: Inside :func:`quietly`: changes too many to tell of one by one.
_quiet: ContextVar[bool] = ContextVar("iki_entities_quiet", default=False)


def is_quiet() -> bool:
    """Whether the change being made is inside :func:`quietly`."""
    return _quiet.get()


@contextmanager
def quietly() -> Iterator[None]:
    """Changes made inside tell no webhook and fire no trigger, one by one: a bulk import
    (ADR 0056) tells of them all in one event, and fires nothing per entity."""
    token = _quiet.set(True)
    try:
        yield
    finally:
        _quiet.reset(token)


def _announce(
    entity_id: Any,  # noqa: ANN401 - a UUID
    revision: Revision,
    user: User | None,
    *,
    registered: Revision | None = None,
) -> None:
    """Tells the owner project's and the organization's webhooks, and fires its triggers,
    in the change's transaction."""
    if _quiet.get():
        return
    from iki_server.entities import services
    from iki_server.runs import triggers
    from iki_server.webhooks import events
    from iki_server.webhooks.services import emit

    entity = Entity.objects.select_related("owner_project__team").get(pk=entity_id)
    state = (
        {"state": services.state_change(entity.type, revision)}
        if revision.kind == RevisionKind.STATE_CHANGED
        else {}
    )
    data = {
        "entity": events.entity_data(entity),
        "revision": {
            "number": revision.number,
            "kind": revision.kind,
            "changed_fields": revision.changed_fields,
            "via": revision.via,
            "reason": revision.reason,
        },
        **state,
        "actor": events.actor_data(user),
        "agent": agent_data(revision.agent),
    }
    emit(_EVENTS[RevisionKind(revision.kind)], data, project=entity.owner_project)
    # And the project's triggers that it fires (ADR 0080).
    triggers.noticed(entity, revision, registered=registered)
    triggers.source_changed(entity, revision)


def _announce_entry(entry_id: Any, revision: Revision, user: User | None) -> None:  # noqa: ANN401 - a UUID
    """Tells the webhooks of the projects holding an entry that its values changed (ADR
    0115): which entry, which fields, and for a registrar's change, who. A holder's change
    is told without who made it, as the other holders don't learn that. Its registration
    is told as its record's creation, to the project that registered it."""
    if _quiet.get() or revision.kind == RevisionKind.CREATED:
        return
    from iki_server.entities import entries
    from iki_server.runs import triggers

    entries.announce(entry_id, revision, user)
    triggers.source_changed(Entity.objects.get(pk=entry_id), revision)
