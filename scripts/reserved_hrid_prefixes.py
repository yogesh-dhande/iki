"""Lists the HRID prefixes Iki uses and reserves, in ``iki_spec/reserved_hrid_prefixes.json``.

A deployment's types share one space of HRID prefixes (ADR 0033). Iki publishes the
prefixes its own types use, and those it reserves for first-party types on its roadmap,
so a lab plugin can stay clear of them: the validator warns one that takes a prefix Iki's
plugins use or reserve (``reserved-hrid-prefix``), and refuses one that core uses.

- ``core``: the core plugin's types, and the records the server numbers itself (runs,
  batches, workflows and setup builds). No plugin may use one, and no deployment can
  give them another.
- ``plugins``: the types of Iki's fetched plugins (``plugins/``).
- ``planned``: first-party types the roadmap names that aren't built yet.

``scripts/first_party_trees.py`` (``make first-party``) runs it, so the list is
refreshed whenever a first-party plugin changes. With ``--check``, it changes nothing
and fails if the list is out of date.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections.abc import Iterable
from pathlib import Path

from iki_spec import first_party, hrid_prefixes, naming
from iki_spec.model import LIST_HRID_PREFIX
from iki_spec.yamlsource import read_documents

ROOT = Path(__file__).resolve().parents[1]
LIST = ROOT / "spec" / "src" / "iki_spec" / hrid_prefixes.FILENAME
CORE_TYPES = ROOT / "spec" / "src" / "iki_spec" / "core" / "types"
#: Where the server keeps the prefixes of the records it numbers itself.
SERVER_PREFIXES = ROOT / "server" / "src" / "iki_server" / "runs" / "models.py"
#: What each of those constants numbers.
SERVER_RECORDS = {
    "HRID_PREFIX": "Iki's action runs",
    "BATCH_HRID_PREFIX": "Iki's batches of runs",
    "WORKFLOW_HRID_PREFIX": "Iki's workflow runs",
    "SETUP_HRID_PREFIX": "Iki's environment setup builds",
}
#: First-party types the roadmap names that aren't on main yet, with the prefix each is
#: built with. Specific complex types beyond the antibody (roadmap, "to scope"; built on
#: deferred/complex-types). An entry leaves this list when its type lands in plugins/.
PLANNED = {
    "AAC": "iki/antibody.AntibodyAntigenComplex",
    "FCF": "iki/antibody.FcFusion",
    "MSA": "iki/antibody.MultispecificAntibody",
}


def _types(plugin: str, directory: Path) -> Iterable[tuple[str, str]]:
    """Each concrete type in ``directory`` with an HRID prefix, as (prefix, qualified)."""
    for file in sorted([*directory.rglob("*.yaml"), *directory.rglob("*.yml")]):
        documents, problems = read_documents(file, str(file.relative_to(ROOT)))
        if problems:
            raise ValueError(f"{file.relative_to(ROOT)} doesn't read: {problems[0].message}")
        for document in documents:
            data = document.data
            if not isinstance(data, dict) or data.get("kind") != "EntityType":
                continue
            hrid = data.get("hrid")
            if data.get("abstract") or not isinstance(hrid, dict) or "prefix" not in hrid:
                continue
            yield str(hrid["prefix"]), f"{plugin}.{data['name']}"


def _server_records() -> Iterable[tuple[str, str]]:
    tree = ast.parse(SERVER_PREFIXES.read_text("utf-8"))
    for node in tree.body:
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id.endswith("HRID_PREFIX")
        ):
            continue
        name = node.targets[0].id
        if name not in SERVER_RECORDS or not isinstance(node.value, ast.Constant):
            raise ValueError(
                f"{SERVER_PREFIXES.relative_to(ROOT)} has {name}: say what it numbers in "
                "SERVER_RECORDS, in scripts/reserved_hrid_prefixes.py."
            )
        yield str(node.value.value), SERVER_RECORDS[name]


def _add(into: dict[str, str], found: Iterable[tuple[str, str]], taken: dict[str, str]) -> None:
    for prefix, what in found:
        if prefix in taken:
            raise ValueError(f"{taken[prefix]} and {what} both use the HRID prefix {prefix}.")
        into[prefix] = taken[prefix] = what


def current() -> dict[str, dict[str, str]]:
    taken: dict[str, str] = {}
    core: dict[str, str] = {}
    plugins: dict[str, str] = {}
    _add(core, _types(naming.CORE, CORE_TYPES), taken)
    _add(core, _server_records(), taken)
    # Every lab list's options share one sequence (ADR 0295).
    _add(core, [(LIST_HRID_PREFIX, "Iki's lab list options")], taken)
    for name, directory in first_party.catalog(ROOT / "plugins").items():
        _add(plugins, _types(name, directory / "types"), taken)
    landed = set(plugins.values())
    planned: dict[str, str] = {}
    _add(planned, ((p, t) for p, t in PLANNED.items() if t not in landed), taken)
    return {
        "core": dict(sorted(core.items())),
        "plugins": dict(sorted(plugins.items())),
        "planned": dict(sorted(planned.items())),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="Fail if the list is out of date.")
    args = parser.parse_args(argv)
    try:
        made = current()
    except ValueError as error:
        sys.stderr.write(f"{error}\n")
        return 1
    text = json.dumps(made, indent=2, ensure_ascii=False) + "\n"
    if args.check:
        if not LIST.is_file() or LIST.read_text("utf-8") != text:
            sys.stderr.write(f"{LIST.relative_to(ROOT)} is out of date: run `make first-party`.\n")
            return 1
        return 0
    if LIST.is_file() and LIST.read_text("utf-8") == text:
        return 0
    LIST.write_text(text, "utf-8")
    counts = ", ".join(f"{len(v)} {k}" for k, v in made.items())
    sys.stdout.write(f"Listed the HRID prefixes Iki uses and reserves: {counts}.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
