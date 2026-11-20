import hashlib
import math
import re
from fractions import Fraction
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


@pytest.mark.parametrize(
    ("value", "source", "target", "beyond"),
    [
        # Past what a float holds: 10³⁹⁶ g, or a gram in units of 10⁻³⁹⁶ g.
        (1, "10*99.10*99.10*99.10*99.g", "g", "a number"),
        (1, "g", "10*-99.10*-99.10*-99.10*-99.g", "a number"),
        (1, "10*99.10*99.g", "10*-99.10*-99.g", "a number"),
        # Outside a special unit's function: no concentration has a pH of 0 mol/L, or below.
        (0, "mol/L", "[pH]", "that unit"),
        (-1e-7, "mol/L", "[pH]", "that unit"),
        (-400, "[pH]", "mol/L", "a number"),
    ],
)
def test_a_value_with_none_in_the_target_unit_doesnt_convert(
    value: float, source: str, target: str, beyond: str
) -> None:
    with pytest.raises(UnitError, match=f"has no value in `{re.escape(target)}`: .* {beyond}"):
        convert(value, source, target)


def test_division_and_multiplication_read_left_to_right() -> None:
    # UCUM reads a/b.c as (a/b)·c.
    assert parse("m/s.s").dimension == parse("m").dimension
    assert parse("m/(s.s)").dimension == parse("m/s2").dimension


# UCUM 2.2's own examples (https://ucum.org/ucum), section by section. Expected values are
# what the specification states, not what Iki computes.


@pytest.mark.parametrize(
    ("code", "same_as", "ratio"),
    [
        # §3: "10*" ends in a non-digit, so it is a symbol; the table gives it the value 10.
        ("10*", "1", 10),
        # §6: g% is a metric atom, so mg% is a thousandth of it.
        ("mg%", "g%", Fraction(1, 1000)),
        # §6, §12: annotations don't change the unit, and one on its own is the unity.
        ("%{vol}", "%", 1),
        ("kg{total}", "kg", 1),
        ("{RBC}", "1", 1),
        # §7: a leading solidus inverts the unit that directly follows it.
        ("/m", "m-1", 1),
        # §7: …only that unit; the rest reads left to right, so /s.m is s⁻¹·m.
        ("/s.m", "m/s", 1),
        ("/s.m", "m.s-1", 1),
        ("/s/m", "s-1.m-1", 1),
        ("/(s.m)", "s-1.m-1", 1),
        # §7: strictly left to right, so a/b.c is (a/b)·c; parentheses give a/(b·c) (§10).
        ("m/s.s", "m", 1),
        ("m/(s.s)", "m/s2", 1),
        # §8: "123" is a positive integer; the period only multiplies, so "2.5" is 2 times 5.
        ("123", "1", 123),
        ("2.5", "1", 10),
        # §9: the prefix is raised with the atom, so 1 cm3 is 10⁻⁶ m³, not 10⁻² m³.
        ("cm3", "m3", Fraction(1, 10**6)),
        # §9: "2+10 means 2¹⁰ = 1024"; a sign delimits an integer's exponent.
        ("2+10", "1", 1024),
        ("2+10.m", "m", 1024),
        ("2-1", "1", Fraction(1, 2)),
        ("10+3/uL", "10*3/uL", 1),
        ("23", "1", 23),  # no sign: twenty-three, not 2³
    ],
)
def test_ucum_specification_examples(code: str, same_as: str, ratio: Fraction | int) -> None:
    unit, reference = parse(code), parse(same_as)
    assert unit.is_commensurable(reference)
    assert unit.factor / reference.factor == ratio
    assert convert(1, code, same_as) == pytest.approx(float(ratio), rel=1e-15)
    assert convert(float(ratio), same_as, code) == pytest.approx(1, rel=1e-15)


def test_a_leading_solidus_in_the_table_inverts_one_unit() -> None:
    # The table defines the oersted as 250 /[pi].A/m: (250/π) A/m, the CGS unit of magnetic
    # field strength (NIST SP 811, B.8: 1 Oe = (1000/4π) A/m ≈ 79.577 47 A/m).
    assert parse("Oe").is_commensurable(parse("A/m"))
    assert convert(1, "Oe", "A/m") == pytest.approx(1000 / (4 * math.pi), rel=1e-15)


def test_the_tables_leading_solidus_definitions_have_the_right_dimension() -> None:
    # The gilbert is defined as Oe.cm, so it follows the oersted: (10/4π) A (NIST SP 811,
    # B.8: 1 Gb = (10/4π) A ≈ 0.795 774 7 A).
    assert convert(1, "Gb", "A") == pytest.approx(10 / (4 * math.pi), rel=1e-15)


