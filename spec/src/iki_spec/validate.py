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
import keyword
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path as FilePath
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from iki_spec import API_VERSION, actions, naming, schemas, titles, triggers, units, workflows
from iki_spec.model import (
    BUILTIN_TYPES,
    Action,
    EntityType,
    Environment,
    Extension,
    Field,
    Plugin,
    Trigger,
    Workflow,
    matches_format,
    qualify,
)
from iki_spec.problems import Path, Problem, Severity
from iki_spec.yamlsource import Document, parse_documents, read_documents

__all__ = ["Result", "check_settings_values", "load_plugin", "read_settings"]

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
}

#: Where each kind of manifest lives in a plugin.
DIRECTORIES = {
    "EntityType": TYPES_DIR,
    "EntityTypeExtension": TYPES_DIR,
    "Action": "actions",
    "Environment": "environments",
    "Workflow": "workflows",
    "Trigger": "triggers",
}

MAX_IDENTIFIER = naming.MAX_IDENTIFIER

# B-tree index entries must fit in about a third of a page (2704 bytes). With
# up to 4 bytes per character, that is safely 600 characters.
MAX_INDEXED_LENGTH = 600

# The largest single object S3 stores.
MAX_FILE_SIZE = 5 * 1024**4

RESERVED_PLUGIN_NAMES = frozenset(
    {
        "iki",
        "core",
        "django",
        "admin",
        "auth",
        "contenttypes",
        "sessions",
        "messages",
        "staticfiles",
        "postgres",
        "oauth2_provider",
        "allauth",
        "account",
        "socialaccount",
        "openid_connect",
        "saml",
        "test",
        "tests",
    }
    | set(keyword.kwlist)
)

# Columns and attributes every entity table or model already has.
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
    }
    | {k for k in keyword.kwlist if k.islower()}
)

SECRET_WORDS = frozenset(
    {"password", "passwd", "secret", "secrets", "token", "apikey", "credential", "credentials"}
)
SECRET_PAIRS = (("api", "key"), ("private", "key"), ("access", "key"), ("client", "secret"))

