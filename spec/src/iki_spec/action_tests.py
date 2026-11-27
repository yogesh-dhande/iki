"""A plugin's action tests: fixtures in its ``tests/`` directory (ADR 0093).

Each YAML document there of ``kind: ActionTest`` runs one action on given values::

    apiVersion: iki/v1
    kind: ActionTest
    action: compute_properties
    inputs:
      protein:
        - values: { sequence: MVLSPADKTNVKAAWGKVGAHAGEYGAEALERMF }
    expect:
      output:
        updates:
          protein:
            - values: { length: 34 }

:func:`load_tests` reads them and checks each against the plugin: the action, its
params and inputs, the fields of each value, and the files they name.
:func:`run_input` makes the run input the server would give the action's command, and
:func:`compare` says where an output differs from what a test expects.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from iki_spec import API_VERSION, schemas
from iki_spec.effective import lineage, resolve_one
from iki_spec.formats import FormatRegistry
from iki_spec.model import Action, EntityType, Field, Input, Plugin, included_reference
from iki_spec.problems import Path as DocPath
from iki_spec.problems import Problem, Severity
from iki_spec.yamlsource import Document, read_documents

__all__ = [
    "DIRECTORY",
    "KIND",
    "TOLERANCE",
    "ActionTest",
    "Loaded",
    "compare",
    "effects",
    "fixture_files",
    "load_tests",
    "run_input",
]

DIRECTORY = "tests"
KIND = "ActionTest"
#: How far a number may be from the expected one, relative to it, unless a test says.
TOLERANCE = 1e-9
#: What a fixture names a file with, in a value or param.
FILE_KEY = "$file"
#: The fixed seed of every test run, so a command's random numbers repeat.
SEED = 1


@dataclass(frozen=True)
class ActionTest:
    name: str
    #: The fixture file, relative to the plugin's directory.
    file: str
    action: Action
    params: dict[str, Any] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    #: Each input's entities: their ``values``, and ``hrid`` and ``type`` if given.
    inputs: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: File names, each to its path in the plugin.
    files: dict[str, str] = field(default_factory=dict)
    output: dict[str, Any] | None = None
    error: str | None = None
    tolerance: float = TOLERANCE
    upgrade: bool = False
    after: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


@dataclass
class Loaded:
    tests: list[ActionTest]
    problems: list[Problem]

    @property
    def ok(self) -> bool:
        return not any(p.is_error for p in self.problems)


def fixture_files(directory: Path) -> list[Path]:
    """Every file a plugin's tests may be in: YAML (``.yaml`` or ``.yml``) anywhere under
    its ``tests/``, in path order. CI picks plugins to run by these, as this loader reads
    them, so a fixture can't be where one reads it and the other doesn't."""
    root = directory / DIRECTORY
    if not root.is_dir():
        return []
    return sorted(
        (p for p in root.rglob("*") if p.suffix in (".yaml", ".yml") and p.is_file()),
        key=lambda p: p.relative_to(directory).as_posix(),
    )


def load_tests(
    directory: Path, plugin: Plugin, types: Mapping[str, EntityType] | None = None
) -> Loaded:
    """The plugin's tests, checked against it. ``types`` are every type the plugin can see,
    by qualified name: its own, its dependencies' and the core plugin's."""
    every = dict(types or {})
    every.update({t.qualified_name: t for t in plugin.types.values()})
    tests: list[ActionTest] = []
    problems: list[Problem] = []
    seen: dict[str, str] = {}
    for file in fixture_files(directory):
        name = file.relative_to(directory).as_posix()
        documents, read = read_documents(file, name)
        problems += read
        documents = [d for d in documents if d.data is not None]
        for document in documents:
            check = _Check(name, document, directory, plugin, every)
            test = check.test(file.stem, len(documents) > 1)
            problems += check.problems
            if test is None:
                continue
            if test.name in seen:
                check.add(
                    "duplicate-test",
                    f"There's already a test called {test.name}, in {seen[test.name]}.",
                    ("name",),
                    hint="Give one of them another `name`.",
                )
                problems += check.problems[-1:]
                continue
            seen[test.name] = name
            tests.append(test)
    return Loaded(tests, sorted(problems, key=Problem.sort_key))


class _Check:
    def __init__(
        self,
        name: str,
        document: Document,
        directory: Path,
        plugin: Plugin,
        types: Mapping[str, EntityType],
    ) -> None:
        self.name = name
        self.document = document
        self.directory = directory
        self.plugin = plugin
        self.types = types
        self.problems: list[Problem] = []

    def add(self, code: str, message: str, path: DocPath = (), *, hint: str | None = None) -> None:
        self.problems.append(
            Problem(
                code=code,
                message=message,
                severity=Severity.ERROR,
                file=self.name,
                location=self.document.locate(path),
                path=path,
                hint=hint,
            )
        )

    def test(self, stem: str, numbered: bool) -> ActionTest | None:
        data = self.document.data
        if not isinstance(data, dict) or data.get("kind") != KIND:
            self.add(
                "not-a-test",
                f"Files in {DIRECTORY}/ hold action tests: this isn't one.",
                hint=f"Start it with `apiVersion: {API_VERSION}` and `kind: {KIND}`.",
            )
            return None
        for issue in schemas.check("action-test", data):
            self.add("schema", issue.message, issue.path, hint=issue.hint)
        if self.problems:
            return None
        default = stem.replace("-", "_").lower()
        name = data.get("name") or (f"{default}_{self.document.index + 1}" if numbered else default)
        action = self.plugin.actions.get(data["action"])
        if action is None:
            known = ", ".join(sorted(self.plugin.actions)) or "none"
            self.add(
                "unknown-action",
                f"{self.plugin.name} has no action {data['action']}.",
                ("action",),
                hint=f"Its actions: {known}.",
            )
            return None
        files = dict(data.get("files") or {})
        for key, path in files.items():
            if not (self.directory / path).is_file():
                self.add(
                    "missing-file",
                    f"There's no file at `{path}`.",
                    ("files", key),
                    hint="Give it relative to the plugin's directory.",
                )
        params = dict(data.get("params") or {})
        for param in params:
            if param not in action.params:
                self.add(
                    "unknown-param",
                    f"{action.name} has no param {param}.",
                    ("params", param),
                    hint=_known("Its params", action.params),
                )
        for param, f in action.params.items():
            if params.get(param) is None and f.required and not f.has_default:
                self.add("missing-param", f"Give {action.name}'s {param}.", ("params",))
        self._files_named(params, files, ("params",))
        inputs = {k: list(v) for k, v in (data.get("inputs") or {}).items()}
        self._inputs(action, inputs, files)
        after = data.get("after", {})
        for input_name, expected in after.items():
            if input_name not in inputs or len(expected) != len(inputs[input_name]):
                self.add(
                    "upgrade-after",
                    f"Give one expected value map per {input_name} sample.",
                    ("after", input_name),
                )
        settings = dict(data.get("settings") or {})
        for setting in settings:
            if setting not in self.plugin.settings:
                self.add(
                    "unknown-setting",
                    f"{self.plugin.name} has no setting {setting}.",
                    ("settings", setting),
                    hint=_known("Its settings", self.plugin.settings),
                )
        if self.problems:
            return None
        expect = data.get("expect") or {}
        return ActionTest(
            name=name,
            file=self.name,
            action=action,
            params=params,
            settings=settings,
            inputs=inputs,
            files=files,
            output=expect.get("output"),
            error=expect.get("error"),
            tolerance=float(expect.get("tolerance", TOLERANCE)),
            upgrade=data.get("upgrade", False),
            after=after,
        )

    def _inputs(
        self, action: Action, inputs: dict[str, list[dict[str, Any]]], files: dict[str, str]
    ) -> None:
        for name, entities in inputs.items():
            at: DocPath = ("inputs", name)
            given = action.inputs.get(name)
            if given is None:
                self.add(
                    "unknown-input",
                    f"{action.name} has no input {name}.",
                    at,
                    hint=_known("Its inputs", action.inputs),
                )
                continue
            if given.cardinality == "one" and not 1 <= len(entities) <= action.batchable:
                packs = (
                    f": {action.name} packs up to {action.batchable} runs into one job, so give "
                    f"at most {action.batchable}"
                    if action.batchable > 1
                    else ""
                )
                self.add("input-count", f"{name} takes one entity, not {len(entities)}{packs}.", at)
            if given.max is not None and len(entities) > given.max:
                self.add("input-count", f"{name} takes at most {given.max} entities.", at)
            for index, entity in enumerate(entities):
                type_name = entity.get("type") or given.type
                entity_type = self.types.get(type_name)
                if entity_type is None or not self._is_a(entity_type, given.type):
                    self.add(
                        "wrong-type",
                        f"{type_name} isn't a {given.type}, which {name} takes.",
                        (*at, index, "type"),
                    )
                    continue
                fields = self.fields(entity_type)
                for field_name in entity["values"]:
                    if field_name not in fields:
                        self.add(
                            "unknown-field",
                            f"{type_name} has no field {field_name}.",
                            (*at, index, "values", field_name),
                            hint=_known("Its fields", fields),
                        )
                self._files_named(entity["values"], files, (*at, index, "values"))
                self._included(given, fields, entity, files, (*at, index, "included"))
                if "request" in entity and not self._is_request(entity_type):
                    self.add(
                        "not-a-request",
                        f"{type_name} isn't a request type, so it has no asking project or "
                        "service.",
                        (*at, index, "request"),
                    )
        for name, given in action.inputs.items():
            if name not in inputs and not given.optional:
                self.add("missing-input", f"Give {action.name}'s {name} input.", ("inputs",))

    def _included(
        self,
        given: Input,
        fields: Mapping[str, Field],
        entity: dict[str, Any],
        files: dict[str, str],
        at: DocPath,
    ) -> None:
        """An input entity's ``included``: the entities its references name, for the
        references the input includes (ADR 0105)."""
        for reference, named in (entity.get("included") or {}).items():
            if reference not in given.include:
                self.add(
                    "not-included",
                    f"The input doesn't include {reference}.",
                    (*at, reference),
                    hint=_known("It includes", dict.fromkeys(given.include)),
                )
                continue
            found = included_reference(fields, reference)
            targets = targets_of(found) if found is not None else ()
            # Without a type, an included entity is of the reference's only one.
            to = targets[0] if len(targets) == 1 else None
            one = found is not None and found.type == "ref"
            if one and len(named) > 1:
                self.add(
                    "input-count",
                    f"{reference} is one reference, so give at most one entity.",
                    (*at, reference),
                )
            for index, other in enumerate(named):
                type_name = other.get("type") or to
                other_type = self.types.get(type_name or "")
                if other_type is None or (
                    targets and not any(self._is_a(other_type, t) for t in targets)
                ):
                    self.add(
                        "wrong-type",
                        f"{type_name} isn't a {_either(targets)}, which {reference} names."
                        if type_name
                        else f"{reference} refers to {_either(targets)}: say which `type` "
                        "each entity is.",
                        (*at, reference, index, "type"),
                    )
                    continue
                other_fields = self.fields(other_type)
                for field_name in other["values"]:
                    if field_name not in other_fields:
                        self.add(
                            "unknown-field",
                            f"{type_name} has no field {field_name}.",
                            (*at, reference, index, "values", field_name),
                            hint=_known("Its fields", other_fields),
                        )
                self._files_named(other["values"], files, (*at, reference, index, "values"))

    def _files_named(self, values: Any, files: dict[str, str], at: DocPath) -> None:  # noqa: ANN401 - YAML data
        for path, key in _file_names(values, at):
            if key not in files:
                self.add(
                    "unknown-file",
                    f"There's no file {key} in `files`.",
                    path,
                    hint=_known("Its files", files),
                )

    def _is_request(self, entity_type: EntityType) -> bool:
        return resolve_one(self.types, entity_type.qualified_name).request is not None

    def _is_a(self, entity_type: EntityType, wanted: str) -> bool:
        return wanted in lineage(self.types, entity_type.qualified_name)

    def fields(self, entity_type: EntityType) -> dict[str, Field]:
        """A type's fields as this plugin sees them: its own, its ancestors', and the ones
        this plugin's extensions add, which its actions read and write like any other."""
        out = fields_of(entity_type, self.types)
        chain = set(lineage(self.types, entity_type.qualified_name))
        for extension in self.plugin.extensions:
            if extension.extends_type in chain:
                out.update(extension.fields)
        return out


