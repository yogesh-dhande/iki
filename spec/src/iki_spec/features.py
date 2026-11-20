"""A sequence viewer's features, placed along the same molecule spelled otherwise (ADR 0138).

A feature (``start`` and ``end`` counting from 1, ``strand``, and optionally ``parts``, as
the sequence viewer reads it: ADR 0085) given along a circular sequence from another
origin, or along its other strand, is placed along the entry's by
:class:`~iki_spec.identity.Placement`: every base it covers is the same base after.

It's written as INSDC's feature table writes it along the entry's sequence:

- Its parts keep the order they're read in (5' to 3' of the feature, so a reverse-strand
  join's last part first), and each part's strand flips with the strand it's read from.
- A part across the entry's origin is two, ``join(x..L,1..y)``; two parts of a join that
  were only split by the given sequence's origin are one again. An ``order``'s parts
  (``operator: order``: found in that order, nothing said of joining them) are never
  made one. A feature that's then one part from its start to its end lists none, unless
  an end is partial.
- A site between two bases (``5^6``) stays between the same two bases.
- A part's partial ends (``partial_start``, ``partial_end``: INSDC's ``<`` and ``>``, an
  end beyond the base it names) stay on the ends they were on, which from the other
  strand swap.
- A ``location`` qualifier holding an INSDC location (a GenBank import's own text) is
  written again along the entry's, its partial ends kept on the ends they were on. Text
  that isn't a location is left as it is.
- INSDC's other qualifiers that name a position do so in the record's own numbering too,
  so they are written again the same way: ``rpt_unit_range``, and the ``pos:`` of
  ``transl_except`` and ``anticodon``. Left as submitted, such a position would name
  another codon or another base once the sequence is read from the entry's origin. As
  with every other coordinate here, the submitted text is what placing the feature back
  along the record's own sequence gives.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, TypeGuard

from iki_spec.identity import Placement

__all__ = ["place_feature", "place_location"]

_FLIPPED = {"forward": "reverse", "reverse": "forward"}
_RANGE = re.compile(r"(<)?([0-9]+)\.\.(>)?([0-9]+)")
_BETWEEN = re.compile(r"([0-9]+)\^([0-9]+)")
_BASE = re.compile(r"([<>])?([0-9]+)")
#: INSDC qualifiers whose value is wholly a location in the record's own numbering.
_LOCATION_QUALIFIERS = ("location", "rpt_unit_range")
#: INSDC qualifiers that hold one inside a ``pos:``: ``/transl_except=(pos:213..215,
#: aa:Trp)`` names the codon whose translation is exceptional, and ``/anticodon=(pos:
#: 34..36,aa:Phe,seq:gaa)`` the anticodon's bases.
_POSITIONED_QUALIFIERS = ("transl_except", "anticodon")
_POS = re.compile(r"(?<![A-Za-z_])pos:")


@dataclass(frozen=True)
class _Span:
    """Bases ``start`` to ``end`` (from 1; an end before the start wraps the origin) on
    ``strand``, or the site between them; ``fuzzy_start`` and ``fuzzy_end`` mark a
    location's ``<`` and ``>``, and ``single`` a location's single base (``7``)."""

    start: int
    end: int
    strand: str | None = None
    between: bool = False
    fuzzy_start: bool = False
    fuzzy_end: bool = False
    single: bool = False

    @property
    def wraps(self) -> bool:
        return self.end < self.start and not self.between

    @property
    def reverse(self) -> bool:
        """Read from its end to its start: on the reverse strand."""
        return self.strand == "reverse"


def place_feature(
    feature: Any,  # noqa: ANN401 - JSON
    placement: Placement,
    length: int,
    *,
    circular: bool,
) -> Any:  # noqa: ANN401 - JSON
    """``feature``, given along a sequence of ``length`` that lies in the entry's as
    ``placement`` says, placed along the entry's (see above). What isn't a feature is
    left as it is."""
    if not isinstance(feature, dict) or length <= 0:
        return feature
    start, end = feature.get("start"), feature.get("end")
    if not _position(start, length) or not _position(end, length):
        return feature
    out = dict(feature)
    bounds = _moved(_Span(start, end), placement, length)
    out["start"], out["end"] = bounds.start, bounds.end
    strand = feature.get("strand")
    if placement.reverse and strand in _FLIPPED:
        out["strand"] = _FLIPPED[strand]
    given = _parts(feature, start, end, length)
    ordered = feature.get("operator") == "order"
    if given is not None:
        placed = _placed(given, placement, length, circular=circular, ordered=ordered)
        (first, *rest) = placed
        if (
            not rest
            and not first.between
            and not (first.fuzzy_start or first.fuzzy_end)
            and (first.start, first.end) == (bounds.start, bounds.end)
        ):
            out.pop("parts", None)
        else:
            out["parts"] = [_part(span) for span in placed]
    qualifiers = feature.get("qualifiers")
    if isinstance(qualifiers, dict):
        out["qualifiers"] = _place_qualifiers(qualifiers, placement, length, circular=circular)
    return out


def _pos_in(text: str) -> tuple[int, int] | None:
    """Where the location after a ``pos:`` begins and ends in a qualifier's value: up to
    the comma that ends it, counting the parentheses of a ``complement`` or a ``join``."""
    found = _POS.search(text)
    if found is None:
        return None
    start = found.end()
    depth = 0
    for at in range(start, len(text)):
        here = text[at]
        if here == "(":
            depth += 1
        elif here == ")":
            if depth == 0:
                return start, at
            depth -= 1
        elif here == "," and depth == 0:
            return start, at
    return start, len(text)


def _place_qualifiers(
    qualifiers: dict[str, Any], placement: Placement, length: int, *, circular: bool
) -> dict[str, Any]:
    """``qualifiers``, with every position they name written along the entry's sequence
    and the text as the record gave it kept beside it (see above)."""
    out = dict(qualifiers)
    for name in (*_LOCATION_QUALIFIERS, *_POSITIONED_QUALIFIERS):
        text = qualifiers.get(name)
        if not isinstance(text, str) or not text:
            continue
        if name in _LOCATION_QUALIFIERS:
            written = place_location(text, placement, length, circular=circular)
        else:
            where = _pos_in(text)
            if where is None:
                continue
            inner = place_location(text[where[0] : where[1]], placement, length, circular=circular)
            written = None if inner is None else text[: where[0]] + inner + text[where[1] :]
        # None means it isn't a position on this sequence -- another record's, or not a
        # location at all -- so there's nothing to move and the text stays as it is.
        if written is not None:
            out[name] = written
    return out


def place_location(text: str, placement: Placement, length: int, *, circular: bool) -> str | None:
    """An INSDC location (``complement(join(90..100,1..5))``) along a sequence of
    ``length``, written along the one ``placement`` places it in; None if it isn't one
    on this sequence: on another record, unreadable, or off its ends."""
    operators: set[str] = set()
    try:
        spans = _read(text, "forward", operators)
    except ValueError:
        return None
    if len(operators) > 1 or not all(_fits(s, length, circular=circular) for s in spans):
        return None
    operator = operators.pop() if operators else "join"
    placed = _placed(spans, placement, length, circular=circular, ordered=operator == "order")
    return _written(placed, operator)


def _position(value: object, length: int) -> TypeGuard[int]:
    """Whether ``value`` is a base of a sequence of ``length``, counting from 1."""
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= length


def _parts(feature: dict[str, Any], start: int, end: int, length: int) -> list[_Span] | None:
    """Its parts, or the one part from its start to its end if it lists none; None if
    they can't be read, which leaves them as they are."""
    parts = feature.get("parts")
    if parts is None:
        strand = feature.get("strand")
        return [_Span(start, end, strand if strand != "mixed" else None)]
    if not isinstance(parts, list) or not parts:
        return None
    out: list[_Span] = []
    for part in parts:
        if not isinstance(part, dict):
            return None
        first, last = part.get("start"), part.get("end")
        if not _position(first, length) or not _position(last, length):
            return None
        out.append(
            _Span(
                first,
                last,
                part.get("strand"),
                between=part.get("between") is True,
                fuzzy_start=part.get("partial_start") is True,
                fuzzy_end=part.get("partial_end") is True,
            )
        )
    return out


