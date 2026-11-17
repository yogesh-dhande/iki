"""Row locks take only the rows they name.

``select_for_update()`` with ``select_related()`` is ``FOR UPDATE`` over the whole join:
it locks the related rows too, such as a run's project, team and launcher, which every
run of theirs shares. Completing a person's runs then waits on each other, one at a
time. Every such lock in the server says which rows it means with ``of=``.
"""

import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src"


def _chain(call: ast.Call) -> list[ast.Call]:
    """The calls of a method chain, outermost first: ``a.b().c()`` → ``[c(), b()]``."""
    calls: list[ast.Call] = []
    node: ast.expr = call
    while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        calls.append(node)
        node = node.func.value
    return calls


def _unscoped(root: Path = SOURCE) -> list[str]:
    found = []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "select_for_update" not in text:
            continue
        tree = ast.parse(text)
        inner = {
            id(c) for node in ast.walk(tree) if isinstance(node, ast.Call) for c in _chain(node)[1:]
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or id(node) in inner:
                continue
            calls = _chain(node)
            names = [c.func.attr for c in calls if isinstance(c.func, ast.Attribute)]
            if "select_for_update" not in names or "select_related" not in names:
                continue
            lock = calls[names.index("select_for_update")]
            if not any(k.arg == "of" for k in lock.keywords):
                found.append(f"{path.relative_to(root)}:{node.lineno}")
    return found


def test_locks_with_related_rows_name_the_rows_they_lock() -> None:
    assert _unscoped() == []


def test_the_check_finds_one_that_doesnt(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text(
        "Run.objects.select_for_update().select_related('project').get(pk=1)\n"
        "Run.objects.select_for_update(of=('self',)).select_related('project').get(pk=1)\n",
        encoding="utf-8",
    )
    assert _unscoped(tmp_path) == ["bad.py:1"]
