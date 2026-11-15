"""Apps' expressions (ADR 0090), checked by the server with iki-wasm, the module browsers
evaluate them with."""

import threading
from pathlib import Path

import pytest

from iki_server import expressions

pytestmark = pytest.mark.wasm


def test_says_which_names_each_expression_reads() -> None:
    checked = expressions.check(
        [
            "size(proteins.selection) > 0 && inputs.entity.hrid != ''",
            "proteins.rows.map(r, r.values.length).exists(l, l > limit)",
            "'{{ not a name }}'",
        ]
    )

    assert checked == [
        expressions.Checked(names=("inputs", "proteins")),
        # map and exists bind r and l: they aren't names it reads.
        expressions.Checked(names=("limit", "proteins")),
        expressions.Checked(names=()),
    ]
    assert all(c.valid for c in checked)


def test_says_why_an_expression_doesnt_read() -> None:
    deep = "(" * 40 + "1" + ")" * 40
    checked = expressions.check(["1 +", "", deep, "a" * 1001])

    assert [c.valid for c in checked] == [False] * 4
    assert (
        checked[0].error is not None
        and checked[0].error == "It ends too soon, at line 1, column 4."
    )
    assert [c.error for c in checked[1:]] == [
        "It's empty.",
        "It nests more than 32 deep.",
        "It's over 1000 characters, the most an expression may be.",
    ]


def test_each_thread_has_an_instance_of_its_own() -> None:
    found: list[list[expressions.Checked]] = []

    def work() -> None:
        found.append(expressions.check([f"a{n} + b" for n in range(50)]))

    threads = [threading.Thread(target=work) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(found) == 4
    assert all(f == found[0] for f in found)
    assert found[0][7] == expressions.Checked(names=("a7", "b"))


def test_nothing_to_check_needs_no_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(expressions, "WASM", Path("/nowhere/iki_wasm.wasm"))
    assert expressions.check([]) == []


def test_a_missing_module_says_how_to_build_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(expressions, "WASM", Path("/nowhere/iki_wasm.wasm"))
    monkeypatch.setattr(expressions, "_compiled", None)
    monkeypatch.setattr(expressions, "_local", threading.local())

    with pytest.raises(expressions.UnavailableError, match=r"Run scripts/build-wasm\.sh"):
        expressions.check(["1"])
