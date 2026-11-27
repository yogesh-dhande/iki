"""A plugin as Iki understands it, once its manifests have been read and checked.

Codegen (YAML → Django app) and the build read these objects, never the raw
YAML, so every rule `iki plugin validate` enforces holds for them.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from typing import Any

from iki_spec import naming
from iki_spec.formula import LOSS_PATTERN

__all__ = [
    "APPROVAL_ROLES",
    "BUILTIN_TYPES",
    "CONDITION_OPERATORS",
    "FIELD_TYPES",
    "FORMATS",
    "LIST_FIELDS",
    "NUMERIC_OPERATORS",
    "SIGNATURE_MEANINGS",
    "TIERS",
    "Action",
    "Approval",
    "Condition",
    "Creates",
    "EntityType",
    "Environment",
    "Exclusion",
    "Extension",
    "Field",
    "FileFormat",
    "FileSignature",
    "Guard",
    "Input",
    "LabRole",
    "ListSpec",
    "Maker",
    "Meaning",
    "Metric",
    "Plugin",
    "Related",
    "Resources",
    "ReviewFlow",
    "ReviewNeeds",
    "ReviewProcedure",
    "ReviewSpec",
    "ReviewState",
    "Rule",
    "Signature",
    "Slot",
    "State",
    "StateMove",
    "TypeGuard",
    "TypeNotification",
    "Updates",
    "Volume",
    "Workflow",
    "WorkflowStep",
    "action_from_dict",
    "names_people",
    "person_role",
    "plugin_from_dict",
    "plugin_to_dict",
    "unique_rules",
    "workflow_from_dict",
]

FIELD_TYPES = (
    "string",
    "text",
    "int",
    "float",
    "bool",
    "enum",
    "date",
    "datetime",
    "quantity",
    "file",
    "ref",
    "person",
    "list",
    "json",
)

#: Built-in string formats, as regular expressions both Python and PostgreSQL
#: understand (the database checks them too). Sequences accept IUPAC codes in
#: either case, since lower case often marks masked regions.
FORMATS: dict[str, str] = {
    "dna_sequence": r"^[ACGTRYSWKMBDHVNacgtryswkmbdhvn]*$",
    "rna_sequence": r"^[ACGURYSWKMBDHVNacguryswkmbdhvn]*$",
    "protein_sequence": r"^[ACDEFGHIKLMNPQRSTVWYBZXUOJacdefghiklmnpqrstvwybzxuoj*]*$",
    # HELM 2 (ADR 0144): the database checks its shape, its four sections and version;
    # iki-wasm reads the rest, as `value_problems` does.
    "helm": r"^(PEPTIDE|RNA|CHEM|BLOB)[0-9]+[{].*[$].*[$].*[$].*[$](V2[.]0)?$",
    "atom_loss_formula": LOSS_PATTERN,
    "email": r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    "uri": r"^[A-Za-z][A-Za-z0-9+.-]*:[^\s]+$",
    # A DNS name in lower case, such as lims.example.com: what an action's `network`
    # can name through a setting.
    "hostname": r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$",
}

_FORMAT_RE = {name: re.compile(pattern) for name, pattern in FORMATS.items()}


def matches_format(value: str, name: str) -> bool:
    return _FORMAT_RE[name].fullmatch(value) is not None


@dataclass(frozen=True)
class Field:
    """A field of an entity type or extension, a list's item, or a plugin setting."""

    name: str
    type: str
    label: str = ""
    description: str | None = None
    required: bool = False
    immutable: bool = False
    #: Set by Iki, not by a person: by an action's runs (``computed: true``), or worked out
    #: at every save from the record's own values by the CEL expression ``cel``
    #: (``computed: {cel: …}``, ADR 0297).
    computed: bool = False
    #: A computed field's CEL expression, which the server evaluates at every save (ADR
    #: 0297); None for a field that's typed or that a run sets.
    cel: str | None = None
    #: The record's fields ``cel`` reads, as the validator's type check found them, which
    #: the server works computed fields out in the order of.
    cel_reads: tuple[str, ...] = ()
    #: For an editable boolean, clearing a true value requires a reason in the edit.
    reason_on_clear: bool = False
    #: For a type with an identity: ``derived`` or ``fact``, a field of its registry entry
    #: rather than of each project's record (ADR 0115). None for a record's own field.
    registry: str | None = None
    indexed: bool = False
    #: For text: a search finds its words (``matches``), from a full-text index of its first
    #: ``conditions.WORDS_SEARCHED`` characters (ADR 0233). False for machine text, such as
    #: a Molfile, which gets no full-text index.
    searchable: bool = True
    #: A key, such as another system's ID for the record (ADR 0082): it's found by its value,
    #: a run's create can match on it, and it may link out. Its uniqueness is a one-field
    #: rule (:func:`unique_rules`): no two records in a project share a value, or for a
    #: registry's field, no two entries in a registry (ADR 0082).
    unique: bool = False
    #: Where the value leads in another system: a URL with `{value}` (ADR 0082).
    link: str | None = None
    #: Gone from the type, but kept in its history (removed fields are never deleted).
    removed: bool = False
    has_default: bool = False
    default: Any = None
    format: str | None = None
    #: A sequence's: the enum fields saying whether it's circular and double-stranded,
    #: which its identity reads it by (ADR 0138).
    topology: str | None = None
    strands: str | None = None
    pattern: str | None = None
    #: What the pattern asks for, in words, finishing "Use …": said instead of it.
    pattern_help: str | None = None
    min_length: int | None = None
    max_length: int | None = None
    min: float | None = None
    max: float | None = None
    values: tuple[str, ...] = ()
    unit: str | None = None
    display_unit: str | None = None
    media_types: tuple[str, ...] = ()
    max_size: int | None = None  # bytes
    #: A reference's targets, qualified: it refers to an entity of one of these types, or
    #: of one of their subtypes. One for most references; ``iki/core.Entity`` for any.
    to: tuple[str, ...] = ()
    #: For a field naming people: the lab role they must hold in the record's project, by
    #: its qualified name (ADR 0116). None names anyone who can read the project.
    role: str | None = None
    #: For a field naming people (or a list of them): whoever it names is assigned the
    #: record (ADR 0122), while its state field ``assignment_field`` (ADR 0125) is in one
    #: of ``assignment_states``; in any state, without one.
    assignment: bool = False
    assignment_field: str | None = None
    assignment_states: tuple[str, ...] = ()
    items: Field | None = None
    min_items: int | None = None
    max_items: int | None = None
    schema: dict[str, Any] | None = None
    #: A state field's (``type: state``, ADR 0125): the states it can hold, by name, and
    #: the one new records start in.
    states: dict[str, State] = field(default_factory=dict)
    initial: str | None = None
    #: A reviewed type's review field (ADR 0301): its records' states, guards and roles are
    #: their pinned procedure's flow's. ``states`` holds only the ones the type names.
    procedure: bool = False
    #: A removed state field's: where its records' states go (ADR 0125).
    moved_to: StateMove | None = None
    #: A list's item that's a row (``type: row``, ADR 0137): its fields, in order.
    fields: dict[str, Field] = field(default_factory=dict)
    #: A row's fields that tell two rows apart in its type's identity (ADR 0138); all of
    #: them when empty.
    identity: tuple[str, ...] = ()
    #: A row's string field holding its stable ID, which the server gives a new row and
    #: keeps through edits; None for rows without one.
    row_id: str | None = None
    #: A row's enum field whose value is its role, and what each role takes.
    by: str | None = None
    roles: dict[str, RowRole] = field(default_factory=dict)
    #: A row whose roles are every role it may have: a subtype's (ADR 0164), or those of a
    #: row whose role is a string. A row of an enum's other roles takes any number.
    closed: bool = False

    @property
    def worked_out(self) -> bool:
        """Whether Iki works it out from the record's own values, at every save (ADR 0297):
        no person, agent or run sets it."""
        return self.cel is not None

    @property
    def set_by_runs(self) -> bool:
        """Whether it's computed by an action's runs or by Iki's own code
        (``computed: true``), not by an expression."""
        return self.computed and self.cel is None

    @property
    def is_rows(self) -> bool:
        """Whether it's a list of rows (ADR 0137)."""
        return self.type == "list" and self.items is not None and self.items.type == "row"

    @property
    def live_states(self) -> tuple[str, ...]:
        """A state field's states a record can be in: not the removed ones."""
        return tuple(n for n, s in self.states.items() if not s.removed)

    @property
    def locking_states(self) -> frozenset[str]:
        """A state field's states that lock a record (ADR 0125)."""
        return frozenset(n for n, s in self.states.items() if s.locks and not s.removed)

    @classmethod
    def from_manifest(cls, name: str, data: dict[str, Any], *, plugin: str) -> Field:
        items = data.get("items")
        to = data.get("to")
        role = data.get("role")
        assignment = data.get("assignment", False)
        moved = data.get("moved_to") if data.get("type") == "state" else None
        computed = data.get("computed")
        cel = computed.get("cel") if isinstance(computed, dict) else None
        return cls(
            name=name,
            type=data["type"],
            label=data.get("label") or name.replace("_", " ").capitalize(),
            description=data.get("description"),
            required=bool(data.get("required", False)),
            immutable=bool(data.get("immutable", False)),
            computed=bool(data.get("computed", False)),
            cel=cel if isinstance(cel, str) else None,
            reason_on_clear=bool(data.get("reason_on_clear", False)),
            registry=data.get("registry"),
            indexed=bool(data.get("indexed", False)),
            searchable=bool(data.get("searchable", True)),
            unique=bool(data.get("unique", False)),
            link=data.get("link"),
            removed=bool(data.get("removed", False)),
            has_default="default" in data,
            default=data.get("default"),
            format=data.get("format"),
            topology=data.get("topology"),
            strands=data.get("strands"),
            pattern=data.get("pattern"),
            pattern_help=data.get("pattern_help"),
            min_length=data.get("min_length"),
            max_length=data.get("max_length"),
            min=data.get("min"),
            max=data.get("max"),
            values=tuple(data.get("values", ())),
            unit=data.get("unit"),
            display_unit=data.get("display_unit"),
            media_types=tuple(data.get("media_types", ())),
            max_size=parse_size(data["max_size"]) if "max_size" in data else None,
            to=(qualify(to, plugin),)
            if isinstance(to, str)
            else tuple(qualify(t, plugin) for t in to if isinstance(t, str))
            if isinstance(to, list)
            else (),
            role=(role if "." in role else f"{plugin}.{role}") if isinstance(role, str) else None,
            assignment=bool(assignment),
            assignment_field=assignment.get("field") if isinstance(assignment, dict) else None,
            assignment_states=tuple(assignment.get("states") or ())
            if isinstance(assignment, dict)
            else (),
            items=cls.from_manifest(name, items, plugin=plugin)
            if isinstance(items, dict)
            else None,
            min_items=data.get("min_items"),
            max_items=data.get("max_items"),
            schema=data.get("schema"),
            states={
                n: State.from_manifest(n, st, plugin=plugin)
                for n, st in (data.get("states") or {}).items()
                if isinstance(st, dict)
            },
            initial=data.get("initial"),
            moved_to=StateMove(
                field=moved["field"], states=tuple(sorted((moved.get("states") or {}).items()))
            )
            if isinstance(moved, dict) and isinstance(moved.get("field"), str)
            else None,
            fields={
                n: cls.from_manifest(n, cell, plugin=plugin)
                for n, cell in (data.get("fields") or {}).items()
                if isinstance(cell, dict)
            }
            if data.get("type") == "row"
            else {},
            identity=tuple(data.get("identity") or ())
            if data.get("type") == "row" and isinstance(data.get("identity"), list)
            else (),
            row_id=data.get("row_id") if data.get("type") == "row" else None,
            by=data.get("by") if data.get("type") == "row" else None,
            # A role that's a string has only the roles named (ADR 0164).
            closed=data.get("type") == "row"
            and bool(data.get("roles"))
            and isinstance((data.get("fields") or {}).get(data.get("by")), dict)
            and (data.get("fields") or {})[data["by"]].get("type") == "string",
            roles={
                n: RowRole.from_manifest(n, r, plugin=plugin)
                for n, r in (data.get("roles") or {}).items()
                if isinstance(r, dict)
            }
            if data.get("type") == "row"
            else {},
        )


def names_people(f: Field) -> bool:
    """Whether a field names people: ``type: person``, or a list of them (ADR 0122)."""
    return f.type == "person" or (
        f.type == "list" and f.items is not None and f.items.type == "person"
    )


