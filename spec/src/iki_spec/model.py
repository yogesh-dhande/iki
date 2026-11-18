"""A plugin as Iki understands it, once its manifests have been read and checked.

Codegen (YAML → Django app) and the build read these objects, never the raw
YAML, so every rule `iki plugin validate` enforces holds for them.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass, field, fields
from typing import Any

from iki_spec import naming

__all__ = [
    "APPROVAL_ROLES",
    "BUILTIN_TYPES",
    "CONDITION_OPERATORS",
    "FIELD_TYPES",
    "FORMATS",
    "NUMERIC_OPERATORS",
    "STATE_COLUMN",
    "TIERS",
    "Action",
    "Approval",
    "Condition",
    "Creates",
    "EntityType",
    "Environment",
    "Extension",
    "Field",
    "Input",
    "LabRole",
    "Metric",
    "Plugin",
    "Resources",
    "State",
    "Transition",
    "Updates",
    "Volume",
    "Workflow",
    "WorkflowStep",
    "action_from_dict",
    "plugin_from_dict",
    "plugin_to_dict",
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
    computed: bool = False
    #: For a type with an identity: ``derived`` or ``fact``, a field of its registry entry
    #: rather than of each project's record (ADR 0115). None for a record's own field.
    registry: str | None = None
    indexed: bool = False
    #: No two entities in a project share a value (ADR 0082); for a registry fact, no two
    #: entries in a registry.
    unique: bool = False
    #: Where the value leads in another system: a URL with `{value}` (ADR 0082).
    link: str | None = None
    #: Gone from the type, but kept in its history (removed fields are never deleted).
    removed: bool = False
    has_default: bool = False
    default: Any = None
    format: str | None = None
    pattern: str | None = None
    min_length: int | None = None
    max_length: int | None = None
    min: float | None = None
    max: float | None = None
    values: tuple[str, ...] = ()
    unit: str | None = None
    display_unit: str | None = None
    media_types: tuple[str, ...] = ()
    max_size: int | None = None  # bytes
    to: str | None = None  # a qualified type name
    items: Field | None = None
    min_items: int | None = None
    max_items: int | None = None
    schema: dict[str, Any] | None = None

    @classmethod
    def from_manifest(cls, name: str, data: dict[str, Any], *, plugin: str) -> Field:
        items = data.get("items")
        to = data.get("to")
        return cls(
            name=name,
            type=data["type"],
            label=data.get("label") or name.replace("_", " ").capitalize(),
            description=data.get("description"),
            required=bool(data.get("required", False)),
            immutable=bool(data.get("immutable", False)),
            computed=bool(data.get("computed", False)),
            registry=data.get("registry"),
            indexed=bool(data.get("indexed", False)),
            unique=bool(data.get("unique", False)),
            link=data.get("link"),
            removed=bool(data.get("removed", False)),
            has_default="default" in data,
            default=data.get("default"),
            format=data.get("format"),
            pattern=data.get("pattern"),
            min_length=data.get("min_length"),
            max_length=data.get("max_length"),
            min=data.get("min"),
            max=data.get("max"),
            values=tuple(data.get("values", ())),
            unit=data.get("unit"),
            display_unit=data.get("display_unit"),
            media_types=tuple(data.get("media_types", ())),
            max_size=parse_size(data["max_size"]) if "max_size" in data else None,
            to=qualify(to, plugin) if isinstance(to, str) else None,
            items=cls.from_manifest(name, items, plugin=plugin)
            if isinstance(items, dict)
            else None,
            min_items=data.get("min_items"),
            max_items=data.get("max_items"),
            schema=data.get("schema"),
        )


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


@dataclass(frozen=True)
class State:
    """One of the states a type's records move through (ADR 0118)."""

    name: str
    label: str
    description: str | None = None
    initial: bool = False
    #: A record in it can't change until a transition takes it out.
    locks: bool = False
    #: No record is in it any more: its records moved to ``moved_to``.
    removed: bool = False
    moved_to: str | None = None


@dataclass(frozen=True)
class Transition:
    """A way from some states to another, made by someone with one of ``roles``."""

    name: str
    label: str
    from_states: tuple[str, ...]
    to: str
    #: Qualified lab roles (ADR 0116), any one of which may make it.
    roles: tuple[str, ...]
    description: str | None = None
    #: "required" or "optional": whether whoever makes it says why; None asks nothing.
    reason: str | None = None
    #: Separation of duties: whoever made the latest of these on the record can't make it.
    not_by: tuple[str, ...] = ()


#: The column a record's state is kept in, in its declaring type's table (ADR 0118).
STATE_COLUMN = "iki_state"


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
    file: str | None = None
    #: The states its records move through, and how (ADR 0118). Subtypes have their
    #: ancestor's: see :func:`state_machine`.
    states: dict[str, State] = field(default_factory=dict)
    transitions: dict[str, Transition] = field(default_factory=dict)

    @property
    def qualified_name(self) -> str:
        return f"{self.plugin}.{self.name}"

    @property
    def initial_state(self) -> str | None:
        return next((s.name for s in self.states.values() if s.initial), None)

    @property
    def live_states(self) -> tuple[str, ...]:
        """The states a record can be in: not the removed ones."""
        return tuple(s.name for s in self.states.values() if not s.removed)

    @property
    def locking_states(self) -> frozenset[str]:
        return frozenset(s.name for s in self.states.values() if s.locks and not s.removed)

    @property
    def table(self) -> str:
        return naming.type_table(self.qualified_name)

    @property
    def current_fields(self) -> dict[str, Field]:
        """The fields entities have now, without the removed ones."""
        return {name: f for name, f in self.fields.items() if not f.removed}


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
class Extension:
    plugin: str
    extends_type: str  # qualified
    fields: dict[str, Field]
    description: str | None = None
    file: str | None = None

    @property
    def table(self) -> str:
        return naming.extension_table(self.plugin, self.extends_type)

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
    schema: str | None = None  # a schema-version constraint
    cardinality: str = "one"
    max: int | None = None
    optional: bool = False
    label: str = ""
    description: str | None = None
    #: Its type's reference fields whose entities the run reads too.
    include: tuple[str, ...] = ()


