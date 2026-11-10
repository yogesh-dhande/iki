"""Units of measure, in UCUM: check unit codes and convert values between them.

UCUM (the Unified Code for Units of Measure) is the units standard used by
lab systems, HL7 and FHIR. Iki reads the official UCUM table,
`ucum-essence.xml`, which ships unmodified beside this module under the UCUM
License (see `UCUM-LICENSE.md`). The parser and converter here are Iki's own.

Codes are case-sensitive ("c/s" UCUM): `mg` is a milligram and `Mg` a
megagram. A few special units (degrees Celsius, pH, decibels and the like)
are defined by functions instead of factors; they can't be multiplied,
divided or raised to a power, because they don't scale from zero.

    >>> convert(37, "Cel", "[degF]")
    98.6...
    >>> parse("mg/dL").is_commensurable(parse("g/L"))
    True
"""

from __future__ import annotations

import difflib
import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from fractions import Fraction
from functools import cache
from importlib.resources import files

__all__ = [
    "BASE_UNITS",
    "UCUM_VERSION",
    "Unit",
    "UnitError",
    "convert",
    "parse",
]

UCUM_VERSION = "2.2"
_NS = {"u": "http://unitsofmeasure.org/ucum-essence"}

#: UCUM's seven base units, in the order of `Unit.dimension`.
BASE_UNITS = ("m", "s", "g", "rad", "K", "C", "cd")

# Longest accepted code. Real codes are short; this bounds parsing work.
MAX_LENGTH = 200


