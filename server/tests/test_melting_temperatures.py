"""Every module that quotes an oligo's melting temperature gives what it gave, and the 50 mM
Na+ temperature is one number wherever it is read (ADR 0191).

The values are in server/tests/data/melting_temperatures.json, recorded by
server/tests/data/record_melting_temperatures.py, which says how the 21,360 oligos are
made and where each column comes from. That they are right is the modules' own tests'
business, against SantaLucia 1998 (DOI 10.1073/pnas.95.4.1460) and Primer3; these hold the
modules to them, oligo by oligo.
"""

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

DATA = Path(__file__).resolve().parent / "data"


def _recorder() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "record_melting_temperatures", DATA / "record_melting_temperatures.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RECORDER = _recorder()


@pytest.fixture(scope="module")
def recorded() -> dict[str, Any]:
    values: dict[str, Any] = json.loads((DATA / "melting_temperatures.json").read_text())
    return values


@pytest.fixture(scope="module")
def made(recorded: dict[str, Any]) -> list[str]:
    made: list[str] = RECORDER.oligos()
    # The oligos are made, not stored; a change to how would show here first, rather than
    # as thousands of temperatures that moved.
    assert len(made) == recorded["oligos"]
    assert RECORDER.digest(made) == recorded["bases_sha256"]
    return made


def _differences(
    made: list[str], got: list[float | None], expected: list[float | None]
) -> list[str]:
    return [
        f"{bases}: {now} where it was {then}"
        for bases, now, then in zip(made, got, expected, strict=True)
        if now != then
    ][:10]


def test_the_oligos_include_the_palindromes_where_conventions_part(made: list[str]) -> None:
    palindromes = {bases for bases in made if bases == RECORDER.reverse_complement(bases)}
    # Every one of 4 to 10 bases -- 16 + 64 + 256 + 1,024 -- and the few drawn at random.
    assert len(palindromes) >= 1_360
    assert {"GGATCC", "GAATTC", "AAGCTT", "GCGGCCGC"} <= palindromes


@pytest.mark.parametrize(
    "module", ["iki_wasm", "oligo-measurement", "oligo-checks", "plasmid-tools"]
)
def test_every_module_gives_the_one_sodium_temperature(
    module: str, made: list[str], recorded: dict[str, Any], sodium: dict[str, list[float | None]]
) -> None:
    # The sequence record's measurement in the server and the DNA plugin, the primer checks
    # under Conditions::PRIMER, and the plasmid plugin's primer alone: one implementation,
    # so one number for each of 21,360 oligos, palindromes included.
    assert _differences(made, sodium[module], recorded["sodium"]) == []


@pytest.fixture(scope="module")
def sodium(made: list[str]) -> dict[str, list[float | None]]:
    found: dict[str, list[float | None]] = RECORDER.sodium_measurements(made)
    return found


def test_the_primer_checks_buffer_temperature_is_unchanged(
    made: list[str], recorded: dict[str, Any]
) -> None:
    assert _differences(made, RECORDER.checked(made, None), recorded["buffer"]) == []


def test_a_binding_site_is_two_molecules_even_for_a_palindrome(
    made: list[str], recorded: dict[str, Any]
) -> None:
    # A primer on a template pairs with another molecule, so a palindrome's site keeps the
    # two-strand reading -- GGATCC -13.7, where alone against its own complement it is
    # -11.4 -- and every other site is the sodium temperature.
    priming = RECORDER.script("priming", RECORDER.PLASMID)
    expected = [
        recorded["on_template"].get(str(at), recorded["sodium"][at]) for at in range(len(made))
    ]
    assert (
        _differences(made, [priming.melting_temperature(bases, bases) for bases in made], expected)
        == []
    )
    assert recorded["on_template"][str(made.index("GGATCC"))] == -13.7
    assert recorded["sodium"][made.index("GGATCC")] == -11.4


def test_a_mismatched_site_is_unchanged(made: list[str], recorded: dict[str, Any]) -> None:
    priming = RECORDER.script("priming", RECORDER.PLASMID)
    got = [priming.melting_temperature(bases, RECORDER.mismatched(bases)) for bases in made]
    assert _differences(made, got, recorded["mismatched"]) == []