@dataclass(frozen=True)
class Creates:
    type: str  # qualified
    cardinality: str = "one"
    derived_from: tuple[str, ...] = ()
    #: A unique field: a create with a value an entity in the project has updates it.
    match_on: str | None = None


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
    members beside their project role, and transitions and approvals can require it."""

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
#: registered (ADR 0115), a record moved to another state (ADR 0118), an entity changed,
#: or its schedule.
TRIGGER_EVENTS = (
    "entity.created",
    "entity.registered",
    "entity.transitioned",
    "entity.updated",
    "schedule",
)
#: The events that fire a trigger for an entity.
ENTITY_EVENTS = ("entity.created", "entity.registered", "entity.transitioned", "entity.updated")


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
    #: For entity.transitioned: it fires only when a record enters one of these; empty for
    #: any transition.
    states: tuple[str, ...] = ()
    #: The plugin's action it runs.
    run: str = ""
    #: The action's input that takes the entity: given, or the only one that takes its type.
    input: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    file: str | None = None

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
    #: The module's SHA-256, in hex, as the plugin was read: what the build copies.
    sha256: str = ""
    file: str | None = None

    @property
    def qualified_name(self) -> str:
        return f"{self.plugin}.{self.name}"


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
    Field,
    State,
    Transition,
    EntityType,
    Extension,
    Plugin,
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
    Trigger,
    Component,
    ComponentSlot,
    ComponentSearch,
    ComponentEntities,
    Viewer,
    Track,
    LabRole,
)


def plugin_to_dict(plugin: Plugin) -> dict[str, Any]:
    """A JSON-ready dict; `plugin_from_dict` reads it back to an equal `Plugin`."""
    return _to_json(plugin)  # type: ignore[no-any-return]


def _to_json(value: Any) -> Any:  # noqa: ANN401 - walks dataclasses of JSON-shaped values
    if isinstance(value, _DATACLASSES):
        return {f.name: _to_json(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {k: _to_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_json(v) for v in value]
    return value


def _viewer(data: dict[str, Any]) -> Viewer:
    tracks = tuple(Track(**t) for t in data.get("tracks") or ())
    return Viewer(**{**data, "tracks": tracks})


def _transition(data: dict[str, Any]) -> Transition:
    return Transition(
        **{
            **data,
            "from_states": tuple(data["from_states"]),
            "roles": tuple(data["roles"]),
            "not_by": tuple(data["not_by"]),
        }
    )


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
                        # ...or states.
                        "states": {n: State(**s) for n, s in (v.get("states") or {}).items()},
                        "transitions": {
                            n: _transition(t) for n, t in (v.get("transitions") or {}).items()
                        },
                    }
                )
                for k, v in data["types"].items()
            },
            "extensions": tuple(
                Extension(**{**e, "fields": {n: _field(f) for n, f in e["fields"].items()}})
                for e in data["extensions"]
            ),
            "actions": {k: _action(v) for k, v in data["actions"].items()},
            "environments": {k: _environment(v) for k, v in data["environments"].items()},
            # Plugins built before workflows have none.
            "workflows": {k: _workflow(v) for k, v in (data.get("workflows") or {}).items()},
            # ...or triggers.
            "triggers": {
                k: Trigger(
                    **{**v, "fields": tuple(v["fields"]), "states": tuple(v.get("states", ()))}
                )
                for k, v in (data.get("triggers") or {}).items()
            },
            # ...or components.
            "components": {k: _component(v) for k, v in (data.get("components") or {}).items()},
            # ...or lab roles.
            "roles": {k: LabRole(**v) for k, v in (data.get("roles") or {}).items()},
        }
    )


def _component(data: dict[str, Any]) -> Component:
    def slot(x: dict[str, Any]) -> ComponentSlot:
        search = x.get("search")
        return ComponentSlot(**{**x, "search": ComponentSearch(**search) if search else None})

    # Plugins built before ADR 0117 show only the entity whose page they're on.
    entities = data.get("entities")
    return Component(
        **{
            **data,
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
            "secrets": tuple(
                Secret(**s) if isinstance(s, dict) else Secret(s) for s in data["secrets"]
            ),
            "metrics": {n: Metric(**m) for n, m in (data.get("metrics") or {}).items()},
            "retry_on": tuple(data["retry_on"]),
            "software": tuple(Software(**w) for w in data.get("software") or ()),
        }
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
    return Field(
        **{
            **data,
            "values": tuple(data["values"]),
            "media_types": tuple(data["media_types"]),
            "items": _field(data["items"]) if data["items"] is not None else None,
        }
    )
