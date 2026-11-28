"""Read a plugin's manifests and check everything about them.

A plugin is a directory with `plugin.yaml` at its root, entity types under
`types/`, actions under `actions/` and environments under `environments/`
(any number of `.yaml` files in each, each holding one or more documents).
Checks run in two stages: each document against its JSON Schema, then the
rules no schema can express, across documents and plugins. Dependencies are
other plugins' directories, given with `--with`; references to them can only
be checked when they are there.
"""

from __future__ import annotations

import dataclasses
import json
import keyword
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path as FilePath
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from iki_spec import (
    API_VERSION,
    actions,
    apis,
    components,
    conditions,
    effective,
    helm,
    naming,
    notifications,
    procedures,
    releases,
    rules,
    schemas,
    titles,
    triggers,
    units,
    workflows,
)
from iki_spec.builtin import CORE_DIRECTORY, core_plugin, shipped_core
from iki_spec.formats import FormatRegistry
from iki_spec.identity import IDENTITY_TYPES, NUCLEIC_FORMATS, SEQUENCE_FORMATS
from iki_spec.model import (
    BUILTIN_TYPES,
    GRID_COLUMNS,
    GRID_POSITIONS,
    GRID_ROWS,
    GUARD_GROUPS,
    LIST_CODE,
    LIST_FIELDS,
    LIST_LABEL,
    LIST_LABEL_PATTERN,
    LIST_POSITION,
    Action,
    Amount,
    AnyExport,
    Component,
    CompoundExport,
    DrawTarget,
    EntityType,
    Environment,
    Export,
    Extension,
    Field,
    FileFormat,
    Grid,
    Guard,
    LabRole,
    ListSpec,
    Plugin,
    PluginExport,
    ProteinExport,
    Refinement,
    ReportExport,
    RequestSpec,
    ResultSpec,
    ReviewProcedure,
    ReviewSpec,
    RowRole,
    State,
    StockCatalogue,
    Storage,
    Track,
    Trigger,
    TypeNotification,
    Viewer,
    Workflow,
    WorkItemSpec,
    lab_role_name,
    list_fields,
    matches_format,
    names_people,
    qualify,
    split_entry,
)
from iki_spec.problems import Location, Path, Problem, Severity
from iki_spec.writers import manifest as writer_manifest
from iki_spec.yamlsource import Document, parse_documents, read_documents

__all__ = [
    "Result",
    "check_compiled_rules",
    "check_settings_values",
    "load_plugin",
    "read_settings",
]

PLUGIN_FILES = ("plugin.yaml", "plugin.yml")
TYPES_DIR = "types"

KINDS = {
    "Plugin": "plugin",
    "EntityType": "entity-type",
    "EntityTypeExtension": "entity-type-extension",
    "Action": "action",
    "Environment": "environment",
    "Workflow": "workflow",
    "Trigger": "trigger",
    "Component": "component",
    "ReviewProcedure": "review-procedure",
}

#: Where each kind of manifest lives in a plugin.
DIRECTORIES = {
    "EntityType": TYPES_DIR,
    "EntityTypeExtension": TYPES_DIR,
    "Action": "actions",
    "Environment": "environments",
    "Workflow": "workflows",
    "Trigger": "triggers",
    "Component": "components",
    "ReviewProcedure": "procedures",
}

MAX_IDENTIFIER = naming.MAX_IDENTIFIER

# B-tree index entries must fit in about a third of a page (2704 bytes). With
# up to 4 bytes per character, that is safely 600 characters.
MAX_INDEXED_LENGTH = 600

# The largest single object S3 stores.
MAX_FILE_SIZE = 5 * 1024**4

# Columns and attributes every entity table or model already has.
#: A row's table's own columns (ADR 0137), which its fields can't be named.
ROW_COLUMNS = frozenset({"entity", "entity_id", "position", "pk", "id"})

RESERVED_FIELD_NAMES = frozenset(
    {
        "id",
        "pk",
        "entity",
        "entity_id",
        "uuid",
        "hrid",
        "type",
        "project",
        "projects",
        "owner_project",
        "created_at",
        "created_by",
        "created_by_run",
        "updated_at",
        "updated_by",
        "archived",
        "archived_at",
        "archived_by",
        "identity_hash",
        "locked",
        "locked_by",
        "revision",
        "revisions",
        "schema_version",
        "history",
        "provenance",
        "links",
        "objects",
        # Django model methods.
        "save",
        "asave",
        "delete",
        "adelete",
        "clean",
        "clean_fields",
        "full_clean",
        "validate_unique",
        "validate_constraints",
        "refresh_from_db",
        "arefresh_from_db",
        "get_deferred_fields",
        "serializable_value",
        "prepare_database_save",
        "from_db",
        "check",
        "unique_error_message",
        "date_error_message",
        "get_constraints",
        # What a plugin's generated models.py names in its classes' bodies (iki_codegen):
        # a field so named would hide it from the fields after it.
        "models",
        "datetime",
        "checks",
    }
    | {k for k in keyword.kwlist if k.islower()}
    # What a search names on every entity (ADR 0037): a field so named couldn't be searched,
    # such as `held`, what a rule holds for review (ADR 0135).
    | set(conditions.BUILT_IN)
)

SECRET_WORDS = frozenset(
    {"password", "passwd", "secret", "secrets", "token", "apikey", "credential", "credentials"}
)
SECRET_PAIRS = (("api", "key"), ("private", "key"), ("access", "key"), ("client", "secret"))

INDEXABLE = frozenset(
    {
        "string",
        "int",
        "float",
        "bool",
        "enum",
        "state",
        "date",
        "datetime",
        "quantity",
        "ref",
        "person",
    }
)
TITLE_TYPES = frozenset(
    {"string", "text", "int", "float", "enum", "date", "datetime", "quantity", "bool"}
)


@dataclass
class Result:
    """What `load_plugin` found: the plugin (if its manifests could be read) and the problems."""

    directory: FilePath
    plugin: Plugin | None
    problems: list[Problem] = field(default_factory=list)

    @property
    def errors(self) -> list[Problem]:
        return [p for p in self.problems if p.is_error]

    @property
    def warnings(self) -> list[Problem]:
        return [p for p in self.problems if not p.is_error]

    @property
    def ok(self) -> bool:
        return not self.errors


class _Checker:
    def __init__(self, directory: FilePath) -> None:
        self.directory = directory
        self.problems: list[Problem] = []

    def add(
        self,
        code: str,
        message: str,
        source: Document | None = None,
        path: Path = (),
        *,
        hint: str | None = None,
        key: bool = False,
        severity: Severity = Severity.ERROR,
        file: str | None = None,
    ) -> None:
        self.problems.append(
            Problem(
                code=code,
                message=message,
                severity=severity,
                file=source.file if source else file,
                location=source.locate(path, key=key) if source else None,
                path=path,
                hint=hint,
            )
        )


# --- Loading -----------------------------------------------------------------------------------


def load_plugin(
    directory: FilePath | str,
    *,
    dependencies: Sequence[FilePath | str] = (),
    core: bool = False,
    require_setting_policy: bool = True,
    historical: bool = False,
) -> Result:
    """Read and check the plugin in `directory`, resolving references with `dependencies`.

    ``core`` is for :mod:`iki_spec.builtin` only: the core plugin itself, which
    has the reserved name and can't use its own types before they're read.
    An upgrade probe uses ``historical`` to build the earlier commit under the manifest
    rules it shipped with. Current builds enforce the setting and signed-state policies.
    """
    root = FilePath(directory)
    loaded = _read(root)
    if loaded.plugin is None:
        return loaded.result()
    others: dict[str, _Loaded] = {}
    for dependency in dependencies:
        other = _read(FilePath(dependency))
        where = str(dependency)
        errors = [p for p in other.checker.problems if p.is_error]
        if other.plugin is None or errors:
            unreadable = all(p.code in _NOT_A_PLUGIN for p in errors)
            loaded.checker.add(
                "dependency-invalid",
                f"`{where}` isn't a plugin Iki can read."
                if unreadable
                else f"The plugin in `{where}` has {len(errors)} error(s) of its own.",
                file=where,
                hint=f"Run `iki plugin validate {where}` and fix those first.",
            )
            continue
        if other.plugin.name == loaded.plugin.name:
            loaded.checker.add(
                "dependency-is-self",
                f"`{where}` is this plugin ({other.plugin.name}) again.",
                file=where,
            )
            continue
        others[other.plugin.name] = other
    _Semantics(loaded, others, core=core, historical=historical).run(
        require_setting_policy=require_setting_policy and not historical
    )
    return loaded.result()


def check_compiled_rules(plugin: Plugin, installed: Mapping[str, Plugin]) -> Plugin:
    """Recheck a compiled manifest's rule expressions and field references, without source files.

    Recompute the fields each rule reads. A compiled manifest may have come from an API
    caller, so those derived lists are no more authoritative than the expressions themselves.
    """
    path = FilePath("manifest.json")
    source = Document(path.name, 0, {}, Location(1, 1))
    loaded = _Loaded(
        path,
        _Checker(path),
        plugin,
        source,
        type_sources=dict.fromkeys(plugin.types, source),
        extension_sources=[source] * len(plugin.extensions),
    )
    others = {
        name: _Loaded(path, loaded.checker, other, source)
        for name, other in installed.items()
        if name != plugin.name
    }
    semantics = _Semantics(loaded, others, core=plugin.name == naming.CORE)

    def file_choices(fields: Mapping[str, Field], path: Path) -> None:
        for name, f in fields.items():
            at = (*path, name)
            semantics.file_choices(f, source, at)
            if f.items:
                semantics.file_choices(f.items, source, (*at, "items"))
                file_choices(f.items.fields, (*at, "items", "fields"))

    for name, t in plugin.types.items():
        file_choices(t.fields, ("types", name, "fields"))
    for i, extension in enumerate(plugin.extensions):
        file_choices(extension.fields, ("extensions", i, "fields"))
    for name, action in plugin.actions.items():
        file_choices(action.params, ("actions", name, "params"))
    types, extensions = rules.check(semantics)
    for problem in loaded.checker.problems:
        if problem.is_error:
            raise ValueError(f"{'.'.join(map(str, problem.path))}: {problem.message}")
    return dataclasses.replace(plugin, types=types, extensions=extensions)


_NOT_A_PLUGIN = frozenset({"not-a-directory", "missing-plugin-manifest"})


@dataclass
class _Loaded:
    directory: FilePath
    checker: _Checker
    plugin: Plugin | None = None
    plugin_source: Document | None = None
    type_sources: dict[str, Document] = field(default_factory=dict)
    invalid_types: set[str] = field(default_factory=set)
    extension_sources: list[Document] = field(default_factory=list)
    action_sources: dict[str, Document] = field(default_factory=dict)
    environment_sources: dict[str, Document] = field(default_factory=dict)
    workflow_sources: dict[str, Document] = field(default_factory=dict)
    trigger_sources: dict[str, Document] = field(default_factory=dict)
    component_sources: dict[str, Document] = field(default_factory=dict)
    procedure_sources: dict[str, Document] = field(default_factory=dict)
    # The raw (schema-valid) documents, for checks on things the model drops.
    raw: dict[int, dict[str, Any]] = field(default_factory=dict)

    def result(self) -> Result:
        problems = sorted(self.checker.problems, key=Problem.sort_key)
        # A plugin with errors isn't handed on: nothing should build from it.
        plugin = None if any(p.is_error for p in problems) else self.plugin
        return Result(self.directory, plugin, problems)


def _rel(root: FilePath, path: FilePath) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _read(root: FilePath) -> _Loaded:
    checker = _Checker(root)
    loaded = _Loaded(root, checker)
    if not root.is_dir():
        checker.add("not-a-directory", f"`{root}` isn't a directory.", file=str(root))
        return loaded
    manifest = next((root / n for n in PLUGIN_FILES if (root / n).is_file()), None)
    if manifest is None:
        checker.add(
            "missing-plugin-manifest",
            "There's no plugin.yaml here, so this isn't a plugin.",
            file="plugin.yaml",
            hint="Start one with `apiVersion: iki/v1`, `kind: Plugin`, a `name` and a `version`.",
        )
        return loaded
    documents = _documents(root, manifest, checker)
    plugin_docs = [d for d in documents if _kind(d) == "Plugin"]
    for document in documents:
        if _kind(document) != "Plugin":
            checker.add(
                "wrong-file",
                f"plugin.yaml holds only the Plugin manifest, not {_kind_phrase(document)}.",
                document,
                hint="Move it to a file under "
                f"{DIRECTORIES.get(_kind(document) or '', TYPES_DIR)}/.",
            )
    if len(plugin_docs) > 1:
        checker.add(
            "duplicate-plugin-manifest",
            "plugin.yaml has more than one Plugin manifest.",
            plugin_docs[1],
        )
    if not plugin_docs:
        if documents or not checker.problems:
            checker.add(
                "missing-plugin-manifest",
                "plugin.yaml doesn't hold a Plugin manifest.",
                file=_rel(root, manifest),
                hint="Start it with `apiVersion: iki/v1`, `kind: Plugin`, a `name` and a "
                "`version`.",
            )
        return loaded
    plugin_doc = plugin_docs[0]
    if not _schema_ok(plugin_doc, checker):
        return loaded
    data = plugin_doc.data
    name = data["name"]
    if "/" not in name:
        checker.add(
            "unqualified-name",
            f"`{name}` needs a namespace: plugins are named `namespace/name`.",
            plugin_doc,
            ("name",),
            hint=f"Name it under your lab's or company's namespace, such as `acme/{name}`. "
            "Iki's own plugins are in `iki`.",
        )
        return loaded
    loaded.plugin_source = plugin_doc
    if "iki" in data and not releases.fits(data["iki"]):
        checker.add(
            "iki-version",
            f"{name} works with Iki {data['iki'].strip()}, and this is Iki {releases.iki()}.",
            plugin_doc,
            ("iki",),
            hint=f"Use a release of {name} made for Iki {releases.iki()}, or deploy an Iki "
            "it works with.",
        )

    types: dict[str, EntityType] = {}
    extensions: list[Extension] = []
    for document in _manifests(root, TYPES_DIR, checker, invalid_types=loaded.invalid_types):
        body = document.data
        if _kind(document) == "EntityType":
            entity_type = _entity_type(name, body, document)
            if entity_type.qualified_name in types:
                first = loaded.type_sources[entity_type.qualified_name]
                checker.add(
                    "duplicate-type",
                    f"`{entity_type.name}` is defined twice "
                    f"(first in {first.file}, line {first.start.line}).",
                    document,
                    ("name",),
                )
                continue
            types[entity_type.qualified_name] = entity_type
            loaded.type_sources[entity_type.qualified_name] = document
        else:
            extensions.append(
                Extension(
                    plugin=name,
                    extends_type=qualify(body["extends_type"], name),
                    fields=_fields(body.get("fields", {}), name),
                    description=body.get("description"),
                    file=document.file,
                    rules=rules.read(body.get("rules"), name, extension=True),
                )
            )
            loaded.extension_sources.append(document)
        loaded.raw[id(document)] = body

    environments: dict[str, Environment] = {}
    for document in _manifests(root, "environments", checker):
        environment = actions.read_environment(name, document.data, document)
        if _unique(
            "duplicate-environment",
            environment.name,
            environments,
            loaded.environment_sources,
            document,
            checker,
        ):
            environments[environment.name] = environment
    found: dict[str, Action] = {}
    for document in _manifests(root, "actions", checker):
        action = actions.read_action(name, document.data, document)
        if _unique(
            "duplicate-action", action.name, found, loaded.action_sources, document, checker
        ):
            found[action.name] = action
    flows: dict[str, Workflow] = {}
    for document in _manifests(root, "workflows", checker):
        workflow = workflows.read_workflow(name, document.data, document)
        if _unique(
            "duplicate-workflow",
            workflow.name,
            flows,
            loaded.workflow_sources,
            document,
            checker,
        ):
            flows[workflow.name] = workflow
    fires: dict[str, Trigger] = {}
    for document in _manifests(root, "triggers", checker):
        trigger = triggers.read_trigger(name, document.data, document)
        if _unique(
            "duplicate-trigger",
            trigger.name,
            fires,
            loaded.trigger_sources,
            document,
            checker,
        ):
            fires[trigger.name] = trigger
    shown: dict[str, Component] = {}
    for document in _manifests(root, "components", checker):
        component = components.read_component(name, document.data, document)
        if _unique(
            "duplicate-component",
            component.name,
            shown,
            loaded.component_sources,
            document,
            checker,
        ):
            shown[component.name] = component

    found_procedures: dict[str, ReviewProcedure] = {}
    for document in _manifests(root, "procedures", checker):
        procedure = procedures.read_procedure(name, document.data, document.file)
        if _unique(
            "duplicate-procedure",
            procedure.qualified_name,
            found_procedures,
            loaded.procedure_sources,
            document,
            checker,
        ):
            found_procedures[procedure.qualified_name] = procedure

    roles = {
        f"{name}.{role}": LabRole(
            plugin=name,
            name=role,
            label=spec["label"].strip() or role,
            description=(spec.get("description") or "").strip() or None,
        )
        for role, spec in (data.get("roles") or {}).items()
    }
    loaded.plugin = Plugin(
        name=name,
        version=data["version"],
        title=data.get("title") or name.replace("_", " ").capitalize(),
        description=data.get("description"),
        license=data.get("license"),
        homepage=data.get("homepage"),
        iki=data.get("iki"),
        dependencies={
            plugin: dict(spec.get("types", {}))
            for plugin, spec in data.get("dependencies", {}).items()
        },
        settings=_fields(data.get("settings", {}), name),
        types=types,
        extensions=tuple(extensions),
        actions=found,
        environments=environments,
        workflows=flows,
        triggers=fires,
        components=shown,
        roles=roles,
        formats={
            f"{name}.{local}": FileFormat.read(f"{name}.{local}", spec, order)
            for order, (local, spec) in enumerate(data.get("formats", {}).items())
        },
        starters=dict(data.get("starters", {})),
        apis=apis.read_apis(name, data),
        writers=writer_manifest.read_writers(name, data, root),
        procedures=found_procedures,
    )
    # Each action's uses of an outside API, with the API as the plugin declares it.
    loaded.plugin = apis.resolve(loaded.plugin)
    return loaded


def _manifests(
    root: FilePath, directory: str, checker: _Checker, *, invalid_types: set[str] | None = None
) -> list[Document]:
    """The schema-valid manifests under `directory`, after reporting any that belong elsewhere."""
    usable = []
    for file in _files(root, directory):
        for document in _documents(root, file, checker):
            kind = _kind(document)
            if kind not in KINDS:
                continue
            if DIRECTORIES.get(kind) != directory:
                here = sorted(
                    {_kind_phrase_plural(k) for k, d in DIRECTORIES.items() if d == directory}
                )
                checker.add(
                    "wrong-file",
                    f"Files under {directory}/ hold {' and '.join(here)}, not "
                    f"{_kind_phrase(document)}.",
                    document,
                    ("kind",),
                    hint="The Plugin manifest belongs in plugin.yaml."
                    if kind == "Plugin"
                    else f"Move it to a file under {DIRECTORIES[kind]}/.",
                )
                continue
            if _schema_ok(document, checker):
                usable.append(document)
            elif (
                invalid_types is not None
                and kind == "EntityType"
                and isinstance(name := document.data.get("name"), str)
            ):
                invalid_types.add(name)
    return usable


def _unique(
    code: str,
    name: str,
    found: dict[str, Any],
    sources: dict[str, Document],
    document: Document,
    checker: _Checker,
) -> bool:
    if name in found:
        first = sources[name]
        checker.add(
            code,
            f"`{name}` is defined twice (first in {first.file}, line {first.start.line}).",
            document,
            ("name",),
        )
        return False
    sources[name] = document
    return True


def _documents(root: FilePath, file: FilePath, checker: _Checker) -> list[Document]:
    name = _rel(root, file)
    if file.is_symlink():
        checker.add(
            "symlink",
            "Manifests can't be symbolic links.",
            file=name,
            hint="Copy the file into the plugin instead, so what's reviewed is what's built.",
        )
        return []
    documents, problems = read_documents(file, name)
    checker.problems.extend(problems)
    usable = []
    for document in documents:
        if document.data is None:
            continue  # an empty document, such as a trailing `---`
        if not isinstance(document.data, dict):
            checker.add(
                "not-a-mapping", "A manifest must be a mapping of keys to values.", document
            )
            continue
        if not _header_ok(document, checker):
            continue
        usable.append(document)
    return usable


def _files(root: FilePath, name: str) -> list[FilePath]:
    directory = root / name
    if not directory.is_dir():
        return []
    found = [
        p
        for p in directory.rglob("*")
        if p.suffix in (".yaml", ".yml")
        and not any(part.startswith(".") for part in p.relative_to(root).parts)
        and (p.is_file() or p.is_symlink())
    ]
    return sorted(found, key=lambda p: p.relative_to(root).as_posix())


def _kind(document: Document) -> str | None:
    kind = document.data.get("kind") if isinstance(document.data, dict) else None
    return kind if isinstance(kind, str) else None


def _kind_phrase(document: Document) -> str:
    kind = _kind(document)
    return {
        "EntityType": "an entity type",
        "EntityTypeExtension": "an extension",
        "Action": "an action",
        "Environment": "an environment",
        "Workflow": "a workflow",
        "Trigger": "a trigger",
        "Component": "a component",
        "ReviewProcedure": "a review procedure",
    }.get(kind or "", f"a {kind}")


def _kind_phrase_plural(kind: str) -> str:
    return {
        "EntityType": "entity types",
        "EntityTypeExtension": "extensions",
        "Action": "actions",
        "Environment": "environments",
        "Workflow": "workflows",
        "Trigger": "triggers",
        "Component": "components",
        "ReviewProcedure": "review procedures",
    }[kind]