def test_arbitrary_units_never_convert_to_one_another() -> None:
    # UCUM 2.2 §24 ■3: arbitrary units "are not 'commensurable with' any other unit", and
    # §25 ■1: a term with one "is not comparable with any other arbitrary unit or term".
    # So [IU] and [iU] stay apart although the table relates them: an IU of one substance
    # isn't an IU of another.
    with pytest.raises(UnitError, match="measure different things"):
        convert(1, "[IU]", "[iU]")
    assert not parse("[iU]").is_commensurable(parse("[arb'U]"))
    assert not parse("[iU]").is_commensurable(parse("1"))
    # Within one code, prefixes still scale.
    assert convert(1000, "m[IU]", "[IU]") == pytest.approx(1)


@pytest.mark.parametrize("ratio", ["[IU]/[IU]", "[arb'U]/[arb'U]", "[IU].mL/[IU]", "m[IU]/[IU]"])
def test_a_ratio_of_arbitrary_units_stays_arbitrary(ratio: str) -> None:
    # UCUM 2.2 §25 ■1: "Any term involving arbitrary units" is itself arbitrary, even when
    # its exponents cancel. [IU]/[IU] is a ratio within one assay; the code alone can't
    # say it's an ordinary ratio, so it isn't 100 %, or 1.
    unit = parse(ratio)
    assert not unit.is_dimensionless
    for ordinary in ("1", "%", "mL", "[ppm]"):
        assert not unit.is_commensurable(parse(ordinary))
        with pytest.raises(UnitError, match="measure different things"):
            convert(1, ratio, ordinary)
    # A ratio of one arbitrary unit isn't one of another.
    assert not parse("[IU]/[IU]").is_commensurable(parse("[arb'U]/[arb'U]"))
    # Ratios of the same arbitrary unit still convert, by their prefixes.
    assert convert(1, "m[IU]/[IU]", "[IU]/[IU]") == pytest.approx(1e-3)
    assert convert(1, "[IU]/[IU]", "[IU]/[IU]") == 1


@pytest.mark.parametrize(
    ("source", "target", "expected"),
    [
        ("mL", "uL", (1000, 0)),
        ("mg/mL", "g/L", (1, 0)),
        # Degrees convert by a scale and an offset: °F = 1.8 °C + 32 (NIST SP 811, B.9).
        ("Cel", "[degF]", (1.8, 32)),
        ("[degF]", "Cel", (5 / 9, -160 / 9)),
        ("Cel", "K", (1, 273.15)),
        ("[degRe]", "Cel", (1.25, 0)),
    ],
)
def test_linear_pairs_have_a_scale_and_an_offset(
    source: str, target: str, expected: tuple[float, float]
) -> None:
    found = units.scale_and_offset(source, target)
    assert found == pytest.approx(expected, rel=1e-15, abs=1e-12)
    # …which give what converting does.
    assert found is not None
    for value in (-40.0, 0.0, 37.0, 1e6):
        assert found[0] * value + found[1] == pytest.approx(
            convert(value, source, target), rel=1e-12, abs=1e-9
        )


@pytest.mark.parametrize(
    ("source", "target", "value", "expected"),
    [
        # UCUM Table 20: dB[SPL] is 2 lg of a pressure relative to 20 µPa, so 200 dB is
        # 20 µPa · 10^(200/20) = 0.2 MPa: nothing a scale and an offset give, though three
        # values of it lie within 1e-12 MPa of a line.
        ("dB[SPL]", "MPa", 200, 0.2),
        ("dB[SPL]", "Pa", 94, 20e-6 * 10 ** (94 / 20)),
        # pH is minus lg of a concentration in mol/L: 1e-7 mol/L is pH 7, and 0 has none.
        ("mol/L", "[pH]", 1e-7, 7),
        ("[pH]", "mol/L", 7, 1e-7),
        ("%[slope]", "deg", 100, 45),
        # A bel of a field quantity, such as a voltage, is 2 lg of it: 1 B[V] is √10 V.
        ("B[V]", "V", 1, 10**0.5),
    ],
)
def test_nonlinear_pairs_have_no_scale_and_offset(
    source: str, target: str, value: float, expected: float
) -> None:
    assert units.scale_and_offset(source, target) is None
    assert convert(value, source, target) == pytest.approx(expected, rel=1e-9)


def test_units_that_dont_convert_have_no_scale_and_offset() -> None:
    assert units.scale_and_offset("mg", "mL") is None
    assert units.scale_and_offset("[IU]/[IU]", "%") is None