def fields_of(entity_type: EntityType, types: Mapping[str, EntityType]) -> dict[str, Field]:
    """A type's fields now, its ancestors' included, as its records have them."""
    return resolve_one(types, entity_type.qualified_name).current_fields


def targets_of(f: Field) -> tuple[str, ...]:
    """The types a reference, or a list of them, names (ADR 0136)."""
    if f.type == "list" and f.items is not None:
        return f.items.to
    return f.to


def _either(names: tuple[str, ...]) -> str:
    if len(names) <= 1:
        return names[0] if names else "an entity"
    return ", ".join(names[:-1]) + f" or {names[-1]}"


def _file_names(values: Any, at: DocPath) -> Iterator[tuple[DocPath, str]]:  # noqa: ANN401 - YAML data
    if isinstance(values, dict):
        if set(values) == {FILE_KEY} and isinstance(values[FILE_KEY], str):
            yield at, values[FILE_KEY]
            return
        for key, value in values.items():
            yield from _file_names(value, (*at, key))
    elif isinstance(values, list):
        for index, value in enumerate(values):
            yield from _file_names(value, (*at, index))


def _known(what: str, names: Mapping[str, Any]) -> str:
    return f"{what}: {', '.join(sorted(names))}." if names else f"{what}: none."


# --- The run input ------------------------------------------------------------------------------