def _header_ok(document: Document, checker: _Checker) -> bool:
    data = document.data
    version = data.get("apiVersion")
    kind = data.get("kind")
    if version is None:
        checker.add(
            "missing-api-version",
            "`apiVersion` is missing.",
            document,
            hint=f"Start each manifest with `apiVersion: {API_VERSION}`.",
        )
        return False
    if version != API_VERSION:
        checker.add(
            "unsupported-api-version",
            f"This Iki reads `{API_VERSION}` manifests, not `{version}`.",
            document,
            ("apiVersion",),
            hint="`iki plugin upgrade` moves manifests to a newer version."
            if isinstance(version, str) and version.startswith("iki/")
            else None,
        )
        return False
    if kind is None:
        checker.add(
            "missing-kind",
            "`kind` is missing.",
            document,
            hint="Say what this manifest is: " + ", ".join(f"`{k}`" for k in KINDS) + ".",
        )
        return False
    if kind not in KINDS:
        close = schemas._did_you_mean(str(kind), list(KINDS))
        checker.add(
            "unknown-kind",
            f"`{kind}` isn't a kind of manifest {API_VERSION} has.",
            document,
            ("kind",),
            hint=close or "Kinds: " + ", ".join(f"`{k}`" for k in KINDS) + ".",
        )
        return False
    return True


def _schema_ok(document: Document, checker: _Checker) -> bool:
    issues = schemas.check(KINDS[document.data["kind"]], document.data)
    for issue in issues:
        checker.add(
            "schema", issue.message, document, issue.path, hint=issue.hint, key=issue.at_key
        )
    return not issues


def _entity_type(plugin: str, body: dict[str, Any], document: Document) -> EntityType:
    hrid = body.get("hrid") or {}
    name = body["name"]
    fields = _fields(body.get("fields", {}), plugin)
    listed = body.get("list")
    list_spec: ListSpec | None = None
    identity = tuple(body.get("identity", ()))
    title = (body.get("display") or {}).get("title")
    review = ReviewSpec.read(body.get("review"))
    if review is not None:
        # A declared field of the same name is refused by the semantic checks.
        fields = {**fields, **{n: f for n, f in _review_fields(review).items() if n not in fields}}
    if listed is True or isinstance(listed, dict):
        # A lab list (ADR 0270): its options' code is their identity, and their label,
        # description and place in the list are facts, as are the type's other fields.
        options = listed.get("options") if isinstance(listed, dict) else None
        list_spec = ListSpec(
            options=tuple(o for o in options if isinstance(o, dict))
            if isinstance(options, list)
            else ()
        )
        own = {
            n: f if f.registry is not None else dataclasses.replace(f, registry="fact")
            for n, f in fields.items()
            if n not in LIST_FIELDS
        }
        fields = {**list_fields(), **own}
        identity = identity or (LIST_CODE,)
        title = title or "{{ label }}"
    return EntityType(
        plugin=plugin,
        name=name,
        schema_version=body["schema_version"],
        label=body.get("label") or _words(name),
        # "DNA sequence" → "DNA sequences": from its label, which may be worded unlike its name.
        label_plural=body.get("label_plural") or (body.get("label") or _words(name)) + "s",
        description=body.get("description"),
        extends=qualify(body["extends"], plugin) if "extends" in body else None,
        abstract=bool(body.get("abstract", False)),
        sample=bool(body.get("sample", False)),
        hrid_prefix=hrid.get("prefix"),
        hrid_digits=hrid.get("digits", 6),
        identity=identity,
        fields=fields,
        display_title=title,
        viewers=tuple(_viewer(v) for v in (body.get("display") or {}).get("viewers") or ()),
        exports=tuple(_export(x, plugin) for x in body.get("exports") or ()),
        file=document.file,
        due=body.get("due"),
        rules=rules.read(body.get("rules"), plugin),
        draw_guards={
            n: Guard.from_manifest(n, g) for n, g in (body.get("draw_guards") or {}).items()
        },
        draw_purposes={
            n: tuple(lab_role_name(role, plugin) for role in policy["by"])
            for n, policy in (body.get("draw_purposes") or {}).items()
        },
        draw_target=_draw_target(body.get("draw_target"), plugin),
        notifications={
            n: TypeNotification.from_manifest(n, x, plugin=plugin)
            for n, x in (body.get("notifications") or {}).items()
            if isinstance(x, dict)
        },
        request=_request(body.get("request"), plugin),
        work_item=WorkItemSpec(**body["work_item"]) if "work_item" in body else None,
        result=ResultSpec(
            of=body["result"]["of"],
            material=body["result"].get("material"),
            released_field=body["result"]["released"]["field"],
            released_states=tuple(body["result"]["released"]["states"]),
        )
        if isinstance(body.get("result"), dict)
        else None,
        amount=_amount(body.get("amount"), plugin),
        draws_received=body.get("draws_received"),
        stock_catalogue=StockCatalogue.read(body["stock_catalogue"], plugin)
        if body.get("stock_catalogue")
        else None,
        storage=_storage(body.get("storage"), plugin),
        roles=dict(body.get("roles") or {}),
        refines={
            name: Refinement.from_manifest(body_refines, plugin=plugin)
            for name, body_refines in (body.get("refines") or {}).items()
            if isinstance(body_refines, dict)
        },
        list_spec=list_spec,
        review=review,
    )


def _review_fields(review: ReviewSpec) -> dict[str, Field]:
    """The fields Iki adds to a reviewed type (ADRs 0301, 0302): the state field its pinned
    procedure drives, which names only the states the type needs, and the procedure and
    flow the server stamps at create."""
    out: dict[str, Field] = {}
    if not review.opens:
        named = review.needs.named
        out[review.field] = Field(
            name=review.field,
            type="state",
            label=review.label,
            description="The record's review, under the procedure and flow pinned on it.",
            states={s: State(name=s, label=s.replace("_", " ").capitalize()) for s in named},
            initial=review.needs.open,
            procedure=True,
        )
    out["review_procedure"] = Field(
        name="review_procedure",
        type="string",
        label="Review procedure",
        description="The review procedure pinned on the record when it was created.",
        computed=True,
        immutable=True,
        indexed=True,
        searchable=False,
        max_length=160,
    )
    out["review_flow"] = Field(
        name="review_flow",
        type="string",
        label="Review flow",
        description="The procedure's flow pinned on the record when it was created.",
        computed=True,
        immutable=True,
        indexed=True,
        searchable=False,
        max_length=50,
    )
    return out


def _draw_target(data: dict[str, Any] | None, plugin: str) -> DrawTarget | None:
    if data is None:
        return None
    return DrawTarget(
        from_field=data["from"],
        source=data.get("source"),
        state_field=data["state"]["field"],
        state=data["state"]["is"],
        by=tuple(lab_role_name(role, plugin) for role in data.get("by", [])),
        ready=data.get("ready"),
        amount=data.get("amount"),
        stored=data.get("stored", False),
        unit=data.get("unit"),
    )


def _amount(data: Any, plugin: str) -> Amount | None:  # noqa: ANN401 - YAML
    if not isinstance(data, dict):
        return None
    aliquots = data.get("aliquots") or {}
    return Amount(
        unit=data["unit"],
        label=data.get("label") or "Amount",
        description=data.get("description"),
        display_unit=data.get("display_unit"),
        concentration=data.get("concentration"),
        aliquots=qualify(aliquots["type"], plugin) if "type" in aliquots else None,
        aliquots_of=aliquots.get("of"),
        material=data.get("material"),
        follows=tuple(data.get("follows") or ()),
        whole_units=data.get("whole_units", False),
    )


def _request(data: Any, plugin: str) -> RequestSpec | None:  # noqa: ANN401 - YAML
    if not isinstance(data, dict):
        return None
    given = data.get("receives")
    receives: dict[str, Any] = given if isinstance(given, dict) else {}
    return RequestSpec(
        service_fields=tuple(data.get("service_fields") or ()),
        sends=tuple(data.get("sends") or ()),
        materials=tuple(data.get("materials") or ()),
        completed_field=(data.get("completed") or {}).get("field"),
        completed_state=(data.get("completed") or {}).get("state"),
        receives=receives.get("field"),
        receives_by=tuple(lab_role_name(role, plugin) for role in receives.get("by") or ()),
    )


def _storage(data: Any, plugin: str) -> Storage | None:  # noqa: ANN401 - YAML
    if not isinstance(data, dict):
        return None
    grid = data.get("grid")
    return Storage(
        holds=tuple(qualify(t, plugin) for t in data.get("holds") or ()),
        grid=Grid(
            rows=grid["rows"],
            columns=grid["columns"],
            labels=grid.get("labels", "letter_number"),
            order=grid.get("order", "by_row"),
        )
        if isinstance(grid, dict)
        else None,
        barcode=data.get("barcode"),
        label=data.get("label"),
        confirm=tuple(data.get("confirm") or ()),
    )


#: Media top-level types whose dotted subtypes are literal MIME choices, alongside the
#: long-standing chemical community tree. A qualified, declared format ID wins first.
_MEDIA_TREES = frozenset(
    [
        "application",
        "audio",
        "chemical",
        "example",
        "font",
        "haptics",
        "image",
        "message",
        "model",
        "multipart",
        "text",
        "video",
    ]
)


#: What each viewer's fields must be: (role, as it reads, test).
_VIEWER_ROLES: dict[str, tuple[tuple[str, str, Callable[[Field], bool]], ...]] = {
    "sequence": (
        (
            "sequence",
            "a DNA, RNA or protein sequence",
            lambda f: (
                f.type == "string"
                and f.format in ("dna_sequence", "rna_sequence", "protein_sequence")
            ),
        ),
        ("features", "a json field", lambda f: f.type == "json"),
    ),
    "structure": (
        (
            "structure",
            "a file field of mmCIF or PDB files (media types chemical/x-mmcif or chemical/x-pdb)",
            lambda f: f.type == "file" and bool(f.media_types),
        ),
    ),
    "alignment": (
        (
            "alignment",
            "a file field of FASTA files (media types chemical/seq-aa-fasta, "
            "chemical/seq-na-fasta or text/plain)",
            lambda f: f.type == "file" and bool(f.media_types),
        ),
        (
            "rows",
            "a list of references",
            lambda f: f.type == "list" and f.items is not None and f.items.type == "ref",
        ),
        ("reference", "a reference", lambda f: f.type == "ref"),
    ),
}

#: Each viewer's name, when a type doesn't give it one.
VIEWER_LABELS = {"sequence": "Sequence", "structure": "Structure", "alignment": "Alignment"}


def _export(body: dict[str, Any], plugin: str) -> AnyExport:
    if "fields" in body:
        # A plugin's writer (ADR 0230): one of this plugin's by its local name, or qualified.
        return PluginExport(export=qualify(body["export"], plugin), fields=dict(body["fields"]))
    if body["export"] == "signed_report":
        return ReportExport(body["export"], body["report"], body["review"], tuple(body["issued"]))
    if body["export"] == "protein":
        return ProteinExport(
            export="protein",
            sequence=body["sequence"],
            description=body.get("description", ""),
            organism=body.get("organism", ""),
        )
    if body["export"] == "compound":
        return CompoundExport(export="compound", structure=body["structure"])
    return Export(
        export=body["export"],
        sequence=body["sequence"],
        features=body.get("features", ""),
        description=body.get("description", ""),
        accession=body.get("accession", ""),
        organism=body.get("organism", ""),
    )


def _viewer(body: dict[str, Any]) -> Viewer:
    kind = body["viewer"]
    return Viewer(
        viewer=kind,
        label=body.get("label") or VIEWER_LABELS[kind],
        sequence=body.get("sequence", ""),
        features=body.get("features", ""),
        structure=body.get("structure", ""),
        alignment=body.get("alignment", ""),
        rows=body.get("rows", ""),
        reference=body.get("reference", ""),
        tracks=tuple(
            Track(field=t["field"], label=t.get("label", "")) for t in body.get("tracks") or ()
        ),
    )


def per_residue(field: Field) -> bool:
    """Whether a field holds one number per residue, as a track shows: a json field whose
    schema is an array of numbers, or of numbers and nulls."""
    schema = field.schema or {}
    items = schema.get("items")
    if field.type != "json" or schema.get("type") != "array" or not isinstance(items, dict):
        return False
    kinds = items.get("type")
    kinds = {kinds} if isinstance(kinds, str) else set(kinds or ())
    return bool(kinds & {"number", "integer"}) and kinds <= {"number", "integer", "null"}


def _words(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)


def _fields(data: dict[str, Any], plugin: str) -> dict[str, Field]:
    return {name: Field.from_manifest(name, spec, plugin=plugin) for name, spec in data.items()}


# --- Rules across documents and plugins --------------------------------------------------------