def test_a_scale_no_double_holds_isnt_given() -> None:
    """A unit of 10^396 g parses (each exponent is at most 99), but 10^-396, or 10^396,
    isn't a double: the scale would be 0 (every value shown as 0) or overflow. Values
    still convert one at a time, so pages show them in the field's own unit instead."""
    huge = ".".join(["10*99"] * 4) + ".g"
    assert units.scale_and_offset("g", huge) is None
    assert units.scale_and_offset(huge, "g") is None
    # A subnormal double has lost its precision: 10^-310 keeps a few digits of it.
    assert units.scale_and_offset("g", "10*99.10*99.10*99.10*13.g") is None
    assert convert(1e100, "g", huge) == pytest.approx(1e-296)
    assert convert(1e-100, huge, "g") == pytest.approx(1e296)
    # Within a double's range, the scale is given.
    assert units.scale_and_offset("g", "10*99.10*99.10*99.g") == pytest.approx((1e-297, 0))


@pytest.mark.parametrize("code", ["0", "00", "0.m", "m/0", "0+2", "10*3.0"])
def test_zero_is_not_a_unit(code: str) -> None:
    # §8 allows a *positive* integer in place of a unit; 0 would map every value to 0.
    with pytest.raises(UnitError, match="must be positive"):
        parse(code)


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
        ("2+10m", "joined to a unit with `.`", None, 0),
        ("2+", "needs digits after it", None, 1),
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


@pytest.mark.parametrize(
    ("code", "printed"),
    [
        ("Ao", "Å"),
        ("Cel", "°C"),
        ("ug/mL", "μg/mL"),
        ("m2", "m²"),
        ("kg.m-1", "kg·m⁻¹"),
        ("mg/(kg.d)", "mg/(kg·d)"),
        ("{cells}/uL", "cells/μL"),
        ("10*3/uL", "10³/μL"),
        ("[IU]/mL", "IU/mL"),
        ("g{total}", "g (total)"),
        ("[pH]", "pH"),
        ("not-a-unit", "not-a-unit"),
        # UCUM 2.2 §9: a signed exponent on a positive integer is a power of it, so 2-1 is
        # 2⁻¹ (½) and 2+10 is 2¹⁰ (1,024), not 2 minus 1 or 2 plus 10.
        ("2-1.m", "2⁻¹·m"),
        ("2+10.m", "2¹⁰·m"),
        ("10+3.m", "10³·m"),
        ("10-3.g/L", "10⁻³·g/L"),
        # Without a sign, the digits are the number: twenty-three metres.
        ("23.m", "23·m"),
    ],
)
def test_units_print_as_people_write_them(code: str, printed: str) -> None:
    assert units.symbol(code) == printed


@pytest.mark.parametrize(
    ("code", "offered"),
    [
        ("mg", ("mg", "kg", "g", "ug", "ng", "pg")),
        ("%", ("%", "[ppm]", "[ppb]")),
        ("g/mol", ("g/mol", "kg/mol", "ku", "u")),
        ("dg", ("dg", "kg", "g", "mg", "ug", "ng", "pg")),
        ("{bp}", ("{bp}",)),
        ("{cells}/uL", ("{cells}/uL",)),
        ("1", ("1",)),
        ("[IU]", ("[IU]",)),
    ],
)
def test_common_alternatives_stay_within_a_kind(code: str, offered: tuple[str, ...]) -> None:
    assert units.common_alternatives(code) == offered


def test_common_units_convert_within_their_kind() -> None:
    for kind in units.COMMON_UNITS:
        for code in kind:
            assert units.parse(code).is_commensurable(units.parse(kind[0])), code


def test_daltons_read_as_labs_write_them() -> None:
    assert (units.symbol("u"), units.symbol("ku")) == ("Da", "kDa")


@pytest.mark.parametrize(
    ("code", "at"),
    [
        ("10*100", 3),
        ("10^-100", 3),
        ("km100", 0),
        ("mg-1000000", 0),
        ("2+100", 1),
        ("m.s100", 2),
        ("10*1000000000", 3),
    ],
)
def test_an_exponent_past_99_is_refused_before_its_worked_out(code: str, at: int) -> None:
    with pytest.raises(UnitError) as raised:
        parse(code)
    assert "too large an exponent: a unit's are at most 99." in raised.value.message
    assert raised.value.position == at


def test_exponents_up_to_99_are_units() -> None:
    assert parse("10*99").factor == 10**99
    assert parse("10*-99").factor == Fraction(1, 10**99)
    assert parse("m99").dimension[0] == 99


def test_a_unit_whose_size_no_double_could_hold_is_refused() -> None:
    """Each exponent is within bounds, but together their factor would grow without one."""
    parse(".".join(["10*99"] * 8))  # 10^792: large, but within bounds
    with pytest.raises(UnitError) as raised:
        parse(".".join(["10*99"] * 13))
    assert "too large to work with" in raised.value.message
    with pytest.raises(UnitError):
        parse("/".join(["10*99"] * 15))


def test_parsing_keeps_a_bounded_number_of_units() -> None:
    for n in range(units.CACHED + 10):
        parse(f"{n + 1}.m")
    assert units._parse.cache_info().currsize <= units.CACHED