def _id(kind: int, n: int) -> str:
    """A fixed UUID: kind 1 for the run, 2 the project, 3 entities, 4 files, 5 people, 6
    other projects."""
    return str(uuid.UUID(f"00000000-0000-7000-8{kind:03d}-{n:012d}"))


def run_input(
    test: ActionTest,
    plugin: Plugin,
    types: Mapping[str, EntityType],
    directory: Path,
    settings: Mapping[str, Any],
    relative_to: Path,
    *,
    formats: FormatRegistry | None = None,
) -> dict[str, Any]:
    """The run input the server would give the test's command (``run-input/v1``), without a
    lease. Files are given by path, relative to ``relative_to``, where input.json goes, as
    ``iki-worker run --local`` reads them. ``settings`` are the plugin's defaults."""
    every = {**types, **{t.qualified_name: t for t in plugin.types.values()}}
    file_ids = {key: _id(4, n) for n, key in enumerate(sorted(test.files), start=1)}
    action = test.action
    params = {name: field.default for name, field in action.params.items() if field.has_default}
    params.update({name: value for name, value in test.params.items() if value is not None})
    inputs: dict[str, list[dict[str, Any]]] = {name: [] for name in action.inputs}
    n = 0

    def made(entity: dict[str, Any], type_name: str) -> dict[str, Any]:
        nonlocal n
        n += 1
        entity_type = every[type_name]
        prefix = entity_type.hrid_prefix or "ENT"
        amount = resolve_one(every, type_name).amount
        return {
            **({"material_field": amount.material} if amount and amount.material else {}),
            "id": _id(3, n),
            "hrid": entity.get("hrid") or f"{prefix}-{n:0{entity_type.hrid_digits}d}",
            "type": type_name,
            "schema_version": entity_type.schema_version,
            "revision": 1,
            "values": _with_ids(entity["values"], file_ids),
        }

    for name, entities in test.inputs.items():
        put = action.inputs[name]
        for entity in entities:
            document = made(entity, entity.get("type") or put.type)
            if "request" in entity:
                # A request's two sides, as the server gives them (ADR 0232): by default
                # sent from another project to the test's, which serves it.
                document["request"] = _request_sides(entity["request"])
            if put.include:
                # What its references name: each given one gets an ID, which the reference
                # holds, as the server gives them (ADR 0105).
                fields = fields_of(every[document["type"]], every)
                given = entity.get("included") or {}
                included: dict[str, list[dict[str, Any]]] = {}
                for reference in put.include:
                    found = included_reference(fields, reference)
                    assert found is not None  # noqa: S101 - the action was validated
                    targets = targets_of(found)
                    to = targets[0] if len(targets) == 1 else ""
                    named = given.get(reference) or []
                    included[reference] = [made(o, o.get("type") or to) for o in named]
                    if reference not in given:
                        continue
                    ids = [o["id"] for o in included[reference]]
                    if "." in reference:
                        # A list of rows' references (ADR 0161): each row's, in order.
                        rows_name, _, cell = reference.partition(".")
                        rows = document["values"].get(rows_name) or []
                        document["values"][rows_name] = [
                            {**row, cell: ids[i] if i < len(ids) else row.get(cell)}
                            for i, row in enumerate(rows)
                        ]
                    else:
                        list_of = found.type == "list"
                        document["values"][reference] = ids if list_of else (ids or [None])[0]
                document["included"] = included
            inputs[name].append(document)
    packed = [
        entity
        for name, entities in inputs.items()
        if action.inputs[name].cardinality == "one" and len(entities) > 1
        for entity in entities
    ]
    pack = [
        {"run": _id(1, i), "hrid": f"RUN-{i:06d}", "entity": entity["id"]}
        for i, entity in enumerate(packed, start=1)
    ]
    return {
        "apiVersion": API_VERSION,
        "kind": "RunInput",
        "run": {
            "id": _id(1, 1),
            "hrid": "RUN-000001",
            "attempt": 1,
            "seed": SEED,
            **({"requested_by": _id(5, 1)} if not pack else {}),
            "action": action.qualified_name,
            "plugin": {"name": plugin.name, "version": plugin.version, "sha": None},
            "project": _id(2, 1),
        },
        "command": list(action.command),
        "environment": f"{plugin.name}.{action.environment}",
        "params": _with_ids(params, file_ids),
        "settings": {**settings, **test.settings},
        "inputs": inputs,
        "files": _fixture_files(test, file_ids, directory, relative_to, formats),
        # Entities for an input that takes one: a batchable action's runs packed into one
        # job, as the server packs them, one run each.
        **({"pack": pack} if pack else {}),
    }