class _Semantics:
    def __init__(
        self,
        loaded: _Loaded,
        others: dict[str, _Loaded],
        *,
        core: bool = False,
        historical: bool = False,
    ) -> None:
        assert loaded.plugin is not None and loaded.plugin_source is not None  # noqa: S101 - an invariant, not a check
        self.loaded = loaded
        self.plugin: Plugin = loaded.plugin
        self.source: Document = loaded.plugin_source
        self.others = others
        self.checker = loaded.checker
        self.add = loaded.checker.add
        self.core = core
        self.historical = historical
        self.types: dict[str, EntityType] = {} if core else dict(BUILTIN_TYPES)
        for other in others.values():
            assert other.plugin is not None  # noqa: S101 - an invariant, not a check
            self.types.update(other.plugin.types)
        self.types.update(self.plugin.types)
        invalid = {qualify(n, self.plugin.name) for n in loaded.invalid_types} - self.types.keys()
        self.invalid_types = invalid
        if invalid:
            # A subtype's inherited fields can't be checked until its parent can be read.
            self.invalid_types |= {
                t.qualified_name
                for t in self.plugin.types.values()
                if any(parent.extends in invalid for parent in [t, *self.ancestors(t)])
            }
        self.stock_lots = {
            t.stock_catalogue.lots.type: t for t in self.types.values() if t.stock_catalogue
        }
        #: Lab roles it can require: its own, and its dependencies' (ADR 0116).
        self.roles: dict[str, LabRole] = {}
        for other in others.values():
            assert other.plugin is not None  # noqa: S101 - an invariant, not a check
            self.roles.update(other.plugin.roles)
        self.roles.update(self.plugin.roles)
        # An upgrade probe can load a core from before format declarations. Its original
        # viewers still recognize the MIME types declared in today's shipped core.
        installed = {n: p.plugin for n, p in others.items() if p.plugin is not None}
        installed[self.plugin.name] = self.plugin
        if not core:
            installed.setdefault(naming.CORE, core_plugin())
        if (
            (base := installed.get(naming.CORE))
            and not base.formats
            and loaded.directory != CORE_DIRECTORY
        ):
            installed[naming.CORE] = shipped_core()
        self.formats: FormatRegistry | None
        try:
            self.formats = FormatRegistry(installed.values())
        except ValueError as error:
            self.add("format-conflict", str(error), self.source, ("formats",))
            self.formats = None
        self.unavailable: set[str] = set()
        self.used: set[str] = set()

    def run(self, *, require_setting_policy: bool) -> None:
        self.plugin_manifest()
        self.lab_roles()
        for qualified, entity_type in self.plugin.types.items():
            if qualified not in self.invalid_types:
                self.entity_type(entity_type, self.loaded.type_sources[qualified])
        self.hrid_prefixes()
        self.table_names()
        seen: dict[str, Document] = {}
        for extension, document in zip(
            self.plugin.extensions, self.loaded.extension_sources, strict=True
        ):
            self.extension(extension, document, seen)
        self.settings(require_policy=require_setting_policy)
        self.procedures()
        self.declared_independence()
        # With what each rule and guard reads, for the server to load and point at.
        types, extensions = rules.check(self)
        self.plugin = self.loaded.plugin = dataclasses.replace(
            self.plugin, types=types, extensions=extensions
        )
        actions.check(self)
        apis.check(self)
        writer_manifest.check(self)
        workflows.check(self)
        # With the input each gives its entity, where it was left out and only one fits.
        self.loaded.plugin = dataclasses.replace(self.plugin, triggers=triggers.check(self))
        # With each module's digest.
        self.loaded.plugin = dataclasses.replace(
            self.loaded.plugin, components=components.check(self)
        )
        self.unpinned()

    # The plugin manifest ------------------------------------------------------------------------

    def plugin_manifest(self) -> None:
        plugin, source = self.plugin, self.source
        namespace, short = naming.split(plugin.name)
        # A plugin's app is ``namespace__name``, which no app of Iki's or Django's is, so
        # any name is free but Iki's own namespace and ones that pass for it (ADR 0113).
        if naming.is_reserved(namespace) and not (
            plugin.name in naming.FIRST_PARTY and (self.core or plugin.name != naming.CORE)
        ):
            self.add(
                "reserved-namespace",
                f"The namespace `{namespace}` is reserved: `{naming.IKI_NAMESPACE}` is Iki's "
                "own, for the plugins in Iki's repository, and no other namespace may start "
                f"with `{naming.IKI_NAMESPACE}_`.",
                source,
                ("name",),
                hint=f"Name it under your lab's or company's namespace, such as `acme/{short}`.",
            )
        if plugin.starters and plugin.name not in naming.FIRST_PARTY:
            # A starter is a recipe the server has (ADR 0198); only Iki's own plugins'
            # are in it, so another plugin's `starters` switch nothing on.
            self.add(
                "unknown-starter",
                "Iki has no starter "
                + ", ".join(f"`{s}`" for s in plugin.starters)
                + ": only Iki's own plugins' starters can be offered at project setup, so "
                "this does nothing.",
                source,
                ("starters",),
                key=True,
                severity=Severity.WARNING,
                hint="Leave `starters` off. To let each deployment choose a review procedure, "
                "declare an `enum` setting of the procedures and copy it onto each result "
                "when it's recorded, as the lots plugin's `verification_flow` is.",
            )
        for dependency, constraints in plugin.dependencies.items():
            path: Path = ("dependencies", dependency)
            if "/" not in dependency:
                self.add(
                    "unqualified-dependency",
                    f"`{dependency}` needs its namespace.",
                    source,
                    path,
                    key=True,
                    hint=f"Write `{self._qualified(dependency)}`.",
                )
                continue
            if dependency == plugin.name:
                self.add(
                    "dependency-is-self", "A plugin can't depend on itself.", source, path, key=True
                )
                continue
            other = self.others.get(dependency)
            if other is None or other.plugin is None:
                core_names = {t.name for t in BUILTIN_TYPES.values()}
                if dependency == naming.CORE and set(constraints) <= core_names:
                    continue
                self.unavailable.add(dependency)
                self.add(
                    "dependency-not-available",
                    f"Iki can't check what this plugin uses from `{dependency}` without it.",
                    source,
                    path,
                    key=True,
                    hint=f"Pass the {dependency} plugin's directory with "
                    f"`--with path/to/{naming.short_name(dependency)}`.",
                )
                continue
            if plugin.name in other.plugin.dependencies:
                self.add(
                    "dependency-cycle",
                    f"`{dependency}` depends on this plugin, and this plugin on `{dependency}`.",
                    source,
                    path,
                    key=True,
                    hint="Move what both need into one of them, or into a third plugin.",
                )
            for type_name, constraint in constraints.items():
                qualified = f"{dependency}.{type_name}"
                target = other.plugin.types.get(qualified)
                if target is None:
                    self.add(
                        "unknown-type",
                        f"The {dependency} plugin has no type `{type_name}`.",
                        source,
                        (*path, "types", type_name),
                        key=True,
                        hint=self._suggest(type_name, other.plugin.types),
                    )
                    continue
                if not satisfies(target.schema_version, constraint):
                    self.add(
                        "dependency-version",
                        f"`{qualified}` is at schema version {target.schema_version}, which "
                        f"doesn't match `{constraint}`.",
                        source,
                        (*path, "types", type_name),
                        hint="Update the constraint once this plugin works with that version.",
                    )

    def lab_roles(self) -> None:
        """Its own lab roles: two with one label couldn't be told apart where they're given."""
        seen: dict[str, str] = {}
        for role in self.plugin.roles.values():
            key = role.label.casefold()
            if key in seen:
                self.add(
                    "duplicate-role-label",
                    f"`{role.name}` and `{seen[key]}` are both called “{role.label}”.",
                    self.source,
                    ("roles", role.name, "label"),
                    hint="Give each lab role a label of its own: Admins choose them by it.",
                )
                continue
            seen[key] = role.name

    def lab_role(self, ref: str, source: Document, path: Path) -> LabRole | None:
        """The lab role `ref` names, qualified (``acme/qms.qa``), or None after reporting why
        it can't be used: its plugin's own, or a dependency's (ADR 0116)."""
        plugin, _, name = ref.rpartition(".")
        if ref in self.roles:
            if plugin != self.plugin.name and plugin not in self.plugin.dependencies:
                self.add(
                    "undeclared-dependency",
                    f"`{ref}` belongs to the {plugin} plugin, which plugin.yaml doesn't list "
                    "under `dependencies`.",
                    source,
                    path,
                    hint=f"Add `{plugin}: {{}}` under `dependencies`.",
                )
                return None
            return self.roles[ref]
        if plugin != self.plugin.name and plugin not in self.plugin.dependencies:
            self.add(
                "undeclared-dependency",
                f"`{ref}` belongs to the {plugin} plugin, which plugin.yaml doesn't list under "
                "`dependencies`.",
                source,
                path,
                hint=f"Add `{plugin}: {{}}` under `dependencies`, and pass its directory with "
                "`--with`.",
            )
            return None
        if plugin in self.unavailable:
            return None  # reported once, on the dependency
        other = self.others.get(plugin)
        owner = self.plugin if plugin == self.plugin.name else (other.plugin if other else None)
        if owner is None:
            self.add("unknown-role", f"The {plugin} plugin has no lab roles.", source, path)
            return None
        whose = "This plugin has" if owner is self.plugin else f"The {plugin} plugin has"
        self.add(
            "unknown-role",
            f"{whose} no lab role `{name}`.",
            source,
            path,
            hint=schemas._did_you_mean(name, [r.name for r in owner.roles.values()])
            or (
                f"Declare it under `roles` in {'plugin.yaml' if owner is self.plugin else plugin}"
                ", with a label."
            ),
        )
        return None

    def _qualified(self, plugin: str) -> str:
        """What to write for a plugin named without its namespace: the one plugin of that
        name it was given (or is), else Iki's own, else the name in a namespace to fill in."""
        for candidates in ({*self.others, self.plugin.name}, naming.FIRST_PARTY):
            known = sorted(n for n in candidates if naming.short_name(n) == plugin)
            if known:
                return known[0] if len(known) == 1 else f"<namespace>/{plugin}"
        return f"<namespace>/{plugin}"

    def unpinned(self) -> None:
        for qualified in sorted(self.used):
            plugin, name = qualified.split(".")
            if plugin in self.unavailable or qualified in BUILTIN_TYPES:
                continue
            if name not in self.plugin.dependencies.get(plugin, {}):
                self.add(
                    "dependency-unpinned",
                    f"This plugin uses `{qualified}` without saying which schema versions it "
                    "works with.",
                    self.source,
                    ("dependencies", plugin),
                    key=True,
                    severity=Severity.WARNING,
                    hint=f'Add `types: {{ {name}: ">={self.types[qualified].schema_version}" }}` '
                    f"under `{plugin}`, so upgrades that change it are caught at build time.",
                )

    # Type references ----------------------------------------------------------------------------

    def reference(self, ref: str, source: Document, path: Path) -> EntityType | None:
        """The type `ref` names, or None after reporting why it can't be used."""
        if ref in self.invalid_types:
            return None  # The root schema error has already been reported.
        plugin, name = ref.split(".")
        if "/" not in plugin:
            own = plugin == naming.short_name(self.plugin.name)
            self.add(
                "unqualified-reference",
                f"`{ref}` doesn't say which namespace the {plugin} plugin is in.",
                source,
                path,
                hint=f"Write `{name}`, since it's this plugin's own type, or "
                f"`{self.plugin.name}.{name}`."
                if own
                else f"Write `{self._qualified(plugin)}.{name}`.",
            )
            return None
        if ref in self.types:
            if plugin != self.plugin.name and ref not in BUILTIN_TYPES:
                if plugin not in self.plugin.dependencies:
                    self.add(
                        "undeclared-dependency",
                        f"`{ref}` belongs to the {plugin} plugin, which plugin.yaml doesn't list "
                        "under `dependencies`.",
                        source,
                        path,
                        hint=f"Add `{plugin}: {{ types: {{ {name}: "
                        f'">={self.types[ref].schema_version}" }} }}` under `dependencies`.',
                    )
                    return None
                self.used.add(ref)
            return self.types[ref]
        if plugin == self.plugin.name:
            self.add(
                "unknown-type",
                f"This plugin has no type `{name}`.",
                source,
                path,
                hint=self._suggest(name, self.plugin.types),
            )
            return None
        if plugin == naming.CORE and naming.CORE not in self.others:
            self.add(
                "unknown-type",
                f"`{ref}` isn't a core type. In this version, core provides "
                + ", ".join(f"`{t}`" for t in sorted(BUILTIN_TYPES))
                + ".",
                source,
                path,
            )
            return None
        if plugin not in self.plugin.dependencies:
            self.add(
                "undeclared-dependency",
                f"`{ref}` belongs to the {plugin} plugin, which plugin.yaml doesn't list under "
                "`dependencies`.",
                source,
                path,
                hint=f'Add `{plugin}: {{ types: {{ {name}: ">=1" }} }}` under `dependencies`, '
                f"and pass its directory with `--with`.",
            )
            return None
        if plugin in self.unavailable:
            return None  # reported once, on the dependency
        other = self.others[plugin].plugin
        assert other is not None  # noqa: S101 - an invariant, not a check
        self.add(
            "unknown-type",
            f"The {plugin} plugin has no type `{name}`.",
            source,
            path,
            hint=self._suggest(name, other.types),
        )
        return None

    def _suggest(self, name: str, types: dict[str, EntityType]) -> str | None:
        return schemas._did_you_mean(name, [t.name for t in types.values()])

    def ancestors(self, entity_type: EntityType) -> list[EntityType]:
        """Parents, nearest first, without ``iki/core.Entity``, stopping at a cycle or a type
        that can't be found (:func:`effective.ancestors`)."""
        return effective.ancestors(self.types, entity_type)

    def effective_of(self, entity_type: EntityType) -> effective.Effective:
        """What ``entity_type``'s records have (ADR 0282), as far as its ancestors are here."""
        return effective.resolve_one(self.types, entity_type.qualified_name)

    def registered(self, entity_type: EntityType) -> bool:
        """Whether its entities are registry entries (ADR 0115): it, or an ancestor, has an
        identity."""
        return self.effective_of(entity_type).identity_owner is not None

    def all_fields(self, entity_type: EntityType) -> dict[str, tuple[Field, EntityType]]:
        out: dict[str, tuple[Field, EntityType]] = {}
        for owner in [*reversed(self.ancestors(entity_type)), entity_type]:
            for name, f in owner.fields.items():
                out.setdefault(name, (f, owner))
        return out

    # Entity types -------------------------------------------------------------------------------

    def entity_type(self, entity_type: EntityType, source: Document) -> None:
        if entity_type.extends:
            parent = self.reference(entity_type.extends, source, ("extends",))
            if parent is not None and parent.is_list:
                self.add(
                    "extends-list",
                    f"`{parent.qualified_name}` is a lab list (ADR 0270), which no type extends: "
                    "its options are its own.",
                    source,
                    ("extends",),
                    hint="Give the type a field referring to the list instead.",
                )
            if parent is not None:
                chain = list(effective.lineage(self.types, entity_type.qualified_name)[:-1])
                top = self.types.get(chain[-1])
                if top is not None and top.extends in chain:
                    self.add(
                        "extends-cycle",
                        "These types extend each other in a circle: "
                        + " → ".join(f"`{t}`" for t in [*chain, top.extends])
                        + ".",
                        source,
                        ("extends",),
                    )
        if entity_type.abstract and entity_type.hrid_prefix:
            self.add(
                "hrid-on-abstract",
                "Abstract types have no entities of their own, so they have no HRIDs.",
                source,
                ("hrid",),
                key=True,
                hint="Give each concrete subtype its own `hrid`.",
            )
        if not entity_type.abstract and not entity_type.hrid_prefix:
            self.add(
                "missing-hrid",
                f"`{entity_type.name}` needs an HRID prefix for its human-readable IDs.",
                source,
                ("name",),
                hint=f"Add `hrid: {{ prefix: {_suggest_prefix(entity_type.name)} }}`, which "
                f"gives IDs like {_suggest_prefix(entity_type.name)}-000001.",
            )
        inherited: dict[str, EntityType] = {}
        for ancestor in reversed(self.ancestors(entity_type)):
            for name in ancestor.fields:
                inherited.setdefault(name, ancestor)
        for name in entity_type.fields:
            if name in inherited:
                self.add(
                    "field-shadows-parent",
                    f"`{name}` is already a field of `{inherited[name].qualified_name}`, which "
                    "this type extends.",
                    source,
                    ("fields", name),
                    key=True,
                    hint="Rename it, or use the parent's field.",
                )
        every = self.all_fields(entity_type)
        names = {n: f for n, (f, _) in every.items()}
        for name, f in entity_type.fields.items():
            self.field(f, source, ("fields", name), siblings=names)
        self.identity(entity_type, source, every)
        self.refines(entity_type, source)
        if entity_type.display_title is not None:
            present = {n: f for n, f in names.items() if not f.removed}
            self.title(entity_type.display_title, source, present)
        if entity_type.viewers:
            present = {n: f for n, f in names.items() if not f.removed}
            self.viewers(entity_type, source, present)
        if entity_type.exports:
            present = {n: f for n, f in names.items() if not f.removed}
            self.exports(entity_type, source, present)
        for name, f in entity_type.fields.items():
            if f.type == "state" and not f.procedure:
                self.state_field(entity_type, f, source, ("fields", name), names)
        self.review(entity_type, source)
        self.assignments(entity_type.fields, entity_type, source)
        if entity_type.due is not None:
            self.due(entity_type, source, every)
        self.field_roles(entity_type, source, every)
        notifications.check(self, entity_type, source)
        if entity_type.request is not None:
            self.request(entity_type, source, every)
        if entity_type.result is not None:
            self.result(entity_type, source, every)
        self.work_item(entity_type, source, every)
        self.amount(entity_type, source, every)
        self.draws_received(entity_type, source, every)
        self.stock_catalogue(entity_type, source)
        if entity_type.draw_target is not None:
            self.draw_target(entity_type.draw_target, source, names)
        if entity_type.storage is not None:
            self.storage(entity_type, source, every)
        if entity_type.is_list:
            self.list_type(entity_type, source)

    # Review procedures (ADR 0301) -----------------------------------------------------------

    def available_procedures(self) -> dict[str, ReviewProcedure]:
        """The review procedures its types can be paired with: core's, its dependencies'
        and its own, by qualified name and version."""
        out: dict[str, ReviewProcedure] = {}
        if not self.core:
            out.update(core_plugin().procedures)
        for other in self.others.values():
            if other.plugin is not None:
                out.update(other.plugin.procedures)
        out.update(self.plugin.procedures)
        return out

    def procedures(self) -> None:
        for qualified, procedure in self.plugin.procedures.items():
            source = self.loaded.procedure_sources[qualified]
            for problem in procedures.check_procedure(procedure):
                self.add(
                    problem.code,
                    problem.message,
                    source,
                    problem.path,
                    hint=problem.hint,
                    severity=Severity.WARNING if problem.warning else Severity.ERROR,
                )

    def people_kind(self, entity_type: EntityType) -> Callable[[str], bool | None]:
        """For a person path of its records (``investigator``, ``test.observed_by``):
        whether it names a list of people, one person, or (None) no one."""
        every = self.all_fields(entity_type)

        def kind(path: str) -> bool | None:
            head, _, tail = path.partition(".")
            found = every.get(head)
            if found is None:
                return None
            f = found[0]
            if tail:
                if f.type != "ref" or len(f.to) != 1 or f.to[0] not in self.types:
                    return None
                target = self.all_fields(self.types[f.to[0]]).get(tail)
                if target is None:
                    return None
                f = target[0]
            if not names_people(f):
                return None
            return f.type == "list"

        return kind

    def review_owner(self, entity_type: EntityType) -> EntityType | None:
        """The type in its lineage whose ``review`` its records follow: its own, or the
        nearest ancestor's. The one place a type's effective review is resolved, so the
        shared effective-declaration resolver replaces only this."""
        for t in [entity_type, *self.ancestors(entity_type)]:
            if t.review is not None:
                return t
        return None

    def review(self, entity_type: EntityType, source: Document) -> None:
        """A reviewed type (ADR 0301): what it needs of a procedure, where its procedure is
        pinned from (ADR 0302), and its own guards and exclusions."""
        review = entity_type.review
        if review is None:
            return
        parents = self.ancestors(entity_type)
        parent = parents[0] if parents else None
        ancestor = self.review_owner(parent) if parent is not None else None
        if ancestor is not None:
            self.add(
                "review-inherited",
                f"`{entity_type.name}` is reviewed as `{ancestor.qualified_name}` is, "
                "which it extends: a type is reviewed once in its lineage.",
                source,
                ("review",),
                key=True,
                hint="Remove `review` here: its records follow its parent's review.",
            )
            return
        body = self.loaded.raw.get(id(source)) or {}
        declared = body.get("fields") or {}
        added = [*([] if review.opens else [review.field]), "review_procedure", "review_flow"]
        for name in added:
            if name in declared:
                self.add(
                    "review-field-declared",
                    f"`{name}` is a field Iki adds to a reviewed type, so the type doesn't "
                    "declare it.",
                    source,
                    ("fields", name),
                    key=True,
                    hint="Remove it: Iki adds it, and the server sets it when a record is created.",
                )
        raw = body.get("review") or {}
        if review.opens:
            extra = [
                k
                for k in ("pinned", "derived", "needs", "guards", "independent_of", "field")
                if k in raw
            ]
            for key in extra:
                self.add(
                    "review-opens",
                    f"`{entity_type.name}` only opens work for the records that follow it, so "
                    f"it has no review of its own for `{key}` to describe.",
                    source,
                    ("review", key),
                    key=True,
                )
            return
        if review.by is not None:
            self.review_pinned_by(entity_type, review, source)
        named = set(review.needs.named)
        for name, own in review.guards.items():
            at: Path = ("review", "guards", name)
            if name in procedures.COMPILED_GUARDS:
                self.add(
                    "review-guard-name",
                    f"`{name}` is the name of a guard Iki compiles from the procedure.",
                    source,
                    at,
                    key=True,
                    hint="Give it another name.",
                )
            for index, state in enumerate(own.on):
                if state not in named and state not in GUARD_GROUPS:
                    self.add(
                        "review-unneeded-state",
                        f"`{state}` isn't a state `{entity_type.name}` says it needs.",
                        source,
                        (*at, "on", index),
                        hint="Name it in `needs: states`, so only procedures that have it are "
                        "paired with this type, or put the guard `on: [signed]` or "
                        "`on: [final]`.",
                    )
        kind = self.people_kind(entity_type)
        for index, ex in enumerate(review.independent_of):
            at = ("review", "independent_of", index)
            if ex.kind != "people":
                self.add(
                    "review-exclusion",
                    "A type adds people its fields name (`people:`); the creator, editors "
                    "and signers are the procedure's to exclude.",
                    source,
                    at,
                )
                continue
            self.person_paths(ex.people, kind, source, (*at, "people"))

    def review_pinned_by(
        self, entity_type: EntityType, review: ReviewSpec, source: Document
    ) -> None:
        at: Path = ("review", "pinned", "by")
        found = self.all_fields(entity_type).get(review.by or "")
        f = found[0] if found is not None else None
        if f is None or f.type != "ref":
            self.add(
                "review-pinned-by",
                f"`{review.by}` isn't a reference field of `{entity_type.name}`, so its "
                "records can't follow the procedure of the record it names.",
                source,
                at,
            )
            return
        if not (f.required and f.immutable):
            self.add(
                "review-pinned-by",
                f"`{review.by}` must be `required` and `immutable`: a record follows the "
                "procedure of the one record that opened it, for its life.",
                source,
                at,
            )
        for target in f.to:
            t = self.types.get(target)
            if t is None:
                continue
            if self.review_owner(t) is None:
                self.add(
                    "review-pinned-by",
                    f"`{t.qualified_name}` has no review procedure for `{entity_type.name}` to "
                    "follow.",
                    source,
                    at,
                    hint=f"Give `{t.name}` `review: {{opens: true}}`, so a procedure is "
                    "pinned on it when it's created.",
                )

    def person_paths(
        self,
        paths: Sequence[str],
        kind: Callable[[str], bool | None],
        source: Document,
        at: Path,
    ) -> None:
        for index, path in enumerate(paths):
            if kind(path) is None:
                self.add(
                    "not-a-person",
                    f"`{path}` isn't a field naming people, or a reference's one.",
                    source,
                    (*at, index),
                    hint="Name a `person` field, a list of people, or one a single "
                    "reference away, such as `test.observed_by`.",
                )

    def declared_independence(self) -> None:
        """Compiles each ordinary state's ``maker`` and ``independent_of`` (ADR 0301) into
        its guards, for the server to enforce as it enforces any guard."""
        types = dict(self.plugin.types)
        for qualified, entity_type in self.plugin.types.items():
            source = self.loaded.type_sources.get(qualified)
            if source is None or qualified in self.invalid_types:
                continue
            kind = self.people_kind(entity_type)
            fields = dict(entity_type.fields)
            for name, f in entity_type.fields.items():
                if f.type != "state" or f.procedure:
                    continue
                labels = {s: st.label for s, st in f.states.items()}
                states = dict(f.states)
                for state_name, state in f.states.items():
                    if state.maker is None and not state.independent_of:
                        continue
                    at: Path = ("fields", name, "states", state_name)
                    guards = dict(state.guards)
                    if state.maker is not None:
                        if state.maker.person is None:
                            self.add(
                                "maker-without-window",
                                "On a state field without a procedure, the maker is the "
                                "person a field names: `maker: {person: field}`.",
                                source,
                                (*at, "maker"),
                            )
                        else:
                            self.person_paths([state.maker.person], kind, source, (*at, "maker"))
                            guards = self.compiled(
                                guards,
                                procedures.maker_guard(name, state.maker, f.initial or "", labels),
                                source,
                                at,
                            )
                    if state.independent_of:
                        for index, ex in enumerate(state.independent_of):
                            where: Path = (*at, "independent_of", index)
                            if ex.kind == "editors" and not ex.window:
                                self.add(
                                    "editors-without-window",
                                    "On a state field without a procedure, say which states' "
                                    "edits count: `{editors: {in: [states]}}`.",
                                    source,
                                    where,
                                )
                            for s in (*ex.window, *ex.states):
                                if s not in f.states:
                                    self.state_ref(s, set(f.live_states), source, where)
                            if ex.kind == "people":
                                self.person_paths(ex.people, kind, source, (*where, "people"))
                        guards = self.compiled(
                            guards,
                            procedures.exclusion_guard(
                                name, state.independent_of, open=None, labels=labels, people=kind
                            ),
                            source,
                            at,
                        )
                    states[state_name] = dataclasses.replace(state, guards=guards)
                fields[name] = dataclasses.replace(f, states=states)
            types[qualified] = dataclasses.replace(entity_type, fields=fields)
        self.plugin = self.loaded.plugin = dataclasses.replace(self.plugin, types=types)
        self.types.update(types)

    def compiled(
        self, guards: dict[str, Guard], guard: Guard, source: Document, at: Path
    ) -> dict[str, Guard]:
        if guard.name in guards:
            self.add(
                "compiled-guard-name",
                f"`{guard.name}` is the guard Iki compiles from this state's "
                f"`{'maker' if guard.name == 'maker' else 'independent_of'}`.",
                source,
                (*at, "guards", guard.name),
                key=True,
                hint="Give the hand-written guard another name.",
            )
            return guards
        return {**guards, guard.name: guard}

    # Lab lists (ADR 0270) --------------------------------------------------------------------

    #: What a list's type can't declare: its options are registry entries its curators keep,
    #: never records a project works on, so nothing about records applies to them.
    _NOT_FOR_LISTS = (
        "identity",
        "extends",
        "abstract",
        "sample",
        "rules",
        "notifications",
        "request",
        "result",
        "work_item",
        "amount",
        "draws_received",
        "draw_guards",
        "draw_purposes",
        "draw_target",
        "storage",
        "stock_catalogue",
        "due",
        "exports",
        "refines",
    )

    def list_type(self, entity_type: EntityType, source: Document) -> None:
        """A lab list's type: its options' fields, and its starting options."""
        body = source.data if isinstance(source.data, dict) else {}
        for key in self._NOT_FOR_LISTS:
            if key in body:
                self.add(
                    "list-key",
                    f"`{key}` isn't for a lab list: its options are registry entries its "
                    "curators keep, not records a project works on.",
                    source,
                    (key,),
                    key=True,
                    hint="Leave it off, or make a type of records that refers to the list."
                    if key != "identity"
                    else "A list's options are told apart by their `code`: leave `identity` off.",
                )
        if (body.get("display") or {}).get("viewers"):
            self.add(
                "list-key",
                "A lab list's options have no viewers: they're shown as a dropdown's choices.",
                source,
                ("display", "viewers"),
                key=True,
            )
        raw = body.get("fields")
        declared: dict[str, Any] = raw if isinstance(raw, dict) else {}
        for name in declared:
            if name in LIST_FIELDS:
                self.add(
                    "list-field-reserved",
                    f"`{name}` is every list option's own field already (ADR 0270).",
                    source,
                    ("fields", name),
                    key=True,
                    hint="List types have `code`, `label`, `description` and `position`: give "
                    "this field another name.",
                )
        for name, f in entity_type.fields.items():
            if name in LIST_FIELDS:
                continue
            at: Path = ("fields", name)
            if f.registry == "derived" or f.computed:
                self.add(
                    "list-computed",
                    f"`{name}` would be computed, but nothing computes a list's options: its "
                    "curators give their values.",
                    source,
                    (*at, "registry" if f.registry == "derived" else "computed"),
                    key=True,
                    hint="Leave `registry` and `computed` off: a list's fields are its options' "
                    "facts.",
                )
            if f.type == "state" or (f.items is not None and f.items.type == "state"):
                self.add(
                    "list-state",
                    f"`{name}` is a state field, but a list's options have no states: an "
                    "option is offered, or archived.",
                    source,
                    at,
                )
            for target in self._list_targets(f):
                found = self.types.get(target)
                if found is not None and not found.is_list:
                    self.add(
                        "list-refers-to-entry",
                        f"`{name}` refers to `{target}`, which isn't a lab list: every project "
                        "in a registry reads its lists, so their options refer only to other "
                        "lists' options.",
                        source,
                        at,
                        hint="Make the field a reference to another list, such as a category's.",
                    )
        if entity_type.list_spec is not None:
            self.list_options(entity_type, source)

    @staticmethod
    def _list_targets(f: Field) -> list[str]:
        targets = list(f.to) if f.type == "ref" else []
        if f.type == "list" and f.items is not None:
            targets += list(f.items.to) if f.items.type == "ref" else []
            targets += [t for c in f.items.fields.values() if c.type == "ref" for t in c.to]
        return targets

    def list_options(self, entity_type: EntityType, source: Document) -> None:
        """A list's starting options: each code and label once, and values its fields take."""
        assert entity_type.list_spec is not None  # noqa: S101 - only lists have options
        codes: dict[str, int] = {}
        labels: dict[str, int] = {}
        fields = entity_type.fields
        for index, option in enumerate(entity_type.list_spec.options):
            at: Path = ("list", "options", index)
            code = option.get(LIST_CODE)
            if isinstance(code, str):
                if code in codes:
                    self.add(
                        "list-option-twice",
                        f"`{code}` is option {codes[code] + 1}'s code already: a code names one "
                        "option.",
                        source,
                        (*at, LIST_CODE),
                    )
                else:
                    codes[code] = index
            label = option.get(LIST_LABEL)
            if isinstance(label, str):
                key = label.casefold()
                if key in labels:
                    self.add(
                        "list-label-twice",
                        f"“{label}” is option {labels[key] + 1}'s label already: people pick an "
                        "option by its label, so each is the list's once.",
                        source,
                        (*at, LIST_LABEL),
                    )
                else:
                    labels[key] = index
            for name, value in option.items():
                f = fields.get(name)
                if name == LIST_POSITION or f is None or f.removed:
                    self.add(
                        "unknown-field",
                        f"`{name}` isn't a field of `{entity_type.name}`'s options."
                        if name != LIST_POSITION
                        else "An option's place is where the manifest lists it.",
                        source,
                        (*at, name),
                        key=True,
                        hint=schemas._did_you_mean(name, [n for n in fields if n != LIST_POSITION])
                        if name != LIST_POSITION
                        else "Leave `position` off, and list the options in their order.",
                    )
                    continue
                self.list_option_value(f, value, source, (*at, name))
            for name, f in fields.items():
                if f.required and not f.removed and option.get(name) is None:
                    if name == LIST_POSITION:
                        continue
                    self.add(
                        "list-option-incomplete",
                        f"Option {index + 1} needs `{name}`, which is required.",
                        source,
                        at,
                    )

    def list_option_value(self, f: Field, value: Any, source: Document, at: Path) -> None:  # noqa: ANN401 - manifest data is JSON-shaped
        """A starting option's value for one of its type's fields. A reference to another
        list's option names it by its code, which that list must start with."""
        if f.type == "ref" or (f.type == "list" and f.items is not None and f.items.type == "ref"):
            item = f if f.type == "ref" else f.items
            assert item is not None  # noqa: S101 - lists have items
            values = value if f.type == "list" and isinstance(value, list) else [value]
            if f.type == "list" and not isinstance(value, list):
                self.add("list-option-value", "must be a list of codes.", source, at)
                return
            target = self.types.get(item.to[0]) if item.to else None
            for code in values:
                if not isinstance(code, str):
                    self.add(
                        "list-option-value",
                        "must name the option it refers to by its code.",
                        source,
                        at,
                    )
                    continue
                if target is None or target.list_spec is None:
                    continue  # Its target's problem is reported where it's named.
                if not any(o.get(LIST_CODE) == code for o in target.list_spec.options):
                    self.add(
                        "list-option-unknown",
                        f"`{code}` isn't one of `{target.qualified_name}`'s starting options: "
                        "every registry starts with what a starting option refers to.",
                        source,
                        at,
                        hint=schemas._did_you_mean(
                            code, [str(o.get(LIST_CODE)) for o in target.list_spec.options]
                        ),
                    )
            return
        problems = value_problems(f, value)
        if (
            f.name == LIST_LABEL
            and isinstance(value, str)
            and not re.fullmatch(LIST_LABEL_PATTERN, value)
        ):
            problems = ["must be one line, with no spaces at its start or end."]
        for problem in problems:
            self.add("list-option-value", f"`{f.name}` {problem}", source, at)

    def stock_catalogue(self, entity_type: EntityType, source: Document) -> None:
        # Validate a lot in its own plugin too. A later plugin can introduce new
        # stock lot subtypes after the catalogue that declared their ledger contract.
        chain = [entity_type, *self.ancestors(entity_type)]
        if not entity_type.abstract:
            for ancestor in chain:
                catalogue = self.stock_lots.get(ancestor.qualified_name)
                if catalogue is None or catalogue.stock_catalogue is None:
                    continue
                spec = catalogue.stock_catalogue
                unit_field = self.all_fields(catalogue).get(spec.unit)
                amount = next((t.amount for t in chain if t.amount), None)
                if (
                    amount is None
                    or amount.material != spec.lots.item
                    or (unit_field and amount.unit not in unit_field[0].values)
                ):
                    self.add(
                        "stock-catalogue-lot",
                        f"{entity_type.name} needs a ledger in a catalogue unit "
                        "with its item as material.",
                        source,
                        ("amount",) if entity_type.amount else ("name",),
                    )
        config = entity_type.stock_catalogue
        if config is None:
            return
        at: Path = ("stock_catalogue",)
        for index, role in enumerate(config.notify):
            self.lab_role(role, source, (*at, "notify", index))

        def field_of(owner: EntityType, name: str, kinds: set[str], path: Path) -> Field | None:
            found = self.all_fields(owner).get(name)
            if (
                found
                and not found[0].removed
                and found[0].type in kinds
                and found[0].registry is None
            ):
                return found[0]
            self.add(
                "stock-catalogue-field",
                f"`{name}` on {owner.name} must be a record field of type "
                f"{' or '.join(sorted(kinds))}.",
                source,
                (*at, *path),
            )
            return None

        field_of(entity_type, config.name, {"string", "text"}, ("name",))
        unit = field_of(entity_type, config.unit, {"enum"}, ("unit",))
        if unit and not (unit.required and unit.immutable):
            self.add(
                "stock-catalogue-unit",
                "The stock unit must be required and immutable.",
                source,
                (*at, "unit"),
            )
        threshold = field_of(entity_type, config.threshold, {"int", "float"}, ("threshold",))
        # An item without a threshold isn't reordered by its level: it's never low.
        if threshold and (threshold.min is None or threshold.min < 0):
            self.add(
                "stock-catalogue-threshold",
                "The low-stock threshold must have a nonnegative minimum.",
                source,
                (*at, "threshold"),
            )
        for key, mapping in (("lots", config.lots), ("orders", config.orders)):
            owner = self.reference(mapping.type, source, (*at, key, "type"))
            if owner is None:
                continue
            item = field_of(owner, mapping.item, {"ref"}, (key, "item"))
            if item and (
                item.to != (entity_type.qualified_name,) or not item.required or not item.immutable
            ):
                self.add(
                    "stock-catalogue-item",
                    "The item reference must be required, immutable and point to this catalogue.",
                    source,
                    (*at, key, "item"),
                )
            if key == "orders":
                ordered = field_of(owner, config.orders.ordered, {"int"}, (key, "ordered"))
                received = field_of(owner, config.orders.received, {"int"}, (key, "received"))
                if ordered and (
                    not ordered.required
                    or not ordered.immutable
                    or not ordered.min
                    or ordered.min < 1
                ):
                    self.add(
                        "stock-catalogue-order",
                        "Ordered packs must be required, immutable and positive.",
                        source,
                        (*at, key, "ordered"),
                    )
                if received and (not received.computed or received.min is None or received.min < 0):
                    self.add(
                        "stock-catalogue-order",
                        "Received packs must be computed and nonnegative.",
                        source,
                        (*at, key, "received"),
                    )
                field_of(owner, config.orders.reference, {"string", "text"}, (key, "reference"))
            else:
                field_of(owner, config.lots.number, {"string", "text"}, (key, "number"))
                field_of(owner, config.lots.expiry, {"date"}, (key, "expiry"))

    def draws_received(
        self, entity_type: EntityType, source: Document, every: dict[str, tuple[Field, EntityType]]
    ) -> None:
        """One ledger field is inherited through the type's lineage. Every receipt must
        be a new value on a project's record that starts empty."""
        lineage = [entity_type, *self.ancestors(entity_type)]
        declarations = [t.draws_received for t in lineage if t.draws_received is not None]
        if not declarations:
            return
        name = declarations[0]
        field = every[name][0] if name in every else None
        if len(set(declarations)) != 1:
            self.add(
                "draws-received-field",
                "A subtype keeps its parent's material draw field.",
                source,
                ("draws_received",),
            )
        elif (
            field is None
            or field.type != "json"
            or not field.computed
            or field.removed
            or field.immutable
            or field.required
            or field.has_default
            or field.registry is not None
            or any(name in t.identity for t in lineage)
        ):
            self.add(
                "draws-received-field",
                f"`{name}` must be an optional computed JSON field on the project's record "
                "that starts empty and can receive later draws.",
                source,
                ("draws_received",) if entity_type.draws_received else ("name",),
            )

    def draw_target(self, target: DrawTarget, source: Document, fields: dict[str, Field]) -> None:
        """A target opts in using an assigned container and amount, both fixed at creation.
        The amount is a quantity in its field's unit, or a float in the unit another field
        names, such as a planned volume or mass (ADR 0243)."""
        for key, name, kinds in (
            ("from", target.from_field, ("ref",)),
            ("source", target.source, ("ref",)),
            ("amount", target.amount, ("float",) if target.unit else ("quantity",)),
            ("unit", target.unit, ("enum", "string")),
        ):
            if name is None:
                continue
            field = fields.get(name)
            if (
                field is None
                or field.type not in kinds
                or not field.immutable
                or not field.required
            ):
                self.add(
                    "invalid-draw-target",
                    f"`{name}` must be a required, immutable {' or '.join(kinds)} field.",
                    source,
                    ("draw_target", key),
                )
        if target.unit is not None and target.amount is None:
            self.add(
                "invalid-draw-target",
                "A planned amount's unit needs the planned amount it's the unit of.",
                source,
                ("draw_target", "unit"),
            )
        field = fields.get(target.state_field)
        if field is None or field.type != "state" or target.state not in field.live_states:
            self.add(
                "invalid-draw-target",
                "Name a live state of the draw target.",
                source,
                ("draw_target", "state"),
            )
        if target.ready is not None and (
            target.ready not in fields or not fields[target.ready].computed
        ):
            self.add(
                "invalid-draw-target",
                f"`{target.ready}` must be a computed field set when preparing the record.",
                source,
                ("draw_target", "ready"),
            )
        for index, role in enumerate(target.by):
            self.lab_role(role, source, ("draw_target", "by", index))

    # Where material is kept (ADR 0180) --------------------------------------------------------

    def storage_of(self, entity_type: EntityType) -> Storage | None:
        """Where ``entity_type``'s records are kept: its own ``storage`` or its nearest
        ancestor's."""
        return self.effective_of(entity_type).storage

    def storage(
        self, entity_type: EntityType, source: Document, every: dict[str, tuple[Field, EntityType]]
    ) -> None:
        """What a container holds, its grid's shape, its barcode, its label's line and what
        needs a scan: each of the type's own or its ancestors' fields, of the kind it needs."""
        spec = entity_type.storage
        assert spec is not None  # noqa: S101 - only called with one
        lineage = [entity_type, *self.ancestors(entity_type)]
        if any(t.identity for t in lineage):
            self.add(
                "storage-on-identity",
                f"`{entity_type.name}` has an identity, so each project has one record of each "
                "of its entries: its records aren't things the lab keeps somewhere.",
                source,
                ("storage",),
                key=True,
                hint="Give `storage` to a type of what the lab keeps (a tube, a box) that "
                "refers to the entry.",
            )
            return
        present = {n: f for n, (f, _) in every.items() if not f.removed}
        for index, held in enumerate(spec.holds):
            at: Path = ("storage", "holds", index)
            target = self.reference(held, source, at)
            if target is not None and self.storage_of(target) is None:
                self.add(
                    "storage-holds-unstored",
                    f"`{target.qualified_name}` has no `storage`, so its records aren't kept "
                    "anywhere.",
                    source,
                    at,
                    hint=f"Give `{target.name}` `storage`, even `storage: {{}}`.",
                )
        if spec.grid is not None:
            if not spec.holds:
                self.add(
                    "storage-grid-without-holds",
                    f"`{entity_type.name}` has positions, but holds nothing.",
                    source,
                    ("storage", "grid"),
                    key=True,
                    hint="Say what it holds, such as `holds: [Tube]`.",
                )
            self.grid(spec.grid, source, present)
        if spec.barcode is not None:
            f = present.get(spec.barcode)
            if f is None or f.type != "string" or not f.unique:
                self.add(
                    "storage-barcode",
                    f"`barcode` names a unique string field of the type, and `{spec.barcode}` "
                    + (
                        "isn't one."
                        if f is None
                        else f"is a {f.type} field."
                        if f.type != "string"
                        else "isn't unique: a scan finds one record."
                    ),
                    source,
                    ("storage", "barcode"),
                    hint="Such as `barcode: { type: string, unique: true, max_length: 64 }`.",
                )
        if spec.label is not None:
            for problem, hint in _title_problems(spec.label, present):
                self.add("storage-label", problem, source, ("storage", "label"), hint=hint)
        if "draw" in spec.confirm and not any(t.amount for t in lineage):
            self.add(
                "storage-confirm-without-amount",
                f"`{entity_type.name}` has no amount to draw, so no draw needs a scan.",
                source,
                ("storage", "confirm"),
                hint="Leave `draw` off, or give the type an `amount`.",
            )

    def grid(self, grid: Grid, source: Document, present: dict[str, Field]) -> None:
        """A grid's rows and columns: numbers within what Iki keeps, or the type's own
        required, immutable int fields bounded by them, so a container's shape never
        changes under what's in it."""
        largest = {"rows": GRID_ROWS, "columns": GRID_COLUMNS}
        sizes: dict[str, int] = {}
        for side in ("rows", "columns"):
            value = getattr(grid, side)
            at: Path = ("storage", "grid", side)
            if isinstance(value, int):
                if not 1 <= value <= largest[side]:
                    self.add(
                        "storage-grid",
                        f"A grid has from 1 to {largest[side]} {side}, not {value}.",
                        source,
                        at,
                    )
                sizes[side] = value
                continue
            f = present.get(value)
            bounded = (
                f is not None
                and f.min is not None
                and f.max is not None
                and f.min >= 1
                and f.max <= largest[side]
            )
            if f is None or f.type != "int" or not (f.required and f.immutable) or not bounded:
                self.add(
                    "storage-grid",
                    f"`{side}` is a number, or names a required, immutable int field of the "
                    f"type from 1 to at most {largest[side]}, and `{value}` "
                    + (
                        "isn't one."
                        if f is None
                        else f"is a {f.type} field."
                        if f.type != "int"
                        else "isn't both required and immutable: a container's shape can't "
                        "change under what's in it."
                        if not (f.required and f.immutable)
                        else f"isn't bounded from 1 to at most {largest[side]}."
                    ),
                    source,
                    at,
                    hint=f"Such as `{side}: {{ type: int, required: true, immutable: true, "
                    f"min: 1, max: {largest[side]} }}`.",
                )
                continue
            assert f.max is not None  # noqa: S101 - bounded
            sizes[side] = int(f.max)
        if len(sizes) == 2 and sizes["rows"] * sizes["columns"] > GRID_POSITIONS:
            self.add(
                "storage-grid",
                f"A grid has at most {GRID_POSITIONS} positions, and this one can have "
                f"{sizes['rows'] * sizes['columns']}.",
                source,
                ("storage", "grid"),
                hint="Lower its rows' or columns' maximum.",
            )

    # Requests between projects (ADR 0132) -------------------------------------------------------

    def request_of(self, entity_type: EntityType) -> EntityType | None:
        """The type, of ``entity_type`` and its ancestors, that makes its records requests."""
        return next(
            (t for t in [entity_type, *self.ancestors(entity_type)] if t.request is not None),
            None,
        )

    def request(
        self, entity_type: EntityType, source: Document, every: dict[str, tuple[Field, EntityType]]
    ) -> None:
        """A request type names the fields its service writes, and the references whose
        records go with it."""
        spec = entity_type.request
        assert spec is not None  # noqa: S101 - only called with one
        above = next((a for a in self.ancestors(entity_type) if a.request is not None), None)
        if above is not None:
            # A subtype may name more of the service's fields, its own (ADR 0232): a lab's
            # request type adds what its service writes, such as a ranking. The rest is
            # its parent's, and stays so.
            changed = [
                key
                for key, value in (
                    ("sends", spec.sends),
                    ("materials", spec.materials),
                    ("completed", spec.completed_field),
                    ("receives", spec.receives),
                )
                if value
            ]
            if changed or not spec.service_fields:
                self.add(
                    "request-redeclared",
                    f"`{entity_type.name}` extends `{above.qualified_name}`, which is a "
                    "request type already: its subtypes are requests as it says, and can only "
                    "add service fields of their own.",
                    source,
                    ("request", changed[0]) if changed else ("request",),
                    key=True,
                    hint="Leave `request` off, or give it only `service_fields`: the "
                    "subtype's own fields that its service writes.",
                )
                return
            self.service_fields(entity_type, spec, source, every)
            return
        if self.registered(entity_type):
            self.add(
                "request-with-identity",
                f"`{entity_type.name}` has an identity, so its records are registry entries' "
                "records: a request is the asking project's own record of what it asks for.",
                source,
                ("request",),
                key=True,
                hint="Make the request a type of its own that refers to what it's about.",
            )
            return
        if entity_type.result is not None:
            self.add(
                "result-is-a-request",
                f"`{entity_type.name}` can't be both a request and a result: the asking "
                "project owns a request, and the service its results.",
                source,
                ("result",),
                key=True,
            )
        present = [n for n, (f, _) in every.items() if not f.removed]
        self.service_fields(entity_type, spec, source, every)
        if spec.completed_field is not None:
            found = every.get(spec.completed_field)
            state = found[0].states.get(spec.completed_state or "") if found else None
            if (
                spec.completed_field not in spec.service_fields
                or not found
                or found[0].removed
                or found[0].type != "state"
                or state is None
                or state.removed
                or found[0].initial == spec.completed_state
            ):
                self.add(
                    "request-completion-state",
                    "Completion must name a non-initial state of a state field the service writes.",
                    source,
                    ("request", "completed"),
                )
        for index, name in enumerate(spec.materials):
            if name not in spec.sends:
                self.add(
                    "request-material-not-sent",
                    f"Request material `{name}` must be one of the fields in `sends`.",
                    source,
                    ("request", "materials", index),
                )
            if name in every:
                field = every[name][0]
                target = field.items if field.type == "list" else field
                if target and self._refers_to_identity(target):
                    self.add(
                        "request-material-identity",
                        "A material field must identify a physical record.",
                        source,
                        ("request", "materials", index),
                    )
        for index, name in enumerate(spec.sends):
            at = ("request", "sends", index)
            found = every.get(name)
            if found is None or found[0].removed:
                self.add(
                    "unknown-field",
                    f"`{name}` isn't a field of `{entity_type.name}`.",
                    source,
                    at,
                    hint=schemas._did_you_mean(name, present),
                )
                continue
            f = found[0]
            refers = f.type == "ref" or (
                f.type == "list" and f.items is not None and f.items.type == "ref"
            )
            if not refers:
                self.add(
                    "request-sends-not-a-reference",
                    f"`{name}` is a {f.type} field: a request sends the records its references "
                    "name.",
                    source,
                    at,
                    hint="Name a `ref` field, or a list of them, such as its samples.",
                )
            elif name in spec.service_fields:
                self.add(
                    "request-sends-service-field",
                    f"`{name}` is one of the service's fields, but what a request sends is the "
                    "asking project's.",
                    source,
                    at,
                )
        if spec.receives is not None:
            self.receives(entity_type, spec, source, every)

    def service_fields(
        self,
        entity_type: EntityType,
        spec: RequestSpec,
        source: Document,
        every: dict[str, tuple[Field, EntityType]],
    ) -> None:
        """The fields a request type names its service's: its own, each one a service can
        write."""
        present = [n for n, (f, _) in every.items() if not f.removed]
        seen: set[str] = set()
        for index, name in enumerate(spec.service_fields):
            at: Path = ("request", "service_fields", index)
            if name in seen:
                self.add("duplicate-field", f"`{name}` is named twice.", source, at)
                continue
            seen.add(name)
            found = every.get(name)
            if found is None or found[0].removed:
                self.add(
                    "unknown-field",
                    f"`{name}` isn't a field of `{entity_type.name}`.",
                    source,
                    at,
                    hint=schemas._did_you_mean(name, present),
                )
                continue
            if name not in entity_type.fields:
                self.add(
                    "service-field-inherited",
                    f"`{name}` is `{found[1].qualified_name}`'s field, and the fields a "
                    "request type names its service's are its own.",
                    source,
                    at,
                    hint="Name it on the type that has the field, or add the field to this type.",
                )
                continue
            self.service_field(found[0], source, at)

    def receives(
        self,
        entity_type: EntityType,
        spec: RequestSpec,
        source: Document,
        every: dict[str, tuple[Field, EntityType]],
    ) -> None:
        """What a request's service receives (ADR 0180): references it sends, to records
        kept somewhere, and the service's lab roles that receive them."""
        name = spec.receives
        assert name is not None  # noqa: S101 - only called with one
        at: Path = ("request", "receives", "field")
        found = every.get(name)
        f = found[0] if found is not None and not found[0].removed else None
        kind = f.items if f is not None and f.type == "list" else f
        if f is None or kind is None or kind.type != "ref" or name not in spec.sends:
            self.add(
                "request-receives",
                f"`receives` names a reference, or a list of them, that the request sends, and "
                f"`{name}` "
                + (
                    "isn't one of its fields."
                    if f is None
                    else "isn't one it sends."
                    if kind is not None and kind.type == "ref"
                    else f"is a {f.type} field."
                ),
                source,
                at,
                hint="Such as `tubes: { type: list, items: { type: ref, to: Tube } }`, listed "
                "in `sends` too.",
            )
        else:
            for target in kind.to:
                found_type = self.types.get(target)
                if found_type is not None and self.storage_of(found_type) is None:
                    self.add(
                        "request-receives",
                        f"`{name}` refers to `{target}`, which has no `storage`: a receipt puts "
                        "what it receives away.",
                        source,
                        at,
                        hint=f"Give `{found_type.name}` `storage`.",
                    )
        for index, role in enumerate(spec.receives_by):
            self.lab_role(role, source, ("request", "receives", "by", index))

    def service_field(self, f: Field, source: Document, at: Path) -> None:
        """A field the service writes: the asking project creates the request without it, and
        it can't name what's in either project."""
        kind = f.items.type if f.type == "list" and f.items is not None else f.type
        if kind in ("ref", "file"):
            self.add(
                "service-field-refers",
                f"`{f.name}` is a {kind} field, and a request's references and files are the "
                "asking project's: the service sends what it makes back as results.",
                source,
                at,
                hint="Make a result type (`result: { of: … }`) for what the service sends back.",
            )
        elif f.required and not f.has_default and f.type != "state":
            self.add(
                "service-field-required",
                f"`{f.name}` is required, but the asking project creates the request without "
                "the service's fields.",
                source,
                at,
                hint="Give it a `default`, or don't make it required.",
            )
        elif f.unique:
            self.add(
                "service-field-unique",
                f"`{f.name}` is unique within the asking project, so the service setting it "
                "would learn what that project's other records hold.",
                source,
                at,
            )
        elif f.immutable:
            self.add(
                "service-field-immutable",
                f"`{f.name}` is immutable: the service would set it once, with no way to "
                "correct it.",
                source,
                at,
            )

    def _refers_to_identity(self, field: Field) -> bool:
        return any(
            ancestor.identity
            for name in field.to
            if (target := self.types.get(name))
            for ancestor in [target, *self.ancestors(target)]
        )

    def work_item(
        self, entity_type: EntityType, source: Document, every: dict[str, tuple[Field, EntityType]]
    ) -> None:
        lineage = [entity_type, *self.ancestors(entity_type)]
        if not any(t.work_item is not None for t in lineage):
            return
        if any(t.identity or t.request is not None or t.result is not None for t in lineage):
            self.add(
                "work-item-conflict",
                "A work item belongs to the service project. It can't be a request, a result "
                "or a registry record.",
                source,
                ("work_item",),
                key=True,
            )
            return
        spec = entity_type.work_item
        if spec is not None:
            self._request_reference(entity_type, source, every, "work_item", spec.of)

    def _request_reference(
        self,
        entity_type: EntityType,
        source: Document,
        every: dict[str, tuple[Field, EntityType]],
        section: str,
        name: str,
    ) -> None:
        """A service record names one request with an immutable, required reference."""
        kind = section.replace("_", " ")
        code = section.replace("_", "-")
        found = every.get(name)
        if found is None or found[0].removed:
            present = [n for n, (f, _) in every.items() if not f.removed]
            self.add(
                "unknown-field",
                f"`{name}` isn't a field of `{entity_type.name}`.",
                source,
                (section, "of"),
                hint=schemas._did_you_mean(name, present),
            )
            return
        f = found[0]
        targets = [self.types.get(t) for t in f.to] if f.type == "ref" else []
        if any(t is None for t in targets):
            return  # reported with the field
        if not targets or any(t is None or self.request_of(t) is None for t in targets):
            self.add(
                f"{code}-of-not-a-request",
                f"`{name}` must refer to a request type, one with `request:`, as the "
                f"request this is a {kind} of.",
                source,
                (section, "of"),
            )
        elif not (f.required and f.immutable):
            self.add(
                f"{code}-of-changes",
                f"`{name}` must be `required` and `immutable`: a {kind} is of one request, "
                "for good.",
                source,
                (section, "of"),
            )

    def result(
        self, entity_type: EntityType, source: Document, every: dict[str, tuple[Field, EntityType]]
    ) -> None:
        """A result type names its reference to its request, and the states it's released
        to the asking project in."""
        spec = entity_type.result
        assert spec is not None  # noqa: S101 - only called with one
        if self.registered(entity_type):
            self.add(
                "result-with-identity",
                f"`{entity_type.name}` has an identity, but a result is the service's own "
                "record of what it did.",
                source,
                ("result",),
                key=True,
            )
            return
        self._request_reference(entity_type, source, every, "result", spec.of)
        if spec.material is not None:
            material = every.get(spec.material)
            if material is None or not (
                material[0].type == "ref"
                and material[0].immutable
                and not self._refers_to_identity(material[0])
                and not material[0].removed
            ):
                self.add(
                    "result-material",
                    "Material must be an immutable reference to a physical record.",
                    source,
                    ("result", "material"),
                )
        found = every.get(spec.released_field)
        state = found[0] if found is not None else None
        if state is None or state.type != "state" or state.removed:
            states = [n for n, (g, _) in every.items() if g.type == "state" and not g.removed]
            self.add(
                "result-released-without-states",
                f"`{spec.released_field}` isn't a state field of `{entity_type.name}`: a "
                "result goes back once the service's own state field says it's released.",
                source,
                ("result", "released", "field"),
                hint=schemas._did_you_mean(spec.released_field, states)
                if states
                else "Add a `type: state` field for the service's review.",
            )
            return
        names = set(state.live_states)
        for index, name in enumerate(spec.released_states):
            at: Path = ("result", "released", "states", index)
            if name not in names:
                self.state_ref(name, names, source, at)
            elif name == state.initial:
                self.add(
                    "result-released-initial",
                    f"`{name}` is where new results start, so they'd go back before anyone "
                    "reviewed them.",
                    source,
                    at,
                    hint="Release a result from a state the service sets after reviewing it.",
                )
            elif state.states[name].on_edit:
                self.add(
                    "result-release-on-edit",
                    f"A result in `{name}` is already released to its requester. "
                    "Give earlier states the return for correction.",
                    source,
                    at,
                )
            elif spec.requires_review(state, name) and not state.states[name].locks:
                self.add(
                    "result-release-lock",
                    f"`{name}` must lock the result to preserve the values signed for at release.",
                    source,
                    at,
                )
        for st in state.states.values():
            if st.on_edit in spec.released_states:
                self.add(
                    "result-release-on-edit",
                    f"Correcting a result in `{st.name}` would release it to the requester. "
                    "Return it to a state before release.",
                    source,
                    ("result", "released", "states"),
                )

    def state_on_edit(self, f: Field, state: State, source: Document, at: Path) -> None:
        """A correction needs a locking source and a destination the editor can enter."""
        name = state.on_edit
        if name is None:
            return
        target = f.states.get(name)
        if state.removed or f.removed:
            message = f"`{state.name}` belongs to a removed field or state, so no one edits it."
        elif not state.locks:
            message = f"`{state.name}` must lock the record to require a return on edit."
        elif state.signatures and not self.historical:
            message = (
                f"Leaving the signed lock `{state.name}` for a correction requires an explicit "
                "signed return. Remove `on_edit` from this state."
            )
        elif target is None or target.removed:
            self.state_ref(name, set(f.live_states), source, (*at, "on_edit"))
            return
        elif target.name == state.name:
            message = f"An edit in `{state.name}` must return the record to a different state."
        elif target.locks:
            message = f"`{name}` locks the record, so the correction cannot change its values."
        elif target.signatures:
            message = f"Entering `{name}` requires a signature, so an edit cannot set it."
        elif not target.set_by:
            message = f"An edit cannot return a record to `{name}` because no lab role can set it."
        else:
            return
        self.add("state-on-edit", message, source, (*at, "on_edit"))

    def state_signatures(self, f: Field, state: State, source: Document, at: Path) -> None:
        """The electronic signatures setting a state needs (ADR 0126): who gives each, and
        whether the state can be signed for at all."""
        for index, wanted in enumerate(state.signatures):
            if wanted.role is not None:
                self.lab_role(wanted.role, source, (*at, "signatures", index, "role"))
        if not state.signatures:
            return
        if state.removed:
            self.add(
                "state-signatures",
                f"`{state.name}` is removed: no record is set to it, so it's signed for by no one.",
                source,
                (*at, "signatures"),
                key=True,
            )
        elif state.name == f.initial:
            self.add(
                "state-signatures",
                f"`{state.name}` is where new records start, without signatures, so setting "
                "it can't need them.",
                source,
                (*at, "signatures"),
                key=True,
                hint="Sign for the states records go to from it.",
            )

    def state_field(
        self,
        entity_type: EntityType,
        f: Field,
        source: Document,
        path: Path,
        fields: dict[str, Field],
    ) -> None:
        """A state field (ADR 0125): its states, who sets each, and where a removed one's
        records go."""
        live = f.live_states
        if f.initial is not None:
            first = f.states.get(f.initial)
            if first is None:
                self.state_ref(f.initial, set(live), source, (*path, "initial"))
            elif first.locks and not first.removed:
                self.add(
                    "state-initial",
                    f"`{f.initial}` is where new records start, so it can't lock them: they "
                    "couldn't be filled in.",
                    source,
                    (*path, "states", f.initial, "locks"),
                    key=True,
                )
        for state in f.states.values():
            at: Path = (*path, "states", state.name)
            for index, role in enumerate(state.set_by):
                self.lab_role(role, source, (*at, "set_by", index))
            self.state_signatures(f, state, source, at)
            self.state_on_edit(f, state, source, at)
            if f.removed and f.moved_to is not None:
                continue  # Where its records go is the field's `moved_to`.
            if state.removed:
                if state.locks:
                    self.add(
                        "state-removed",
                        f"`{state.name}` is removed: no record is in it to lock.",
                        source,
                        (*at, "locks"),
                        key=True,
                    )
                if state.name == f.initial:
                    self.add(
                        "state-initial",
                        f"`{state.name}` is where new records start, so it can't be removed.",
                        source,
                        at,
                        key=True,
                        hint="Make another state `initial` first.",
                    )
                if state.moved_to is None:
                    self.add(
                        "state-removed",
                        f"`{state.name}` is removed, so the records in it move to another "
                        "state: say which, with `moved_to`.",
                        source,
                        at,
                        key=True,
                    )
                elif state.moved_to not in live:
                    self.state_ref(state.moved_to, set(live), source, (*at, "moved_to"))
            elif state.moved_to is not None:
                self.add(
                    "state-removed",
                    f"`{state.name}` isn't removed, so its records stay in it: `moved_to` is "
                    "for a removed state.",
                    source,
                    (*at, "moved_to"),
                    key=True,
                )
            elif not state.set_by and state.name != f.initial and not f.removed:
                self.add(
                    "state-unreachable",
                    f"No one can set `{f.name}` to `{state.name}`: it isn't where records "
                    "start, and no lab role sets it.",
                    source,
                    at,
                    key=True,
                    hint="Give it `set_by:` the lab roles that may, or mark it `removed: true`.",
                )
        if f.removed and f.moved_to is not None:
            self.state_move(entity_type, f, source, path, fields)

    def assignments(
        self, fields: dict[str, Field], entity_type: EntityType, source: Document
    ) -> None:
        """An assignment field counting in some states names a state field of the records'
        type, and states of it (ADR 0122, 0125)."""
        every = self.all_fields(entity_type)
        for name, f in fields.items():
            if f.assignment_field is None:
                continue
            path: Path = ("fields", name, "assignment")
            found = every.get(f.assignment_field)
            target = found[0] if found is not None else None
            if target is None or target.type != "state" or target.removed:
                states = [n for n, (g, _) in every.items() if g.type == "state" and not g.removed]
                self.add(
                    "assignment-without-states",
                    f"`{f.assignment_field}` isn't a state field of `{entity_type.name}`, so "
                    f"`{name}` can't be assigned only in some of its states.",
                    source,
                    (*path, "field"),
                    hint=schemas._did_you_mean(f.assignment_field, states)
                    if states
                    else "Use `assignment: true`, or add a `type: state` field.",
                )
                continue
            names = set(target.live_states)
            for index, state in enumerate(f.assignment_states):
                if state not in names:
                    self.state_ref(state, names, source, (*path, "states", index))

    def due(
        self, entity_type: EntityType, source: Document, every: dict[str, tuple[Field, EntityType]]
    ) -> None:
        """``due`` names a date the records are due by (ADR 0122)."""
        name = entity_type.due
        assert name is not None  # noqa: S101 - only called with one
        found = every.get(name)
        if found is None or found[0].removed:
            self.add(
                "unknown-field",
                f"`{name}` isn't a field of `{entity_type.name}`.",
                source,
                ("due",),
                hint=schemas._did_you_mean(
                    name, [n for n, (f, _) in every.items() if not f.removed]
                ),
            )
            return
        f, owner = found
        if f.type not in ("date", "datetime"):
            self.add(
                "due-not-a-date",
                f"`{name}` is a {f.type} field: what's due is due on a date.",
                source,
                ("due",),
                hint="Name a `date` or `datetime` field.",
            )
        elif f.registry is not None or name in owner.identity:
            self.add(
                "due-in-registry",
                f"`{name}` is its registry entry's, every project's: when work is due is each "
                "project's own.",
                source,
                ("due",),
            )

    def field_roles(
        self, entity_type: EntityType, source: Document, every: dict[str, tuple[Field, EntityType]]
    ) -> None:
        """The fields playing the roles Iki knows (ADR 0284): each a field of its records'
        of the role's kind, named once in a lineage; and a concrete sequence's residues
        (ADR 0285)."""
        ancestors = self.ancestors(entity_type)
        for role, name in entity_type.roles.items():
            at: Path = ("roles", role)
            bound = next((a for a in ancestors if role in a.roles), None)
            if bound is not None:
                self.add(
                    "role-rebound",
                    f"`{bound.qualified_name}` names `{bound.roles[role]}` as its {role}: its "
                    f"subtypes' {role} is that field too, so whatever reads a {bound.name}'s "
                    f"{role} reads each of theirs.",
                    source,
                    at,
                    hint=f"Take `{role}` out of `roles`.",
                )
                continue
            found = every.get(name)
            if found is None or found[0].removed:
                self.add(
                    "unknown-field",
                    f"`{name}` isn't a field of `{entity_type.name}`.",
                    source,
                    at,
                    hint=schemas._did_you_mean(
                        name, [n for n, (f, _) in every.items() if not f.removed]
                    ),
                )
                continue
            f = found[0]
            if role == "residues" and (
                f.type != "string" or f.format not in effective.RESIDUE_ALPHABETS
            ):
                self.add(
                    "role-field",
                    f"`{name}` isn't a sequence of residues: a residues field is a string with "
                    "a sequence format, which says their alphabet.",
                    source,
                    at,
                    hint="Name a string field with `format: "
                    + "`, `".join(sorted(effective.RESIDUE_ALPHABETS))
                    + "`.",
                )
            elif role == "formula" and f.type != "string":
                self.add(
                    "role-field",
                    f"`{name}` is a {f.type} field: a molecular formula is written as a string, "
                    "such as C6H12O6.",
                    source,
                    at,
                )
        if (
            not entity_type.abstract
            and effective.SEQUENCE in self.effective_of(entity_type).lineage
            and "residues" not in self.effective_of(entity_type).roles
        ):
            self.add(
                "residues-unbound",
                f"`{entity_type.name}` is a sequence (`{effective.SEQUENCE}`): name the field "
                "holding its residues, so alignments, exports, runs and agents read them by it.",
                source,
                ("roles",) if isinstance(source.data, dict) and "roles" in source.data else (),
                hint="Add `roles: { residues: <field> }`.",
            )

    def amount(
        self, entity_type: EntityType, source: Document, every: dict[str, tuple[Field, EntityType]]
    ) -> None:
        """A type's amount (ADR 0139): kept in a unit that adds up, on records that are each a
        thing in the lab, and the states that hold it. An abstract type's states may hold
        the amounts its subtypes keep, each in its own unit, as a lot's quarantine holds an
        oligo lot's nmol and a protein lot's mg (ADR 0162); each concrete type must have
        one."""
        lineage = [entity_type, *self.ancestors(entity_type)]
        held = [
            (name, state, owner)
            for name, (f, owner) in every.items()
            if f.type == "state"
            for state, s in f.states.items()
            if s.holds_amount and not s.removed
        ]
        amount = next((t.amount for t in lineage if t.amount is not None), None)
        for purpose, roles in entity_type.draw_purposes.items():
            inherited = next(
                (t.draw_purposes[purpose] for t in lineage[1:] if purpose in t.draw_purposes),
                (),
            )
            for index, role in enumerate(roles):
                path: Path = ("draw_purposes", purpose, "by", index)
                known = self.lab_role(role, source, path)
                if known and inherited and role not in inherited:
                    self.add(
                        "draw-purpose-widens-roles",
                        f"`{role}` is outside the inherited lab roles for `{purpose}` draws.",
                        source,
                        path,
                        hint="Keep the inherited roles or declare a subset.",
                    )
        if amount is None:
            if entity_type.abstract:
                return
            if any(t.draw_purposes for t in lineage):
                self.add(
                    "draw-purposes-without-amount",
                    f"`{entity_type.name}` has draw purposes but has no amount to take.",
                    source,
                    ("draw_purposes",) if entity_type.draw_purposes else ("name",),
                    key=True,
                    hint="Give the type an `amount`.",
                )
            if any(t.draw_guards for t in lineage):
                self.add(
                    "draw-guards-without-amount",
                    f"`{entity_type.name}` has draw guards but has no amount to take.",
                    source,
                    ("draw_guards",) if entity_type.draw_guards else ("name",),
                    key=True,
                    hint="Give the type an `amount`.",
                )
            for name, state, owner in held:
                own = owner is entity_type
                self.add(
                    "holds-without-amount",
                    f"`{state}` holds the record's amount, and `{entity_type.name}` has none."
                    if own
                    else f"`{owner.qualified_name}`'s `{state}` holds the record's amount, and "
                    f"`{entity_type.name}`, which extends it, has none.",
                    source,
                    ("fields", name, "states", state, "holds_amount") if own else ("name",),
                    key=True,
                    hint="Give the type an `amount`, or leave `holds_amount` off."
                    if own
                    else "Give the type an `amount`: its records are things in the lab.",
                )
            return
        if (entity_type.amount is not None or amount.material == "id") and any(
            t.identity for t in lineage
        ):
            self.add(
                "amount-on-identity",
                f"`{entity_type.name}` has an identity, so each project has one record of each "
                "of its entries: its records aren't things in the lab, with an amount each.",
                source,
                ("amount",) if entity_type.amount is not None else ("name",),
                key=True,
                hint="Give the amount to a type of what the lab keeps (a sample, a lot, a tube) "
                "that refers to the entry.",
            )
        if entity_type.amount is None:
            return
        parent = next((t for t in lineage[1:] if t.amount is not None), None)
        if parent is not None:
            self.add(
                "amount-twice",
                f"`{parent.qualified_name}`, which this type extends, has an amount already.",
                source,
                ("amount",),
                key=True,
                hint="A record has one amount: leave this one off.",
            )
        try:
            unit = units.parse(amount.unit)
        except units.UnitError as error:
            self.add("amount-unit", str(error), source, ("amount", "unit"))
            return
        if unit.is_special or not _adds_up(unit):
            self.add(
                "amount-unit",
                f"`{amount.unit}` doesn't measure how much there is of something.",
                source,
                ("amount", "unit"),
                hint="Use a volume (uL), a mass (mg), an amount of substance (umol) or a "
                "count ({vial}).",
            )
            return
        if amount.display_unit is not None:
            self.amount_unit(amount.display_unit, unit, source, ("amount", "display_unit"))
        if amount.concentration is not None:
            self.concentration(amount.concentration, unit, every, source)
        if amount.aliquots is not None:
            self.aliquots(entity_type, amount, source)
        if amount.material == "id":
            if amount.aliquots is None:
                self.add(
                    "amount-aliquots",
                    "A record that is its own material needs a container type for its aliquots.",
                    source,
                    ("amount", "material"),
                    hint="Name a container type and its reference to this record in `aliquots`.",
                )
        elif amount.material is not None:
            self.material(amount.material, every, source)
        for index, name in enumerate(amount.follows):
            found = every.get(name)
            f = found[0] if found is not None else None
            if f is None or f.removed or f.type != "ref":
                self.add(
                    "amount-follows",
                    f"`follows` names references of the type, to where its material came from, "
                    f"and `{name}` "
                    + ("isn't one." if f is None or f.removed else f"is a {f.type} field."),
                    source,
                    ("amount", "follows", index),
                    hint="Such as `lot: { type: ref, to: iki/lots.Lot }`.",
                )

    def amount_unit(self, code: str, unit: units.Unit, source: Document, at: Path) -> None:
        try:
            other = units.parse(code)
        except units.UnitError as error:
            self.add("amount-unit", str(error), source, at)
            return
        if not other.is_commensurable(unit):
            self.add(
                "amount-unit",
                f"`{code}` and `{unit.code}` measure different things.",
                source,
                at,
            )

    def concentration(
        self,
        name: str,
        unit: units.Unit,
        every: dict[str, tuple[Field, EntityType]],
        source: Document,
    ) -> None:
        found = every.get(name)
        f = found[0] if found is not None else None
        at: Path = ("amount", "concentration")
        if f is None or f.removed or f.type != "quantity" or f.unit is None:
            self.add(
                "amount-concentration",
                f"`concentration` names a quantity field of the type, and `{name}` "
                + ("isn't one." if f is None else f"is a {f.type} field."),
                source,
                at,
                hint="Such as `concentration: { type: quantity, unit: mg/mL }`.",
            )
            return
        concentration = units.parse(f.unit)
        litre = units.parse("L")
        held = units.parse(f"({f.unit}).L")
        # Times a volume, it's how much: of what the amount counts, or (for an amount kept
        # as a volume) of anything but another volume.
        fits = _adds_up(held) and (
            not held.is_commensurable(litre)
            if unit.is_commensurable(litre)
            else held.is_commensurable(unit)
        )
        if concentration.is_special or not fits:
            self.add(
                "amount-concentration",
                f"`{name}` is in {f.unit}, which isn't how much of it a volume holds, in a "
                f"unit {unit.code} converts to.",
                source,
                at,
                hint="Such as mg/mL for an amount in uL or in mg.",
            )

    def material(
        self, name: str, every: dict[str, tuple[Field, EntityType]], source: Document
    ) -> None:
        found = every.get(name)
        f = found[0] if found is not None else None
        if f is None or f.removed or f.type != "ref" or not f.immutable:
            self.add(
                "amount-material",
                f"`material` names an immutable reference of the type, to what its "
                f"amount is of, and `{name}` "
                + (
                    "isn't one."
                    if f is None or f.removed
                    else f"is a {f.type} field."
                    if f.type != "ref"
                    else "isn't immutable: what a record is of can't change."
                ),
                source,
                ("amount", "material"),
                hint="Such as `of: { type: ref, to: iki/core.Entity, required: true, "
                "immutable: true }`.",
            )

    def aliquots(self, entity_type: EntityType, amount: Amount, source: Document) -> None:
        target = self.types.get(amount.aliquots or "")
        at: Path = ("amount", "aliquots")
        if amount.aliquots in self.invalid_types:
            return
        if target is None:
            self.add(
                "unknown-type",
                f"`{amount.aliquots}` isn't a type of this plugin or its dependencies.",
                source,
                (*at, "type"),
            )
            return
        lineage = [target, *self.ancestors(target)]
        theirs = next((t.amount for t in lineage if t.amount is not None), None)
        if theirs is None:
            self.add(
                "amount-aliquots",
                f"`{target.qualified_name}` has no amount, so its records can't be aliquots.",
                source,
                (*at, "type"),
                hint="Give that type an `amount` too.",
            )
        elif theirs.material == "id":
            self.add(
                "amount-aliquots",
                "An aliquot cannot identify itself as new material.",
                source,
                (*at, "type"),
            )
        elif _measures_as(theirs.unit, amount.unit) is False:
            # What an aliquot holds is what its source lost: in the same kind of unit.
            self.add(
                "amount-aliquots",
                f"`{target.name}`'s amount is in {theirs.unit}, which doesn't measure what "
                f"`{entity_type.name}`'s does, in {amount.unit}: an aliquot holds what it lost.",
                source,
                (*at, "type"),
            )
        fields = {n: f for t in lineage for n, f in t.fields.items()}
        of = fields.get(amount.aliquots_of or "")
        mine = {
            entity_type.qualified_name,
            *(t.qualified_name for t in self.ancestors(entity_type)),
        }
        if amount.material == "id" and theirs is not None and theirs.material != "id":
            material = fields.get(theirs.material or "")
            if material is None or not (mine | {"iki/core.Entity"}).intersection(material.to):
                self.add(
                    "amount-aliquots",
                    f"`{target.name}` needs a material reference to `{entity_type.name}`.",
                    source,
                    (*at, "type"),
                )
        if of is None or of.type != "ref" or not mine & set(of.to):
            self.add(
                "amount-aliquots",
                f"`of` names `{target.name}`'s reference to the record each aliquot was split "
                f"from, a `{entity_type.name}`, and `{amount.aliquots_of}` isn't one.",
                source,
                (*at, "of"),
                hint=f"Such as `{entity_type.name.lower()}: {{ type: ref, to: "
                f"{entity_type.qualified_name} }}`.",
            )

    def state_ref(self, name: str, names: set[str], source: Document, path: Path) -> None:
        self.add(
            "unknown-state",
            f"`{name}` isn't one of its states, or it's removed.",
            source,
            path,
            hint=schemas._did_you_mean(name, sorted(names)),
        )

    def state_move(
        self,
        entity_type: EntityType,
        f: Field,
        source: Document,
        path: Path,
        fields: dict[str, Field],
    ) -> None:
        """Where a removed state field's records go: another of the type's own state fields,
        each of its states to one of that field's."""
        assert f.moved_to is not None  # noqa: S101 - checked by the caller
        to = entity_type.fields.get(f.moved_to.field)
        if to is None or to.removed or to.type != "state" or to is f:
            self.add(
                "unknown-field",
                f"`{f.moved_to.field}` isn't another of `{entity_type.name}`'s own state fields.",
                source,
                (*path, "moved_to", "field"),
                hint=schemas._did_you_mean(
                    f.moved_to.field,
                    [n for n, x in entity_type.fields.items() if x.type == "state" and x is not f],
                ),
            )
            return
        mapped = dict(f.moved_to.states)
        for name in f.live_states:
            target = mapped.get(name)
            if target is None:
                self.add(
                    "state-removed",
                    f"`{f.name}` is removed: say which of `{to.name}`'s states records in "
                    f"`{name}` move to.",
                    source,
                    (*path, "moved_to", "states"),
                    key=True,
                )
            elif target not in to.live_states:
                self.state_ref(
                    target, set(to.live_states), source, (*path, "moved_to", "states", name)
                )
        for name in mapped:
            if name not in f.live_states:
                self.state_ref(name, set(f.live_states), source, (*path, "moved_to", "states"))

    def identity(
        self, entity_type: EntityType, source: Document, every: dict[str, tuple[Field, EntityType]]
    ) -> None:
        if entity_type.identity:
            self.identity_owner(entity_type, source)
        for index, name in enumerate(entity_type.identity):
            path: Path = ("identity", index)
            if name not in every:
                self.add(
                    "unknown-field",
                    f"`{name}` isn't a field of `{entity_type.name}`.",
                    source,
                    path,
                    hint=schemas._did_you_mean(name, list(every)),
                )
                continue
            f, owner = every[name]
            if owner is not entity_type:
                self.add(
                    "identity-inherited",
                    f"`{name}` is a field of `{owner.qualified_name}`, which this type extends: "
                    "an identity names the type's own fields.",
                    source,
                    path,
                    hint="An identity's fields are its registry entry's (ADR 0115), so they're "
                    "declared with it.",
                )
                continue
            if f.removed:
                self.add(
                    "identity-removed",
                    f"`{name}` is part of the identity, so it can't be removed.",
                    source,
                    path,
                    hint="Entities are told apart by their identity for as long as they exist.",
                )
                continue
            if not _identifies(f):
                self.add(
                    "identity-type",
                    f"`{name}` is a {f.type} field"
                    + (f" of {f.items.type} items" if f.items is not None else "")
                    + ", which can't be part of an identity.",
                    source,
                    path,
                    hint="Identities compare values exactly, so they use text, numbers, enums, "
                    "dates, files, references, or lists of references, values or rows whose "
                    "identity fields are those, not floats, quantities or JSON.",
                )
            for option in (f.topology, f.strands):
                if option is not None and option in every and option not in entity_type.identity:
                    self.add(
                        "identity-incomplete",
                        f"`{name}`'s identity reads it by `{option}`, so `{option}` is part "
                        "of the identity too.",
                        source,
                        path,
                        hint=f"Add `{option}` to `identity`, required and immutable.",
                    )
            if not f.immutable:
                self.add(
                    "identity-not-fixed",
                    f"`{name}` is part of the identity, so it must be immutable.",
                    source,
                    path,
                    hint="An identity that could change can't tell entities apart.",
                )
            if f.registry is not None:
                self.add(
                    "registry-on-identity",
                    f"`{name}` is part of the identity, which is its registry entry's already.",
                    source,
                    ("fields", name, "registry"),
                    key=True,
                    hint="Leave `registry` off identity fields.",
                )
        named = [every[name][0] for name in entity_type.identity if name in every]
        if named and not any(f.required for f in named):
            self.add(
                "identity-not-required",
                "The identity needs a required field: each of its fields could be empty.",
                source,
                ("identity",),
                hint="An identity that could be empty can't tell entities apart. Optional "
                "fields can be part of one beside a required field (an antibody's heavy "
                "and light chains).",
            )
        entry = set(entity_type.identity)
        inherited = next((a for a in self.ancestors(entity_type) if a.identity), None)
        for name, f in entity_type.fields.items():
            # A subtype of a registered type has registry fields of its own (ADR 0165).
            if f.registry is not None and not entity_type.identity and inherited is None:
                self.add(
                    "registry-without-identity",
                    f"`{name}` is marked `registry: {f.registry}`, but `{entity_type.name}` "
                    "has no identity, so it has no registry entries.",
                    source,
                    ("fields", name, "registry"),
                    key=True,
                    hint="Give the type an `identity`, or leave `registry` off.",
                )
                continue
            if name in entry or f.registry is not None:
                self.registry_field(
                    f,
                    source,
                    ("fields", name),
                    identity=name in entry,
                    # A list's label and description are its curators', given when an option
                    # is added (ADR 0270): its label is required, its description free text.
                    curated=entity_type.is_list and name in LIST_FIELDS,
                )

    def identity_owner(self, entity_type: EntityType, source: Document) -> None:
        """A type that declares an identity can't be under another identity, or under a
        concrete type, whose entities are records with no entry (ADR 0115)."""
        ancestors = self.ancestors(entity_type)
        above = next((a for a in ancestors if a.identity), None)
        if above is not None:
            self.add(
                "identity-redeclared",
                f"`{entity_type.name}` extends `{above.qualified_name}`, which has an identity "
                "already.",
                source,
                ("identity",),
                key=True,
                hint="A subtype's entities are told apart by its parent's identity. Leave "
                "`identity` off, or extend an abstract type instead.",
            )
            return
        concrete = next((a for a in ancestors if not a.abstract), None)
        if concrete is not None:
            self.add(
                "identity-under-concrete",
                f"`{entity_type.name}` has an identity, but extends `{concrete.qualified_name}`, "
                "a concrete type without one.",
                source,
                ("identity",),
                key=True,
                hint="References to that type hold its entities themselves, which an "
                "identity's entities aren't: they're registry entries. Extend an abstract type.",
            )

    def registry_field(
        self,
        f: Field,
        source: Document,
        path: Path,
        *,
        identity: bool = False,
        curated: bool = False,
    ) -> None:
        """What a field of a registry entry (an identity, derived or fact field) must be."""
        cells = list(f.items.fields.values()) if f.is_rows and f.items is not None else []
        if (names_people(f) or any(c.type == "person" for c in cells)) and not identity:
            self.add(
                "person-in-registry",
                f"`{f.name}` names people, who are each project's own: it can't be its "
                "registry entry's.",
                source,
                (*path, "registry"),
                key=True,
                hint="Leave `registry` off: who tests or reviews something is the project's "
                "record of it (ADR 0122).",
            )
            return
        targets = list(f.to) if f.type == "ref" else []
        if f.type == "list" and f.items is not None and f.items.type == "ref":
            targets = list(f.items.to)
        # A row's references are its entry's too (ADR 0137).
        targets += [t for c in cells if c.type == "ref" for t in c.to]
        for target in targets:
            found = self.types.get(target)
            if found is None or target == naming.ENTITY:
                # Unknown ones are reported as such; `iki/core.Entity` in an entry is any
                # entry, as the database holds it (ADR 0161).
                continue
            if not self.registered(found):
                self.add(
                    "registry-refers-to-record",
                    f"`{f.name}` is its registry entry's, but refers to `{found.qualified_name}`, "
                    "whose entities are each project's own records.",
                    source,
                    path,
                    key=True,
                    hint="An entry is every holding project's, so it can refer only to other "
                    "entries: types with an identity. Make the field the record's.",
                )
        if identity or f.registry != "fact" or curated:
            return
        item = f.items if f.type == "list" and f.items is not None else f
        if item.type in ("text", "json", "file"):
            self.add(
                "registry-fact-free-text",
                f"`{f.name}` is a {f.type} field, which can't be a registry fact.",
                source,
                (*path, "registry"),
                key=True,
                hint="Facts are curated values, such as codes, classes and numbers, never free "
                "text. Make it an enum, a patterned string, or the record's.",
            )
        elif item.type == "string" and not (item.pattern or item.format):
            self.add(
                "registry-fact-free-text",
                f"`{f.name}` is a string with no `pattern` or `format`, which can't be a "
                "registry fact.",
                source,
                (*path, "registry"),
                key=True,
                hint="Facts are codes and names with rules, never free text: give it a "
                "`pattern` (a CAS number's is `^[1-9][0-9]{1,6}-[0-9]{2}-[0-9]$`).",
            )
        # A fixed fact (ADR 0143) is given when the entry is registered and never changes,
        # as what other entries were registered against: a monomer's structure. It may be
        # required. Any other fact is set when it's known, and registrars correct it.
        for flag, why in (
            (
                "required",
                "a fact is set when it's known, not when an entry is registered, unless it's "
                "fixed (`immutable: true`)",
            ),
            ("computed", "a fact is curated, not computed; derived values are `registry: derived`"),
        ):
            if getattr(f, flag) and not (flag == "required" and f.immutable):
                self.add(
                    f"registry-fact-{flag}",
                    f"Registry facts can't be {flag}: {why}.",
                    source,
                    (*path, flag),
                    key=True,
                )

    def title(self, template: str, source: Document, fields: dict[str, Field]) -> None:
        path: Path = ("display", "title")
        for problem, hint in _title_problems(template, fields):
            self.add("display-title", problem, source, path, hint=hint)

    def viewers(self, entity_type: EntityType, source: Document, fields: dict[str, Field]) -> None:
        """Each viewer names fields the type has, of the kind it shows, once per type."""
        seen: set[str] = set()
        for index, viewer in enumerate(entity_type.viewers):
            path: Path = ("display", "viewers", index)
            if viewer.viewer in seen:
                self.add(
                    "viewer-twice",
                    f"This type has a `{viewer.viewer}` viewer already.",
                    source,
                    (*path, "viewer"),
                    hint="Give each viewer once: it's one tab on the type's pages.",
                )
            seen.add(viewer.viewer)
            for role, wanted, kinds in _VIEWER_ROLES[viewer.viewer]:
                name = getattr(viewer, role)
                if not name:
                    continue
                found = fields.get(name)
                if found is None:
                    self.add(
                        "unknown-field", f"This type has no field `{name}`.", source, (*path, role)
                    )
                elif not kinds(found) or (
                    role in ("alignment", "structure")
                    and not self.format_viewer(found, viewer.viewer)
                ):
                    self.add(
                        "viewer-field",
                        f"`{name}` isn't {wanted}, which a {viewer.viewer} viewer's "
                        f"`{role}` shows.",
                        source,
                        (*path, role),
                    )
            tracked: set[str] = set()
            for at, track in enumerate(viewer.tracks):
                where: Path = (*path, "tracks", at, "field")
                found = fields.get(track.field)
                if found is None:
                    self.add(
                        "unknown-field", f"This type has no field `{track.field}`.", source, where
                    )
                elif not per_residue(found):
                    self.add(
                        "viewer-field",
                        f"`{track.field}` isn't a json field of one number per residue, which a "
                        "track shows.",
                        source,
                        where,
                        hint="Give it a schema such as "
                        '{type: array, items: {type: [number, "null"]}}.',
                    )
                elif track.field in tracked:
                    self.add(
                        "viewer-field",
                        f"This viewer shows `{track.field}` as a track already.",
                        source,
                        where,
                    )
                tracked.add(track.field)

    def format_viewer(self, field: Field, viewer: str) -> bool:
        if self.formats is None:
            return True  # A declaration conflict already identifies the formats to fix.
        for choice in field.media_types:
            fmt = self.formats.formats.get(choice) or self.formats.by_media_type(choice)
            if not fmt or not fmt.viewer or fmt.viewer.partition("-")[0] != viewer:
                return False
        return True

    def exports(self, entity_type: EntityType, source: Document, fields: dict[str, Field]) -> None:
        """Each export names fields the type has, of the kind it writes, once per type. A
        sequence export's sequence is a DNA or RNA field, or a reference to a type that
        exports a sequence (ADR 0171); a protein export's, a protein field; a compound
        export's structure, a text field holding a Molfile (ADR 0229)."""
        seen: set[str] = set()
        for index, export in enumerate(entity_type.exports):
            path: Path = ("exports", index)
            if export.export in seen:
                self.add(
                    "export-twice",
                    f"This type has a `{export.export}` export already.",
                    source,
                    (*path, "export"),
                    hint="Give each export once: its files are offered on the record's page.",
                )
            seen.add(export.export)
            if isinstance(export, ReportExport):
                self.report_export(export, source, path, fields)
                continue
            if isinstance(export, PluginExport):
                writer_manifest.check_export(self, entity_type, export, source, path, fields)
                continue
            for role, wanted, kinds in self._export_roles(export.export):
                name = getattr(export, role)
                if not name:
                    continue
                found = fields.get(name)
                if found is None:
                    self.add(
                        "unknown-field", f"This type has no field `{name}`.", source, (*path, role)
                    )
                elif not kinds(found):
                    self.add(
                        "export-field",
                        f"`{name}` isn't {wanted}, which a {export.export} export's `{role}` "
                        "writes.",
                        source,
                        (*path, role),
                    )

    def report_export(
        self, export: ReportExport, source: Document, path: Path, fields: dict[str, Field]
    ) -> None:
        report, review = fields.get(export.report), fields.get(export.review)
        if report is None or report.type != "json":
            self.add(
                "export-field",
                f"`{export.report}` must be a JSON field for a signed report.",
                source,
                (*path, "report"),
            )
        if review is None or review.type != "state":
            self.add(
                "export-field",
                f"`{export.review}` must be a state field for a signed report.",
                source,
                (*path, "review"),
            )
            return
        for index, name in enumerate(export.issued):
            state = review.states.get(name)
            if (
                state is None
                or state.removed
                or not state.signatures
                or not state.locks
                or state.on_edit
            ):
                self.add(
                    "export-state",
                    f"`{name}` must be a signed locking state of `{export.review}` with no "
                    "edit destination.",
                    source,
                    (*path, "issued", index),
                )

    def _export_roles(self, kind: str) -> tuple[tuple[str, str, Callable[[Field], bool]], ...]:
        described: tuple[str, str, Callable[[Field], bool]] = (
            "description",
            "a string or text field",
            lambda f: f.type in ("string", "text"),
        )
        organism: tuple[str, str, Callable[[Field], bool]] = (
            "organism",
            "a reference to iki/core.Organism",
            lambda f: f.type == "ref" and f.to == ("iki/core.Organism",),
        )
        if kind == "protein":
            return (
                (
                    "sequence",
                    "a protein sequence (format protein_sequence)",
                    lambda f: f.type == "string" and f.format == "protein_sequence",
                ),
                described,
                organism,
            )
        if kind == "compound":
            return (("structure", "a text field holding a Molfile", lambda f: f.type == "text"),)
        return (
            (
                "sequence",
                "a DNA or RNA sequence, or a reference to a type that exports one",
                lambda f: (
                    (f.type == "string" and f.format in NUCLEIC_FORMATS)
                    or (f.type == "ref" and len(f.to) == 1 and self._exports_sequence(f.to[0]))
                ),
            ),
            ("features", "a json field", lambda f: f.type == "json"),
            described,
            ("accession", "a string field", lambda f: f.type == "string"),
            organism,
        )

    def _exports_sequence(self, qualified: str) -> bool:
        """Whether a type, or its nearest ancestor that declares exports, exports a sequence
        written from a field of its own: a reference to one that only refers on isn't."""
        target = self.types.get(qualified)
        if target is None:
            return False
        declared = self.effective_of(target).exports
        fields = {n: f for n, (f, _) in self.all_fields(target).items()}
        return any(
            isinstance(e, Export)
            and (f := fields.get(e.sequence)) is not None
            and f.type == "string"
            and f.format in NUCLEIC_FORMATS
            for e in declared
        )

    def hrid_prefixes(self) -> None:
        seen: dict[str, str] = {}
        for other in self.others.values():
            assert other.plugin is not None  # noqa: S101 - an invariant, not a check
            for t in other.plugin.types.values():
                if t.hrid_prefix:
                    seen[t.hrid_prefix] = t.qualified_name
        for qualified, t in self.plugin.types.items():
            if not t.hrid_prefix:
                continue
            if t.hrid_prefix in seen:
                self.add(
                    "duplicate-hrid-prefix",
                    f"`{seen[t.hrid_prefix]}` already uses the HRID prefix `{t.hrid_prefix}`.",
                    self.loaded.type_sources[qualified],
                    ("hrid", "prefix"),
                    hint="Each type needs its own prefix, so an ID says what it is.",
                )
            else:
                seen[t.hrid_prefix] = qualified

    def table_names(self) -> None:
        """Every table and model Iki generates must have its own name, and fit PostgreSQL."""
        tables: dict[str, str] = {}
        models: dict[str, str] = {}
        generated: list[tuple[str, str, str, Document, Path, str]] = []

        def add(
            table: str,
            model: str,
            what: str,
            source: Document,
            path: Path,
            kind: str,
            fields: dict[str, Field],
        ) -> None:
            generated.append((table, model, what, source, path, kind))
            generated.extend(
                (
                    naming.list_table(table, f.name),
                    naming.list_model(model, f.name),
                    f"the list `{f.name}` of {what}",
                    source,
                    ("fields", f.name),
                    "field",
                )
                for f in _list_tables(fields)
            )

        for qualified, t in self.plugin.types.items():
            source = self.loaded.type_sources[qualified]
            record, entry = split_entry(t.fields, t.identity)
            add(t.table, t.name, f"`{t.name}`", source, ("name",), "type", record)
            if t.identity:
                add(
                    naming.entry_table(t.table),
                    naming.entry_model(t.name),
                    f"the registry entries of `{t.name}`",
                    source,
                    ("name",),
                    "type",
                    entry,
                )
        targets: set[str] = set()
        for extension, source in zip(
            self.plugin.extensions, self.loaded.extension_sources, strict=True
        ):
            if extension.extends_type in targets:
                continue  # reported as a duplicate extension
            targets.add(extension.extends_type)
            record, entry = split_entry(extension.fields)
            add(
                extension.table,
                naming.extension_model(extension.extends_type),
                f"the extension of `{extension.extends_type}`",
                source,
                ("extends_type",),
                "plugin",
                record,
            )
            if entry:
                add(
                    naming.entry_table(extension.table),
                    naming.entry_model(naming.extension_model(extension.extends_type)),
                    f"the extension of `{extension.extends_type}`'s registry entries",
                    source,
                    ("extends_type",),
                    "plugin",
                    entry,
                )
        for table, model, what, source, path, shorten in generated:
            key = path[0] == "fields"
            clash = tables.get(table) or models.get(model.lower())
            if clash is not None:
                self.add(
                    "duplicate-table",
                    f"{what[0].upper()}{what[1:]} and {clash} would share the "
                    f"{'table' if table in tables else 'model'} "
                    f"`{table if table in tables else model}`; names ignore case.",
                    source,
                    path,
                    key=key,
                    hint="Rename one of them.",
                )
            tables.setdefault(table, what)
            models.setdefault(model.lower(), what)
            # Lists keep their history in their owner's history table.
            names = (table,) if shorten == "field" else (table, naming.history_table(table))
            for name in names:
                if len(name) > MAX_IDENTIFIER:
                    self.add(
                        "name-too-long",
                        f"The table `{name}` for {what} would be {len(name)} characters; "
                        f"PostgreSQL allows {MAX_IDENTIFIER}.",
                        source,
                        path,
                        key=key,
                        hint={
                            "type": "Shorten the type's name, or the plugin's or its namespace.",
                            "plugin": "Shorten the plugin's name or its namespace.",
                            "field": "Shorten the field's name, the type's, or the plugin's.",
                        }[shorten],
                    )
                    break

    # Extensions ---------------------------------------------------------------------------------

    def extension(self, extension: Extension, source: Document, seen: dict[str, Document]) -> None:
        path: Path = ("extends_type",)
        if extension.extends_type in BUILTIN_TYPES:
            self.add(
                "extends-builtin",
                f"Plugins can't add fields to `{extension.extends_type}`, since that would add "
                "them to every entity of every type.",
                source,
                path,
            )
            return
        if extension.extends_type.split(".")[0] == self.plugin.name:
            self.add(
                "extension-of-own-type",
                f"`{extension.extends_type}` is this plugin's own type.",
                source,
                path,
                hint="Add the fields to the type itself.",
            )
            return
        target = self.reference(extension.extends_type, source, path)
        if target is None:
            return
        if extension.extends_type in seen:
            first = seen[extension.extends_type]
            self.add(
                "duplicate-extension",
                f"This plugin already extends `{extension.extends_type}` "
                f"(in {first.file}, line {first.start.line}).",
                source,
                path,
                hint="Put all the fields this plugin adds to a type in one extension.",
            )
            return
        seen[extension.extends_type] = source
        taken = {
            name: f"`{owner.qualified_name}`"
            for name, (_, owner) in self.all_fields(target).items()
        }
        for other in self.others.values():
            assert other.plugin is not None  # noqa: S101 - an invariant, not a check
            for other_extension in other.plugin.extensions:
                if other_extension.extends_type == extension.extends_type:
                    for name in other_extension.fields:
                        taken.setdefault(name, f"the {other.plugin.name} plugin's extension")
        names = {n: f for n, (f, _) in self.all_fields(target).items()} | dict(extension.fields)
        for name, f in extension.fields.items():
            field_path: Path = ("fields", name)
            if f.type == "state":
                self.add(
                    "state-field-in-extension",
                    "A state field is its type's own plugin's (ADR 0125): an extension can't "
                    "add one.",
                    source,
                    (*field_path, "type"),
                )
            if name in taken:
                self.add(
                    "extension-field-conflict",
                    f"`{name}` is already a field of `{extension.extends_type}` "
                    f"(from {taken[name]}).",
                    source,
                    field_path,
                    key=True,
                    hint="Choose another name, for example "
                    f"`{naming.short_name(self.plugin.name)}_{name}`.",
                )
            if f.required:
                self.add(
                    "extension-field-required",
                    f"Fields a plugin adds to another's type can't be required: `{name}` would be "
                    "empty on existing entities, and the owning plugin doesn't know to fill it in.",
                    source,
                    (*field_path, "required"),
                    key=True,
                )
            self.field(f, source, field_path, siblings=names)
            if f.assignment_field is not None:
                self.assignments({name: f}, target, source)
            if target.is_list and f.registry != "fact":
                self.add(
                    "list-extension-record-field",
                    f"`{extension.extends_type}` is a lab list (ADR 0270), whose options are "
                    f"registry entries with no records: `{name}` must be its options' own.",
                    source,
                    field_path,
                    key=True,
                    hint="Mark it `registry: fact`: its curators give each option's value.",
                )
                continue
            if target.is_list:
                for list_target in self._list_targets(f):
                    found = self.types.get(list_target)
                    if found is not None and not found.is_list:
                        self.add(
                            "list-refers-to-entry",
                            f"`{name}` refers to `{list_target}`, which isn't a lab list: every "
                            "project in a registry reads its lists, so their options refer only "
                            "to other lists' options.",
                            source,
                            field_path,
                        )
            if f.registry is None:
                continue
            if not self.registered(target):
                self.add(
                    "registry-without-identity",
                    f"`{name}` is marked `registry: {f.registry}`, but `{extension.extends_type}` "
                    "has no identity, so it has no registry entries.",
                    source,
                    (*field_path, "registry"),
                    key=True,
                    hint="Leave `registry` off.",
                )
            else:
                self.registry_field(f, source, field_path)

    # Fields -------------------------------------------------------------------------------------

    def field(
        self,
        f: Field,
        source: Document,
        path: Path,
        *,
        siblings: dict[str, Field],
        item: bool = False,
    ) -> None:
        if f.topology is not None or f.strands is not None:
            self.sequence_options(f, source, path, siblings)
        if not item:
            if (
                f.name in RESERVED_FIELD_NAMES
                or f.name.endswith("_ptr")
                or f.name.startswith("iki_")
            ):
                self.add(
                    "reserved-name",
                    f"`{f.name}` is reserved; every entity already has it, or Python or Django "
                    "use it.",
                    source,
                    path,
                    key=True,
                    hint="Choose a more specific name, such as `sample_type` instead of `type`.",
                )
            if f.type in ("ref", "person") and f"{f.name}_id" in siblings:
                what = "reference" if f.type == "ref" else "person field"
                self.add(
                    "column-conflict",
                    f"The {what} `{f.name}` is stored in a column named `{f.name}_id`, which "
                    "is also the name of another field.",
                    source,
                    path,
                    key=True,
                )
            if f.assignment and not names_people(f):
                self.add(
                    "assignment-not-people",
                    f"`{f.name}` is a {f.type} field: only a field naming people assigns its "
                    "records to them.",
                    source,
                    (*path, "assignment"),
                    key=True,
                    hint="Make it `type: person`, or a list with `items: { type: person }`.",
                )
            if f.computed and f.has_default:
                self.add(
                    "computed-with-default",
                    "Computed fields are set by actions, so they can't have a default.",
                    source,
                    (*path, "default"),
                    key=True,
                )
            if f.computed and f.required:
                self.add(
                    "computed-required",
                    "Computed fields are set by actions, so they can't be required: people "
                    "creating an entity couldn't fill them in.",
                    source,
                    (*path, "required"),
                    key=True,
                )
            if f.reason_on_clear and (f.computed or f.immutable or f.registry or f.removed):
                self.add(
                    "clear-reason-field",
                    "Only an editable record field can require a reason when it is cleared.",
                    source,
                    (*path, "reason_on_clear"),
                    key=True,
                )
            if f.indexed:
                self.index(f, source, path)
            if f.link is not None:
                self.link(f.link, source, (*path, "link"))
            if f.unique and not f.indexed:
                self.add(
                    "unique-needs-index",
                    "Unique fields must be indexed, so a value is found, and checked, with one "
                    "lookup.",
                    source,
                    (*path, "unique"),
                    key=True,
                    hint="Add `indexed: true` and a `max_length`.",
                )
        self.options(f, source, path)
        if f.type == "quantity":
            self.quantity(f, source, path)
        if f.type == "ref":
            self.targets(f, source, (*path, "to"))
        if f.type == "person" and f.role is not None:
            self.lab_role(f.role, source, (*path, "role"))
        if f.type == "file" and f.max_size is not None and f.max_size > MAX_FILE_SIZE:
            self.add(
                "file-too-large",
                "Files can be at most 5 TiB (the most object storage holds in one object).",
                source,
                (*path, "max_size"),
            )
        if f.type == "json" and f.schema is not None:
            try:
                Draft202012Validator.check_schema(f.schema)
            except SchemaError as error:
                self.add(
                    "invalid-json-schema",
                    f"`schema` isn't a valid JSON Schema: {error.message}.",
                    source,
                    (*path, "schema", *error.path),
                )
            else:
                # A form starts from the schema's default, so it must be a value it takes.
                if "default" in f.schema:
                    wrong = next(
                        iter(Draft202012Validator(f.schema).iter_errors(f.schema["default"])),
                        None,
                    )
                    if wrong is not None:
                        self.add(
                            "invalid-default",
                            f"The default doesn't fit `schema`: {wrong.message}.",
                            source,
                            (*path, "schema", "default", *wrong.absolute_path),
                        )
        if f.is_rows:
            assert f.items is not None  # noqa: S101 - lists have items
            self.rows(f.items, source, (*path, "items"))
        elif f.type == "list" and f.items is not None:
            if f.items.type == "state":
                self.add(
                    "state-field-in-list",
                    "A list's items can't be states: give the type a state field of its own.",
                    source,
                    (*path, "items", "type"),
                )
            self.field(f.items, source, (*path, "items"), siblings=siblings, item=True)
        if f.has_default:
            for message in value_problems(f, f.default):
                self.add("invalid-default", f"The default {message}", source, (*path, "default"))

    def sequence_options(
        self, f: Field, source: Document, path: Path, siblings: dict[str, Field]
    ) -> None:
        """A sequence's ``topology`` and ``strands`` (ADR 0138): each an enum field beside
        it with the values its identity reads it by."""
        for option, wanted, formats in (
            ("topology", ("circular",), SEQUENCE_FORMATS),
            ("strands", ("double", "single"), NUCLEIC_FORMATS),
        ):
            name = getattr(f, option)
            if name is None:
                continue
            if f.format not in formats:
                self.add(
                    "sequence-option",
                    f"`{option}` is for a "
                    + ("sequence" if option == "topology" else "DNA or RNA sequence")
                    + f", and `{f.name}` "
                    + (f"is a {f.format.replace('_', ' ')}." if f.format else "has no format."),
                    source,
                    (*path, option),
                    key=True,
                    hint="Leave it off, or give the field a sequence `format`.",
                )
                continue
            named = siblings.get(name)
            if named is None or named.type != "enum" or not set(wanted) <= set(named.values):
                self.add(
                    "sequence-option",
                    f"`{option}` names an enum field beside `{f.name}` with the values "
                    f"{' and '.join(wanted)}, and `{name}` "
                    + (
                        "isn't one."
                        if named is None
                        else f"is a {named.type} field."
                        if named.type != "enum"
                        else f"has {', '.join(named.values)}."
                    ),
                    source,
                    (*path, option),
                    hint="Such as `topology: { type: enum, values: [linear, circular] }`.",
                )

    def row_id(self, row: Field, source: Document, path: Path) -> None:
        """A row's ID field: a string Iki fills in, so not one a new row must give."""
        assert row.row_id is not None  # noqa: S101 - the caller's check
        cell = row.fields.get(row.row_id)
        wrong = (
            f"isn't one of its fields; they are: {', '.join(row.fields)}."
            if cell is None
            else f"is a {cell.type} field, and an ID is a string."
            if cell.type != "string"
            else "is required, but a new row leaves its ID out for Iki to give."
            if cell.required
            else "has a default, but each row's ID is its own."
            if cell.has_default
            else f"holds at most {cell.max_length} characters, and an ID is a 36-character UUID."
            if cell.max_length is not None and cell.max_length < 36
            else None
        )
        if wrong is not None:
            self.add(
                "row-id-field",
                f"`row_id` names the row's field that holds its stable ID, and `{row.row_id}` "
                + wrong,
                source,
                (*path, "row_id"),
                hint="Name an optional string field, such as `observation_id: { type: string, "
                "max_length: 36 }`.",
            )

    def rows(self, row: Field, source: Document, path: Path) -> None:
        """A list's rows (ADR 0137): each of their fields as a field, the one naming each
        row's role an enum, and each role's counts and references within the fields'."""
        for name, cell in row.fields.items():
            at = (*path, "fields", name)
            if name in ROW_COLUMNS:
                self.add(
                    "reserved-name",
                    f"`{name}` is reserved in a row: each row's table has it already.",
                    source,
                    at,
                    key=True,
                    hint="Choose a more specific name.",
                )
            self.field(cell, source, at, siblings=row.fields)
        for name in row.identity:
            if name not in row.fields:
                self.add(
                    "unknown-row-field",
                    f"The row has no field `{name}`.",
                    source,
                    (*path, "identity", row.identity.index(name)),
                    hint=f"Its fields are: {', '.join(row.fields)}.",
                )
        if row.row_id is not None:
            self.row_id(row, source, path)
        if row.by is None:
            return
        by = row.fields.get(row.by)
        if by is None or by.type not in ("enum", "string"):
            self.add(
                "row-role-not-enum",
                f"`by` names the row's field that holds its role, an enum or a string, and "
                f"`{row.by}` "
                + ("isn't one of its fields." if by is None else f"is a {by.type} field."),
                source,
                (*path, "by"),
                hint="Add an enum field, such as `role: { type: enum, values: [heavy, light] }`.",
            )
            return
        if not by.required:
            self.add(
                "row-role-optional",
                f"Every row has a role, so `{row.by}` must be required.",
                source,
                (*path, "fields", row.by, "required"),
                hint="Add `required: true`.",
            )
        self.row_roles(row, by, row.roles, source, (*path, "roles"))

    def row_roles(
        self,
        row: Field,
        by: Field,
        roles: Mapping[str, RowRole],
        source: Document,
        path: Path,
        within: Mapping[str, RowRole] | None = None,
        *,
        entries: bool = False,
    ) -> None:
        """What each role of a list's rows takes: a role its ``by`` field can hold, at most
        as many rows as it can have, and references within the row's (and, for a subtype's
        refinement, ``within`` its ancestor's roles: ADR 0164)."""
        for name, role in roles.items():
            at = (*path, name)
            if by.type == "enum" and name not in by.values:
                self.add(
                    "unknown-row-role",
                    f"`{name}` isn't one of `{row.by}`'s values.",
                    source,
                    at,
                    key=True,
                    hint=f"Its values are: {', '.join(by.values)}.",
                )
            elif by.type == "string" and value_problems(by, name):
                self.add(
                    "unknown-row-role",
                    f"`{name}` isn't a value `{row.by}` can hold: "
                    + " ".join(value_problems(by, name)),
                    source,
                    at,
                    key=True,
                )
            above = within.get(name) if within is not None else None
            if within is not None and above is None:
                self.add(
                    "refines-unknown-role",
                    f"`{name}` isn't one of the roles this type's parent gives its rows.",
                    source,
                    at,
                    key=True,
                    hint=f"Its roles are: {', '.join(within)}." if within else None,
                )
            if role.min is not None and role.max is not None and role.min > role.max:
                self.add(
                    "min-above-max",
                    f"`min` ({role.min}) is more than `max` ({role.max}).",
                    source,
                    (*at, "min"),
                )
            for cell_name, types in role.to.items():
                ref = row.fields.get(cell_name)
                if ref is None or ref.type != "ref":
                    self.add(
                        "row-role-not-reference",
                        f"A role says what a row's references may refer to, and "
                        f"`{cell_name}` "
                        + ("isn't one of its fields." if ref is None else "isn't a reference."),
                        source,
                        (*at, "to", cell_name),
                        key=True,
                    )
                    continue
                for index, target in enumerate(types):
                    where = (*at, "to", cell_name, *((index,) if len(types) > 1 else ()))
                    named = self.reference(target, source, where)
                    if named is None:
                        continue
                    if entries and not self.registered(named):
                        self.add(
                            "registry-refers-to-record",
                            f"`{target}`'s entities are each project's own records, and these "
                            "rows are part of the identity, which refers only to entries.",
                            source,
                            where,
                            hint="Name types with an identity (ADR 0115).",
                        )
                    lineage = {named.qualified_name} | {
                        a.qualified_name for a in self.ancestors(named)
                    }
                    if naming.ENTITY not in ref.to and not lineage & set(ref.to):
                        self.add(
                            "row-role-type-not-allowed",
                            f"`{target}` isn't one of the types `{cell_name}` refers to "
                            f"({', '.join(ref.to)}), or one of their subtypes.",
                            source,
                            where,
                        )
                    elif (
                        above is not None
                        and cell_name in above.to
                        and not lineage & set(above.to[cell_name])
                    ):
                        self.add(
                            "refines-wider",
                            f"`{target}` isn't one of the types a {name} row's "
                            f"`{cell_name}` refers to in this type's parent "
                            f"({', '.join(above.to[cell_name])}), or one of their subtypes.",
                            source,
                            where,
                            hint="A subtype narrows what its parent's rows take.",
                        )
            if above is not None:
                # A reference its parent's role holds to some types is held to them here too:
                # left out, it would refer to anything its field does.
                for cell_name, theirs_to in above.to.items():
                    if cell_name not in role.to:
                        self.add(
                            "refines-wider",
                            f"A {name} row's `{cell_name}` refers to "
                            f"{', '.join(theirs_to)} in this type's parent, and a subtype "
                            "narrows what its parent's rows take, so it can't leave that out.",
                            source,
                            at,
                            hint=f"Give `to: {{ {cell_name}: {', '.join(theirs_to)} }}`, or "
                            "types among those.",
                        )
                for bound, mine, theirs in (
                    ("min", role.min, above.min),
                    ("max", role.max, above.max),
                ):
                    wider = theirs is not None and (
                        mine is None or (mine < theirs if bound == "min" else mine > theirs)
                    )
                    if wider:
                        self.add(
                            "refines-wider",
                            f"A {name} row's `{bound}` is {theirs} in this type's parent, and a "
                            "subtype narrows what its parent's rows take"
                            + (f", so its `{bound}` can't be {mine}." if mine is not None else "."),
                            source,
                            (*at, bound) if mine is not None else at,
                            hint=f"Give `{bound}: {theirs}`, or one within it.",
                        )

        # A role its parent's rows must have is one its rows must have: left out, a subtype's
        # record could lack what its parent's requires.
        for name, above_role in (within or {}).items():
            if name not in roles and above_role.min:
                self.add(
                    "refines-drops-role",
                    f"Its parent's rows have at least {above_role.min} {name} "
                    f"row{'s' if above_role.min != 1 else ''}, and a subtype narrows what its "
                    f"parent's rows take, so it can't leave out the {name} role.",
                    source,
                    path,
                    hint=f"Give the {name} role, with `min: {above_role.min}` or more.",
                )

    def refines(self, entity_type: EntityType, source: Document) -> None:
        """What a subtype's records take in fields it inherits (ADR 0283): each part of a
        refinement one the field's kind has, and narrower than its parent's, so each of the
        subtype's records is one of its parent's too (ADR 0164, 0235)."""
        if not entity_type.refines:
            return
        parent = entity_type.extends
        inherited = (
            effective.resolve_one(self.types, parent).fields
            if parent is not None and parent in self.types
            else {}
        )
        lineage = [entity_type, *self.ancestors(entity_type)]
        for name, r in entity_type.refines.items():
            at: Path = ("refines", name)
            if name in entity_type.fields:
                self.add(
                    "refines-own-field",
                    f"`{name}` is this type's own field: say what it takes there.",
                    source,
                    at,
                    key=True,
                )
                continue
            above = inherited.get(name)
            if above is None or above.removed:
                self.add(
                    "refines-unknown-field",
                    f"`refines` narrows a field this type inherits, and `{name}` isn't a "
                    "field of its parents"
                    + (" now: it was removed." if above is not None else "."),
                    source,
                    at,
                    key=True,
                    hint=schemas._did_you_mean(name, [n for n, f in inherited.items()]),
                )
                continue
            self.narrowing(above, r, source, at)
            if r.to:
                self.refines_to(name, above, r.to, source, at)
            if r.roles:
                self.refines_roles(name, above, r.roles, lineage, source, at)

    def narrowing(self, above: Field, r: Refinement, source: Document, at: Path) -> None:
        """A refinement's ``required``, ``values``, ``min``, ``max`` and ``default``: each one
        the field's kind takes, and within what its parent's records take."""
        name = above.name
        item = above.items if above.type == "list" else None
        if r.required and above.computed:
            self.add(
                "refines-computed",
                f"`{name}` is computed: its action or calculation sets it, so a subtype can't "
                "require it.",
                source,
                (*at, "required"),
                key=True,
            )
        if r.values:
            enum = item if item is not None else above
            if enum.type != "enum":
                self.add(
                    "refines-not-enum",
                    f"`values` narrows an enum, or a list of them, and `{name}` is "
                    f"{_a(above.type)} field.",
                    source,
                    (*at, "values"),
                    key=True,
                )
            else:
                for index, value in enumerate(r.values):
                    if value not in enum.values:
                        self.add(
                            "refines-wider",
                            f"`{value}` isn't one of the values `{name}` takes in this type's "
                            f"parent ({', '.join(f'`{v}`' for v in enum.values)}).",
                            source,
                            (*at, "values", index),
                            hint="A subtype narrows what its parent's records take: give some "
                            "of its values.",
                        )
        for bound in ("min", "max"):
            mine = getattr(r, bound)
            if mine is None:
                continue
            if above.type not in ("int", "float", "quantity"):
                self.add(
                    "refines-not-a-number",
                    f"`{bound}` narrows a number or a quantity, and `{name}` is "
                    f"{_a(above.type)} field.",
                    source,
                    (*at, bound),
                    key=True,
                )
                continue
            if above.type == "int" and (isinstance(mine, bool) or not float(mine).is_integer()):
                self.add(
                    "refines-not-whole",
                    f"`{name}` is a whole number, so its `{bound}` is one too.",
                    source,
                    (*at, bound),
                )
            theirs = getattr(above, bound)
            if theirs is not None and (mine < theirs if bound == "min" else mine > theirs):
                self.add(
                    "refines-wider",
                    f"`{name}`'s `{bound}` is {theirs} in this type's parent, and a subtype "
                    f"narrows what its parent's records take, so its `{bound}` can't be {mine}.",
                    source,
                    (*at, bound),
                    hint=f"Give `{bound}: {theirs}`, or one within it.",
                )
        low = r.min if r.min is not None else above.min
        high = r.max if r.max is not None else above.max
        if low is not None and high is not None and low > high:
            self.add(
                "min-above-max",
                f"`min` ({low}) is more than `max` ({high}).",
                source,
                (*at, "min" if r.min is not None else "max"),
            )
            return
        mine_field = effective.narrowed(above, r)
        if r.has_default:
            if above.computed:
                self.add(
                    "computed-with-default",
                    "Computed fields are set by actions, so they can't have a default.",
                    source,
                    (*at, "default"),
                    key=True,
                )
            elif not _takes_default(above):
                self.add(
                    "refines-default",
                    f"`{name}` is {_a(above.type)} field, which takes no default.",
                    source,
                    (*at, "default"),
                    key=True,
                )
            else:
                for message in value_problems(mine_field, r.default):
                    self.add("invalid-default", f"The default {message}", source, (*at, "default"))
        elif above.has_default and (r.values or r.min is not None or r.max is not None):
            # A new record given no value would have its parent's, which it can't take.
            wrong = value_problems(mine_field, above.default)
            if wrong:
                self.add(
                    "refines-default-outside",
                    f"`{name}`'s default in this type's parent, `{json.dumps(above.default)}`, "
                    f"isn't one its records take: the default {' '.join(wrong)}",
                    source,
                    at,
                    hint="Give a `default` of its own, within what it narrows to.",
                )

    def refines_to(
        self, name: str, above: Field, targets: tuple[str, ...], source: Document, at: Path
    ) -> None:
        """A refinement's ``to`` (ADR 0235): of an inherited reference or list of them, each
        type one of its parent's or a subtype of one."""
        ref = above.items if above.type == "list" else above
        if ref is None or ref.type != "ref":
            self.add(
                "refines-not-a-reference",
                f"`to` narrows the types a reference refers to, and `{name}` isn't a reference.",
                source,
                (*at, "to"),
                key=True,
            )
            return
        several = len(targets) > 1
        for index, target in enumerate(targets):
            where: Path = (*at, "to", index) if several else (*at, "to")
            named = self.reference(target, source, where)
            if named is None or naming.ENTITY in ref.to:
                continue
            kinds = {named.qualified_name} | {a.qualified_name for a in self.ancestors(named)}
            if not kinds & set(ref.to):
                self.add(
                    "refines-wider",
                    f"`{target}` isn't one of the types `{name}` refers to in this type's "
                    f"parent ({', '.join(ref.to)}), or one of their subtypes.",
                    source,
                    where,
                    hint="A subtype narrows what its parent's references refer to.",
                )

    def refines_roles(
        self,
        name: str,
        above: Field,
        roles: Mapping[str, RowRole],
        lineage: list[EntityType],
        source: Document,
        at: Path,
    ) -> None:
        """A refinement's ``roles`` (ADR 0164): of an inherited list of rows with roles by
        one of its fields, every role its rows may have, within its parent's."""
        row = above.items if above.is_rows else None
        if row is None or row.by is None:
            self.add(
                "refines-not-rows",
                f"`roles` gives the roles of a list of rows with roles by one of its fields, "
                f"and `{name}` " + ("isn't a list of rows." if row is None else "has no `by`."),
                source,
                (*at, "roles"),
                key=True,
            )
            return
        by = row.fields.get(row.by)
        if by is None:
            return  # reported on the type that declares it
        owner = next((t for t in lineage if t.identity), None)
        entries = owner is not None and name in owner.identity
        self.row_roles(
            row, by, roles, source, (*at, "roles"), within=row.roles or None, entries=entries
        )

    def targets(self, f: Field, source: Document, path: Path) -> None:
        """A reference's types (ADR 0136): each one the plugin can name, and none of them
        already among another's subtypes, or ``iki/core.Entity`` beside others."""
        several = len(f.to) > 1
        found: dict[str, EntityType] = {}
        for index, target in enumerate(f.to):
            at = (*path, index) if several else path
            named = self.reference(target, source, at)
            if named is not None:
                found[target] = named
        if not several:
            return
        for target, named in found.items():
            if named.is_list:
                self.add(
                    "list-among-types",
                    f"`{target}` is a lab list (ADR 0270): a reference to its options names "
                    "that list alone, as a dropdown offers one list's options.",
                    source,
                    (*path, f.to.index(target)),
                    hint=f"Write `to: {target}`, or give the other types a field of their own.",
                )
        if naming.ENTITY in f.to:
            self.add(
                "reference-to-any-and-more",
                f"`{naming.ENTITY}` is any entity already, so it's the only type a reference "
                "to it names.",
                source,
                path,
                hint=f"Write `to: {naming.ENTITY}` alone, or list only the types it may refer to.",
            )
            return
        for target, named in found.items():
            above = next(
                (
                    other
                    for other in found
                    if other != target
                    and other in {a.qualified_name for a in self.ancestors(named)}
                ),
                None,
            )
            if above is not None:
                self.add(
                    "reference-type-twice",
                    f"`{target}` extends `{above}`, which the reference names already: a "
                    "reference to a type refers to its subtypes too.",
                    source,
                    (*path, f.to.index(target)),
                    hint=f"Leave `{target}` out.",
                )

    def link(self, template: str, source: Document, path: Path) -> None:
        """A field's link: an https URL, with the value and the plugin's settings in it."""
        names = re.findall(r"\{([^{}]*)\}", template)
        for name in names:
            setting = name.removeprefix("settings.")
            if name == "value" or (
                name.startswith("settings.") and setting in self.plugin.settings
            ):
                continue
            self.add(
                "link-unknown-placeholder",
                f"`{{{name}}}` isn't something a link can have: use `{{value}}`, or "
                "`{settings.<name>}` with one of the plugin's settings.",
                source,
                path,
            )
        if "value" not in names:
            self.add(
                "link-without-value",
                "A link leads to the value's record, so it has `{value}` in it.",
                source,
                path,
            )
        if not template.startswith(("https://", "{settings.")):
            self.add(
                "link-not-https",
                "Links start with `https://`, or with a setting that holds the start of one.",
                source,
                path,
            )

    def index(self, f: Field, source: Document, path: Path) -> None:
        if f.type not in INDEXABLE:
            reason = {
                "text": "Long text is searched with full-text search instead.",
                "json": "Index the values you filter on as their own fields.",
                "file": "Files are found by their other fields.",
                "list": "Index the values you filter on as their own fields.",
            }[f.type]
            self.add(
                "unsupported-index",
                f"{f.type.capitalize()} fields can't be indexed. {reason}",
                source,
                (*path, "indexed"),
                key=True,
            )
        elif f.type == "string" and (f.max_length is None or f.max_length > MAX_INDEXED_LENGTH):
            self.add(
                "index-needs-max-length",
                f"Indexed strings need a `max_length` of at most {MAX_INDEXED_LENGTH}, so every "
                "value fits in the index.",
                source,
                (*path, "indexed"),
                key=True,
                hint="Add `max_length: 255`, or drop `indexed` for long values such as sequences.",
            )

    def file_choices(self, field: Field, source: Document, path: Path) -> None:
        if self.formats is None:
            return
        for index, choice in enumerate(field.media_types):
            owner, dot, _ = choice.partition(".")
            if not dot or self.formats.by_media_type(choice):
                continue
            where = (*path, "media_types", index)
            if choice not in self.formats.formats:
                # A dotted MIME subtype may resemble a format ID. Keep standard media
                # trees and private x- types literal, even if an installed plugin happens
                # to be called application/vnd. Other dotted names identify formats.
                top_level = choice.partition("/")[0]
                if top_level in _MEDIA_TREES or top_level.startswith("x-"):
                    continue
                if owner not in self.unavailable:
                    self.add("unknown-format", f"Unknown file format `{choice}`.", source, where)
            elif (
                owner not in {self.plugin.name, naming.CORE}
                and owner not in self.plugin.dependencies
            ):
                self.add(
                    "undeclared-dependency",
                    f"`{choice}` belongs to {owner}. Add `{owner}: {{}}` "
                    "under `dependencies` in plugin.yaml.",
                    source,
                    where,
                )

    def options(self, f: Field, source: Document, path: Path) -> None:
        if f.media_types:
            self.file_choices(f, source, path)
        for low, high, a, b in (
            (f.min, f.max, "min", "max"),
            (f.min_length, f.max_length, "min_length", "max_length"),
            (f.min_items, f.max_items, "min_items", "max_items"),
        ):
            if low is not None and high is not None and low > high:
                self.add(
                    "min-above-max",
                    f"`{a}` ({low}) is more than `{b}` ({high}).",
                    source,
                    (*path, a),
                )
        if f.pattern is not None:
            for message in pattern_problems(f.pattern):
                self.add(
                    "invalid-pattern",
                    message,
                    source,
                    (*path, "pattern"),
                    hint="The database checks patterns too, so they can use only what Python "
                    "and PostgreSQL regular expressions share.",
                )

    def quantity(self, f: Field, source: Document, path: Path) -> None:
        canonical = self.unit(f.unit, source, (*path, "unit"))
        if f.display_unit is None:
            return
        display = self.unit(f.display_unit, source, (*path, "display_unit"))
        if (
            canonical is not None
            and display is not None
            and not canonical.is_commensurable(display)
        ):
            self.add(
                "display-unit-mismatch",
                f"`{f.display_unit}` doesn't measure the same thing as `{f.unit}`, so values "
                "can't be shown in it.",
                source,
                (*path, "display_unit"),
            )

    def unit(self, code: str | None, source: Document, path: Path) -> units.Unit | None:
        assert code is not None  # noqa: S101 - an invariant, not a check
        try:
            return units.parse(code)
        except units.UnitError as error:
            self.add("unknown-unit", error.message, source, path, hint=error.hint)
            return None

    # Settings -----------------------------------------------------------------------------------

    def settings(self, *, require_policy: bool) -> None:
        for name, setting in self.plugin.settings.items():
            path: Path = ("settings", name)
            if (
                require_policy
                and not setting.has_default
                and "required" not in self.source.data["settings"][name]
            ):
                self.add(
                    "setting-policy",
                    f"`{name}` must declare whether it is required or give a default.",
                    self.source,
                    path,
                    key=True,
                    hint="Give a `default`, write `required: true`, or write "
                    "`required: false` to let deployments leave it unset.",
                )
            if setting.required and setting.has_default:
                self.add(
                    "required-with-default",
                    "A setting with a default can't be required: deployments that don't set it "
                    "get the default.",
                    self.source,
                    (*path, "required"),
                    key=True,
                )
            if looks_secret(name):
                self.add(
                    "setting-looks-secret",
                    f"`{name}` looks like a secret. Settings are kept in plain text in each "
                    "deployment's config repository.",
                    self.source,
                    path,
                    key=True,
                    severity=Severity.WARNING,
                    hint="Declare secrets on the actions that need them (`secrets:`); admins map "
                    "them to the secret manager.",
                )
            self.options(setting, self.source, path)
            if setting.has_default:
                for message in value_problems(setting, setting.default):
                    self.add(
                        "invalid-default", f"The default {message}", self.source, (*path, "default")
                    )


