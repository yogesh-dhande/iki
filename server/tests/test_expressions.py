"""Apps' text and expressions (ADR 0090), checked by the server with iki-wasm, the module
browsers render them with."""

import threading
from pathlib import Path
from typing import Any

import pytest

from iki_server import expressions
from iki_spec import wasm

pytestmark = pytest.mark.wasm


def test_says_which_names_and_fields_each_text_reads() -> None:
    checked = expressions.check(
        [
            "{{ size(proteins.selection) > 0 && inputs.entity.hrid != '' }}",
            "Longest: {{ proteins.rows.map(r, r.values.length).exists(l, l > limit) }}",
            "{{ '{{ not a name }}' }}",
            "Just text, {not} an expression.",
        ]
    )

    assert checked == [
        expressions.Checked(
            names=("inputs", "proteins"),
            fields=("inputs.entity", "proteins.selection"),
            whole=True,
        ),
        # map and exists bind r and l: they aren't names it reads.
        expressions.Checked(names=("limit", "proteins"), fields=("proteins.rows",)),
        expressions.Checked(whole=True),
        expressions.Checked(),
    ]
    assert all(c.valid for c in checked)


def test_says_why_text_doesnt_read() -> None:
    deep = "(" * 40 + "1" + ")" * 40
    checked = expressions.check(
        ["{{ 1 + }}", "{{ }}", "{{" + deep + "}}", "{{" + "a" * 1001 + "}}", "{{ open"]
    )

    assert [c.valid for c in checked] == [False] * 5
    assert [c.error for c in checked] == [
        "In `{{ 1 + }}`: It ends too soon, at line 1, column 6.",
        "In `{{ }}`: It's empty.",
        # A long one is quoted by its start, so the message stays short.
        f"In `{{{{{deep[:60]}…}}}}`: It nests more than 32 deep.",
        f"In `{{{{{'a' * 60}…}}}}`: It's over 1000 characters, the most an expression may be.",
        "The `{{` at character 1 isn't closed.",
    ]


def test_each_thread_has_an_instance_of_its_own() -> None:
    found: list[list[expressions.Checked]] = []

    def work() -> None:
        found.append(expressions.check([f"{{{{ a{n} + b }}}}" for n in range(50)]))

    threads = [threading.Thread(target=work) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(found) == 4
    assert all(f == found[0] for f in found)
    assert found[0][7] == expressions.Checked(names=("a7", "b"), whole=True)


def test_nothing_to_check_needs_no_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wasm, "WASM", Path("/nowhere/iki_wasm.wasm"))
    monkeypatch.setattr(wasm, "_compiled", None)
    monkeypatch.setattr(wasm, "_local", threading.local())
    assert expressions.check([]) == []
    # Text without braces has no expressions, so the module isn't needed for it.
    assert expressions.check(["Hello.", ""]) == [expressions.Checked(), expressions.Checked()]


def test_plain_text_reads_as_the_module_says() -> None:
    texts = ["Hello.", "{{ a.b }} and", "", "x }} y", "plain { brace }"]
    found = expressions.check(texts)
    asked: dict[str, Any] = wasm.call("iki_check", {"templates": texts}, wasm.FUEL)
    assert len(found) == len(texts) == len(asked["checked"])
    plain = {"names": [], "fields": [], "whole": False}
    for i in (0, 2, 4):
        assert found[i] == expressions.Checked()
        assert asked["checked"][i] == plain, asked
    assert found[1] == expressions.Checked(names=("a",), fields=("a.b",))
    # A stray closing pair still goes to the module, which reads it as text.
    assert found[3] == expressions.Checked()
    assert asked["checked"][3] == plain


def test_a_missing_module_says_how_to_build_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wasm, "WASM", Path("/nowhere/iki_wasm.wasm"))
    monkeypatch.setattr(wasm, "_compiled", None)
    monkeypatch.setattr(wasm, "_local", threading.local())

    with pytest.raises(expressions.UnavailableError, match=r"Run scripts/build-wasm\.sh"):
        expressions.check(["{{ 1 }}"])