#: The test run's project, which a request is sent to unless its fixture says otherwise.
_TEST_PROJECT = {"slug": "service", "name": "Service"}


def _request_sides(given: Mapping[str, Any]) -> dict[str, Any]:
    def side(value: Mapping[str, Any] | None, n: int) -> dict[str, Any] | None:
        if value is None:
            return None
        return {"id": value.get("id") or _id(6, n), "slug": value["slug"], "name": value["name"]}

    service = given["service"] if "service" in given else {**_TEST_PROJECT, "id": _id(2, 1)}
    return {"asking_project": side(given["asking_project"], 1), "service": side(service, 2)}


def _fixture_files(
    test: ActionTest,
    file_ids: Mapping[str, str],
    directory: Path,
    relative_to: Path,
    formats: FormatRegistry | None,
) -> dict[str, dict[str, str]]:
    files = {
        file_ids[key]: {"path": Path(_relative(directory / path, relative_to)).as_posix()}
        for key, path in test.files.items()
    }
    if not formats or not files:
        return files
    allowed: dict[str, set[str]] = {}
    for param, value in test.params.items():
        field = test.action.params[param]
        for _, key in _file_names(value, ()):
            allowed.setdefault(key, set()).update(
                formats.expand((field.items or field).media_types)
            )
    for key, path in test.files.items():
        with (directory / path).open("rb") as stream:
            identified = formats.by_signature(stream.read(formats.signature_bytes))
        if identified is None:
            # A fixture may deliberately be malformed to test its parser's error path.
            # File params disambiguate shared extensions such as protein vs nucleotide FASTA.
            candidates = formats.by_extension(path)
            accepted = allowed.get(key, set())
            identified = next(
                (fmt for fmt in candidates if fmt.canonical in accepted),
                candidates[0] if candidates else None,
            )
        if identified:
            files[file_ids[key]]["media_type"] = identified.canonical
    return files


