"""Motifs (ADR 0175): bases or residues in IUPAC codes, as a search finds them.

A motif is a field's alphabet's codes, as the IUPAC-IUB nomenclature gives them
(Cornish-Bowden, Nucleic Acids Res. 1985, 13:3021, for nucleotides; IUPAC-IUB JCBN, 1983,
for amino acids): ``GAATTY`` is GAATT, then C or T. Each code stands for a set of
residues, and a code in the motif matches a code in a record when every residue the
record's might be is one the motif's allows. So Y matches C, T and Y, and N matches
anything; but GAATTC doesn't match a record that reads GAANTC, whose fourth base isn't
known to be T. A search compiles a motif to a regular expression of these sets, which
Python and PostgreSQL read alike.

What a sequence is (its topology and strands, ADR 0138) says where else a motif is found:
on the other strand of a duplex, as its reverse complement, and across a circle's origin.
:mod:`iki_server.entities.search` applies those.
"""

from __future__ import annotations

from collections.abc import Mapping

__all__ = ["MEANINGS", "MotifError", "pattern", "plain", "read", "reverse_complement"]


def _nucleotides(t: str) -> dict[str, frozenset[str]]:
    bases = {
        "A": "A",
        "C": "C",
        "G": "G",
        "T": "T",
        "R": "AG",
        "Y": "CT",
        "S": "CG",
        "W": "AT",
        "K": "GT",
        "M": "AC",
        "B": "CGT",
        "D": "AGT",
        "H": "ACT",
        "V": "ACG",
        "N": "ACGT",
    }
    return {
        code.replace("T", t): frozenset(meaning.replace("T", t)) for code, meaning in bases.items()
    }


_AMINO_ACIDS = "ACDEFGHIKLMNPQRSTVWYUO"

#: The residues each code stands for, by sequence format.
MEANINGS: Mapping[str, Mapping[str, frozenset[str]]] = {
    "dna_sequence": _nucleotides("T"),
    "rna_sequence": _nucleotides("U"),
    "protein_sequence": {
        **{a: frozenset(a) for a in _AMINO_ACIDS},
        # Asx, Glx and Xle, which a measurement can't tell apart; X is any residue.
        "B": frozenset("DN"),
        "Z": frozenset("EQ"),
        "J": frozenset("IL"),
        "X": frozenset(_AMINO_ACIDS),
    },
}

_COMPLEMENTS = {
    "dna_sequence": str.maketrans("ACGTRYSWKMBDHVN", "TGCAYRSWMKVHDBN"),
    "rna_sequence": str.maketrans("ACGURYSWKMBDHVN", "UGCAYRSWMKVHDBN"),
}
_NAMES = {"dna_sequence": "DNA", "rna_sequence": "RNA", "protein_sequence": "protein"}


class MotifError(ValueError):
    """A motif that isn't one: what's wrong with it, as a search reports it."""


def read(format: str, text: str) -> str:
    """The motif ``text`` gives, in upper case, without the spaces and line breaks a pasted
    sequence has. Raises :class:`MotifError`."""
    meanings = MEANINGS[format]
    motif = "".join(text.split()).upper()
    what = "residues" if format == "protein_sequence" else "bases"
    if not motif:
        raise MotifError(f"must be some {what}, in IUPAC codes.")
    for letter in motif:
        if letter in meanings:
            continue
        if format == "dna_sequence" and letter == "U":
            raise MotifError("has U, and U isn't a DNA base: DNA has T.")
        if format == "rna_sequence" and letter == "T":
            raise MotifError("has T, and T isn't an RNA base: RNA has U.")
        raise MotifError(
            f"has `{letter}`, and `{letter}` isn't an IUPAC code for {_NAMES[format]}: give "
            f"{', '.join(sorted(meanings))}."
        )
    return motif


def reverse_complement(format: str, motif: str) -> str:
    """The motif as the other strand reads it, 5' to 3': R's complement is Y, and N's N."""
    if format not in _COMPLEMENTS:
        raise ValueError("a protein has no second strand")
    return motif.translate(_COMPLEMENTS[format])[::-1]


def _matched(format: str, code: str) -> str:
    """The codes in a record that ``code`` matches: those it certainly is."""
    meanings = MEANINGS[format]
    wanted = meanings[code]
    return "".join(sorted(c for c, m in meanings.items() if m <= wanted))


def plain(format: str, motif: str) -> bool:
    """Whether each of the motif's codes matches only itself, so it's found as text."""
    return all(_matched(format, code) == code for code in motif)


def pattern(format: str, motif: str) -> str:
    """The motif as a regular expression: each code as itself, or as the set it matches."""
    parts = []
    for code in motif:
        matched = _matched(format, code)
        parts.append(matched if len(matched) == 1 else f"[{matched}]")
    return "".join(parts)