def person_role(f: Field) -> str | None:
    """The lab role whoever a field names must hold, for a person field or a list of them."""
    if f.type == "list" and f.items is not None:
        return f.items.role
    return f.role


def in_entry(f: Field, identity: Collection[str] = ()) -> bool:
    """Whether a field is its registry entry's (ADR 0115): part of the identity, or marked
    ``registry: derived`` or ``registry: fact``. Every other field is each project's record's."""
    return f.name in identity or f.registry is not None


def split_entry(
    fields: Mapping[str, Field], identity: Collection[str] = ()
) -> tuple[dict[str, Field], dict[str, Field]]:
    """A type's own fields (or an extension's) as (its records', its entries')."""
    record = {n: f for n, f in fields.items() if not in_entry(f, identity)}
    entry = {n: f for n, f in fields.items() if in_entry(f, identity)}
    return record, entry


def refined(f: Field, roles: Mapping[str, RowRole]) -> Field:
    """A list of rows ``f`` as a subtype that refines it has it (ADR 0164): with its roles
    the subtype's, which are every role its rows may have."""
    assert f.items is not None  # noqa: S101 - only lists of rows are refined
    return replace(f, items=replace(f.items, roles=dict(roles), closed=True))


def reference_refinement(lineage: Sequence[EntityType], name: str) -> tuple[str, ...] | None:
    """The types a reference, or list of references, ``name`` refers to in the first of
    ``lineage`` (a type, then its ancestors, nearest first) as the nearest type below the one
    declaring it refines it (ADR 0235), or None where none does."""
    for t in lineage:
        if name in t.fields:
            return None
        if name in t.refines_to:
            return t.refines_to[name]
    return None


def as_refined(lineage: Sequence[EntityType], name: str, f: Field) -> Field:
    """Field ``name``, declared as ``f`` by one of ``lineage`` (a type, then its ancestors,
    nearest first), as the first of them has it: a list of rows with the roles it gives
    (ADR 0164), a reference with the types it refers to (ADR 0235)."""
    if (roles := refinement(lineage, name)) is not None:
        return refined(f, roles)
    if (targets := reference_refinement(lineage, name)) is not None:
        if f.type == "list" and f.items is not None:
            return replace(f, items=replace(f.items, to=targets))
        return replace(f, to=targets)
    return f


def refinement(lineage: Sequence[EntityType], name: str) -> dict[str, RowRole] | None:
    """The roles a list of rows ``name`` has in the first of ``lineage`` (a type, then its
    ancestors, nearest first) as the nearest type below the one declaring it refines it
    (ADR 0164), or None where none does."""
    for t in lineage:
        if name in t.fields:
            return None
        if name in t.refines:
            return t.refines[name]
    return None


def included_reference(fields: Mapping[str, Field], name: str) -> Field | None:
    """What an action's input includes by ``name`` (ADR 0105): one of its type's
    references, or a list of them, or a reference of a list's rows, ``components.part``
    (ADR 0161), as a list of references, one per row. None for a name it hasn't."""
    if "." not in name:
        return fields.get(name)
    rows, _, cell = name.partition(".")
    f = fields.get(rows)
    if f is None or not f.is_rows or f.items is None or cell not in f.items.fields:
        return None
    item = f.items.fields[cell]
    return Field(name=name, type="list", label=f"{f.label}: {item.label}", items=item)


def lab_role_name(role: str, plugin: str) -> str:
    """A lab role in full: a plugin's own `qa` is `namespace/plugin.qa` (ADR 0116)."""
    return role if "." in role else f"{plugin}.{role}"


@dataclass(frozen=True)
class State:
    """One of the states a state field can hold (ADR 0125)."""

    name: str
    label: str
    description: str | None = None
    #: Qualified lab roles (ADR 0116): someone with any one of them in a record's project
    #: may set the field to this state, from any other.
    set_by: tuple[str, ...] = ()
    #: A record with a field in it is locked: its other fields can't change.
    locks: bool = False
    #: "required" or "optional": whether whoever sets it says why; None asks nothing.
    reason: str | None = None
    #: No record is in it any more: its records moved to ``moved_to``.
    removed: bool = False
    moved_to: str | None = None
    #: The electronic signatures setting it needs (21 CFR Part 11, ADR 0126): it's set by
    #: the last of them, once each is given on the same revision. Empty: it's set directly.
    signatures: tuple[Signature, ...] = ()
    #: Conditions on setting the field to this state, by name (ADR 0128): each must hold,
    #: besides ``set_by``.
    guards: dict[str, Guard] = field(default_factory=dict)
    #: A record in it is held (ADR 0139): none of its amount is drawn, transferred or split
    #: off, as for a sample in quarantine.
    holds_amount: bool = False
    #: A correction returns this field to the named state and records the editor's reason.
    on_edit: str | None = None
    #: Its signer must be the work's maker (ADR 0301), compiled into its ``maker`` guard.
    maker: Maker | None = None
    #: Whom its signer must not be (ADR 0301), compiled into its ``independent`` guard.
    independent_of: tuple[Exclusion, ...] = ()

    @classmethod
    def from_manifest(cls, name: str, data: dict[str, Any], *, plugin: str) -> State:
        wanted = data.get("signatures")
        return cls(
            name=name,
            label=data.get("label") or name.replace("_", " ").capitalize(),
            description=data.get("description"),
            set_by=tuple(lab_role_name(r, plugin) for r in data.get("set_by") or ()),
            locks=bool(data.get("locks", False)),
            on_edit=data.get("on_edit"),
            holds_amount=bool(data.get("holds_amount", False)),
            reason=data.get("reason"),
            removed=bool(data.get("removed", False)),
            moved_to=data.get("moved_to"),
            signatures=tuple(
                Signature.from_manifest(x, plugin=plugin) for x in wanted if isinstance(x, dict)
            )
            if isinstance(wanted, list)
            else (),
            guards={
                n: Guard.from_manifest(n, g)
                for n, g in (data.get("guards") or {}).items()
                if isinstance(g, dict)
            },
            maker=Maker.read(data.get("maker")),
            independent_of=Exclusion.read_all(data.get("independent_of")),
        )


@dataclass(frozen=True)
class Meaning:
    """What a signature means (21 CFR 11.50(a)(3)): how it's named, the verb its
    manifestation reads with ("Approved by"), and what the signer attests to by default."""

    label: str
    verb: str
    statement: str


#: The meanings a signature can have, by name (ADR 0126).
SIGNATURE_MEANINGS: dict[str, Meaning] = {
    "authorship": Meaning(
        "Authorship", "Authored", "I made this record, and what it says is accurate and complete."
    ),
    "review": Meaning("Review", "Reviewed", "I reviewed this record and found it in order."),
    "approval": Meaning("Approval", "Approved", "I approve this record."),
    "verification": Meaning(
        "Verification", "Verified", "I checked what this record says against its source."
    ),
    "responsibility": Meaning(
        "Responsibility", "Signed off", "I take responsibility for this decision."
    ),
    "witness": Meaning("Witness", "Witnessed", "I witnessed what this record describes."),
}


@dataclass(frozen=True)
class Signature:
    """A signature setting a state needs (ADR 0126): what it means, and who may give it:
    someone with ``role``, or with one of the state's ``set_by`` roles if it has none."""

    meaning: str
    #: A qualified lab role (ADR 0116), or None for the state's own.
    role: str | None = None
    #: How it's named: "QA approval". The meaning's label if the plugin gives none.
    label: str = ""
    #: What the signer attests to, shown before they sign and kept in what they sign.
    statement: str = ""

    @classmethod
    def from_manifest(cls, data: dict[str, Any], *, plugin: str) -> Signature:
        meaning = str(data.get("meaning") or "")
        known = SIGNATURE_MEANINGS.get(meaning)
        role = data.get("role")
        return cls(
            meaning=meaning,
            role=lab_role_name(role, plugin) if isinstance(role, str) else None,
            label=data.get("label") or (known.label if known else meaning),
            statement=data.get("statement") or (known.statement if known else ""),
        )


@dataclass(frozen=True)
class Guard:
    """A CEL condition on setting a state (ADR 0128) or taking an amount (ADR 0139)."""

    name: str
    #: CEL that must be true for the operation to proceed.
    check: str
    #: What whoever tries is told when it isn't: text, with CEL between ``{{`` and ``}}``.
    message: str
    #: CEL: the guard applies only when it's true.
    when: str | None = None
    description: str | None = None
    #: The record's fields its expressions read, filled in by the validator.
    reads: tuple[str, ...] = ()

    @classmethod
    def from_manifest(cls, name: str, data: dict[str, Any]) -> Guard:
        return cls(
            name=name,
            check=data.get("check") or "",
            message=data.get("message") or "",
            when=data.get("when"),
            description=data.get("description"),
        )


@dataclass(frozen=True)
class Maker:
    """A signature that's the maker's own (ADR 0301): whoever created the record or
    edited it in the current window, or the person a field names."""

    #: The person field naming the maker, or None for the creator or a current editor.
    person: str | None = None

    @classmethod
    def read(cls, data: object) -> Maker | None:
        if data is True:
            return cls()
        if isinstance(data, dict) and isinstance(data.get("person"), str):
            return cls(person=data["person"])
        return None


#: What a signer can be independent of (ADR 0301).
EXCLUSIONS = ("creator", "editors", "signers", "people")


@dataclass(frozen=True)
class Exclusion:
    """Someone a signer must not be (ADR 0301): the record's creator, its editors in a
    window, the signers of some states, or the people some fields name."""

    #: One of :data:`EXCLUSIONS`.
    kind: str
    #: ``signers``: the states whose latest signer is excluded.
    states: tuple[str, ...] = ()
    #: ``editors``: only those who changed one of these fields; all editors when empty.
    fields: tuple[str, ...] = ()
    #: ``editors`` of a state field without a procedure: the states whose edits count.
    window: tuple[str, ...] = ()
    #: ``people``: person fields, lists of people, or ones a reference away (``a.b``).
    people: tuple[str, ...] = ()
    #: CEL: it applies only when this is true.
    when: str | None = None

    @classmethod
    def read_all(cls, data: object) -> tuple[Exclusion, ...]:
        if not isinstance(data, list):
            return ()
        out = []
        for item in data:
            if item in ("creator", "editors"):
                out.append(cls(kind=str(item)))
                continue
            if not isinstance(item, dict):
                continue
            when = item.get("when") if isinstance(item.get("when"), str) else None
            if item.get("creator") is True:
                out.append(cls(kind="creator", when=when))
            elif "editors" in item:
                editors = item["editors"]
                spec = editors if isinstance(editors, dict) else {}
                out.append(
                    cls(
                        kind="editors",
                        fields=tuple(spec.get("fields") or ()),
                        window=tuple(spec.get("in") or ()),
                        when=when,
                    )
                )
            elif "signers" in item:
                out.append(cls(kind="signers", states=tuple(item["signers"] or ()), when=when))
            elif "people" in item:
                out.append(cls(kind="people", people=tuple(item["people"] or ()), when=when))
        return tuple(out)


@dataclass(frozen=True)
class Slot:
    """A part someone plays in a review procedure, bound to a lab role at setup (ADR 0304)."""

    name: str
    label: str
    description: str | None = None


@dataclass(frozen=True)
class ReviewState:
    """One of a review procedure's states (ADR 0301)."""

    name: str
    label: str
    description: str | None = None
    #: The slots whose holders may set it.
    set_by: tuple[str, ...] = ()
    locks: bool = False
    reason: str | None = None
    on_edit: str | None = None
    #: The signature setting it needs, if any (ADR 0126).
    signature: Signature | None = None
    maker: Maker | None = None
    independent_of: tuple[Exclusion, ...] = ()


@dataclass(frozen=True)
class ReviewFlow:
    """A way through a review procedure a project can choose (ADR 0301)."""

    name: str
    label: str
    summary: str
    #: Its states in order: records start in the first, and the last is its final state.
    route: tuple[str, ...]
    #: The other moves it allows: into a state, from these states.
    moves: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def open(self) -> str:
        """The state its records start in, where the work is recorded and edited."""
        return self.route[0]

    @property
    def final(self) -> str:
        return self.route[-1]

    @property
    def states(self) -> tuple[str, ...]:
        """Its states: the route's, then those only its moves reach."""
        extra = [s for s in self.moves if s not in self.route]
        return (*self.route, *extra)

    def sources(self, state: str) -> tuple[str, ...]:
        """The states a record may move into ``state`` from."""
        out: list[str] = []
        if state in self.route:
            index = self.route.index(state)
            if index > 0:
                out.append(self.route[index - 1])
        for s in self.moves.get(state, ()):
            if s not in out:
                out.append(s)
        return tuple(out)