def _relative(path: Path, start: Path) -> str:
    import os

    return os.path.relpath(path.resolve(), start.resolve())


def _with_ids(values: Any, file_ids: Mapping[str, str]) -> Any:  # noqa: ANN401 - JSON data
    if isinstance(values, dict):
        if set(values) == {FILE_KEY} and isinstance(values[FILE_KEY], str):
            return file_ids[values[FILE_KEY]]
        return {k: _with_ids(v, file_ids) for k, v in values.items()}
    if isinstance(values, list):
        return [_with_ids(v, file_ids) for v in values]
    return values


# --- What it wrote ------------------------------------------------------------------------------


def effects(action: Action, output: Mapping[str, Any]) -> list[str]:
    """What the output updates that its action doesn't declare, as the server would refuse.
    What it creates is checked as the server checks it, by :mod:`iki_spec.outputs`."""
    out = []
    declared = {u.input: set(u.fields) for u in action.updates}
    for name, updates in (output.get("updates") or {}).items():
        if name not in declared:
            out.append(f"It updates {name}, which {action.name} doesn't declare it updates.")
            continue
        for update in updates:
            extra = sorted(set(update.get("values") or {}) - declared[name])
            if extra:
                out.append(
                    f"It sets {', '.join(extra)} on {name}, which {action.name} doesn't "
                    "declare it updates."
                )
                break
    return out