def _part(span: _Span) -> dict[str, Any]:
    out: dict[str, Any] = {"start": span.start, "end": span.end}
    if span.strand is not None:
        out["strand"] = span.strand
    if span.between:
        out["between"] = True
    if span.fuzzy_start:
        out["partial_start"] = True
    if span.fuzzy_end:
        out["partial_end"] = True
    return out


def _placed(
    spans: list[_Span],
    placement: Placement,
    length: int,
    *,
    circular: bool,
    ordered: bool = False,
) -> list[_Span]:
    """``spans``, in the order they're read, placed: a join's joined where only the given
    origin split them (an order's parts are each their own), moved, and split where the
    entry's origin does."""
    if circular and not ordered:
        spans = _joined(spans, length)
    moved = [_moved(span, placement, length) for span in spans]
    if not circular:
        return moved
    return [piece for span in moved for piece in _split(span, length)]


def _joined(spans: list[_Span], length: int) -> list[_Span]:
    """Each pair of parts read one after the other across the origin, ``x..L`` then
    ``1..y`` (``1..y`` then ``x..L`` on the reverse strand), as one part, ``x..y``."""
    out: list[_Span] = []
    for span in spans:
        last = out[-1] if out else None
        if last is not None and (joined := _join(last, span, length)) is not None:
            out[-1] = joined
        else:
            out.append(span)
    return out