@dataclass(frozen=True)
class ReviewProcedure:
    """A review procedure, declared once by a plugin (ADR 0301), referred to as
    ``plugin.name@version``. A published version never changes."""

    plugin: str
    name: str
    version: int
    label: str
    description: str | None = None
    slots: dict[str, Slot] = field(default_factory=dict)
    states: dict[str, ReviewState] = field(default_factory=dict)
    flows: dict[str, ReviewFlow] = field(default_factory=dict)
    file: str | None = None

    @property
    def qualified_name(self) -> str:
        return f"{self.plugin}.{self.name}@{self.version}"


@dataclass(frozen=True)
class ReviewNeeds:
    """What a reviewed type needs of a procedure (ADR 0301)."""

    #: The state its actions write in: every flow starts in it.
    open: str = "recorded"
    #: The states its readers take as complete: every flow ends in one. Any when empty.
    final: tuple[str, ...] = ()
    #: Other states it names: the procedure has each.
    states: tuple[str, ...] = ()
    #: ``signed``: every signed state locks; ``final``: each final state locks.
    locks: str | None = None
    #: The slots its own fields and guards name.
    slots: tuple[str, ...] = ()

    @property
    def named(self) -> tuple[str, ...]:
        """Every state it names, the open one first."""
        out = [self.open]
        for s in (*self.final, *self.states):
            if s not in out:
                out.append(s)
        return tuple(out)


@dataclass(frozen=True)
class TypeGuard:
    """A reviewed type's own condition on some of its procedure's states (ADR 0301)."""

    guard: Guard
    #: State names, ``signed`` or ``final``.
    on: tuple[str, ...] = ()


#: The groups of states a type's review guard can be ``on``, besides state names.
GUARD_GROUPS = ("signed", "final")


@dataclass(frozen=True)
class ReviewSpec:
    """How a type's records are reviewed (ADRs 0301, 0302): the state field a pinned
    procedure drives, where the procedure is pinned from, and what it needs of one."""

    #: The state field the procedure drives; Iki adds it, and ``review_procedure`` and
    #: ``review_flow`` beside it. Empty for a type that only opens work (``opens``).
    field: str = "review"
    label: str = "Review"
    #: The reference whose record's procedure it follows; None: pinned at create.
    by: str | None = None
    #: It has no review of its own and pins a procedure for the records that follow it.
    opens: bool = False
    #: It re-issues a record already reviewed under the same flow.
    derived: bool = False
    needs: ReviewNeeds = dataclasses.field(default_factory=ReviewNeeds)
    #: People every independent state also excludes.
    independent_of: tuple[Exclusion, ...] = ()
    guards: dict[str, TypeGuard] = dataclasses.field(default_factory=dict)

    @classmethod
    def read(cls, data: object) -> ReviewSpec | None:
        if not isinstance(data, dict):
            return None
        needs: dict[str, Any] = data["needs"] if isinstance(data.get("needs"), dict) else {}
        pinned = data.get("pinned")
        opens = data.get("opens") is True
        return cls(
            field="" if opens else str(data.get("field") or "review"),
            label=str(data.get("label") or "Review"),
            by=pinned["by"]
            if isinstance(pinned, dict) and isinstance(pinned.get("by"), str)
            else None,
            opens=opens,
            derived=data.get("derived") is True,
            needs=ReviewNeeds(
                open=str(needs.get("open") or "recorded"),
                final=tuple(needs.get("final") or ()),
                states=tuple(needs.get("states") or ()),
                locks=needs.get("locks"),
                slots=tuple(needs.get("slots") or ()),
            ),
            independent_of=Exclusion.read_all(data.get("independent_of")),
            guards={
                n: TypeGuard(guard=Guard.from_manifest(n, g), on=tuple(g.get("on") or ()))
                for n, g in (data.get("guards") or {}).items()
                if isinstance(g, dict)
            },
        )


#: The fields Iki adds to a reviewed type beside its review state field (ADR 0302).
PINNED_FIELDS = ("review_procedure", "review_flow")


def _while_value(value: object) -> str:
    """A value a rule's ``while`` counts records by, as text: bools as JSON has them."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


@dataclass(frozen=True)
class Rule:
    """A rule every save of a type's records keeps (ADR 0127): a CEL check of the record,
    a combination of fields no two of a project's records share, or ranges none of them
    overlap. The database holds the last two."""

    name: str
    label: str
    #: The plugin that declares it: the type's own, or an extension's.
    plugin: str
    description: str | None = None
    #: What a person is told when a save breaks it: text, with CEL between ``{{`` and ``}}``.
    message: str = ""
    #: CEL that must be true of the record.
    check: str | None = None
    #: CEL: the check applies only when it's true.
    when: str | None = None
    #: Fields no two of a project's records have the same values of, together.
    unique: tuple[str, ...] = ()
    #: With ``unique``: where no two records share them, ``project`` (a project's records'
    #: own fields) or ``registry`` (registry entries' fields, ADR 0115).
    within: str = "project"
    #: A field's ``unique: true``, as the rule it is (ADR 0082): named for the field.
    from_field: bool = False
    #: The fields a range starts and ends at: no two of a project's records' ranges overlap.
    no_overlap: tuple[str, ...] = ()
    #: With ``no_overlap``: records' ranges overlap only if they have the same values of these.
    per: tuple[str, ...] = ()
    #: With ``unique`` or ``no_overlap``: only records whose field is in one of these states
    #: count, as (field, values).
    only_while: tuple[tuple[str, tuple[str, ...]], ...] = ()
    #: Qualified lab roles (ADR 0116). When given, a save that breaks the check is kept,
    #: with a reason, and the record is held until someone with one of them reviews it.
    hold: tuple[str, ...] = ()
    #: The fields a failure points at: those given, or those its check reads.
    fields: tuple[str, ...] = ()
    #: Every field of the record its expressions read, filled in by the validator.
    reads: tuple[str, ...] = ()
    #: Reference field and revision field pairs for this check's historical evidence.
    reference_revisions: tuple[tuple[str, str], ...] = ()
    recheck_before_signing: bool = False
    #: An extension's rule, rather than a type's own.
    extension: bool = False

    @property
    def kind(self) -> str:
        """``check``, ``unique`` or ``no_overlap``."""
        if self.unique:
            return "unique"
        if self.no_overlap:
            return "no_overlap"
        return "check"

    @property
    def key(self) -> str:
        """Its name among the rules of the types it applies to: an extension's has its
        plugin before it, as another plugin may use the same name."""
        return f"{self.plugin}:{self.name}" if self.extension else self.name

    @classmethod
    def from_manifest(
        cls, name: str, data: dict[str, Any], *, plugin: str, extension: bool = False
    ) -> Rule:
        overlap = data.get("no_overlap")
        hold = data.get("hold")
        only = data.get("while")
        return cls(
            name=name,
            label=data.get("label") or name.replace("_", " ").capitalize(),
            plugin=plugin,
            description=data.get("description"),
            message=data.get("message") or "",
            check=data.get("check"),
            when=data.get("when"),
            reference_revisions=tuple(sorted((data.get("reference_revisions") or {}).items())),
            recheck_before_signing=data.get("recheck_before_signing", False),
            unique=tuple(data.get("unique") or ()),
            within=str(data.get("within") or "project"),
            no_overlap=(str(overlap.get("from", "")), str(overlap.get("to", "")))
            if isinstance(overlap, dict)
            else (),
            per=tuple(overlap.get("per") or ()) if isinstance(overlap, dict) else (),
            only_while=((str(only["field"]), tuple(_while_value(x) for x in only["states"])),)
            if isinstance(only, dict)
            and isinstance(only.get("field"), str)
            and isinstance(only.get("states"), list)
            else (),
            hold=tuple(lab_role_name(r, plugin) for r in (hold.get("reviewed_by") or ()))
            if isinstance(hold, dict)
            else (),
            fields=tuple(data.get("fields") or ()),
            extension=extension,
        )


def unique_rules(
    fields: Mapping[str, Field],
    plugin: str,
    *,
    identity: Collection[str] = (),
    extension: bool = False,
) -> dict[str, Rule]:
    """The rule each field marked ``unique`` is (ADR 0082), named for the field: no two
    records in a project share its value, or for a registry entry's field, no two entries
    in a registry. The database holds it as it holds any ``unique`` rule (ADR 0127)."""
    out = {}
    for name, f in fields.items():
        if not f.unique or f.removed:
            continue
        what = _lower_first(f.label or name.replace("_", " ").capitalize())
        registry = in_entry(f, identity)
        out[name] = Rule(
            name=name,
            label=f"Unique {what}",
            plugin=plugin,
            message=(
                f"Each entry in a registry has its own {what}."
                if registry
                else f"Each record in a project has its own {what}."
            ),
            unique=(name,),
            within="registry" if registry else "project",
            from_field=True,
            fields=(name,),
            reads=(name,),
            extension=extension,
        )
    return out


def _lower_first(label: str) -> str:
    """A label mid-sentence: ``Barcode`` → ``barcode``, but ``LIMS ID`` and ``ChEMBL ID``
    as they are: a first word with capitals after its first is a name."""
    first = label.split(" ", 1)[0]
    if first[:1].isupper() and first[1:].islower():
        return label[0].lower() + label[1:]
    return label


@dataclass(frozen=True)
class RowRole:
    """What a list's rows of one role take (ADR 0137): how many, and for each of the row's
    references, the types a row with this role may refer to."""

    name: str
    label: str = ""
    min: int | None = None
    max: int | None = None
    #: A reference of the row's → the types, qualified, it may refer to in this role.
    to: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @classmethod
    def from_manifest(cls, name: str, data: dict[str, Any], *, plugin: str) -> RowRole:
        to = data.get("to") or {}
        return cls(
            name=name,
            label=data.get("label") or name.replace("_", " ").capitalize(),
            min=data.get("min"),
            max=data.get("max"),
            to={
                cell: (qualify(t, plugin),)
                if isinstance(t, str)
                else tuple(qualify(x, plugin) for x in t if isinstance(x, str))
                for cell, t in to.items()
                if isinstance(t, str | list)
            }
            if isinstance(to, dict)
            else {},
        )


@dataclass(frozen=True)
class StateMove:
    """Where a removed state field's records go (ADR 0125): another state field of the
    type, each of its states to one of that field's."""

    field: str
    #: (its state, the other field's), sorted.
    states: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class TypeNotification:
    """Whom a type's records tell, and when (ADR 0129): on a state's change or a field's, the
    people its fields name, holders of a lab role in its project, its creator or its
    watchers, in Iki and by email. A request's (ADR 0132) may tell its service it's been
    sent, or the asking project that a result of it was released (ADR 0142)."""

    name: str
    #: What the settings page and the type's page call it: "A sample is in review".
    label: str
    #: When it's sent, in a sentence, for the settings page.
    description: str | None = None
    #: When a record is made, rather than when a field changes.
    created: bool = False
    #: A request's: when it's sent to a service, which it tells, by its roles there.
    sent: bool = False
    #: A request's: when one of its results is released back to the project that asked.
    released: bool = False
    #: The field whose change sends it: one of the record's own, or ``ref.field``, a field of
    #: the record one of its references names.
    field: str = ""
    #: Sent only when the field changes to one of these (states, an enum's values, true or
    #: false)...
    to_values: tuple[Any, ...] = ()
    #: ...or from one of these.
    from_values: tuple[Any, ...] = ()
    #: Only for records these conditions find, as the search API takes them.
    where: dict[str, Any] | None = None
    #: Whom it notifies: the people these fields name...
    notify_fields: tuple[str, ...] = ()
    #: ...holders of these lab roles in the record's project, qualified...
    notify_roles: tuple[str, ...] = ()
    #: ...whoever made the record, whoever watches it, and whoever made the change.
    notify_creator: bool = False
    notify_watchers: bool = False
    notify_actor: bool = False
    #: What it says in Iki: templates over the record's fields, as a display title is.
    title: str | None = None
    body: str | None = None
    #: Emailed unless the person turns it off.
    email: bool = True
    #: Nobody can turn it off in Iki, by kind, project or record; email stays their choice.
    required: bool = False

    @property
    def ref(self) -> str | None:
        """For a field of the record a reference names: the reference."""
        return self.field.split(".", 1)[0] if "." in self.field else None

    @property
    def target(self) -> str:
        """The field whose change sends it, of the record itself or of the one ``ref`` names."""
        return self.field.split(".", 1)[1] if "." in self.field else self.field

    @classmethod
    def from_manifest(cls, name: str, data: dict[str, Any], *, plugin: str) -> TypeNotification:
        when: dict[str, Any] = data["when"] if isinstance(data.get("when"), dict) else {}
        notify: dict[str, Any] = data["notify"] if isinstance(data.get("notify"), dict) else {}
        message: dict[str, Any] = data["message"] if isinstance(data.get("message"), dict) else {}

        def values(value: Any) -> tuple[Any, ...]:  # noqa: ANN401 - manifest data
            if value is None:
                return ()
            return tuple(value) if isinstance(value, list) else (value,)

        return cls(
            name=name,
            label=data.get("label") or name.replace("_", " ").capitalize(),
            description=data.get("description"),
            created=bool(when.get("created", False)),
            sent=bool(when.get("sent", False)),
            released=bool(when.get("released", False)),
            field=when.get("field") or "",
            to_values=values(when.get("to")),
            from_values=values(when.get("from")),
            where=data.get("where"),
            notify_fields=tuple(notify.get("fields") or ()),
            notify_roles=tuple(lab_role_name(r, plugin) for r in notify.get("roles") or ()),
            notify_creator=bool(notify.get("creator", False)),
            notify_watchers=bool(notify.get("watchers", False)),
            notify_actor=bool(notify.get("actor", False)),
            title=message.get("title"),
            body=message.get("body"),
            email=bool(data.get("email", True)),
            required=bool(data.get("required", False)),
        )