def compare(expected: Any, got: Any, tolerance: float = TOLERANCE, at: str = "") -> list[str]:  # noqa: ANN401 - JSON data
    """Where ``got`` differs from ``expected``: each key ``expected`` gives, each list's items in
    order, numbers within ``tolerance`` (relative)."""
    where = at or "the output"
    if isinstance(expected, dict):
        if not isinstance(got, dict):
            return [f"{where}: expected a mapping, got {_show(got)}"]
        out = []
        for key, value in expected.items():
            if key not in got:
                out.append(f"{_join(at, key)}: missing (expected {_show(value)})")
            else:
                out += compare(value, got[key], tolerance, _join(at, key))
        return out
    if isinstance(expected, list):
        if not isinstance(got, list):
            return [f"{where}: expected a list, got {_show(got)}"]
        if len(got) != len(expected):
            return [f"{where}: expected {len(expected)} item(s), got {len(got)}"]
        return [
            d
            for i, (e, g) in enumerate(zip(expected, got, strict=True))
            for d in compare(e, g, tolerance, f"{at}[{i}]")
        ]
    if (
        isinstance(expected, int | float)
        and not isinstance(expected, bool)
        and isinstance(got, int | float)
        and not isinstance(got, bool)
    ):
        if math.isclose(expected, got, rel_tol=tolerance, abs_tol=0.0 if expected else tolerance):
            return []
        return [f"{where}: expected {expected!r}, got {got!r}"]
    if expected != got or isinstance(expected, bool) != isinstance(got, bool):
        return [f"{where}: expected {_show(expected)}, got {_show(got)}"]
    return []


def _join(at: str, key: str) -> str:
    return f"{at}.{key}" if at else key


def _show(value: Any) -> str:  # noqa: ANN401 - JSON data
    import json

    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 80 else text[:77] + "…"
