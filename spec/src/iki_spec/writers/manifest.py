"""A plugin's writers as its manifest declares them, and the rules beyond the schema
(ADR 0230).

A writer names a format (ADR 0213) for what its file is, a Rust crate under ``writers/``
for how it's written, and the roles it reads. The rules here check that the format exists
and is the plugin's own or a dependency's, and can't carry active content; that the crate
is a real directory with its ``Cargo.toml`` and ``Cargo.lock``, built as a library a module
can be made of; that its digest is tracked beside it; that a module built here, if there is
one, is one the sandbox would run; and that each role's unit and types make sense. A type
that exports through a writer maps each role to a field of the role's type.
"""

from __future__ import annotations

import hashlib
import platform
import re
import tomllib
from pathlib import Path as FilePath
from typing import TYPE_CHECKING, Any

from iki_spec import naming, units
from iki_spec.model import Field, PluginExport, Writer, WriterRole, parse_size, qualify
from iki_spec.problems import Path
from iki_spec.writers.check import check_module
from iki_spec.writers.runtime import MAX_MEMORY

if TYPE_CHECKING:
    from iki_spec import validate as _validate
    from iki_spec.model import EntityType
    from iki_spec.yamlsource import Document

__all__ = ["ACTIVE_MEDIA_TYPES", "DIGEST", "check", "check_export", "crates", "read_writers"]

#: The digest's file, beside the crate: ``writers/tecan-worklist.wasm.sha256``.
DIGEST = ".wasm.sha256"
#: Media types a browser may run, which a writer's file may never be: Iki serves what a
#: plugin wrote, and a file that ran in the browser would run as Iki.
ACTIVE_MEDIA_TYPES = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "image/svg+xml",
        "text/xsl",
        "application/xslt+xml",
        "text/javascript",
        "application/javascript",
        "application/x-javascript",
        "text/ecmascript",
        "application/ecmascript",
        "application/wasm",
    }
)
_HEX = re.compile(r"[0-9a-f]{64}")


def _digest(root: FilePath, module: str) -> str:
    file = root / f"{module}{DIGEST}"
    if not file.is_file() or file.is_symlink():
        return ""
    words = file.read_text("utf-8", "replace").split()
    return words[0] if words and _HEX.fullmatch(words[0]) else ""


def read_writers(plugin: str, data: dict[str, Any], root: FilePath) -> dict[str, Writer]:
    """The writers ``plugin.yaml`` declares, by qualified ID, each with its tracked digest
    (empty where there's none to read: :func:`check` says so)."""
    out: dict[str, Writer] = {}
    for order, (local, body) in enumerate((data.get("writers") or {}).items()):
        try:
            memory = parse_size(str(body.get("memory") or "64MiB"))
        except ValueError:
            memory = 0  # check() says so
        roles = {
            name: WriterRole(
                type=role["type"],
                required=bool(role.get("required", False)),
                description=role.get("description"),
                unit=role.get("unit"),
                to=tuple(qualify(t, plugin) for t in role.get("types") or ()),
            )
            for name, role in (body.get("roles") or {}).items()
        }
        out[f"{plugin}.{local}"] = Writer(
            plugin=plugin,
            name=local,
            label=str(body["label"]).strip() or local,
            format=qualify(str(body["format"]), plugin),
            module=str(body["module"]),
            many=str(body.get("many") or "file"),
            memory=memory,
            description=(body.get("description") or "").strip() or None,
            roles=roles,
            sha256=_digest(root, str(body["module"])),
            order=order,
            file="plugin.yaml",
        )
    return out