@dataclass(frozen=True)
class RequestSpec:
    """What makes a type's records requests from one project to another's service (ADR
    0132): which of its fields the service writes, and which send records with it."""

    #: The fields the project serving it may change, and the state fields it may set: the
    #: rest are the asking project's.
    service_fields: tuple[str, ...] = ()
    #: Its references (or lists of them) whose records go to the service with it.
    sends: tuple[str, ...] = ()
    #: The list of references it sends that the service receives with a receipt (ADR 0180),
    #: and the service's lab roles that receive them (any Editor's, if none).
    receives: str | None = None
    received_by: tuple[str, ...] = ()
    #: Sent fields that identify the physical material commissioned for testing.
    materials: tuple[str, ...] = ()
    #: The service's state field, and its states, that certify delivery of the request.
    completed_field: str | None = None
    completed_states: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkItemSpec:
    """The reference to the request this service-project work item is for (ADR 0132)."""

    of: str


@dataclass(frozen=True)
class ResultSpec:
    """What makes a type's records the results of a request (ADR 0132): the service owns
    them, and each goes back to the asking project once its state says it's released."""

    #: Its reference to the request it's a result of.
    of: str
    #: The state field, and its states, in which it's released to the asking project.
    released_field: str
    released_states: tuple[str, ...] = ()
    #: Immutable reference to the tested material, if the result concerns one.
    material: str | None = None

    def requires_review(self, field: Field, state: str) -> bool:
        """Whether this signed release follows a separately signed review (ADR 0179)."""
        if field.name != self.released_field or state not in self.released_states:
            return False
        target = field.states.get(state)
        if target is None or target.removed or not target.signatures:
            return False
        return any(
            sig.meaning == "review"
            for other in field.states.values()
            if other.name != state and not other.removed
            for sig in other.signatures
        )


@dataclass(frozen=True)
class Amount:
    """How much of each of a type's records there is (ADR 0139): kept in ``unit``, never
    below zero, changed only by drawing, transferring, splitting into aliquots or
    adjusting it, each a movement of its ledger."""

    unit: str  # UCUM: a volume, a mass, an amount of substance, or a count such as {vial}
    label: str = "Amount"
    description: str | None = None
    display_unit: str | None = None
    #: A quantity field of the type giving how much of it each unit of volume holds, so
    #: an amount can be drawn in a unit the concentration converts it to (50 ug at 2 mg/mL).
    concentration: str | None = None
    #: The type its aliquots are (qualified; its own if None), and that type's reference
    #: naming the record each was split from.
    aliquots: str | None = None
    aliquots_of: str | None = None
    #: A required, immutable reference of the type naming what its amount is of, such as
    #: a lot's molecule, or ``id`` when the record itself is the material. A transfer
    #: into one of its records takes only the same entity's,
    #: and its aliquots are that entity's too (ADR 0162).
    material: str | None = None
    #: References whose records' eligibility a draw from it also meets, such as a tube's
    #: lot (ADR 0180): their holds and draw guards. The first that's set is the draw's source.
    follows: tuple[str, ...] = ()
    #: Discrete stock, such as individual tubes. Applies in the ledger unit after conversion.
    whole_units: bool = False


#: The most a container's grid has (ADR 0180): a 1536-well plate's 32 rows and 48 columns.
GRID_ROWS = 32
GRID_COLUMNS = 48
GRID_POSITIONS = 1536


@dataclass(frozen=True)
class Grid:
    """A container's positions, in rows and columns (ADR 0180): each a number, or the name
    of one of its own required, immutable int fields."""

    rows: int | str
    columns: int | str
    #: ``letter_number`` (A1 to H12) or ``number`` (1 to 96).
    labels: str = "letter_number"
    #: ``by_row`` (A1, A2, …) or ``by_column`` (A1, B1, …): how they're numbered and filled.
    order: str = "by_row"


@dataclass(frozen=True)
class Storage:
    """Its records are kept somewhere, or keep others (ADR 0180): Iki keeps where each is,
    who has it, and every move."""

    #: The types (qualified) of what can be kept in its records, or their ancestors.
    holds: tuple[str, ...] = ()
    grid: Grid | None = None
    #: A unique string field holding the code on it already: what's scanned and printed.
    barcode: str | None = None
    #: A label's line under its title, with ``{{ }}`` parts as a display title has.
    label: str | None = None
    #: What needs its code scanned first: ``draw``, ``move``.
    confirm: tuple[str, ...] = ()


@dataclass(frozen=True)
class StockLots:
    type: str
    item: str
    number: str
    expiry: str


@dataclass(frozen=True)
class StockOrders:
    type: str
    item: str
    reference: str
    ordered: str
    received: str


@dataclass(frozen=True)
class StockCatalogue:
    """Fields identifying a project's stock, routine-use threshold and open orders."""

    name: str
    unit: str
    threshold: str
    #: The lab roles, qualified, whose holders in the item's project a low stock notifies.
    notify_roles: tuple[str, ...]
    lots: StockLots
    orders: StockOrders

    @classmethod
    def read(cls, data: dict[str, Any], plugin: str) -> StockCatalogue:
        """From its manifest."""
        return cls(
            name=data["name"],
            unit=data["unit"],
            threshold=data["threshold"],
            notify_roles=tuple(lab_role_name(r, plugin) for r in data["notify"]["roles"]),
            lots=StockLots(**{**data["lots"], "type": qualify(data["lots"]["type"], plugin)}),
            orders=StockOrders(
                **{**data["orders"], "type": qualify(data["orders"]["type"], plugin)}
            ),
        )


@dataclass(frozen=True)
class DrawTarget:
    """A record intended to receive one matched draw while it awaits material."""

    from_field: str
    source: str | None
    state_field: str
    #: The states of ``state_field`` in which the record awaits its draw.
    states: tuple[str, ...]
    #: The lab roles, qualified, one of which whoever records the draw holds.
    drawn_by: tuple[str, ...]
    ready: str | None
    amount: str | None
    stored: bool = False
    #: A field naming the planned amount's unit, for a float ``amount`` (ADR 0243).
    unit: str | None = None


#: The fields every lab list's options have (ADR 0270), which the list marker gives its type:
#: its code, which is its identity, and its label, description and place in the list, which
#: are registry facts its curators change.
LIST_CODE = "code"
LIST_LABEL = "label"
LIST_DESCRIPTION = "description"
LIST_POSITION = "position"
LIST_FIELDS = (LIST_CODE, LIST_LABEL, LIST_DESCRIPTION, LIST_POSITION)
#: An option's code: as an enum's value is, so an enum can become a list keeping its values.
LIST_CODE_PATTERN = "^[A-Za-z0-9][A-Za-z0-9_.+-]*$"
#: An option's label: one line, without spaces at its ends.
LIST_LABEL_PATTERN = r"^\S(?:.*\S)?$"
#: The most options a list may have, as a dropdown or its search can offer them.
LIST_MAX_OPTIONS = 10000


def list_fields() -> dict[str, Field]:
    """The fields the list marker gives a type (ADR 0270), before the type's own."""
    return {
        LIST_CODE: Field(
            name=LIST_CODE,
            type="string",
            label="Code",
            description="Its identity in the list, which never changes: what exports and "
            "the API give for it. A wrong one is superseded by the right one.",
            required=True,
            immutable=True,
            indexed=True,
            max_length=64,
            pattern=LIST_CODE_PATTERN,
            pattern_help="Use letters and digits, and _ . + or - after the first",
        ),
        LIST_LABEL: Field(
            name=LIST_LABEL,
            type="string",
            label="Label",
            description="What people see and pick. Its curators can change it, with a reason: "
            "records keep naming the option, so they show the new label.",
            required=True,
            registry="fact",
            indexed=True,
            unique=True,
            max_length=200,
            pattern=LIST_LABEL_PATTERN,
            pattern_help="Use one line, with no spaces at its start or end",
        ),
        LIST_DESCRIPTION: Field(
            name=LIST_DESCRIPTION,
            type="text",
            label="Description",
            description="What it means, or when to choose it.",
            registry="fact",
            max_length=2000,
        ),
        LIST_POSITION: Field(
            name=LIST_POSITION,
            type="int",
            label="Position",
            description="Its place in the list's order, from 1: dropdowns offer options in it.",
            registry="fact",
            min=1,
            max=LIST_MAX_OPTIONS,
        ),
    }


@dataclass(frozen=True)
class ListSpec:
    """A type whose entities are the options of a list a lab curates (ADR 0270)."""

    #: The options each registry starts with, in order: each one's code, label and
    #: description, and its values for the type's other fields, by name, as the manifest
    #: gives them (a reference to another list's option by its code).
    options: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class Draws:
    """A type's records' draws (ADR 0139, 0180): what a draw from one meets, and what one
    that receives a draw awaits. Each part is inherited on its own."""

    #: Conditions on taking material. Every subtype keeps its ancestors' guards too.
    guards: dict[str, Guard] = field(default_factory=dict)
    #: Lab roles allowed to draw for each purpose. A subtype may narrow each inherited list.
    purposes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: An optional one-draw ledger target, such as a scheduled stability pull.
    target: DrawTarget | None = None
    #: Optional record field maintained by the amounts ledger when a draw goes into this record.
    received: str | None = None


@dataclass(frozen=True)
class Service:
    """A type's part in a request between projects (ADR 0132): its records are requests,
    a request's results, or a service's work for one."""

    #: Its records are requests between projects. Subtypes have their ancestor's.
    request: RequestSpec | None = None
    #: Its records are a request's results.
    result: ResultSpec | None = None
    #: Its records are a service project's internal work for a request.
    work_item: WorkItemSpec | None = None