INDEXABLE = frozenset(
    {"string", "int", "float", "bool", "enum", "date", "datetime", "quantity", "ref"}
)
IDENTITY_TYPES = frozenset(
    {"string", "text", "int", "bool", "enum", "date", "datetime", "ref", "file"}
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
    directory: FilePath | str, *, dependencies: Sequence[FilePath | str] = (), core: bool = False
) -> Result:
    """Read and check the plugin in `directory`, resolving references with `dependencies`.

    ``core`` is for :mod:`iki_spec.builtin` only: the core plugin itself, which
    has the reserved name and can't use its own types before they're read.
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
    _Semantics(loaded, others, core=core).run()
    return loaded.result()


_NOT_A_PLUGIN = frozenset({"not-a-directory", "missing-plugin-manifest"})


@dataclass
class _Loaded:
    directory: FilePath
    checker: _Checker
    plugin: Plugin | None = None
    plugin_source: Document | None = None
    type_sources: dict[str, Document] = field(default_factory=dict)
    extension_sources: list[Document] = field(default_factory=list)
    action_sources: dict[str, Document] = field(default_factory=dict)
    environment_sources: dict[str, Document] = field(default_factory=dict)
    workflow_sources: dict[str, Document] = field(default_factory=dict)
    trigger_sources: dict[str, Document] = field(default_factory=dict)
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
    loaded.plugin_source = plugin_doc

    types: dict[str, EntityType] = {}
    extensions: list[Extension] = []
    for document in _manifests(root, TYPES_DIR, checker):
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

    loaded.plugin = Plugin(
        name=name,
        version=data["version"],
        title=data.get("title") or name.replace("_", " ").capitalize(),
        description=data.get("description"),
        license=data.get("license"),
        homepage=data.get("homepage"),
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
    )
    return loaded


def _manifests(root: FilePath, directory: str, checker: _Checker) -> list[Document]:
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
    }.get(kind or "", f"a {kind}")


def _kind_phrase_plural(kind: str) -> str:
    return {
        "EntityType": "entity types",
        "EntityTypeExtension": "extensions",
        "Action": "actions",
        "Environment": "environments",
        "Workflow": "workflows",
        "Trigger": "triggers",
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
    return EntityType(
        plugin=plugin,
        name=name,
        schema_version=body["schema_version"],
        label=body.get("label") or _words(name),
        label_plural=body.get("label_plural") or _words(name) + "s",
        description=body.get("description"),
        extends=qualify(body["extends"], plugin) if "extends" in body else None,
        abstract=bool(body.get("abstract", False)),
        hrid_prefix=hrid.get("prefix"),
        hrid_digits=hrid.get("digits", 6),
        identity=tuple(body.get("identity", ())),
        on_duplicate=body.get("on_duplicate"),
        fields=_fields(body.get("fields", {}), plugin),
        display_title=(body.get("display") or {}).get("title"),
        file=document.file,
    )


def _words(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)


def _fields(data: dict[str, Any], plugin: str) -> dict[str, Field]:
    return {name: Field.from_manifest(name, spec, plugin=plugin) for name, spec in data.items()}


# --- Rules across documents and plugins --------------------------------------------------------


class _Semantics:
    def __init__(self, loaded: _Loaded, others: dict[str, _Loaded], *, core: bool = False) -> None:
        assert loaded.plugin is not None and loaded.plugin_source is not None  # noqa: S101 - an invariant, not a check
        self.loaded = loaded
        self.plugin: Plugin = loaded.plugin
        self.source: Document = loaded.plugin_source
        self.others = others
        self.checker = loaded.checker
        self.add = loaded.checker.add
        self.core = core
        self.types: dict[str, EntityType] = {} if core else dict(BUILTIN_TYPES)
        for other in others.values():
            assert other.plugin is not None  # noqa: S101 - an invariant, not a check
            self.types.update(other.plugin.types)
        self.types.update(self.plugin.types)
        self.unavailable: set[str] = set()
        self.used: set[str] = set()

    def run(self) -> None:
        self.plugin_manifest()
        for qualified, entity_type in self.plugin.types.items():
            self.entity_type(entity_type, self.loaded.type_sources[qualified])
        self.hrid_prefixes()
        self.table_names()
        seen: dict[str, Document] = {}
        for extension, document in zip(
            self.plugin.extensions, self.loaded.extension_sources, strict=True
        ):
            self.extension(extension, document, seen)
        self.settings()
        actions.check(self)
        workflows.check(self)
        # With the input each gives its entity, where it was left out and only one fits.
        self.loaded.plugin = dataclasses.replace(self.plugin, triggers=triggers.check(self))
        self.unpinned()

    # The plugin manifest ------------------------------------------------------------------------

    def plugin_manifest(self) -> None:
        plugin, source = self.plugin, self.source
        reserved = plugin.name in RESERVED_PLUGIN_NAMES and not (
            self.core and plugin.name == "core"
        )
        if reserved or plugin.name.startswith("iki_"):
            self.add(
                "reserved-name",
                f"`{plugin.name}` is reserved; Iki or Django already uses it.",
                source,
                ("name",),
                hint="Choose another name, such as your organization's prefix and a topic "
                "(`acme_assays`).",
            )
        for dependency, constraints in plugin.dependencies.items():
            path: Path = ("dependencies", dependency)
            if dependency == plugin.name:
                self.add(
                    "dependency-is-self", "A plugin can't depend on itself.", source, path, key=True
                )
                continue
            other = self.others.get(dependency)
            if other is None or other.plugin is None:
                core_names = {t.name for t in BUILTIN_TYPES.values()}
                if dependency == "core" and set(constraints) <= core_names:
                    continue
                self.unavailable.add(dependency)
                self.add(
                    "dependency-not-available",
                    f"Iki can't check what this plugin uses from `{dependency}` without it.",
                    source,
                    path,
                    key=True,
                    hint=f"Pass the {dependency} plugin's directory with "
                    f"`--with path/to/{dependency}`.",
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
        plugin, name = ref.split(".")
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
        if plugin == "core" and "core" not in self.others:
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
        """Parents, nearest first, stopping at a cycle or a type that can't be found."""
        chain: list[EntityType] = []
        seen = {entity_type.qualified_name}
        current = entity_type
        while current.extends and current.extends in self.types:
            parent = self.types[current.extends]
            if parent.qualified_name in seen:
                break
            seen.add(parent.qualified_name)
            chain.append(parent)
            current = parent
        return chain

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
            if parent is not None:
                chain = [entity_type.qualified_name]
                current: EntityType | None = parent
                while current is not None:
                    if current.qualified_name in chain:
                        self.add(
                            "extends-cycle",
                            "These types extend each other in a circle: "
                            + " → ".join(f"`{t}`" for t in [*chain, current.qualified_name])
                            + ".",
                            source,
                            ("extends",),
                        )
                        break
                    chain.append(current.qualified_name)
                    current = self.types.get(current.extends) if current.extends else None
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
        if entity_type.display_title is not None:
            present = {n: f for n, f in names.items() if not f.removed}
            self.title(entity_type.display_title, source, present)

    def identity(
        self, entity_type: EntityType, source: Document, every: dict[str, tuple[Field, EntityType]]
    ) -> None:
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
            f, _ = every[name]
            if f.removed:
                self.add(
                    "identity-removed",
                    f"`{name}` is part of the identity, so it can't be removed.",
                    source,
                    path,
                    hint="Entities are told apart by their identity for as long as they exist.",
                )
                continue
            if f.type not in IDENTITY_TYPES:
                self.add(
                    "identity-type",
                    f"`{name}` is a {f.type} field, which can't be part of an identity.",
                    source,
                    path,
                    hint="Identities compare values exactly, so they use text, numbers, enums, "
                    "dates, files or references, not floats, quantities, lists or JSON.",
                )
            if not (f.required and f.immutable):
                missing = " and ".join(
                    word
                    for word, ok in (("required", f.required), ("immutable", f.immutable))
                    if not ok
                )
                self.add(
                    "identity-not-fixed",
                    f"`{name}` is part of the identity, so it must be {missing}.",
                    source,
                    path,
                    hint="An identity that could be empty or change can't tell entities apart.",
                )
        if entity_type.on_duplicate and not entity_type.identity:
            inherited = next((a for a in self.ancestors(entity_type) if a.identity), None)
            if inherited is None:
                self.add(
                    "on-duplicate-without-identity",
                    "`on_duplicate` needs an `identity` to say what a duplicate is.",
                    source,
                    ("on_duplicate",),
                    key=True,
                )

    def title(self, template: str, source: Document, fields: dict[str, Field]) -> None:
        path: Path = ("display", "title")
        for problem, hint in _title_problems(template, fields):
            self.add("display-title", problem, source, path, hint=hint)

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
            add(t.table, t.name, f"`{t.name}`", source, ("name",), "type", t.fields)
        targets: set[str] = set()
        for extension, source in zip(
            self.plugin.extensions, self.loaded.extension_sources, strict=True
        ):
            if extension.extends_type in targets:
                continue  # reported as a duplicate extension
            targets.add(extension.extends_type)
            add(
                extension.table,
                naming.extension_model(extension.extends_type),
                f"the extension of `{extension.extends_type}`",
                source,
                ("extends_type",),
                "plugin",
                extension.fields,
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
                            "type": "Shorten the type's name or the plugin's.",
                            "plugin": "Shorten the plugin's name.",
                            "field": "Shorten the field's name, the type's or the plugin's.",
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
            if name in taken:
                self.add(
                    "extension-field-conflict",
                    f"`{name}` is already a field of `{extension.extends_type}` "
                    f"(from {taken[name]}).",
                    source,
                    field_path,
                    key=True,
                    hint=f"Choose another name, for example `{self.plugin.name}_{name}`.",
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
        if not item:
            if f.name in RESERVED_FIELD_NAMES or f.name.endswith("_ptr"):
                self.add(
                    "reserved-name",
                    f"`{f.name}` is reserved; every entity already has it, or Python or Django "
                    "use it.",
                    source,
                    path,
                    key=True,
                    hint="Choose a more specific name, such as `sample_type` instead of `type`.",
                )
            if f.type == "ref" and f"{f.name}_id" in siblings:
                self.add(
                    "column-conflict",
                    f"The reference `{f.name}` is stored in a column named `{f.name}_id`, which "
                    "is also the name of another field.",
                    source,
                    path,
                    key=True,
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
        if f.type == "ref" and f.to is not None:
            self.reference(f.to, source, (*path, "to"))
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
        if f.type == "list" and f.items is not None:
            self.field(f.items, source, (*path, "items"), siblings=siblings, item=True)
        if f.has_default:
            for message in value_problems(f, f.default):
                self.add("invalid-default", f"The default {message}", source, (*path, "default"))

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

    def options(self, f: Field, source: Document, path: Path) -> None:
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

    def settings(self) -> None:
        for name, setting in self.plugin.settings.items():
            path: Path = ("settings", name)
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
        if f.format and not matches_format(value, f.format):
            problems.append(f"isn't a valid {f.format.replace('_', ' ')}.")
        if f.pattern and not pattern_problems(f.pattern) and re.fullmatch(f.pattern, value) is None:
            problems.append(f"doesn't match the pattern `{f.pattern}`.")
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
    if kind == "date":
        return (
            [] if isinstance(value, str) and _is_date(value) else ["must be a date (YYYY-MM-DD)."]
        )
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


def _list_tables(fields: dict[str, Field]) -> list[Field]:
    """Lists of references and files, which get a table of their own."""
    return [
        f
        for f in fields.values()
        if f.type == "list" and f.items and f.items.type in ("ref", "file")
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


def _title_problems(template: str, fields: dict[str, Field]) -> Iterable[tuple[str, str | None]]:
    """Check a title template: text with `{{ a or b or "text" }}` parts."""
    position = 0
    found = False
    while True:
        start = template.find("{{", position)
        stray = template.find("}}", position)
        if stray != -1 and (start == -1 or stray < start):
            yield ("The title has a `}}` without a `{{`.", None)
            return
        if start == -1:
            break
        end = template.find("}}", start + 2)
        if end == -1:
            yield ("The title has a `{{` without a `}}`.", None)
            return
        found = True
        yield from _title_expression(template[start + 2 : end], fields)
        position = end + 2
    if not found:
        yield (
            "The title is the same text for every entity.",
            "Name fields in it, such as `{{ name or hrid }}`.",
        )


def _title_expression(
    expression: str, fields: dict[str, Field]
) -> Iterable[tuple[str, str | None]]:
    tokens = [m for m in _TOKEN.finditer(expression) if m.group().strip()]
    if not tokens:
        yield ("The title has an empty `{{ }}`.", None)
        return
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
                        f"The title names `{word}`, which isn't a field of this type.",
                        schemas._did_you_mean(word, [*fields, "hrid"]),
                    )
                elif fields[word].type not in TITLE_TYPES:
                    yield (f"The title can't show `{word}`, a {fields[word].type} field.", None)
            else:
                yield (
                    f"The title has `{token.group().strip()}` where a field name or quoted text "
                    "belongs.",
                    "Titles combine fields and quoted text with `or`, as in `{{ name or hrid }}`.",
                )
                return
        elif word != "or":
            yield (
                f"The title has `{token.group().strip()}` where `or` belongs.",
                "Titles combine fields and quoted text with `or`, as in `{{ name or hrid }}`.",
            )
            return
        expect_operand = not expect_operand
    if expect_operand:
        yield ("The title ends with `or`.", None)


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
    return sorted(checker.problems, key=Problem.sort_key)