def crates(root: FilePath, writer: Writer) -> list[tuple[str, str]]:
    """Each crate the writer's ``Cargo.lock`` pins, as (name, version), for a reviewer."""
    try:
        lock = tomllib.loads((root / writer.module / "Cargo.lock").read_text("utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    return sorted((str(p.get("name")), str(p.get("version"))) for p in lock.get("package", ()))


def _canonical_host() -> bool:
    return platform.system() == "Linux" and platform.machine().lower() in {"x86_64", "amd64"}


def check(semantics: _validate._Semantics) -> None:
    """The plugin's writers: their formats, crates, digests and roles."""
    plugin = semantics.plugin
    root = semantics.loaded.directory
    source = semantics.source
    for writer in plugin.writers.values():
        path: Path = ("writers", writer.name)
        if not 0 < writer.memory <= MAX_MEMORY:
            semantics.add(
                "writer-memory",
                "A writer's memory is more than nothing and at most 256MiB.",
                source,
                (*path, "memory"),
            )
        _format(semantics, writer, source, path)
        _crate(semantics, root, writer, source, path)
        for name, role in writer.roles.items():
            where: Path = (*path, "roles", name)
            if role.unit is not None:
                try:
                    units.parse(role.unit)
                except units.UnitError as error:
                    semantics.add("unknown-unit", str(error), source, (*where, "unit"))
            for index, target in enumerate(role.to):
                semantics.reference(target, source, (*where, "types", index))


def _format(semantics: _validate._Semantics, writer: Writer, source: Document, path: Path) -> None:
    owner = naming.plugin_of(writer.format)
    if (
        owner != writer.plugin
        and owner not in semantics.plugin.dependencies
        and owner != naming.CORE
    ):
        semantics.add(
            "writer-format-dependency",
            f"`{writer.format}` is the {owner} plugin's format: depend on {owner} to write it.",
            source,
            (*path, "format"),
            hint=f"Add {owner} under `dependencies`.",
        )
        return
    if semantics.formats is None:
        return  # a format conflict already says what to fix
    fmt = semantics.formats.formats.get(writer.format)
    if fmt is None:
        semantics.add(
            "unknown-format",
            f"There's no format `{writer.format}`.",
            source,
            (*path, "format"),
            hint="Declare it under `formats`, or name one a dependency declares.",
        )
        return
    active = sorted(
        {m.partition(";")[0].strip().lower() for m in fmt.media_types} & ACTIVE_MEDIA_TYPES
    )
    if active:
        semantics.add(
            "writer-active-format",
            f"`{writer.format}` is {', '.join(active)}, which a browser can run: a writer's "
            "file can't be.",
            source,
            (*path, "format"),
            hint="Write a format that only holds data, such as CSV, JSON or a binary format.",
        )
    if not fmt.extensions:
        semantics.add(
            "writer-format-extension",
            f"`{writer.format}` declares no extension, which a file Iki names needs.",
            source,
            (*path, "format"),
        )


def _crate(
    semantics: _validate._Semantics,
    root: FilePath,
    writer: Writer,
    source: Document,
    path: Path,
) -> None:
    crate = root / writer.module
    where: Path = (*path, "module")
    if crate.is_symlink() or not crate.is_dir():
        semantics.add(
            "writer-module",
            f"`{writer.module}` isn't a directory in the plugin: a writer is a Rust crate there.",
            source,
            where,
        )
        return
    for name in ("Cargo.toml", "Cargo.lock"):
        file = crate / name
        if file.is_symlink() or not file.is_file():
            semantics.add(
                "writer-module",
                f"`{writer.module}` has no {name}."
                + (
                    " The lockfile pins every crate the module is built from."
                    if name == "Cargo.lock"
                    else ""
                ),
                source,
                where,
                hint="Run `cargo generate-lockfile` in it, then review and commit it."
                if name == "Cargo.lock"
                else None,
            )
            return
    links = [
        p for p in crate.rglob("*") if p.is_symlink() and "target" not in p.relative_to(crate).parts
    ]
    if links:
        semantics.add(
            "writer-module",
            f"`{links[0].relative_to(root)}` is a symbolic link: a writer's crate holds only its "
            "own files, so what's reviewed is what's built.",
            source,
            where,
        )
    try:
        manifest = tomllib.loads((crate / "Cargo.toml").read_text("utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        semantics.add(
            "writer-module", f"`{writer.module}/Cargo.toml` can't be read: {error}.", source, where
        )
        return
    kinds = (manifest.get("lib") or {}).get("crate-type") or []
    if "cdylib" not in kinds:
        semantics.add(
            "writer-module",
            f"`{writer.module}` isn't built as a cdylib, so no WebAssembly module is made of it.",
            source,
            where,
            hint='Add `crate-type = ["cdylib"]` under `[lib]` in its Cargo.toml.',
        )
    if not writer.sha256:
        semantics.add(
            "writer-digest",
            f"`{writer.module}{DIGEST}` is missing or holds no SHA-256: it pins the module "
            "a deployment builds of the crate.",
            source,
            where,
            hint=f"Run `iki plugin writers {root.name} --update`, then commit the digest.",
        )
        return
    built = root / f"{writer.module}.wasm"
    if built.is_file() and not built.is_symlink():
        binary = built.read_bytes()
        for problem in check_module(binary):
            semantics.add(
                "writer-rejected",
                f"`{writer.module}.wasm` can't be a writer: {problem.message}",
                source,
                where,
            )
        made = hashlib.sha256(binary).hexdigest()
        if _canonical_host() and made != writer.sha256:
            semantics.add(
                "writer-digest",
                f"`{writer.module}.wasm` is {made}, but its digest says {writer.sha256}.",
                source,
                where,
                hint="If the Rust changed on purpose, run `iki plugin writers --update` and "
                "commit the digest.",
            )


#: The fields each role's type maps.
_KINDS: dict[str, frozenset[str]] = {
    "string": frozenset({"string"}),
    "text": frozenset({"string", "text"}),
    "int": frozenset({"int"}),
    "float": frozenset({"float", "int"}),
    "bool": frozenset({"bool"}),
    "date": frozenset({"date"}),
    "datetime": frozenset({"datetime"}),
    "enum": frozenset({"enum"}),
    "quantity": frozenset({"quantity"}),
    "ref": frozenset({"ref"}),
}


def find_writer(semantics: _validate._Semantics, qualified: str) -> Writer | None:
    found = semantics.plugin.writers.get(qualified)
    if found is not None:
        return found
    other = semantics.others.get(naming.plugin_of(qualified))
    return other.plugin.writers.get(qualified) if other is not None and other.plugin else None


def check_export(
    semantics: _validate._Semantics,
    entity_type: EntityType,
    export: PluginExport,
    source: Document,
    path: Path,
    fields: dict[str, Field],
) -> None:
    """A type's export through a writer: the writer exists where the type can use it, and
    each role it maps is the writer's, to a field of the role's type; every required role
    is mapped."""
    owner = naming.plugin_of(export.export)
    if owner != entity_type.plugin and owner not in semantics.plugin.dependencies:
        semantics.add(
            "writer-dependency",
            f"`{export.export}` is the {owner} plugin's writer: depend on {owner} to use it.",
            source,
            (*path, "export"),
        )
        return
    writer = find_writer(semantics, export.export)
    if writer is None:
        semantics.add(
            "unknown-writer",
            f"There's no writer `{export.export}`.",
            source,
            (*path, "export"),
            hint="Declare it under `writers` in plugin.yaml, or name a dependency's.",
        )
        return
    for role, name in export.fields.items():
        where: Path = (*path, "fields", role)
        wanted = writer.roles.get(role)
        if wanted is None:
            semantics.add(
                "unknown-role",
                f"`{export.export}` has no role `{role}`; it has "
                f"{', '.join(f'`{r}`' for r in writer.roles)}.",
                source,
                where,
                key=True,
            )
            continue
        found = fields.get(name)
        if found is None:
            semantics.add("unknown-field", f"This type has no field `{name}`.", source, where)
            continue
        problem = _mismatch(semantics, wanted, found)
        if problem:
            semantics.add(
                "export-field",
                f"`{name}` {problem}, which `{export.export}`'s `{role}` reads.",
                source,
                where,
            )
    for role, wanted in writer.roles.items():
        if wanted.required and role not in export.fields:
            semantics.add(
                "export-role-missing",
                f"`{export.export}` needs a field for its `{role}`.",
                source,
                (*path, "fields"),
                hint=f"Map `{role}` to a {wanted.type} field.",
            )


def _mismatch(semantics: _validate._Semantics, role: WriterRole, found: Field) -> str:
    """Why ``found`` can't fill ``role``; empty when it can."""
    if found.type not in _KINDS[role.type]:
        return f"is a {found.type} field, not a {role.type} one"
    if role.type == "quantity" and role.unit and found.unit:
        try:
            if not units.parse(found.unit).is_commensurable(units.parse(role.unit)):
                return f"is in {found.unit}, which doesn't measure what {role.unit} does"
        except units.UnitError:
            return ""
    if role.type == "ref" and role.to:
        for target in found.to:
            entity_type = semantics.types.get(target)
            lineage = {target} | (
                {t.qualified_name for t in semantics.ancestors(entity_type)}
                if entity_type
                else set()
            )
            if not lineage & set(role.to):
                return f"refers to {target}, not to {' or '.join(role.to)}"
    return ""