@dataclass(frozen=True)
class EntityType:
    plugin: str
    name: str
    schema_version: int
    label: str = ""
    label_plural: str = ""
    description: str | None = None
    extends: str | None = None  # qualified
    abstract: bool = False
    hrid_prefix: str | None = None
    hrid_digits: int = 6
    #: The fields that make two of its entities one registry entry (ADR 0115).
    identity: tuple[str, ...] = ()
    fields: dict[str, Field] = field(default_factory=dict)
    display_title: str | None = None
    viewers: tuple[Viewer, ...] = ()
    #: The files Iki writes of its records, such as GenBank (ADR 0171). Subtypes have their
    #: nearest ancestor's unless they declare their own.
    exports: tuple[AnyExport, ...] = ()
    file: str | None = None
    #: The date or datetime field saying when its records are due (ADR 0122): its own or a
    #: parent's. Subtypes have their ancestor's unless they name their own.
    due: str | None = None
    #: Rules every save of its records keeps (ADR 0127), by name, as do its subtypes'.
    rules: dict[str, Rule] = field(default_factory=dict)
    #: Whom its records tell, and when (ADR 0129), by name. Subtypes' records tell what their
    #: ancestors' do too.
    notifications: dict[str, TypeNotification] = field(default_factory=dict)
    #: Its part in a request between projects (ADR 0132).
    service: Service = field(default_factory=Service)
    #: How much of each record there is (ADR 0139): its own or a parent's.
    amount: Amount | None = None
    stock_catalogue: StockCatalogue | None = None
    #: Its records' draws: guards, purposes, and the draw a record awaits and receives.
    draws: Draws = field(default_factory=Draws)
    #: Where its records are kept, and what they keep (ADR 0180): its own or a parent's.
    storage: Storage | None = None
    #: Its records are samples (ADR 0177): the files they hold, and those a run that read
    #: one of them read or made, are its files in a project's Files. Its subtypes' are too.
    sample: bool = False
    #: What each list of rows it inherits takes, by the list's name (ADR 0164): every role
    #: its rows may have, each with how many and what its references may refer to, within
    #: what its ancestor's say. Its subtypes have these unless they name their own.
    refines: dict[str, dict[str, RowRole]] = field(default_factory=dict)
    #: What each reference it inherits, or list of references, refers to, by its name
    #: (ADR 0235): types among its ancestor's, each with its subtypes. Its subtypes have
    #: these unless they name their own.
    refines_to: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: Its entities are a lab list's options (ADR 0270): registry entries every project in
    #: their registry may pick, curated by its registrars and list curators.
    list_spec: ListSpec | None = None
    #: How its records are reviewed (ADR 0301): its own, or an ancestor's.
    review: ReviewSpec | None = None

    @property
    def is_list(self) -> bool:
        """Whether its entities are a lab list's options (ADR 0270)."""
        return self.list_spec is not None

    @property
    def qualified_name(self) -> str:
        return f"{self.plugin}.{self.name}"

    @property
    def table(self) -> str:
        return naming.type_table(self.qualified_name)

    @property
    def current_fields(self) -> dict[str, Field]:
        """The fields entities have now, without the removed ones."""
        return {name: f for name, f in self.fields.items() if not f.removed}


def lineage(type_name: str, types: Mapping[str, EntityType]) -> list[str]:
    """A validated type and each type it extends, nearest first, ending with
    ``iki/core.Entity``: what a run input gives a command as an entity's ``types``."""
    out = [type_name]
    current = types[type_name]
    while current.extends:
        out.append(current.extends)
        current = types[current.extends]
    if out[-1] != naming.ENTITY:
        out.append(naming.ENTITY)
    return out


def amount_of(type_name: str, types: Mapping[str, EntityType]) -> Amount | None:
    """The nearest amount declaration on a validated type or one of its ancestors."""
    current = types.get(type_name)
    while current is not None:
        if current.amount is not None:
            return current.amount
        current = types.get(current.extends) if current.extends else None
    return None


@dataclass(frozen=True)
class Track:
    """Values along a sequence viewer's residues: a json field of one number per residue."""

    field: str
    label: str = ""  # the field's label if ""


@dataclass(frozen=True)
class Viewer:
    """One of Iki's own viewers on a type's pages, and the fields it shows (``display``)."""

    viewer: str  # "sequence", "structure" or "alignment"
    label: str
    sequence: str = ""  # a string field with a sequence format
    features: str = ""  # a json field of regions along it, or ""
    structure: str = ""  # a file field of mmCIF or PDB files
    alignment: str = ""  # a file field of aligned FASTA
    rows: str = ""  # a list of refs to the sequences it aligns, rows headed by their HRIDs
    reference: str = ""  # a ref to the sequence the others were aligned to, or ""
    tracks: tuple[Track, ...] = ()  # the sequence viewer's per-residue values


@dataclass(frozen=True)
class Export:
    """Files Iki writes of a type's records, and the fields they're written from
    (``exports``, ADR 0171). A ``sequence`` export writes GenBank and FASTA."""

    export: str  # "sequence"
    #: A string field with a DNA or RNA format, or a reference to a type that exports a
    #: sequence: its file, under this record's name, with this record's features added.
    sequence: str
    features: str = ""  # a json field of features, as the sequence viewer reads them, or ""
    description: str = ""  # a string or text field, or ""
    accession: str = ""  # a string field: an INSDC accession with its version, or ""
    organism: str = ""  # a reference to iki/core.Organism, or ""


@dataclass(frozen=True)
class ProteinExport:
    """A protein's residues as FASTA (``export: protein``, ADR 0229)."""

    export: str  # "protein"
    sequence: str  # a string field with format protein_sequence
    description: str = ""  # a string or text field, or ""
    organism: str = ""  # a reference to iki/core.Organism, or ""


@dataclass(frozen=True)
class CompoundExport:
    """A compound's structure as a Molfile, and with its fields as an SDF's data
    (``export: compound``, ADR 0229)."""

    export: str  # "compound"
    structure: str  # a text field holding an MDL Molfile, V2000 or V3000


@dataclass(frozen=True)
class ReportExport:
    """A JSON report issued by a signed state change."""

    export: str
    report: str
    review: str
    issued: tuple[str, ...]


@dataclass(frozen=True)
class PluginExport:
    """A type's records as a plugin's writer writes them (ADR 0230): the writer, by its
    qualified ID, and the field each of its roles reads."""

    export: str  # the writer: "acme/liquid_handling.tecan_worklist"
    #: role → the type's field it reads.
    fields: dict[str, str] = field(default_factory=dict)


#: Any of the files a type's records export as.
type AnyExport = Export | ProteinExport | CompoundExport | ReportExport | PluginExport

#: What a writer's roles may be: fields of these types.
WRITER_ROLE_TYPES = (
    "string",
    "text",
    "int",
    "float",
    "bool",
    "date",
    "datetime",
    "enum",
    "quantity",
    "ref",
)


@dataclass(frozen=True)
class WriterRole:
    """What a writer reads of a record: a field of this type, mapped by each type that
    exports through it."""

    type: str
    required: bool = False
    description: str | None = None
    #: A quantity's unit: the mapped field's must measure the same thing, and Iki gives the
    #: writer its value in this one.
    unit: str | None = None
    #: A reference's targets, qualified: the mapped field must refer to one of these (or a
    #: subtype). Empty for any record.
    to: tuple[str, ...] = ()


@dataclass(frozen=True)
class Writer:
    """A plugin's writer of a file format (ADR 0230): a WebAssembly module built from the
    Rust crate at ``module``, pinned by the digest tracked beside it."""

    plugin: str
    name: str
    label: str
    #: The format it writes, qualified (ADR 0213): its media type and extension.
    format: str
    #: The crate's directory in the plugin: ``writers/tecan-worklist``.
    module: str
    #: ``file``: many records make one file; ``zip``: one file each, in a ZIP; ``one``:
    #: one record only.
    many: str = "file"
    #: Its memory's cap, in bytes.
    memory: int = 64 * 1024 * 1024
    description: str | None = None
    roles: dict[str, WriterRole] = field(default_factory=dict)
    #: The SHA-256 of the module the Linux x86-64 builder makes, as the plugin tracks it.
    sha256: str = ""
    order: int = 0
    file: str | None = None

    @property
    def qualified_name(self) -> str:
        return f"{self.plugin}.{self.name}"


@dataclass(frozen=True)
class Extension:
    plugin: str
    adds_to: str  # qualified
    fields: dict[str, Field]
    description: str | None = None
    file: str | None = None
    #: Rules every save of the extended type's records keeps (ADR 0127), by name.
    rules: dict[str, Rule] = field(default_factory=dict)

    @property
    def table(self) -> str:
        return naming.extension_table(self.plugin, self.adds_to)

    @property
    def current_fields(self) -> dict[str, Field]:
        return {name: f for name, f in self.fields.items() if not f.removed}


#: Where actions run, from a machine a team registered to a virtual machine Iki starts.
TIERS = ("self_hosted", "backend", "container", "vm")


@dataclass(frozen=True)
class Resources:
    """A machine's size: what an environment needs at least, or an action more."""

    cpu: float | None = None
    memory: int | None = None  # bytes
    gpu_count: int = 0
    gpu_memory: int | None = None  # bytes, per GPU
    scratch_disk: int | None = None  # bytes

    @classmethod
    def from_manifest(cls, data: dict[str, Any] | None) -> Resources:
        data = data or {}
        gpu = data.get("gpu") or {}
        return cls(
            cpu=data.get("cpu"),
            memory=_quantity(data.get("memory")),
            gpu_count=gpu.get("count", 0),
            gpu_memory=_quantity(gpu.get("min_memory")),
            scratch_disk=_quantity(data.get("scratch_disk")),
        )

    def at_least(self, other: Resources) -> Resources:
        """The larger of each of these and `other`'s."""

        def most(a: Any, b: Any) -> Any:  # noqa: ANN401 - numbers or None
            return b if a is None else a if b is None else max(a, b)

        return Resources(
            cpu=most(self.cpu, other.cpu),
            memory=most(self.memory, other.memory),
            gpu_count=max(self.gpu_count, other.gpu_count),
            gpu_memory=most(self.gpu_memory, other.gpu_memory),
            scratch_disk=most(self.scratch_disk, other.scratch_disk),
        )


@dataclass(frozen=True)
class Input:
    name: str
    type: str  # qualified
    schema_version: str | None = None  # a constraint on the type's schema versions
    cardinality: str = "one"
    max: int | None = None
    optional: bool = False
    label: str = ""
    description: str | None = None
    #: Its type's reference fields whose entities the run reads too.
    include: tuple[str, ...] = ()
    #: Not chosen: each run is given every record of its type its project owns, not
    #: archived, such as a core's ranking of the projects it serves (ADR 0232).
    from_project: bool = False


@dataclass(frozen=True)
class Creates:
    type: str  # qualified
    cardinality: str = "one"
    derived_from: tuple[str, ...] = ()
    #: A unique field: a create with a value an entity in the project has updates it.
    match_on: str | None = None
    #: What a create that matches does: ``update`` the entity with its values, or ``keep``
    #: it as it is, as whoever made it first made it.
    on_match: str = "update"
    #: For a fresh physical lot, the singular input naming its storage container.
    receive_into: str | None = None


@dataclass(frozen=True)
class Updates:
    input: str
    fields: tuple[str, ...]


@dataclass(frozen=True)
class Secret:
    """A secret an action's runs get as an environment variable, which each deployment
    keeps in its own secret manager."""

    name: str
    #: A required secret the deployment hasn't set keeps the action from running.
    required: bool = True
    #: For the administrators who set it: what it's for, and where to get one.
    description: str = ""


@dataclass(frozen=True)
class Software:
    """A tool an action's runs make entities with, by name and version (ADR 0106)."""

    name: str
    version: str
    url: str = ""


@dataclass(frozen=True)
class Metric:
    """A number an action's runs report about themselves (``metrics`` in their output), which
    a workflow's conditions can read (ADR 0079)."""

    name: str
    label: str = ""
    description: str = ""


@dataclass(frozen=True)
class ApiCredential:
    """How an outside API's key goes with each call (ADR 0130): the egress proxy adds it, as
    ``header: prefix + key``, from the run secret `secret`. Runs' commands never see it."""

    secret: str
    header: str = "Authorization"
    prefix: str = "Bearer "
    #: For the administrators who set it: what it's for, and where to get one.
    description: str = ""


@dataclass(frozen=True)
class ApiWebhook:
    """How the API says a job is done: a callback signed with the run secret `secret`, by
    `signature` (``standard-webhooks``, or ``hmac-sha256`` of the body, hex, in `header`)."""

    signature: str
    secret: str
    header: str = ""


@dataclass(frozen=True)
class ApiJobs:
    """An API whose work is a job to submit, then poll: what a parked run polls (ADR 0130).
    Paths take the job's ID as ``{id}``; `status`, `message` and `usage` are locators in the
    poll's answer."""

    poll: str
    status: str
    done: tuple[str, ...]
    failed: tuple[str, ...]
    message: str = ""
    cancel: str = ""
    every: int = 30  # seconds
    timeout: int = 86400  # seconds
    #: Usage read from the last poll's answer, by name: what a job used in all.
    usage: dict[str, str] = field(default_factory=dict)
    webhook: ApiWebhook | None = None