class UnitError(ValueError):
    """A unit code that isn't valid UCUM. `hint` suggests a fix when there is one."""

    def __init__(self, message: str, *, code: str, position: int, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.position = position
        self.hint = hint


# --- Special units ---------------------------------------------------------------
#
# A special unit's value x maps to a magnitude by a function: the magnitude is
# from_special(x) times the unit's reference (the `value` and `Unit` of its
# <function> in the table). `to_special` is the inverse.


@dataclass(frozen=True)
class _Function:
    from_special: Callable[[float], float]
    to_special: Callable[[float], float]


def _degrees_from_slope(x: float) -> float:
    # %[slope] is 100·tan(angle), with its reference in degrees.
    return math.degrees(math.atan(x / 100))


def _slope_from_degrees(x: float) -> float:
    return 100 * math.tan(math.radians(x))


_FUNCTIONS: dict[str, _Function] = {
    "Cel": _Function(lambda x: x + 273.15, lambda x: x - 273.15),
    "degF": _Function(lambda x: x + 459.67, lambda x: x - 459.67),
    "degRe": _Function(lambda x: x + 218.52, lambda x: x - 218.52),
    "pH": _Function(lambda x: 10**-x, lambda x: -math.log10(x)),
    "ln": _Function(math.exp, math.log),
    "lg": _Function(lambda x: 10**x, math.log10),
    "lgTimes2": _Function(lambda x: 10 ** (x / 2), lambda x: 2 * math.log10(x)),
    "ld": _Function(lambda x: 2**x, math.log2),
    "tanTimes100": _Function(lambda x: math.atan(x / 100), lambda x: 100 * math.tan(x)),
    "100tan": _Function(_degrees_from_slope, _slope_from_degrees),
    "sqrt": _Function(lambda x: x * x, math.sqrt),
    "hpX": _Function(lambda x: 10**-x, lambda x: -math.log10(x)),
    "hpC": _Function(lambda x: 100**-x, lambda x: -math.log(x) / math.log(100)),
    "hpM": _Function(lambda x: 1000**-x, lambda x: -math.log(x) / math.log(1000)),
    "hpQ": _Function(lambda x: 50000**-x, lambda x: -math.log(x) / math.log(50000)),
}


@dataclass(frozen=True)
class _Special:
    function: str
    scale: Fraction  # a prefix's factor, applied to the special value
    reference: Unit  # the magnitude one "function unit" stands for


# --- The table ---------------------------------------------------------------------


@dataclass(frozen=True)
class _Atom:
    code: str
    name: str
    metric: bool
    arbitrary: bool
    special: str | None  # function name
    value: str  # definition: a number…
    unit: str  # …times a unit expression


@dataclass(frozen=True)
class _Table:
    prefixes: dict[str, Fraction]
    prefix_names: dict[str, str]
    atoms: dict[str, _Atom]
    base: dict[str, int]  # base unit code → its index in BASE_UNITS


@cache
def _table() -> _Table:
    source = files("iki_spec.units").joinpath("ucum-essence.xml").read_bytes()
    root = ET.fromstring(source)  # noqa: S314 (the bundled UCUM table, not untrusted input)
    prefixes: dict[str, Fraction] = {}
    prefix_names: dict[str, str] = {}
    for p in root.findall("u:prefix", _NS):
        code = p.get("Code", "")
        value = p.find("u:value", _NS)
        assert value is not None  # noqa: S101 - an invariant, not a check
        prefixes[code] = Fraction(value.get("value", ""))
        prefix_names[code] = _text(p, "name")
    atoms: dict[str, _Atom] = {}
    base: dict[str, int] = {}
    for b in root.findall("u:base-unit", _NS):
        code = b.get("Code", "")
        base[code] = BASE_UNITS.index(code)
        atoms[code] = _Atom(code, _text(b, "name"), True, False, None, "1", "1")
    for u in root.findall("u:unit", _NS):
        value = u.find("u:value", _NS)
        assert value is not None  # noqa: S101 - an invariant, not a check
        function = value.find("u:function", _NS)
        special = None
        number, expression = value.get("value", "1"), value.get("Unit", "1")
        if u.get("isSpecial") == "yes":
            assert function is not None  # noqa: S101 - an invariant, not a check
            special = function.get("name", "")
            number, expression = function.get("value", "1"), function.get("Unit", "1")
        code = u.get("Code", "")
        atoms[code] = _Atom(
            code=code,
            name=_text(u, "name"),
            metric=u.get("isMetric") == "yes",
            arbitrary=u.get("isArbitrary") == "yes",
            special=special,
            value=number,
            unit=expression,
        )
    return _Table(prefixes, prefix_names, atoms, base)


def _text(element: ET.Element, tag: str) -> str:
    found = element.find(f"u:{tag}", _NS)
    return "".join(found.itertext()).strip() if found is not None else ""


# --- Units -------------------------------------------------------------------------


@dataclass(frozen=True)
class Unit:
    """A parsed UCUM unit.

    A value in this unit times `factor` is the same quantity in base units,
    whose exponents are `dimension` (in `BASE_UNITS` order). Arbitrary units,
    such as international units (`[IU]`), only compare with themselves, so
    they are kept apart in `arbitrary`.
    """

    code: str
    factor: Fraction
    dimension: tuple[int, ...]
    arbitrary: tuple[tuple[str, int], ...] = ()
    _special: _Special | None = field(default=None, repr=False, compare=False)

    @property
    def is_special(self) -> bool:
        """True for units defined by a function, such as `Cel` or `[pH]`."""
        return self._special is not None

    @property
    def is_dimensionless(self) -> bool:
        return not any(self.dimension) and not self.arbitrary

    def is_commensurable(self, other: Unit) -> bool:
        """Whether values convert between the two units (both measure the same kind of thing)."""
        return self.dimension == other.dimension and self.arbitrary == other.arbitrary

    def to_base(self, value: float) -> float:
        """The value, given in this unit, as a magnitude in base units."""
        if self._special is None:
            return float(Fraction(value) * self.factor)
        s = self._special
        return _FUNCTIONS[s.function].from_special(value * float(s.scale)) * float(
            s.reference.factor
        )

    def from_base(self, magnitude: float) -> float:
        """A magnitude in base units, as a value in this unit."""
        if self._special is None:
            return float(Fraction(magnitude) / self.factor)
        s = self._special
        return _FUNCTIONS[s.function].to_special(magnitude / float(s.reference.factor)) / float(
            s.scale
        )


def convert(value: float, source: str | Unit, target: str | Unit) -> float:
    """Convert `value` between units. Raises UnitError if they measure different things."""
    a = source if isinstance(source, Unit) else parse(source)
    b = target if isinstance(target, Unit) else parse(target)
    if not a.is_commensurable(b):
        raise UnitError(
            f"`{a.code}` and `{b.code}` measure different things, so values can't convert.",
            code=b.code,
            position=0,
        )
    return b.from_base(a.to_base(value))


_ONE = Unit("1", Fraction(1), (0,) * len(BASE_UNITS))


def parse(code: str) -> Unit:
    """Parse a UCUM code, raising UnitError with a position and, where possible, a hint."""
    if not code:
        raise UnitError("A unit can't be empty. Use `1` for a plain number.", code="", position=0)
    if len(code) > MAX_LENGTH:
        raise UnitError(
            f"A unit can be at most {MAX_LENGTH} characters long.", code=code, position=0
        )
    return _parse(code)


@cache
def _parse(code: str) -> Unit:
    return _Parser(code).main()


# --- Parsing -------------------------------------------------------------------------
#
# UCUM's grammar:
#
#   main-term  := "/" term | term
#   term       := component (("." | "/") component)*      (left to right: a/b.c is (a/b)·c)
#   component  := annotatable annotation? | annotation | factor | "(" term ")"
#   annotatable:= simple-unit exponent?
#   simple-unit:= atom | prefix metric-atom
#
# Atoms may contain square brackets, inside which any character is literal
# (for example `[m/s2/Hz^(1/2)]` or `B[10.nV]`).

_OPERATORS = "./(){}"
_EXPONENT = re.compile(r"[+-]?[0-9]+$")


@dataclass
class _Term:
    factor: Fraction
    dimension: list[int]
    arbitrary: dict[str, int]
    special: _Special | None = None
    components: int = 1

    @classmethod
    def one(cls) -> _Term:
        return cls(Fraction(1), [0] * len(BASE_UNITS), {})

    def combine(self, other: _Term, sign: int) -> _Term:
        arbitrary = dict(self.arbitrary)
        for k, v in other.arbitrary.items():
            arbitrary[k] = arbitrary.get(k, 0) + sign * v
        return _Term(
            self.factor * other.factor**sign,
            [a + sign * b for a, b in zip(self.dimension, other.dimension, strict=True)],
            {k: v for k, v in arbitrary.items() if v},
            self.special or other.special,
            components=self.components + other.components,
        )

    def power(self, exponent: int) -> _Term:
        return _Term(
            self.factor**exponent,
            [d * exponent for d in self.dimension],
            {k: v * exponent for k, v in self.arbitrary.items()},
        )


class _Parser:
    def __init__(self, code: str) -> None:
        self.code = code
        self.pos = 0
        self.table = _table()

    def fail(self, message: str, position: int | None = None, hint: str | None = None) -> UnitError:
        return UnitError(
            message,
            code=self.code,
            position=self.pos if position is None else position,
            hint=hint,
        )

    def peek(self) -> str:
        return self.code[self.pos] if self.pos < len(self.code) else ""

    def main(self) -> Unit:
        bad = re.search(r"[^\x21-\x7e]", self.code)
        if bad:
            char = bad.group()
            hint = None
            if char in "µμ":
                hint = f"UCUM writes micro as `u`: `{self.code.replace(char, 'u')}`."
            elif char == "°":
                hint = _KNOWN_MISTAKES.get(self.code)
            elif char.isspace():
                hint = "Join units with `.` to multiply, for example `N.m`."
            raise self.fail(
                "Units use plain ASCII with no spaces."
                if char.isspace()
                else f"`{char}` isn't allowed in a UCUM unit.",
                bad.start(),
                hint or _hint_for(self.code),
            )
        if self.peek() == "/":
            self.pos += 1
            term = _Term.one().combine(self.term(), -1)
        else:
            term = self.term()
        if self.pos != len(self.code):
            raise self.fail(f"Unexpected `{self.peek()}`.")
        if term.special is not None and term.components > 1:
            raise self.fail(
                f"`{self.code}` combines a special unit with others. Special units such as "
                "`Cel`, `[pH]` or `dB` can't be multiplied, divided or raised to a power.",
                0,
                "For temperature differences or rates, use `K` (for example `K/min`).",
            )
        return Unit(
            code=self.code,
            factor=term.factor,
            dimension=tuple(term.dimension),
            arbitrary=tuple(sorted(term.arbitrary.items())),
            _special=term.special,
        )

    def term(self) -> _Term:
        result = self.component()
        while self.peek() in (".", "/") and self.peek():
            operator = self.peek()
            self.pos += 1
            result = result.combine(self.component(), 1 if operator == "." else -1)
        return result

    def component(self) -> _Term:
        start = self.pos
        char = self.peek()
        if not char:
            raise self.fail("A unit is missing after the operator." if start else "Empty unit.")
        if char == "(":
            self.pos += 1
            inner = self.term()
            if self.peek() != ")":
                raise self.fail("A `(` isn't closed.", start)
            self.pos += 1
            if self.peek() and self.peek() not in "./)":
                raise self.fail(
                    "UCUM can't raise a parenthesised term to a power.",
                    hint="Write the exponent on each unit, for example `m2/s2`.",
                )
            inner.components = max(inner.components, 2) if inner.special else inner.components
            return inner
        if char == "{":
            self.annotation()
            return _Term.one()
        if char in ")}":
            raise self.fail(f"Unexpected `{char}`.")
        if char in "./":
            raise self.fail("A unit is missing before the operator.")
        if char.isdigit():
            digits = self.read_digits()
            if digits == "10" and self.peek() in ("*", "^"):
                self.pos += 1
                return self.annotatable(f"10{self.code[self.pos - 1]}", start, self.exponent())
            if self.peek() and self.peek() not in "./)":
                raise self.fail(
                    "A number must be joined to a unit with `.`, as in "
                    f"`{digits}.{self.code[self.pos :]}`.",
                    start,
                )
            return _Term(Fraction(int(digits)), [0] * len(BASE_UNITS), {})
        symbol = self.read_symbol()
        exponent_match = _EXPONENT.search(symbol)
        exponent = 1
        if exponent_match and exponent_match.start() > 0:
            exponent = int(exponent_match.group())
            symbol = symbol[: exponent_match.start()]
        return self.annotatable(symbol, start, exponent)

    def annotatable(self, symbol: str, start: int, exponent: int) -> _Term:
        term = self.simple_unit(symbol, start)
        if self.peek() == "{":
            self.annotation()
        if exponent != 1:
            if term.special is not None:
                raise self.fail(
                    f"The special unit `{symbol}` can't be raised to a power.",
                    start,
                )
            term = term.power(exponent)
        return term

    def exponent(self) -> int:
        start = self.pos
        sign = ""
        if self.peek() in ("+", "-") and self.peek():
            sign = self.peek()
            self.pos += 1
        if not self.peek().isdigit():
            if sign:
                raise self.fail("A sign needs digits after it, as in `10*-3`.", start)
            return 1
        return int(sign + self.read_digits())

    def read_digits(self) -> str:
        start = self.pos
        while self.peek().isdigit():
            self.pos += 1
        return self.code[start : self.pos]

    def read_symbol(self) -> str:
        start = self.pos
        while self.peek() and self.peek() not in _OPERATORS:
            if self.peek() == "[":
                close = self.code.find("]", self.pos)
                if close < 0:
                    raise self.fail("A `[` isn't closed.")
                self.pos = close + 1
            elif self.peek() == "]":
                raise self.fail("Unexpected `]`.")
            else:
                self.pos += 1
        return self.code[start : self.pos]

    def annotation(self) -> None:
        start = self.pos
        close = self.code.find("}", self.pos)
        if close < 0:
            raise self.fail("A `{` isn't closed.", start)
        if "{" in self.code[start + 1 : close]:
            raise self.fail("Annotations can't contain `{`.", start)
        self.pos = close + 1

    def simple_unit(self, symbol: str, start: int) -> _Term:
        atoms = self.table.atoms
        if symbol in atoms:
            return _atom_term(symbol)
        for prefix, factor in self.table.prefixes.items():
            rest = symbol[len(prefix) :]
            if symbol.startswith(prefix) and rest in atoms:
                atom = atoms[rest]
                if not atom.metric:
                    raise self.fail(
                        f"`{rest}` ({atom.name}) can't take a prefix such as `{prefix}` "
                        f"({self.table.prefix_names[prefix]}).",
                        start,
                    )
                term = _atom_term(rest)
                if term.special is not None:
                    special = term.special
                    return _Term(
                        term.factor,
                        term.dimension,
                        term.arbitrary,
                        _Special(special.function, special.scale * factor, special.reference),
                    )
                return _Term(term.factor * factor, term.dimension, term.arbitrary)
        raise self.fail(f"`{symbol}` isn't a UCUM unit.", start, _hint_for(symbol))


@cache
def _atom_term(code: str) -> _Term:
    return _resolve(code, frozenset())


def _resolve(code: str, seen: frozenset[str]) -> _Term:
    table = _table()
    if code in seen:  # pragma: no cover (the table has no cycles; this guards a broken table)
        raise UnitError(
            f"The UCUM table defines `{code}` in terms of itself.", code=code, position=0
        )
    if code in table.base:
        dimension = [0] * len(BASE_UNITS)
        dimension[table.base[code]] = 1
        return _Term(Fraction(1), dimension, {})
    atom = table.atoms[code]
    if atom.arbitrary:
        return _Term(Fraction(1), [0] * len(BASE_UNITS), {code: 1})
    reference = _definition(atom.unit, seen | {code})
    term = _Term(Fraction(atom.value) * reference.factor, reference.dimension, reference.arbitrary)
    if atom.special is not None:
        if atom.special not in _FUNCTIONS:  # pragma: no cover (guards a newer table)
            raise UnitError(f"Iki can't convert `{code}` yet.", code=code, position=0)
        reference_unit = Unit(
            atom.unit, term.factor, tuple(term.dimension), tuple(sorted(term.arbitrary.items()))
        )
        return _Term(
            Fraction(1),
            term.dimension,
            term.arbitrary,
            _Special(atom.special, Fraction(1), reference_unit),
        )
    return term


def _definition(expression: str, seen: frozenset[str]) -> _Term:
    # Definitions in the table are UCUM terms themselves. Resolve them with a
    # parser whose atoms resolve recursively (guarding against cycles).
    parser = _DefinitionParser(expression, seen)
    if parser.peek() == "/":
        parser.pos += 1
        return _Term.one().combine(parser.term(), -1)
    return parser.term()


class _DefinitionParser(_Parser):
    def __init__(self, code: str, seen: frozenset[str]) -> None:
        super().__init__(code)
        self.seen = seen

    def simple_unit(self, symbol: str, start: int) -> _Term:
        atoms = self.table.atoms
        if symbol in atoms:
            return _resolve(symbol, self.seen)
        for prefix, factor in self.table.prefixes.items():
            rest = symbol[len(prefix) :]
            if symbol.startswith(prefix) and rest in atoms:
                term = _resolve(rest, self.seen)
                return _Term(term.factor * factor, term.dimension, term.arbitrary)
        raise self.fail(f"`{symbol}` isn't a UCUM unit.", start)  # pragma: no cover


# --- Hints ---------------------------------------------------------------------------

# Spellings people often use that UCUM writes differently.
_KNOWN_MISTAKES: dict[str, str] = {
    "Da": "`u` is the unified atomic mass unit (the dalton); `g/mol` is molar mass.",
    "kDa": "`ku` is a kilodalton (UCUM's unified atomic mass unit is `u`).",
    "°C": "UCUM writes degrees Celsius as `Cel`.",
    "degC": "UCUM writes degrees Celsius as `Cel`.",
    "°F": "UCUM writes degrees Fahrenheit as `[degF]`.",
    "degF": "UCUM writes degrees Fahrenheit as `[degF]`.",
    "hr": "UCUM writes hours as `h`.",
    "hrs": "UCUM writes hours as `h`.",
    "hour": "UCUM writes hours as `h`.",
    "sec": "UCUM writes seconds as `s`.",
    "secs": "UCUM writes seconds as `s`.",
    "mins": "UCUM writes minutes as `min`.",
    "day": "UCUM writes days as `d`.",
    "days": "UCUM writes days as `d`.",
    "yr": "UCUM writes years as `a`.",
    "year": "UCUM writes years as `a`.",
    "week": "UCUM writes weeks as `wk`.",
    "M": "UCUM writes molar as `mol/L`.",
    "mM": "UCUM writes millimolar as `mmol/L`.",
    "uM": "UCUM writes micromolar as `umol/L`.",
    "nM": "UCUM writes nanomolar as `nmol/L`.",
    "pM": "UCUM writes picomolar as `pmol/L`.",
    "cc": "UCUM writes cubic centimetres as `cm3` (or millilitres as `mL`).",
    "rpm": "UCUM writes revolutions per minute as `{rev}/min`.",
    "ppm": "UCUM writes parts per million as `[ppm]`.",
    "ppb": "UCUM writes parts per billion as `[ppb]`.",
    "IU": "UCUM writes international units as `[IU]`.",
    "pH": "UCUM writes pH as `[pH]`.",
    "psi": "UCUM writes pounds per square inch as `[psi]`.",
    "in": "UCUM writes inches as `[in_i]`.",
    "inch": "UCUM writes inches as `[in_i]`.",
    "ft": "UCUM writes feet as `[ft_i]`.",
    "lb": "UCUM writes pounds as `[lb_av]`.",
    "lbs": "UCUM writes pounds as `[lb_av]`.",
    "oz": "UCUM writes ounces as `[oz_av]`.",
    "mmHg": "UCUM writes millimetres of mercury as `mm[Hg]`.",
    "CFU": "UCUM writes colony-forming units as `[CFU]`.",
    "cfu": "UCUM writes colony-forming units as `[CFU]`.",
    "bp": "UCUM has no base pairs; count them with an annotation, `{bp}`.",
    "kb": "UCUM has no base pairs; count them with an annotation, `{bp}`.",
    "aa": "UCUM has no residues; count them with an annotation, `{aa}`.",
}


def _hint_for(symbol: str) -> str | None:
    if symbol in _KNOWN_MISTAKES:
        return _KNOWN_MISTAKES[symbol]
    candidates = list(_codes())
    same_letters = [c for c in candidates if c.lower() == symbol.lower()]
    if same_letters:
        return "UCUM codes are case-sensitive. Did you mean " + _or(same_letters) + "?"
    close = difflib.get_close_matches(symbol, candidates, n=3, cutoff=0.75)
    if close:
        return "Did you mean " + _or(close) + "?"
    return None


def _or(codes: list[str]) -> str:
    quoted = [f"`{c}`" for c in codes]
    return quoted[0] if len(quoted) == 1 else ", ".join(quoted[:-1]) + " or " + quoted[-1]


@cache
def _codes() -> tuple[str, ...]:
    table = _table()
    return tuple(_iter_codes(table))


def _iter_codes(table: _Table) -> Iterator[str]:
    yield from table.atoms
    common = ("m", "g", "L", "l", "mol", "s", "Pa", "J", "W", "V", "A", "Hz", "eq", "kat", "U")
    for prefix in ("k", "c", "m", "u", "n", "p", "f", "M", "G"):
        for atom in common:
            yield prefix + atom
