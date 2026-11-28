"""Only the write pipeline writes values, revisions and history (ADR 0280).

Every change to an entity is planned by its operation, checked by
:func:`iki_server.entities.pipeline.evaluate` and written by
:func:`~iki_server.entities.pipeline.commit`, so a save's rules hold whichever operation
makes it (ADR 0035, ADR 0127). This holds the server's code to it: no other module
reaches for the pipeline's writers, makes a revision, or bumps an entity's revision,
but those that restore or insert in bulk what the pipeline can't, each said why.
"""

import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src"

PIPELINE = "iki_server/entities/pipeline.py"

#: The pipeline's own writers.
WRITERS = frozenset({"_write", "_write_each", "_insert", "_revise", "_write_history"})

#: What a manager writes with.
WRITES = frozenset(
    {"create", "bulk_create", "update", "bulk_update", "get_or_create", "update_or_create"}
)

#: SQL that writes a revision, or an entity's revision number.
SQL = ("insert into iki_revision", "update iki_revision", "update iki_entity set revision")

#: Modules that write revisions themselves, and why the pipeline can't for them.
ALLOWED = {
    # A bundle's restore (ADR 0096) puts back the revisions a record had where it came
    # from, as they were, numbers, authors and times included: it writes nothing new, and
    # checks what it restores against the rules after.
    "iki_server/bundles/applying.py": "restores a bundle's revisions as they were",
    # A bulk import (ADR 0056) inserts its millions of records set-based, with their first
    # revisions, and checks their rules a chunk at a time.
    "iki_server/runs/imports.py": "inserts a bulk import's records set-based",
    # A plugin migration's data operations (ADR 0212) run in the migration, against its
    # historical models and its database alias, and write the migrated revisions there.
    "iki_server/plugins/operations.py": "writes a plugin migration's migrated revisions",
}


def _root(node: ast.expr) -> str:
    """The name a method chain starts from: ``Revision.objects.filter().update`` →
    ``Revision``."""
    while True:
        if isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.Attribute):
            node = node.value
        else:
            return node.id if isinstance(node, ast.Name) else ""


def _writes(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(a.name in WRITERS for a in node.names):
            found.append((node.lineno, "imports a pipeline writer"))
        elif isinstance(node, ast.Attribute) and node.attr in WRITERS:
            found.append((node.lineno, f"calls {node.attr}"))
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "Revision":
                found.append((node.lineno, "makes a Revision"))
            elif isinstance(func, ast.Attribute) and func.attr in WRITES:
                root = _root(func.value)
                if root == "Revision":
                    found.append((node.lineno, f"Revision.{func.attr}"))
                elif root == "Entity" and any(k.arg == "revision" for k in node.keywords):
                    found.append((node.lineno, "sets an entity's revision"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = " ".join(node.value.lower().split())
            if any(s in text for s in SQL):
                found.append((node.lineno, "writes a revision in SQL"))
    return found


def _outside(root: Path = SOURCE) -> dict[str, list[tuple[int, str]]]:
    """Each module but the pipeline that writes revisions, with where and how."""
    out: dict[str, list[tuple[int, str]]] = {}
    for path in sorted(root.rglob("*.py")):
        name = path.relative_to(root).as_posix()
        if name == PIPELINE or "/migrations/" in name:
            continue
        found = _writes(ast.parse(path.read_text(encoding="utf-8")))
        if found:
            out[name] = found
    return out


def test_only_the_pipeline_writes_revisions() -> None:
    found = _outside()
    assert {n: f for n, f in found.items() if n not in ALLOWED} == {}
    # Each module allowed still needs to be: one that stops is taken off the list.
    assert sorted(found.keys() & ALLOWED.keys()) == sorted(ALLOWED)


def test_the_check_finds_each_way_of_writing_one(tmp_path: Path) -> None:
    (tmp_path / "iki_server").mkdir()
    (tmp_path / "iki_server" / "bad.py").write_text(
        "from iki_server.entities.pipeline import _revise\n"
        "services._write(form, rows, values, pk)\n"
        "Revision.objects.create(entity_id=pk, number=2)\n"
        "Revision.objects.bulk_create([Revision(entity_id=pk, number=2)])\n"
        "Entity.objects.filter(pk=pk).update(revision=2)\n"
        'cursor.execute("INSERT INTO  iki_revision (id) VALUES (%s)", [pk])\n'
        "Entity.objects.filter(pk=pk).update(archived_at=None)\n"
        "Revision.objects.filter(entity_id=pk).first()\n",
        encoding="utf-8",
    )
    found = _outside(tmp_path)["iki_server/bad.py"]
    assert sorted({line for line, _ in found}) == [1, 2, 3, 4, 5, 6]