@dataclass(frozen=True)
class OutsideApi:
    """A third party's API a plugin's actions call, such as a hosted model (ADR 0130). Org
    Admins approve it per project; runs call it through the egress proxy, which adds the
    credential and records each call. A locator is a JSON pointer in a JSON answer (or in
    a streamed answer's last event that has it), or ``header:<name>``."""

    plugin: str
    name: str
    label: str
    host: str
    purpose: str
    credential: ApiCredential | None = None
    #: Locators of the model and its version, in each answer.
    model: str = ""
    version: str = ""
    #: JSON pointers in each request's body whose values are kept as its parameters.
    parameters: tuple[str, ...] = ()
    #: Usage read from each call's answer, by name, such as ``gpu_seconds``.
    usage: dict[str, str] = field(default_factory=dict)
    jobs: ApiJobs | None = None

    @property
    def qualified_name(self) -> str:
        return f"{self.plugin}.{self.name}"


@dataclass(frozen=True)
class ApiUse:
    """An outside API an action calls, and what of a run it sends there: which fields of
    each input's entities, and which params. People see it before they launch (ADR 0130).
    `api` is the plugin's API, as installed."""

    name: str
    inputs: dict[str, tuple[str, ...]] = field(default_factory=dict)
    params: tuple[str, ...] = ()
    api: OutsideApi | None = None


@dataclass(frozen=True)
class Action:
    plugin: str
    name: str
    tier: str
    environment: str
    command: tuple[str, ...]
    label: str = ""
    description: str | None = None
    inputs: dict[str, Input] = field(default_factory=dict)
    params: dict[str, Field] = field(default_factory=dict)
    creates: tuple[Creates, ...] = ()
    updates: tuple[Updates, ...] = ()
    lifecycle: str | None = None  # container and vm: ephemeral or persistent
    resources: Resources = field(default_factory=Resources)
    deterministic: bool = False
    checkpointable: bool = False
    batchable: int = 1
    artifacts: tuple[str, ...] = ()  # glob patterns
    artifact_retention: int | None = None  # seconds
    network: tuple[str, ...] = ()
    secrets: tuple[Secret, ...] = ()
    #: The plugin's settings it can't run without, which a deployment may leave unset.
    requires_settings: tuple[str, ...] = ()
    #: The outside APIs its runs call, through the egress proxy (ADR 0130).
    apis: tuple[ApiUse, ...] = ()
    metrics: dict[str, Metric] = field(default_factory=dict)
    software: tuple[Software, ...] = ()
    timeout: int = 3600  # seconds
    retry_max: int = 0
    retry_on: tuple[str, ...] = ()
    file: str | None = None

    @property
    def qualified_name(self) -> str:
        return f"{self.plugin}.{self.name}"


@dataclass(frozen=True)
class Volume:
    name: str
    size: int  # bytes
    mount: str
    access: str = "read_only_many"
    performance: str = "standard"
    populated_by: str | None = None


@dataclass(frozen=True)
class Environment:
    plugin: str
    name: str
    tiers: tuple[str, ...]
    label: str = ""
    description: str | None = None
    #: What a self-hosted machine must have to run it, sorted (ADR 0112).
    labels: tuple[str, ...] = ()
    resources: Resources = field(default_factory=Resources)
    image: str | None = None
    python_version: str | None = None
    requirements: str | None = None  # a path in the plugin
    conda_lockfile: str | None = None  # a path in the plugin
    volumes: dict[str, Volume] = field(default_factory=dict)
    setup_script: str | None = None  # a path in the plugin
    setup_timeout: int | None = None  # seconds
    setup_requires_approval: bool = False
    setup_network: tuple[str, ...] = ()
    file: str | None = None
    #: Provisioned runs need an image supplied by this deployment.
    customer_image_required: bool = False


#: A condition's operators: how its value compares with its operand.
CONDITION_OPERATORS = ("is", "is_not", "in", "at_least", "at_most", "above", "below")
NUMERIC_OPERATORS = ("at_least", "at_most", "above", "below")


@dataclass(frozen=True)
class Condition:
    """Something a step's `when` says must hold for it to run (ADR 0079): its `value`
    (``$params.<name>`` or ``$steps.<id>.metrics.<name>``) compared, by `operator`, with
    `operand`: a value, a list of them for ``in``, or ``$params.<name>``."""

    value: str
    operator: str
    operand: Any = None

    def step(self) -> str | None:
        """The step whose metric it reads, if it reads one."""
        parts = self.value.split(".")
        return parts[1] if parts[0] == "$steps" and len(parts) == 4 else None


#: The project roles a workflow step's approval may need, lowest first. It may need a lab
#: role instead (ADR 0116), named in full: `acme/qms.qa`.
APPROVAL_ROLES = ("viewer", "editor", "runner", "admin")


@dataclass(frozen=True)
class LabRole:
    """A named role a plugin declares, such as QA (ADR 0116). A project's Admins give it to
    members beside their project role, and state fields and approvals can require it."""

    plugin: str
    name: str
    label: str
    description: str | None = None

    @property
    def qualified_name(self) -> str:
        return f"{self.plugin}.{self.name}"


def is_lab_role(role: str) -> bool:
    """Whether an approval's role is a lab role (``acme/qms.qa``) rather than a project role."""
    return "." in role


@dataclass(frozen=True)
class Approval:
    """A person approves a workflow step before it starts (ADR 0079): someone with `role`
    in the project, other than whoever started the workflow."""

    role: str = "editor"
    instructions: str = ""


@dataclass(frozen=True)
class WorkflowStep:
    """One step of a workflow: a run of one of the plugin's actions (ADR 0079)."""

    id: str
    action: str  # the action's name in the plugin
    label: str = ""
    #: The action's input → where it comes from: ``$inputs.<name>``,
    #: ``$steps.<id>.inputs.<name>`` or ``$steps.<id>.created.<namespace/plugin.Type>``.
    inputs: dict[str, str] = field(default_factory=dict)
    #: The action's param → a value, or ``$params.<name>``.
    params: dict[str, Any] = field(default_factory=dict)
    needs: tuple[str, ...] = ()
    #: The input it runs once for each entity of, as a batch (ADR 0079), if any.
    each: str | None = None
    #: What must hold for it to run; otherwise it's skipped, and the steps after it.
    when: tuple[Condition, ...] = ()
    #: Who approves it before it starts, if anyone must.
    approval: Approval | None = None

    def after(self) -> set[str]:
        """The steps it runs after: those it takes from, and those it needs. Naming
        itself is a mistake the build reports, not a cycle."""
        found = set(self.needs)
        for ref in self.inputs.values():
            if ref.startswith("$steps."):
                found.add(ref.split(".")[1])
        found.update(s for c in self.when if (s := c.step()) is not None)
        return found - {self.id}


@dataclass(frozen=True)
class Workflow:
    """Steps that each run one of the plugin's actions, wired together (ADR 0079)."""

    plugin: str
    name: str
    label: str = ""
    description: str | None = None
    inputs: dict[str, Input] = field(default_factory=dict)
    params: dict[str, Field] = field(default_factory=dict)
    steps: dict[str, WorkflowStep] = field(default_factory=dict)
    file: str | None = None

    @property
    def qualified_name(self) -> str:
        return f"{self.plugin}.{self.name}"

    def order(self) -> list[str] | None:
        """Its steps, each after those it runs after (by ID where that leaves a choice);
        None if some come before themselves."""
        done: list[str] = []
        left = dict(self.steps)
        while left:
            ready = sorted(
                i for i, step in left.items() if step.after() & self.steps.keys() <= set(done)
            )
            if not ready:
                return None
            done.extend(ready)
            for i in ready:
                del left[i]
        return done


#: What fires a trigger: an entity of its type created, a new registry entry of it
#: registered (ADR 0115), a record's state field set (ADR 0125), an entity changed, a
#: request sent to the project as a service (ADR 0232), or its schedule.
TRIGGER_EVENTS = (
    "entity.created",
    "entity.registered",
    "entity.state_changed",
    "entity.updated",
    "request.received",
    "schedule",
)
#: The events that fire a trigger for an entity.
ENTITY_EVENTS = (
    "entity.created",
    "entity.registered",
    "entity.state_changed",
    "entity.updated",
    "request.received",
)
#: The events whose trigger's runs are launched as a service account the Admin who turns
#: it on chooses, on their authority (ADR 0081, ADR 0232): nobody in the project made
#: them happen.
ACCOUNT_EVENTS = ("request.received", "schedule")


#: Which way a trigger's related entities are (ADR 0123): derived from the entity, or what
#: it was derived from.
RELATED_DIRECTIONS = ("downstream", "upstream")
#: The kinds of edge a trigger's walk may be narrowed to.
RELATED_EDGES = ("reference", "run", "file", "aliquot", "transfer", "used")


@dataclass(frozen=True)
class Related:
    """What a trigger gives one of its action's inputs, besides the entity that fired it
    (ADR 0123): the input's type's entities in the project ``direction`` of the entity,
    up to ``depth`` edges, along ``edges`` (all, if empty)."""

    direction: str = "downstream"
    depth: int = 10
    edges: tuple[str, ...] = ()


@dataclass(frozen=True)
class Trigger:
    """Runs one of the plugin's actions by itself when an entity of a type is created or
    changed, or on a schedule, in projects whose Admins turn it on (ADR 0080)."""

    plugin: str
    name: str
    label: str = ""
    description: str | None = None
    #: One of :data:`TRIGGER_EVENTS`.
    on: str = "entity.created"
    #: The entities it fires for, with their subtypes, qualified; empty on a schedule.
    type: str = ""
    #: On a schedule: cron's five fields, in `timezone` (UTC when empty).
    schedule: str = ""
    timezone: str = ""
    #: For entity.updated: it fires only when one of these changed; empty for any change.
    fields: tuple[str, ...] = ()
    #: For entity.state_changed: the state field it watches (ADR 0125), and the states
    #: it fires on a record entering; empty for any. Its manifest calls it ``field``.
    state_field: str = ""
    states: tuple[str, ...] = ()
    #: The plugin's action it runs.
    action: str = ""
    #: The action's input that takes the entity: given, or the only one that takes its type.
    input: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    file: str | None = None
    #: Inputs given the entities related to the one that fired it, by input (ADR 0123).
    related: dict[str, Related] = field(default_factory=dict)
    recheck: str = ""
    watch_included: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def qualified_name(self) -> str:
        return f"{self.plugin}.{self.name}"


#: Where a component can appear (ADR 0084, ADR 0117).
SLOTS = ("entity.tab", "search.results", "project.page", "person.home")
#: What a declared search's condition can compare with: the place's context.
CONTEXT = ("entity", "me")


@dataclass(frozen=True)
class ComponentSearch:
    """The entities a slot is filled with, as the search API takes them (ADR 0117).

    A condition's value may be ``{{ entity }}``, the entity whose page it is, or
    ``{{ me }}``, the person viewing it, worked out when it's shown."""

    where: dict[str, Any] | None = None
    #: A field, or empty for the order they were made in.
    sort: str = ""
    descending: bool = False


@dataclass(frozen=True)
class ComponentSlot:
    """Where a component appears: ``entity.tab``, on the pages of entities of ``type``;
    ``search.results``, the grid's results of ``type``; ``project.page`` and
    ``person.home``, sections of what ``search`` finds there."""

    slot: str
    #: Qualified. For a tab, the entities whose pages show it (their subtypes' too); for
    #: the others, the component's ``entities`` type.
    type: str
    label: str = ""
    search: ComponentSearch | None = None


@dataclass(frozen=True)
class ComponentEntities:
    """The many entities a component shows: their type (with its subtypes), the fields it
    reads of each, and the most it holds at once (ADR 0117)."""

    type: str
    fields: tuple[str, ...] = ()
    limit: int = 500


@dataclass(frozen=True)
class Component:
    """A Web Component the plugin adds to Iki's pages, run in a sandboxed frame (ADR 0084)."""

    plugin: str
    name: str
    label: str = ""
    description: str | None = None
    #: Its JavaScript module, relative to the plugin's root (``./components/x.js``).
    module: str = ""
    #: The custom element the module defines.
    element: str = ""
    slots: tuple[ComponentSlot, ...] = ()
    #: The many entities it shows, in the places that give it them; None if it shows one.
    entities: ComponentEntities | None = None
    #: The type of the entities it can select (qualified), or empty if it selects none.
    selects: str = ""
    #: The plugin's actions it may offer to run on its tab's entity (qualified): Iki's run
    #: page runs them, for whoever may.
    runs: tuple[str, ...] = ()
    #: The module's SHA-256, in hex, as the plugin was read: what the build copies.
    sha256: str = ""
    file: str | None = None

    @property
    def qualified_name(self) -> str:
        return f"{self.plugin}.{self.name}"