# --- Shared rules ------------------------------------------------------------------------------


def satisfies(version: int, constraint: str) -> bool:
    for part in constraint.split(","):
        match = re.fullmatch(r"\s*(==|!=|>=|<=|>|<)\s*([0-9]+)\s*", part)
        assert match is not None  # the schema checked the form  # noqa: S101 - an invariant, not a check
        op, number = match.group(1), int(match.group(2))
        ok = {
            "==": version == number,
            "!=": version != number,
            ">=": version >= number,
            "<=": version <= number,
            ">": version > number,
            "<": version < number,
        }[op]
        if not ok:
            return False
    return True


def looks_secret(name: str) -> bool:
    words = name.split("_")
    if any(w in SECRET_WORDS for w in words):
        return True
    return any(pair == tuple(words[i : i + 2]) for pair in SECRET_PAIRS for i in range(len(words)))


_UNPORTABLE = (
    (re.compile(r"\(\?[=!]"), "lookahead (`(?=` or `(?!`)"),
    (re.compile(r"\(\?<[=!]"), "lookbehind (`(?<=` or `(?<!`)"),
    (re.compile(r"\(\?P?<[A-Za-z_]"), "named groups"),
    (re.compile(r"\(\?P[=>]"), "named group references"),
    (re.compile(r"\\[1-9]"), "backreferences"),
    (re.compile(r"\(\?[aiLmsux-]+[:)]"), "inline flags"),
    (re.compile(r"\\[AZzG]"), "`\\A`, `\\Z`, `\\z` or `\\G` anchors"),
    (re.compile(r"[*+?}]\+"), "possessive quantifiers"),
    (re.compile(r"\(\?>"), "atomic groups"),
)


