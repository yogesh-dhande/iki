import hashlib
import math
from importlib.resources import files

import pytest

from iki_spec import units
from iki_spec.units import UnitError, convert, parse

# UCUM 2.2's ucum-essence.xml as published (github.com/ucum-org/ucum, tag v2.2).
# The UCUM License forbids modifying it, so any change must fail here.
UCUM_2_2_SHA256 = "dfccea1b5dc284245ebae97edd1dc03c45864da4e87df55bc9851797b4fd0b61"


def test_the_ucum_table_ships_unmodified() -> None:
    table = files("iki_spec.units").joinpath("ucum-essence.xml").read_bytes()
    assert hashlib.sha256(table).hexdigest() == UCUM_2_2_SHA256
    assert b'version="2.2"' in table
    assert units.UCUM_VERSION == "2.2"


def test_the_ucum_license_ships_with_it() -> None:
    text = files("iki_spec.units").joinpath("UCUM-LICENSE.md").read_text()
    assert "UCUM Copyright Notice and License" in text
    assert "Regenstrief Institute" in text


def test_every_unit_in_the_table_parses() -> None:
    table = units._table()
    for code in table.atoms:
        unit = parse(code)
        assert unit.code == code


@pytest.mark.parametrize(
    ("value", "source", "target", "expected"),
    [
        (1, "[in_i]", "m", 0.0254),
        (1, "[lb_av]", "g", 453.59237),
        (1, "h", "s", 3600),
        (1, "L", "dm3", 1),
        (1, "mL", "cm3", 1),
        (1, "mg/dL", "g/L", 0.01),
        (5, "mmol/L", "umol/mL", 5),
        (1, "[psi]", "kPa", 6.894757293168361),
        (760, "mm[Hg]", "kPa", 101.32472),  # UCUM: 1 m[Hg] = 133.322 kPa,
        (1, "kat", "mol/s", 1),
        (1, "u", "g", 1.66053906660e-24),
        (1, "ku", "u", 1000),
        (1, "%", "1", 0.01),
        (1, "10*", "1", 10),
        (1, "10*3/uL", "/L", 1e9),
        (1, "/min", "Hz", 1 / 60),
        (2, "kg.m/s2", "N", 2),
        (1, "J/s", "W", 1),
        (1, "deg", "rad", math.pi / 180),
        (1, "a", "d", 365.25),
        (3, "[IU]/mL", "[IU]/L", 3000),
        (1, "m[IU]", "[IU]", 0.001),
        (1, "{cells}/uL", "/uL", 1),
        (1, "B[10.nV]", "B[10.nV]", 1),
    ],
)
def test_converts_between_units(value: float, source: str, target: str, expected: float) -> None:
    assert convert(value, source, target) == pytest.approx(expected, rel=1e-12)


@pytest.mark.parametrize(
    ("value", "source", "target", "expected"),
    [
        (37, "Cel", "[degF]", 98.6),
        (0, "Cel", "K", 273.15),
        (-40, "[degF]", "Cel", -40),
        (80, "[degRe]", "Cel", 100),
        (7, "[pH]", "mol/L", 1e-7),
        (1e-3, "mol/L", "[pH]", 3),
        (20, "dB", "B", 2),
        (1, "B", "1", 10),
        (100, "%[slope]", "deg", 45),
        (100, "[p'diop]", "rad", math.pi / 4),
        (3, "bit_s", "1", 8),
        (1, "Np", "1", math.e),
        (2, "[hp'_X]", "1", 0.01),
        (1, "[hp'_C]", "1", 0.01),
        (60, "B[SPL]", "Pa", 2e-5 * 10**30),
    ],
)
def test_converts_special_units(value: float, source: str, target: str, expected: float) -> None:
    assert convert(value, source, target) == pytest.approx(expected, rel=1e-9)
    # …and back again.
    assert convert(convert(value, source, target), target, source) == pytest.approx(value, rel=1e-9)


def test_prefixes_are_case_sensitive() -> None:
    assert parse("mg").factor * 10**9 == parse("Mg").factor
    assert parse("cd").dimension == (0, 0, 0, 0, 0, 0, 1)  # candela, not centi-day


def test_commensurability() -> None:
    assert parse("mg/dL").is_commensurable(parse("kg/m3"))
    assert not parse("mg").is_commensurable(parse("mL"))
    assert not parse("[IU]").is_commensurable(parse("1"))
    assert not parse("[IU]").is_commensurable(parse("[arb'U]"))
    assert parse("Cel").is_commensurable(parse("K"))
    assert parse("{bp}").is_dimensionless
    assert not parse("[IU]").is_dimensionless


def test_units_that_measure_different_things_dont_convert() -> None:
    with pytest.raises(UnitError, match="measure different things"):
        convert(1, "mg", "mL")


def test_division_and_multiplication_read_left_to_right() -> None:
    # UCUM reads a/b.c as (a/b)·c.
    assert parse("m/s.s").dimension == parse("m").dimension
    assert parse("m/(s.s)").dimension == parse("m/s2").dimension


@pytest.mark.parametrize(
    ("code", "message", "hint", "position"),
    [
        ("Da", "`Da` isn't a UCUM unit.", "`u` is the unified atomic mass unit", 0),
        ("kDa", "`kDa` isn't a UCUM unit.", "`ku`", 0),
        ("°C", "`°` isn't allowed", "`Cel`", 0),
        ("µg", "`µ` isn't allowed", "`ug`", 0),
        ("mg /dL", "plain ASCII with no spaces", "Join units with `.`", 2),
        ("KG", "`KG` isn't a UCUM unit.", "case-sensitive. Did you mean `kg`?", 0),
        ("mgg", "`mgg` isn't a UCUM unit.", "Did you mean `mg`", 0),
        ("uM", "`uM` isn't a UCUM unit.", "`umol/L`", 0),
        ("hr", "`hr` isn't a UCUM unit.", "`h`", 0),
        ("mg/", "missing after the operator", None, 3),
        ("/", "missing after the operator", None, 1),
        ("(m/s", "isn't closed", None, 0),
        ("(m/s)2", "parenthesised term to a power", "m2/s2", 5),
        ("5mg", "joined to a unit with `.`", None, 0),
        ("10*-", "needs digits after it", None, 3),
        ("m{cells", "isn't closed", None, 1),
        ("[in_i", "isn't closed", None, 0),
        ("m]", "Unexpected `]`", None, 1),
        ("m)", "Unexpected `)`", None, 1),
        (".m", "missing before the operator", None, 0),
        ("Cel/h", "combines a special unit", "`K/min`", 0),
        ("Cel2", "can't be raised to a power", None, 0),
        ("k[degF]", "can't take a prefix", None, 0),
        ("", "can't be empty", None, 0),
        ("m" * 201, "at most 200 characters", None, 0),
    ],
)
def test_explains_what_is_wrong(code: str, message: str, hint: str | None, position: int) -> None:
    with pytest.raises(UnitError) as raised:
        parse(code)
    assert message in raised.value.message
    if hint is None:
        assert raised.value.hint is None
    else:
        assert raised.value.hint is not None
        assert hint in raised.value.hint
    assert raised.value.position == position


def test_parsed_units_are_cached_and_equal() -> None:
    assert parse("mg") is parse("mg")
    assert parse("mg") == parse("mg")