def _join(one: _Span, then: _Span, length: int) -> _Span | None:
    if one.strand != then.strand or one.between or then.between or one.wraps or then.wraps:
        return None
    # The part read first, and the one read after it, in the sequence's own direction.
    low, high = (one, then) if one.reverse else (then, one)
    if high.end != length or low.start != 1 or high.fuzzy_end or low.fuzzy_start:
        return None
    if low.end >= high.start:
        return None  # they overlap: not one stretch round the circle
    return replace(high, end=low.end, fuzzy_end=low.fuzzy_end, single=False)


def _moved(span: _Span, placement: Placement, length: int) -> _Span:
    first, last = placement.position(span.start, length), placement.position(span.end, length)
    if not placement.reverse:
        return replace(span, start=first, end=last)
    # Read from the other strand, it runs the other way: its ends swap, and its strand flips.
    return replace(
        span,
        start=last,
        end=first,
        strand=_FLIPPED.get(span.strand or "", span.strand),
        fuzzy_start=span.fuzzy_end,
        fuzzy_end=span.fuzzy_start,
    )


def _split(span: _Span, length: int) -> list[_Span]:
    """A part across the origin as INSDC writes it, ``x..L`` and ``1..y``, in the order
    they're read."""
    if not span.wraps:
        return [span]
    high = replace(span, end=length, fuzzy_end=False, single=False)
    low = replace(span, start=1, fuzzy_start=False, single=False)
    return [low, high] if span.reverse else [high, low]


# --- INSDC locations ------------------------------------------------------------------------


def _read(text: str, strand: str, operators: set[str]) -> list[_Span]:
    """A location's spans in the order they're read, as the dna plugin's GenBank import
    reads it: ``complement``, ``join`` and ``order`` around ranges, single bases and sites."""
    text = text.strip()
    for wrapper in ("complement", "join", "order"):
        inner = _inside(text, wrapper)
        if inner is None:
            continue
        if wrapper == "complement":
            return list(reversed(_read(inner, _FLIPPED[strand], operators)))
        operators.add(wrapper)
        return [span for piece in _top_level(inner) for span in _read(piece, strand, operators)]
    if found := _RANGE.fullmatch(text):
        start, end = int(found[2]), int(found[4])
        return [_Span(start, end, strand, fuzzy_start=bool(found[1]), fuzzy_end=bool(found[3]))]
    if found := _BETWEEN.fullmatch(text):
        return [_Span(int(found[1]), int(found[2]), strand, between=True)]
    if found := _BASE.fullmatch(text):
        at = int(found[2])
        mark = found[1] or ""
        return [_Span(at, at, strand, fuzzy_start=mark == "<", fuzzy_end=mark == ">", single=True)]
    raise ValueError(text)


def _inside(text: str, wrapper: str) -> str | None:
    """What ``wrapper(…)`` wraps, if ``text`` is all one such."""
    if not (text.startswith(f"{wrapper}(") and text.endswith(")")):
        return None
    inner = text[len(wrapper) + 1 : -1]
    depth = 0
    for c in inner:
        depth += (c == "(") - (c == ")")
        if depth < 0:
            return None  # its parenthesis closes before the end: `complement(1..2),(…)`
    return inner if depth == 0 else None


def _top_level(text: str) -> list[str]:
    """``text`` split at the commas outside parentheses."""
    out, depth, here = [], 0, 0
    for at, c in enumerate(text):
        depth += (c == "(") - (c == ")")
        if c == "," and depth == 0:
            out.append(text[here:at])
            here = at + 1
    out.append(text[here:])
    return out


def _fits(span: _Span, length: int, *, circular: bool) -> bool:
    if not (1 <= span.start <= length and 1 <= span.end <= length):
        return False
    if span.between:
        return span.end == span.start + 1 or (circular and (span.start, span.end) == (length, 1))
    return span.start <= span.end or circular


def _written(spans: list[_Span], operator: str) -> str:
    """Spans, in the order they're read, as GenBank writes them: a reverse-strand join as
    ``complement(join(…))``, its parts in the sequence's order."""
    if len(spans) == 1:
        return _strand(spans[0])
    if all(span.reverse for span in spans):
        return f"complement({operator}({','.join(_bases(s) for s in reversed(spans))}))"
    return f"{operator}({','.join(_strand(s) for s in spans)})"


def _strand(span: _Span) -> str:
    return f"complement({_bases(span)})" if span.reverse else _bases(span)


def _bases(span: _Span) -> str:
    if span.between:
        return f"{span.start}^{span.end}"
    if span.single and span.start == span.end:
        return f"{'<' if span.fuzzy_start else '>' if span.fuzzy_end else ''}{span.start}"
    return (
        f"{'<' if span.fuzzy_start else ''}{span.start}..{'>' if span.fuzzy_end else ''}{span.end}"
    )