def pattern_problems(pattern: str) -> list[str]:
    try:
        re.compile(pattern)
    except re.error as error:
        return [f"`pattern` isn't a valid regular expression: {error}."]
    return [
        f"`pattern` uses {what}, which PostgreSQL can't check."
        for regex, what in _UNPORTABLE
        if regex.search(pattern)
    ]


def value_problems(f: Field, value: Any) -> list[str]:  # noqa: ANN401 - manifest data is JSON-shaped
    """Why `value` doesn't fit `f`, as sentences that follow "The value " or "The default "."""
    kind = f.type
    if kind in ("string", "text"):
        if not isinstance(value, str):
            return ["must be text."]
        problems = []
        if value == "" and f.min_length != 0:
            return ["can't be empty; leave it out instead."]
        if f.type == "string" and ("\n" in value or "\r" in value):
            problems.append("must be one line; use a `text` field for longer text.")
        if f.min_length is not None and len(value) < f.min_length:
            problems.append(f"must be at least {f.min_length} characters long.")
        if f.max_length is not None and len(value) > f.max_length:
            problems.append(f"must be at most {f.max_length} characters long.")
        if f.format == "helm":
            if (wrong := helm.problem(value)) is not None:
                problems.append(wrong)
        elif f.format and not matches_format(value, f.format):
            problems.append(f"isn't a valid {f.format.replace('_', ' ')}.")
        if f.pattern and not pattern_problems(f.pattern) and re.fullmatch(f.pattern, value) is None:
            problems.append(
                f"must use {f.pattern_help}."
                if f.pattern_help
                else f"doesn't match the pattern `{f.pattern}`."
            )
        return problems
    if kind in ("int", "float", "quantity"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return ["must be a number."]
        if kind == "int" and not isinstance(value, int):
            return ["must be a whole number."]
        problems = []
        if f.min is not None and value < f.min:
            problems.append(f"must be at least {f.min}.")
        if f.max is not None and value > f.max:
            problems.append(f"must be at most {f.max}.")
        return problems
    if kind == "bool":
        return [] if isinstance(value, bool) else ["must be `true` or `false`."]
    if kind == "enum":
        if value not in f.values:
            return [f"must be one of: {', '.join(f'`{v}`' for v in f.values)}."]
        return []
    if kind == "state":
        if value not in f.live_states:
            return [f"must be one of its states: {', '.join(f'`{v}`' for v in f.live_states)}."]
        return []
    if kind == "date":
        return (
            [] if isinstance(value, str) and _is_date(value) else ["must be a date (YYYY-MM-DD)."]
        )
    if kind == "row":
        if not isinstance(value, dict):
            return ["must be an object of the row's fields."]
        problems = [f"has no field `{k}`." for k in value if k not in f.fields]
        for name, cell in f.fields.items():
            if value.get(name) is None:
                if cell.required and not cell.has_default:
                    problems.append(f"needs `{name}`.")
                continue
            problems.extend(f"`{name}` {m}" for m in value_problems(cell, value[name]))
        return problems
    if kind == "list":
        if not isinstance(value, list):
            return ["must be a list."]
        problems = []
        if f.min_items is not None and len(value) < f.min_items:
            problems.append(f"needs at least {f.min_items} item(s).")
        if f.max_items is not None and len(value) > f.max_items:
            problems.append(f"can have at most {f.max_items} items.")
        if f.items is not None:
            for index, item in enumerate(value):
                problems.extend(f"item {index + 1} {m}" for m in value_problems(f.items, item))
        return problems
    return []  # pragma: no cover (other types take no default or value here)


def _a(kind: str) -> str:
    """A field kind with its article: "an enum", "a string"."""
    return f"an {kind}" if kind[0] in "aeiou" else f"a {kind}"


def _takes_default(f: Field) -> bool:
    """Whether a field of ``f``'s kind can have a default: not a reference, file, person,
    datetime, json or state, nor a list of those or of rows, which have tables of their own."""
    if f.type == "list":
        return f.items is not None and f.items.type not in ("ref", "file", "person", "row")
    return f.type in ("string", "text", "int", "float", "bool", "enum", "date", "quantity")


def _list_tables(fields: dict[str, Field]) -> list[Field]:
    """Lists of references, files, people and rows, which get a table of their own."""
    return [
        f
        for f in fields.values()
        if f.type == "list" and f.items and f.items.type in ("ref", "file", "person", "row")
    ]


def _is_date(value: str) -> bool:
    import datetime as dt

    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return False
    return re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is not None


def _suggest_prefix(name: str) -> str:
    capitals = "".join(c for c in name if c.isupper())
    letters = (capitals if len(capitals) >= 2 else name[:3]).upper()
    return letters[:8] if len(letters) >= 2 else (name.upper() + "X")[:3]


_TOKEN = titles.TOKEN


def _title_problems(
    template: str,
    fields: dict[str, Field],
    *,
    noun: str = "The title",
    kinds: frozenset[str] = TITLE_TYPES,
    constant: bool = False,
) -> Iterable[tuple[str, str | None]]:
    """Check a title template: text with `{{ a or b or "text" }}` parts. A notification's
    message (ADR 0129) is checked the same way, as ``noun``, showing ``kinds`` of field, and
    may be the same text every time (``constant``)."""
    position = 0
    found = False
    while True:
        start = template.find("{{", position)
        stray = template.find("}}", position)
        if stray != -1 and (start == -1 or stray < start):
            yield (f"{noun} has a `}}}}` without a `{{{{`.", None)
            return
        if start == -1:
            break
        end = template.find("}}", start + 2)
        if end == -1:
            yield (f"{noun} has a `{{{{` without a `}}}}`.", None)
            return
        found = True
        yield from _title_expression(template[start + 2 : end], fields, noun=noun, kinds=kinds)
        position = end + 2
    if not found and not constant:
        yield (
            f"{noun} is the same text for every entity.",
            "Name fields in it, such as `{{ name or hrid }}`.",
        )


def _title_expression(
    expression: str,
    fields: dict[str, Field],
    *,
    noun: str = "The title",
    kinds: frozenset[str] = TITLE_TYPES,
) -> Iterable[tuple[str, str | None]]:
    tokens = [m for m in _TOKEN.finditer(expression) if m.group().strip()]
    if not tokens:
        yield (f"{noun} has an empty `{{{{ }}}}`.", None)
        return
    combine = (
        "Titles combine" if noun == "The title" else "Messages combine"
    ) + " fields and quoted text with `or`, as in `{{ name or hrid }}`."
    expect_operand = True
    for token in tokens:
        word, string = token.group("word"), token.group("string")
        if expect_operand:
            if string is not None:
                pass
            elif word is not None and word != "or":
                if word == "hrid":
                    pass
                elif word not in fields:
                    yield (
                        f"{noun} names `{word}`, which isn't a field of this type.",
                        schemas._did_you_mean(word, [*fields, "hrid"]),
                    )
                elif fields[word].type not in kinds:
                    yield (f"{noun} can't show `{word}`, a {fields[word].type} field.", None)
            else:
                yield (
                    f"{noun} has `{token.group().strip()}` where a field name or quoted text "
                    "belongs.",
                    combine,
                )
                return
        elif word != "or":
            yield (f"{noun} has `{token.group().strip()}` where `or` belongs.", combine)
            return
        expect_operand = not expect_operand
    if expect_operand:
        yield (f"{noun} ends with `or`.", None)


# --- Settings values ---------------------------------------------------------------------------


def check_settings_values(plugin: Plugin, file: FilePath, name: str) -> list[Problem]:
    """Check a deployment's settings for `plugin` (a YAML mapping of setting → value)."""
    documents, problems = read_documents(file, name)
    if problems:
        return problems
    return _settings_problems(plugin, documents, name)


def read_settings(
    plugin: Plugin, file: FilePath | None, name: str
) -> tuple[dict[str, Any] | None, list[Problem]]:
    """A deployment's settings for `plugin`, with defaults filled in, and the problems.

    Without a file, every setting takes its default. The values are None when
    there are errors.
    """
    if file is None:
        documents: list[Document] = []
    else:
        documents, problems = read_documents(file, name)
        if problems:
            return None, problems
    problems = _settings_problems(plugin, documents, name)
    if any(p.is_error for p in problems):
        return None, problems
    given = next((d.data for d in documents if d.data is not None), None) or {}
    values = {
        key: given.get(key, setting.default if setting.has_default else None)
        for key, setting in sorted(plugin.settings.items())
    }
    return values, problems


def check_settings_text(plugin: Plugin, text: str, name: str) -> list[Problem]:
    documents, problems = parse_documents(text, name)
    if problems:
        return problems
    return _settings_problems(plugin, documents, name)


def _settings_problems(plugin: Plugin, documents: list[Document], name: str) -> list[Problem]:
    checker = _Checker(FilePath())
    documents = [d for d in documents if d.data is not None]
    if len(documents) > 1:
        checker.add("settings-file", "A settings file holds one mapping.", documents[1])
        return checker.problems
    if not documents:
        values: dict[str, Any] = {}
        document = None
    else:
        document = documents[0]
        if not isinstance(document.data, dict):
            checker.add("settings-file", "Settings must be a mapping of name to value.", document)
            return checker.problems
        values = document.data
    for key, value in values.items():
        setting = plugin.settings.get(key)
        if setting is None:
            checker.add(
                "unknown-setting",
                f"The {plugin.name} plugin has no setting `{key}`.",
                document,
                (key,),
                key=True,
                hint=schemas._did_you_mean(key, list(plugin.settings))
                or (
                    "Its settings: " + ", ".join(f"`{s}`" for s in plugin.settings) + "."
                    if plugin.settings
                    else f"The {plugin.name} plugin takes no settings."
                ),
            )
            continue
        for message in value_problems(setting, value):
            checker.add("invalid-setting", f"`{key}` {message}", document, (key,))
    for key, setting in plugin.settings.items():
        if setting.required and key not in values:
            checker.add(
                "missing-setting",
                f"The {plugin.name} plugin needs `{key}` to be set.",
                document,
                file=name,
                hint=setting.description,
            )
    # What its actions can't run without (ADR 0169): a deployment may leave it unset, and
    # those actions then can't run there, so it's told which.
    for action in sorted(plugin.actions.values(), key=lambda a: a.name):
        for key in action.requires_settings:
            setting = plugin.settings.get(key)
            if setting is None or setting.required or setting.has_default:
                continue  # a plugin problem, or one every deployment has
            if values.get(key) in (None, "", []):
                checker.add(
                    "setting-needed",
                    f"`{key}` isn't set, so {action.label} can't run on this deployment.",
                    document,
                    file=name,
                    severity=Severity.WARNING,
                    hint=setting.description,
                )
    return sorted(checker.problems, key=Problem.sort_key)


def _identifies(f: Field) -> bool:
    """Whether ``f`` can be part of an identity (ADR 0138): a value compared exactly, or a
    list of them, references or rows whose identity fields are, read as a multiset. Not
    files in a list: a row's file is its ID, not its content's hash."""
    if f.type != "list":
        return f.type in IDENTITY_TYPES
    item = f.items
    if item is None:
        return False
    if item.type == "row":
        names = item.identity or tuple(item.fields)
        return all(
            n in item.fields and item.fields[n].type in IDENTITY_TYPES - {"file"} for n in names
        )
    return item.type in IDENTITY_TYPES - {"file"}


#: What an amount can be kept in: how much there is of something, which adds up when
#: it's split and pooled. A volume (length cubed), a mass, or a number of things: moles,
#: counts ({vial}) and arbitrary units such as [IU] (ADR 0139).
_LITRE = units.parse("L")
_GRAM = units.parse("g")


def _measures_as(one: str, other: str) -> bool | None:
    """Whether two units measure the same thing; None if either isn't a unit."""
    try:
        return units.parse(one).is_commensurable(units.parse(other))
    except units.UnitError:
        return None


def _adds_up(unit: units.Unit) -> bool:
    return (
        unit.is_commensurable(_LITRE) or unit.is_commensurable(_GRAM) or (not any(unit.dimension))
    ) and not (unit.is_dimensionless and unit.code in ("%", "[ppm]", "[ppb]"))