@dataclass(frozen=True)
class FileSignature:
    """An exact byte sequence at a fixed offset from the start of the file."""

    hex: str
    offset: int = 0


@dataclass(frozen=True)
class FileFormat:
    """One plugin's declaration, qualified by its owner (ADR 0213)."""

    id: str
    label: str
    media_types: tuple[str, ...]
    extensions: tuple[str, ...]
    signatures: tuple[FileSignature, ...] = ()
    reader: str | None = None
    viewer: str | None = None

    # Keep the default for shared extensions even when a compiled JSON object's keys
    # are sorted for hashing or stored in a database.
    order: int = 0

    @property
    def canonical(self) -> str:
        return self.media_types[0]

    @classmethod
    def read(cls, id: str, data: dict[str, Any], order: int) -> FileFormat:
        return cls(
            id=id,
            order=order,
            label=data["label"],
            media_types=tuple(data["media_types"]),
            extensions=tuple(data["extensions"]),
            signatures=tuple(FileSignature(**s) for s in data.get("signatures", ())),
            reader=data.get("reader"),
            viewer=data.get("viewer"),
        )


@dataclass(frozen=True)
class Plugin:
    name: str
    version: str
    title: str = ""
    description: str | None = None
    license: str | None = None
    homepage: str | None = None
    #: The Iki releases it works with, such as `>=0.1,<0.3`, or None to say nothing.
    iki: str | None = None
    #: plugin name → {type name → schema-version constraint}
    dependencies: dict[str, dict[str, str]] = field(default_factory=dict)
    settings: dict[str, Field] = field(default_factory=dict)
    types: dict[str, EntityType] = field(default_factory=dict)
    extensions: tuple[Extension, ...] = ()
    actions: dict[str, Action] = field(default_factory=dict)
    environments: dict[str, Environment] = field(default_factory=dict)
    workflows: dict[str, Workflow] = field(default_factory=dict)
    triggers: dict[str, Trigger] = field(default_factory=dict)
    components: dict[str, Component] = field(default_factory=dict)
    #: Its lab roles, by qualified name (``acme/qms.qa``), as declared.
    roles: dict[str, LabRole] = field(default_factory=dict)
    #: Installed project starter recipes and their versions (ADR 0198).
    starters: dict[str, str] = field(default_factory=dict)
    #: File formats this plugin owns, by qualified ID, in declaration order.
    formats: dict[str, FileFormat] = field(default_factory=dict)
    #: The outside APIs its actions call, by name (ADR 0130).
    apis: dict[str, OutsideApi] = field(default_factory=dict)
    #: The writers of export formats it declares, by qualified ID (ADR 0230).
    writers: dict[str, Writer] = field(default_factory=dict)
    #: The review procedures it declares, by qualified name and version (ADR 0301).
    procedures: dict[str, ReviewProcedure] = field(default_factory=dict)


class _CoreTypes(Mapping[str, EntityType]):
    """The core plugin's types, read from its manifests (``iki_spec/core``) when first used."""

    def _types(self) -> Mapping[str, EntityType]:
        from iki_spec.builtin import core_plugin

        return core_plugin().types

    def __getitem__(self, key: str) -> EntityType:
        return self._types()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._types())

    def __len__(self) -> int:
        return len(self._types())


#: Types every plugin can use without declaring a dependency: the core plugin's.
BUILTIN_TYPES: Mapping[str, EntityType] = _CoreTypes()


def qualify(ref: str, plugin: str) -> str:
    return ref if "." in ref else f"{plugin}.{ref}"


_SIZE_UNITS = {
    "B": 1,
    "kB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "TB": 1000**4,
    "KiB": 1024,
    "MiB": 1024**2,
    "GiB": 1024**3,
    "TiB": 1024**4,
}


def parse_size(text: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)([A-Za-z]+)", text)
    if match is None or match.group(2) not in _SIZE_UNITS:
        raise ValueError(text)
    return int(match.group(1)) * _SIZE_UNITS[match.group(2)]


_QUANTITY_UNITS = {"Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4}


def parse_quantity(text: str) -> int:
    """Bytes in a Kubernetes-style quantity of memory or disk, such as `64Gi`."""
    match = re.fullmatch(r"([1-9][0-9]*)(Mi|Gi|Ti)", text)
    if match is None:
        raise ValueError(text)
    return int(match.group(1)) * _QUANTITY_UNITS[match.group(2)]


def _quantity(text: str | None) -> int | None:
    return parse_quantity(text) if text is not None else None


_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> int:
    """Seconds in a duration such as `90s`, `30m`, `8h` or `365d`."""
    match = re.fullmatch(r"([1-9][0-9]*)([smhd])", text)
    if match is None:
        raise ValueError(text)
    return int(match.group(1)) * _DURATION_UNITS[match.group(2)]


# --- JSON ---------------------------------------------------------------------------------------
# `iki plugin build` writes each plugin as JSON next to its generated models,
# so the server reads labels, units and rules without the YAML.


_DATACLASSES = (
    Amount,
    StockCatalogue,
    StockOrders,
    StockLots,
    DrawTarget,
    Draws,
    Service,
    Grid,
    Storage,
    Field,
    RowRole,
    State,
    Signature,
    Guard,
    Maker,
    Exclusion,
    Slot,
    ReviewState,
    ReviewFlow,
    ReviewProcedure,
    ReviewNeeds,
    TypeGuard,
    ReviewSpec,
    Rule,
    StateMove,
    TypeNotification,
    RequestSpec,
    ResultSpec,
    WorkItemSpec,
    ListSpec,
    EntityType,
    Export,
    ProteinExport,
    CompoundExport,
    ReportExport,
    PluginExport,
    Writer,
    WriterRole,
    Extension,
    Plugin,
    FileFormat,
    FileSignature,
    Action,
    Metric,
    Software,
    Secret,
    Input,
    Creates,
    Updates,
    Resources,
    Environment,
    Volume,
    Workflow,
    WorkflowStep,
    Condition,
    Approval,
    Related,
    Trigger,
    Component,
    ComponentSlot,
    ComponentSearch,
    ComponentEntities,
    Viewer,
    Track,
    LabRole,
    ApiCredential,
    ApiWebhook,
    ApiJobs,
    OutsideApi,
    ApiUse,
)


def plugin_to_dict(plugin: Plugin) -> dict[str, Any]:
    """A JSON-ready dict; `plugin_from_dict` reads it back to an equal `Plugin`."""
    return _to_json(plugin)  # type: ignore[no-any-return]


def _to_json(value: Any) -> Any:  # noqa: ANN401 - walks dataclasses of JSON-shaped values
    if isinstance(value, _DATACLASSES):
        return {
            f.name: _to_json(getattr(value, f.name))
            for f in fields(value)
            if not (isinstance(value, Draws) and f.name == "target" and value.target is None)
            and not (isinstance(value, EntityType) and f.name == "draws" and value.draws == Draws())
            and not (
                isinstance(value, EntityType) and f.name == "service" and value.service == Service()
            )
            and not (isinstance(value, Service) and getattr(value, f.name) is None)
            and not (isinstance(value, Plugin) and f.name == "formats" and not value.formats)
            and not (isinstance(value, Plugin) and f.name == "writers" and not value.writers)
            and not (
                f.name in {"reason_on_clear", "cel", "cel_reads"}
                and isinstance(value, Field)
                and not getattr(value, f.name)
            )
            and not (
                isinstance(value, RequestSpec)
                and f.name in {"materials", "completed_field", "completed_states"}
                and not getattr(value, f.name)
            )
            and not (
                isinstance(value, ResultSpec) and f.name == "material" and value.material is None
            )
            and not (
                isinstance(value, EntityType) and f.name == "refines_to" and not value.refines_to
            )
            and not (
                isinstance(value, EntityType) and f.name == "list_spec" and value.list_spec is None
            )
            and not (isinstance(value, EntityType) and f.name == "review" and value.review is None)
            and not (isinstance(value, Field) and f.name == "procedure" and not value.procedure)
            and not (
                isinstance(value, State)
                and f.name in {"maker", "independent_of"}
                and not getattr(value, f.name)
            )
            and not (isinstance(value, Plugin) and f.name == "procedures" and not value.procedures)
            and not (
                f.name == "customer_image_required"
                and isinstance(value, Environment)
                and not value.customer_image_required
            )
            and not (
                isinstance(value, Rule)
                and f.name in {"recheck_before_signing", "reference_revisions"}
                and not getattr(value, f.name)
            )
        }
    if isinstance(value, dict):
        return {k: _to_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_json(v) for v in value]
    return value


def _viewer(data: dict[str, Any]) -> Viewer:
    tracks = tuple(Track(**t) for t in data.get("tracks") or ())
    return Viewer(**{**data, "tracks": tracks})


def _export(data: dict[str, Any]) -> AnyExport:
    if "fields" in data:
        return PluginExport(export=data["export"], fields=dict(data["fields"]))
    if data["export"] == "signed_report":
        return ReportExport(**{**data, "issued": tuple(data["issued"])})
    if data["export"] == "protein":
        return ProteinExport(**data)
    if data["export"] == "compound":
        return CompoundExport(**data)
    return Export(**data)


def plugin_from_dict(data: dict[str, Any]) -> Plugin:
    return Plugin(
        **{
            **data,
            "settings": {k: _field(v) for k, v in data["settings"].items()},
            "types": {
                k: EntityType(
                    **{
                        **v,
                        "identity": tuple(v["identity"]),
                        "fields": {n: _field(f) for n, f in v["fields"].items()},
                        # Plugins built before viewers have none.
                        "viewers": tuple(_viewer(x) for x in v.get("viewers") or ()),
                        # ...or exports.
                        "exports": tuple(_export(x) for x in v.get("exports") or ()),
                        # ...or rules.
                        "rules": {n: _rule(r) for n, r in (v.get("rules") or {}).items()},
                        # ...or notifications.
                        "notifications": {
                            n: _notification(x) for n, x in (v.get("notifications") or {}).items()
                        },
                        # ...or requests.
                        "service": _service(v.get("service") or {}),
                        "stock_catalogue": _stock_catalogue(v["stock_catalogue"])
                        if v.get("stock_catalogue")
                        else None,
                        # ...or amounts (ADR 0139).
                        "amount": _amount(v["amount"]) if v.get("amount") else None,
                        # ...or storage (ADR 0180).
                        "storage": _storage(v.get("storage")),
                        "draws": _draws(v.get("draws") or {}),
                        # ...or refined rows (ADR 0164).
                        "refines": {
                            n: {r: _row_role(x) for r, x in roles.items()}
                            for n, roles in (v.get("refines") or {}).items()
                        },
                        # ...or refined references (ADR 0235).
                        "refines_to": {
                            n: tuple(to) for n, to in (v.get("refines_to") or {}).items()
                        },
                        # ...or lists (ADR 0270).
                        "list_spec": ListSpec(
                            options=tuple(dict(o) for o in v["list_spec"]["options"])
                        )
                        if v.get("list_spec")
                        else None,
                        # ...or reviews (ADR 0301).
                        "review": _review(v["review"]) if v.get("review") else None,
                    }
                )
                for k, v in data["types"].items()
            },
            "extensions": tuple(
                Extension(
                    **{
                        **e,
                        "fields": {n: _field(f) for n, f in e["fields"].items()},
                        "rules": {n: _rule(r) for n, r in (e.get("rules") or {}).items()},
                    }
                )
                for e in data["extensions"]
            ),
            "actions": {k: _action(v) for k, v in data["actions"].items()},
            "environments": {k: _environment(v) for k, v in data["environments"].items()},
            # Plugins built before workflows have none.
            "workflows": {k: _workflow(v) for k, v in (data.get("workflows") or {}).items()},
            # ...or triggers.
            "triggers": {
                k: Trigger(
                    **{
                        **v,
                        "fields": tuple(v["fields"]),
                        "state_field": v.get("state_field", ""),
                        "states": tuple(v.get("states", ())),
                        "watch_included": {
                            n: tuple(fields)
                            for n, fields in (v.get("watch_included") or {}).items()
                        },
                        # ...or related entities.
                        "related": {
                            n: Related(**{**r, "edges": tuple(r["edges"])})
                            for n, r in (v.get("related") or {}).items()
                        },
                    }
                )
                for k, v in (data.get("triggers") or {}).items()
            },
            # ...or components.
            "components": {k: _component(v) for k, v in (data.get("components") or {}).items()},
            # ...or format declarations.
            "formats": {
                k: FileFormat.read(v["id"], v, v["order"])
                for k, v in sorted(
                    data.get("formats", {}).items(), key=lambda item: (item[1]["order"], item[0])
                )
            },
            # ...or lab roles.
            "roles": {k: LabRole(**v) for k, v in (data.get("roles") or {}).items()},
            # ...or outside APIs.
            "apis": {k: outside_api(v) for k, v in (data.get("apis") or {}).items()},
            # ...or writers.
            "writers": {k: _writer(v) for k, v in (data.get("writers") or {}).items()},
            # ...or review procedures (ADR 0301).
            "procedures": {k: _procedure(v) for k, v in (data.get("procedures") or {}).items()},
        }
    )


def _exclusions(data: list[dict[str, Any]] | None) -> tuple[Exclusion, ...]:
    return tuple(
        Exclusion(
            **{
                **x,
                "states": tuple(x.get("states") or ()),
                "fields": tuple(x.get("fields") or ()),
                "window": tuple(x.get("window") or ()),
                "people": tuple(x.get("people") or ()),
            }
        )
        for x in data or ()
    )


def _procedure(data: dict[str, Any]) -> ReviewProcedure:
    return ReviewProcedure(
        **{
            **data,
            "slots": {k: Slot(**v) for k, v in data["slots"].items()},
            "states": {
                k: ReviewState(
                    **{
                        **v,
                        "set_by": tuple(v["set_by"]),
                        "signature": Signature(**v["signature"]) if v.get("signature") else None,
                        "maker": Maker(**v["maker"]) if v.get("maker") else None,
                        "independent_of": _exclusions(v.get("independent_of")),
                    }
                )
                for k, v in data["states"].items()
            },
            "flows": {
                k: ReviewFlow(
                    **{
                        **v,
                        "route": tuple(v["route"]),
                        "moves": {s: tuple(f) for s, f in (v.get("moves") or {}).items()},
                    }
                )
                for k, v in data["flows"].items()
            },
        }
    )


def _review(data: dict[str, Any]) -> ReviewSpec:
    needs = data["needs"]
    return ReviewSpec(
        **{
            **data,
            "needs": ReviewNeeds(
                **{
                    **needs,
                    "final": tuple(needs["final"]),
                    "states": tuple(needs["states"]),
                    "slots": tuple(needs["slots"]),
                }
            ),
            "independent_of": _exclusions(data.get("independent_of")),
            "guards": {
                n: TypeGuard(guard=_guards({n: g["guard"]})[n], on=tuple(g["on"]))
                for n, g in (data.get("guards") or {}).items()
            },
        }
    )


def _writer(data: dict[str, Any]) -> Writer:
    return Writer(
        **{
            **data,
            "roles": {
                k: WriterRole(**{**r, "to": tuple(r.get("to") or ())})
                for k, r in (data.get("roles") or {}).items()
            },
        }
    )


def _notification(data: dict[str, Any]) -> TypeNotification:
    return TypeNotification(
        **{
            **data,
            "to_values": tuple(data["to_values"]),
            "from_values": tuple(data["from_values"]),
            "notify_fields": tuple(data["notify_fields"]),
            "notify_roles": tuple(data["notify_roles"]),
        }
    )


def _request(data: dict[str, Any] | None) -> RequestSpec | None:
    if data is None:
        return None
    return RequestSpec(
        service_fields=tuple(data["service_fields"]),
        sends=tuple(data["sends"]),
        materials=tuple(data.get("materials") or ()),
        completed_field=data.get("completed_field"),
        completed_states=tuple(data.get("completed_states") or ()),
        receives=data.get("receives"),
        received_by=tuple(data.get("received_by") or ()),
    )


def _service(data: dict[str, Any]) -> Service:
    return Service(
        request=_request(data.get("request")),
        result=_result(data.get("result")),
        work_item=WorkItemSpec(**data["work_item"]) if data.get("work_item") else None,
    )


def _stock_catalogue(data: dict[str, Any]) -> StockCatalogue:
    return StockCatalogue(
        **{
            **data,
            "notify_roles": tuple(data["notify_roles"]),
            "lots": StockLots(**data["lots"]),
            "orders": StockOrders(**data["orders"]),
        }
    )


def _draws(data: dict[str, Any]) -> Draws:
    target = data.get("target")
    return Draws(
        guards=_guards(data.get("guards") or {}),
        purposes={n: tuple(roles) for n, roles in (data.get("purposes") or {}).items()},
        target=DrawTarget(
            **{
                **target,
                "states": tuple(target["states"]),
                "drawn_by": tuple(target["drawn_by"]),
            }
        )
        if target
        else None,
        received=data.get("received"),
    )


def _amount(data: dict[str, Any]) -> Amount:
    return Amount(**{**data, "follows": tuple(data.get("follows") or ())})


def _storage(data: dict[str, Any] | None) -> Storage | None:
    if not data:
        return None
    grid = data.get("grid")
    return Storage(
        holds=tuple(data.get("holds") or ()),
        grid=Grid(**grid) if grid else None,
        barcode=data.get("barcode"),
        label=data.get("label"),
        confirm=tuple(data.get("confirm") or ()),
    )


def _result(data: dict[str, Any] | None) -> ResultSpec | None:
    if data is None:
        return None
    return ResultSpec(**{**data, "released_states": tuple(data["released_states"])})


def _component(data: dict[str, Any]) -> Component:
    def slot(x: dict[str, Any]) -> ComponentSlot:
        search = x.get("search")
        return ComponentSlot(**{**x, "search": ComponentSearch(**search) if search else None})

    # Plugins built before ADR 0117 show only the entity whose page they're on, and those
    # built before ADR 0170 offer nothing to run.
    entities = data.get("entities")
    return Component(
        **{
            **data,
            "runs": tuple(data.get("runs", ())),
            "slots": tuple(slot(x) for x in data["slots"]),
            "entities": ComponentEntities(**{**entities, "fields": tuple(entities["fields"])})
            if entities
            else None,
        }
    )


def workflow_from_dict(data: dict[str, Any]) -> Workflow:
    """A workflow from its JSON form, as a workflow run keeps it."""
    return _workflow(data)


def _workflow(data: dict[str, Any]) -> Workflow:
    return Workflow(
        **{
            **data,
            "inputs": {n: _input(i) for n, i in data["inputs"].items()},
            "params": {n: _field(f) for n, f in data["params"].items()},
            "steps": {
                i: WorkflowStep(
                    **{
                        **step,
                        "needs": tuple(step["needs"]),
                        "when": tuple(Condition(**c) for c in step.get("when") or ()),
                        "approval": Approval(**step["approval"]) if step.get("approval") else None,
                    }
                )
                for i, step in data["steps"].items()
            },
        }
    )


def _input(data: dict[str, Any]) -> Input:
    return Input(**{**data, "include": tuple(data.get("include") or ())})


def action_from_dict(data: dict[str, Any]) -> Action:
    """An action from its JSON form (``dataclasses.asdict``), as a run keeps it."""
    return _action(data)


def _action(data: dict[str, Any]) -> Action:
    return Action(
        **{
            **data,
            "command": tuple(data["command"]),
            "inputs": {n: _input(i) for n, i in data["inputs"].items()},
            "params": {n: _field(f) for n, f in data["params"].items()},
            "creates": tuple(
                Creates(**{**c, "derived_from": tuple(c["derived_from"])}) for c in data["creates"]
            ),
            "updates": tuple(
                Updates(**{**u, "fields": tuple(u["fields"])}) for u in data["updates"]
            ),
            "resources": Resources(**data["resources"]),
            "artifacts": tuple(data["artifacts"]),
            "network": tuple(data["network"]),
            # Actions built before it need no settings.
            "requires_settings": tuple(data.get("requires_settings") or ()),
            "secrets": tuple(
                Secret(**s) if isinstance(s, dict) else Secret(s) for s in data["secrets"]
            ),
            "metrics": {n: Metric(**m) for n, m in (data.get("metrics") or {}).items()},
            # Actions built before outside APIs call none.
            "apis": tuple(api_use(u) for u in data.get("apis") or ()),
            "retry_on": tuple(data["retry_on"]),
            "software": tuple(Software(**w) for w in data.get("software") or ()),
        }
    )


def outside_api(data: dict[str, Any]) -> OutsideApi:
    """An outside API from its JSON form, as a plugin's manifest or a run keeps it."""
    jobs = data.get("jobs")
    return OutsideApi(
        **{
            **data,
            "credential": ApiCredential(**data["credential"]) if data.get("credential") else None,
            "parameters": tuple(data.get("parameters") or ()),
            "usage": dict(data.get("usage") or {}),
            "jobs": ApiJobs(
                **{
                    **jobs,
                    "done": tuple(jobs["done"]),
                    "failed": tuple(jobs["failed"]),
                    "usage": dict(jobs.get("usage") or {}),
                    "webhook": ApiWebhook(**jobs["webhook"]) if jobs.get("webhook") else None,
                }
            )
            if jobs
            else None,
        }
    )


def api_use(data: dict[str, Any]) -> ApiUse:
    return ApiUse(
        name=data["name"],
        inputs={n: tuple(f) for n, f in (data.get("inputs") or {}).items()},
        params=tuple(data.get("params") or ()),
        api=outside_api(data["api"]) if data.get("api") else None,
    )


def _environment(data: dict[str, Any]) -> Environment:
    return Environment(
        **{
            **data,
            "tiers": tuple(data["tiers"]),
            "labels": tuple(data.get("labels", ())),
            "resources": Resources(**data["resources"]),
            "volumes": {n: Volume(**v) for n, v in data["volumes"].items()},
            "setup_network": tuple(data["setup_network"]),
        }
    )


def _field(data: dict[str, Any]) -> Field:
    moved = data.get("moved_to")
    return Field(
        **{
            **data,
            "values": tuple(data["values"]),
            "media_types": tuple(data["media_types"]),
            # Plugins built before references to several types name one.
            "to": (data["to"],) if isinstance(data.get("to"), str) else tuple(data.get("to") or ()),
            "items": _field(data["items"]) if data["items"] is not None else None,
            "assignment_states": tuple(data.get("assignment_states") or ()),
            "cel_reads": tuple(data.get("cel_reads") or ()),
            # Plugins built before state fields have none.
            "states": {
                n: State(
                    **{
                        **st,
                        "set_by": tuple(st["set_by"]),
                        # Plugins built before signatures have none.
                        "signatures": tuple(Signature(**x) for x in st.get("signatures") or ()),
                        # ...or guards.
                        "guards": _guards(st.get("guards") or {}),
                        # ...or declared independence (ADR 0301).
                        "maker": Maker(**st["maker"]) if st.get("maker") else None,
                        "independent_of": _exclusions(st.get("independent_of")),
                    }
                )
                for n, st in (data.get("states") or {}).items()
            },
            "moved_to": StateMove(
                field=moved["field"], states=tuple((a, b) for a, b in moved["states"])
            )
            if moved
            else None,
            # Plugins built before rows (ADR 0137) have none.
            "fields": {n: _field(c) for n, c in (data.get("fields") or {}).items()},
            "identity": tuple(data.get("identity") or ()),
            # Plugins built before row IDs have none.
            "row_id": data.get("row_id"),
            "by": data.get("by"),
            "roles": {n: _row_role(r) for n, r in (data.get("roles") or {}).items()},
        }
    )


def _guards(data: dict[str, Any]) -> dict[str, Guard]:
    return {n: Guard(**{**g, "reads": tuple(g.get("reads") or ())}) for n, g in data.items()}


def _row_role(data: dict[str, Any]) -> RowRole:
    return RowRole(**{**data, "to": {c: tuple(t) for c, t in (data.get("to") or {}).items()}})


def _rule(data: dict[str, Any]) -> Rule:
    return Rule(
        **{
            **data,
            "reference_revisions": tuple((k, v) for k, v in data.get("reference_revisions") or ()),
            "unique": tuple(data.get("unique") or ()),
            "no_overlap": tuple(data.get("no_overlap") or ()),
            "per": tuple(data.get("per") or ()),
            "only_while": tuple((k, tuple(v)) for k, v in data.get("only_while") or ()),
            "hold": tuple(data.get("hold") or ()),
            "fields": tuple(data.get("fields") or ()),
            "reads": tuple(data.get("reads") or ()),
        }
    )
