"""Codon optimization of a coding sequence for a host, and the measures that say whether
it worked.

The protein is never changed. Every codon the optimizer writes codes for the amino acid
the template's codon coded for, read by the same NCBI translation table, and the run
translates both and refuses to register anything if the two proteins differ. What changes
is which of an amino acid's synonymous codons is used, and that is all a codon optimizer
may change.

What the numbers mean
--------------------

**Codon usage** is counted from a host's own coding sequences: ``data/codon_usage.json``
holds the counts, one table per host, each counted from the RefSeq coding sequences of
the assembly it names (``data/build_codon_usage.py`` builds it, ``data/README.md`` says
from what).

**RSCU**, the relative synonymous codon usage of a codon, is its count divided by the
mean count of the codons for the same amino acid: 1 where an amino acid's codons are used
equally, above 1 for a codon used more than its share (Sharp, Tuohy and Mosurski,
*Nucleic Acids Res* 14:5125, 1986).

**Relative adaptiveness**, w, is a codon's count divided by the count of the most-used
codon for the same amino acid, so the most-used codon of each family has w = 1 (Sharp and
Li, *Nucleic Acids Res* 15:1281, 1987). A codon absent from the table is counted as half
an occurrence, as Sharp and Li say to do, rather than given w = 0, which no gene using it
could have a CAI for.

**CAI**, the codon adaptation index, is the geometric mean of w over a gene's codons
(Sharp and Li, 1987). Codons whose amino acid has only one codon in the table being used
(ATG and TGG in the standard code) carry no information about codon choice and are left
out, as are stop codons and the initiator codon, which is read as methionine whatever it
is. CAI is 1 when every codon is its family's most-used codon, and nearer 0 the more rare
codons a gene has.

CAI depends on the table it is computed against. Sharp and Li computed w from a reference
set of very highly expressed genes; a table counted from a whole genome, as these are,
gives a different scale, so a CAI here is comparable with another CAI computed against
the same table and not with a published figure from a different reference set. Every
record says which table its CAI is against.

What is avoided, and why
------------------------

A gene that is only shifted to the most-used codons is often worse than one that is not:
it becomes repetitive, its GC content goes to an extreme, and it gains sequences that
stop transcription, splicing or translation where they should not happen. So the optimizer
is a constrained design, as published gene designers are (Hoover and Lubkowski, *Nucleic
Acids Res* 30:e43, 2002; Villalobos et al., *BMC Bioinformatics* 7:285, 2006): it picks
codons by the host's usage and then repairs what breaks a constraint, by substituting
another synonymous codon.

- **Rare codons.** A codon with w below the threshold isn't used at all, unless it is the
  only codon its amino acid has. Rare codons are decoded by rare tRNAs and cause pausing
  and truncation; this is what the classic AGA/AGG problem in *E. coli* is.
- **GC content**, overall and in a window. Both extremes make a gene hard to synthesise
  and hard to express; in *E. coli* the GC content of the first stretch of a gene is one
  of the strongest things that predicts how much protein is made (Kudla et al., *Science*
  324:255, 2009).
- **Runs of one base** (homopolymers) and **direct repeats**: both are where chemical
  synthesis and the polymerase slip, and where a construct recombines.
- **Hairpins**: an inverted repeat long enough to pair with itself. Structure in the mRNA,
  particularly near its 5' end, lowers how much protein is made (Kudla et al., 2009).
- **Restriction sites** the lab names: a gene with an internal BamHI site can't be cloned
  with BamHI, which is why every synthesis order says which sites to keep out.
- **Host motifs**, which differ by host kind and are described in ``HOST_MOTIFS``: for a
  bacterium, internal Shine-Dalgarno-like sequences, which pause the ribosome inside a
  gene (Li, Oh and Weissman, *Nature* 484:538, 2012); for a eukaryote, the strong splice
  donor and acceptor consensus sequences (Shapiro and Senapathy, *Nucleic Acids Res*
  15:7155, 1987) and the AATAAA polyadenylation signal (Proudfoot and Brownlee, *Nature*
  263:211, 1976), each of which would be used inside a coding sequence that carries it.
- **Anything else the lab names**, as IUPAC motifs, read on both strands.

A constraint the optimizer can't satisfy is reported, not hidden: a stretch of protein
whose every synonymous choice leaves a run of six A's has no answer, and the record says
where. Nothing is registered that doesn't code for the same protein; everything else is
best effort, and said to be.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from Bio import Restriction
from Bio.Data import CodonTable

#: The 64 codons in NCBI's order, as the tables are written.
CODONS = [a + b + c for a in "TCAG" for b in "TCAG" for c in "TCAG"]
COMPLEMENT = str.maketrans("ACGTRYKMBVDHNSW", "TGCAYRMKVBHDNSW")
#: What each IUPAC nucleotide code stands for (IUBMB, 1985), for reading motifs.
IUPAC = {
    "A": "A", "C": "C", "G": "G", "T": "T", "U": "T",
    "R": "AG", "Y": "CT", "S": "CG", "W": "AT", "K": "GT", "M": "AC",
    "B": "CGT", "D": "AGT", "H": "ACT", "V": "ACG", "N": "ACGT",
}  # fmt: skip
#: Half an occurrence for a codon the table never shows, as Sharp and Li (1987) say to
#: use so that a gene carrying it still has a CAI.
ABSENT = 0.5
#: Tables whose stop codons are read as sense codons in some places (NCBI's notes to
#: tables 27, 28 and 31), which the bases alone can't settle, so a gene can't be read by
#: them; `find_orfs` refuses them for the same reason (ADR 0172).
CONTEXT_DEPENDENT = frozenset({27, 28, 31})
#: How many repair passes the optimizer makes before it reports what is left.
PASSES = 12
#: The longest coding region a run optimizes: a gene, not a genome.
LONGEST = 30_000


class Refused(ValueError):  # noqa: N818 - reads as "the run refuses"
    """Why the sequence can't be optimized as asked."""


def reverse_complement(bases: str) -> str:
    return bases.translate(COMPLEMENT)[::-1]


def motif_pattern(motif: str) -> re.Pattern[str]:
    """A motif in IUPAC codes as a pattern, read on one strand."""
    out = []
    for code in motif.upper():
        bases = IUPAC.get(code)
        if not bases:
            raise Refused(
                f"{motif} isn't a nucleotide motif: {code} is not an IUPAC code. Write it "
                "in A, C, G, T and the ambiguity codes (R, Y, N and the rest)."
            )
        out.append(bases[0] if len(bases) == 1 else f"[{bases}]")
    return re.compile("".join(out))


def genetic_code(number: int) -> Any:  # noqa: ANN401 - Biopython's CodonTable
    """One of NCBI's translation tables, as Biopython holds them."""
    if number in CONTEXT_DEPENDENT:
        raise Refused(
            f"Translation table {number}'s stop codons are read as sense codons in some "
            "places, and the bases alone don't say which, so a coding sequence can't be "
            "optimized under it."
        )
    found = CodonTable.unambiguous_dna_by_id.get(number)
    if found is None:
        offered = ", ".join(str(n) for n in sorted(CodonTable.unambiguous_dna_by_id))
        raise Refused(f"There's no NCBI translation table {number}: choose one of {offered}.")
    return found


def table_name(number: int) -> str:
    table = genetic_code(number)
    names = [name for name in table.names if name and not name.startswith("SGC")]
    return ", ".join(names)


def residue_of(codon: str, table: Any) -> str:  # noqa: ANN401
    """What a codon reads as: its amino acid, or ``*`` for a stop."""
    if codon in table.stop_codons:
        return "*"
    found: str | None = table.forward_table.get(codon)
    if found is None:
        raise Refused(
            f"{codon} isn't a codon of A, C, G and T, so the coding sequence can't be "
            "read: optimization needs an unambiguous reading frame."
        )
    return found


def translate(bases: str, table: Any) -> str:  # noqa: ANN401
    """A coding sequence's protein, one letter per codon, a stop written ``*``."""
    if len(bases) % 3:
        raise Refused(
            f"The coding region is {len(bases)} bases, which isn't a whole number of "
            "codons: give the region of the sequence that is the coding sequence."
        )
    return "".join(residue_of(bases[at : at + 3], table) for at in range(0, len(bases), 3))


# --- the host's codon usage ------------------------------------------------------------


@dataclass(frozen=True)
class Usage:
    """One host's codon usage: the counts, and what they were counted from."""

    key: str
    name: str
    strain: str
    common: str
    assembly: str
    assembly_name: str
    genetic_code: int
    coding_sequences: int
    codons: int
    counts: dict[str, int]

    @property
    def described(self) -> str:
        """The host as a record names it: its name, its strain, and the assembly its
        codon usage was counted from."""
        named = f"{self.name} {self.strain}".strip()
        return f"{named} ({self.assembly} {self.assembly_name}, {self.coding_sequences:,} CDS)"

    def families(self, table: Any) -> dict[str, list[str]]:  # noqa: ANN401
        """The codons for each amino acid under ``table``, stops under ``*``."""
        out: dict[str, list[str]] = {}
        for codon in CODONS:
            out.setdefault(residue_of(codon, table), []).append(codon)
        return out

    def rscu(self, table: Any) -> dict[str, float]:  # noqa: ANN401
        """Each codon's relative synonymous codon usage: its count over the mean count of
        the codons for the same amino acid (Sharp, Tuohy and Mosurski, 1986)."""
        out: dict[str, float] = {}
        for codons in self.families(table).values():
            counts = [max(self.counts.get(codon, 0), 0) or ABSENT for codon in codons]
            mean = sum(counts) / len(counts)
            for codon, count in zip(codons, counts, strict=True):
                out[codon] = count / mean
        return out

    def weights(self, table: Any) -> dict[str, float]:  # noqa: ANN401
        """Each codon's relative adaptiveness w: its count over the count of the most-used
        codon for the same amino acid (Sharp and Li, 1987)."""
        out: dict[str, float] = {}
        for codons in self.families(table).values():
            counts = {codon: max(self.counts.get(codon, 0), 0) or ABSENT for codon in codons}
            most = max(counts.values())
            for codon, count in counts.items():
                out[codon] = count / most
        return out

    def fractions(self, table: Any) -> dict[str, float]:  # noqa: ANN401
        """Each codon's share of its amino acid's codons in this host."""
        out: dict[str, float] = {}
        for codons in self.families(table).values():
            counts = {codon: max(self.counts.get(codon, 0), 0) or ABSENT for codon in codons}
            total = sum(counts.values())
            for codon, count in counts.items():
                out[codon] = count / total
        return out


def read_usage(data: dict[str, Any]) -> dict[str, Usage]:
    """The usage tables of ``codon_usage.json``, by their key."""
    out = {}
    for organism in data["organisms"]:
        counts = {codon: int(organism["counts"][codon]) for codon in CODONS}
        out[organism["key"]] = Usage(
            key=organism["key"],
            name=organism["name"],
            strain=organism["strain"],
            common=organism["common"],
            assembly=organism["assembly"],
            assembly_name=organism["assembly_name"],
            genetic_code=int(organism["genetic_code"]),
            coding_sequences=int(organism["coding_sequences"]),
            codons=int(organism["codons"]),
            counts=counts,
        )
    return out


def cai(bases: str, weights: dict[str, float], table: Any) -> float | None:  # noqa: ANN401
    """A coding sequence's codon adaptation index against ``weights`` (Sharp and Li,
    1987): the geometric mean of w over its codons, leaving out its stop codons and the
    codons whose amino acid has only one codon in the table -- ATG and TGG in the
    standard code, which Sharp and Li leave out by name, since their w is 1 whatever the
    gene is. None when nothing is left to take a mean of.

    This is what Biopython's ``Bio.SeqUtils.CodonAdaptationIndex`` computes for a
    standard-code gene with its stop codon left off, and the tests check that it is.
    """
    logs = []
    for at in range(0, len(bases) - len(bases) % 3, 3):
        codon = bases[at : at + 3]
        residue = residue_of(codon, table)
        if residue == "*":
            continue
        # A single-codon family says nothing about codon choice: Sharp and Li leave out
        # methionine and tryptophan for exactly this reason.
        if len([c for c in CODONS if residue_of(c, table) == residue]) == 1:
            continue
        logs.append(math.log(weights[codon]))
    if not logs:
        return None
    return math.exp(sum(logs) / len(logs))


def gc_content(bases: str) -> float | None:
    """The share of G and C, as a percentage, or None for no bases."""
    if not bases:
        return None
    return 100 * sum(base in "GC" for base in bases.upper()) / len(bases)


# --- what a design avoids -------------------------------------------------------------

#: The motifs a host kind is designed away from, each with why it is avoided. A bacterial
#: host's gene is kept free of Shine-Dalgarno-like sequences, which pause the ribosome
#: inside a coding sequence (Li, Oh and Weissman, Nature 484:538, 2012). A eukaryotic
#: host's is kept free of the strong splice donor and acceptor consensus sequences
#: (Shapiro and Senapathy, Nucleic Acids Res 15:7155, 1987), which a coding sequence
#: carrying them can be spliced at, and of the AATAAA polyadenylation signal (Proudfoot
#: and Brownlee, Nature 263:211, 1976), which would end the message inside the gene.
HOST_MOTIFS: dict[str, list[tuple[str, str]]] = {
    "bacterial": [
        ("AGGAGG", "a Shine-Dalgarno-like sequence, which pauses the ribosome"),
    ],
    "eukaryotic": [
        ("MAGGTRAGT", "the 5' splice donor consensus"),
        ("AATAAA", "the polyadenylation signal"),
    ],
}
#: Which motif set a host gets: the bacterial one for a host read by NCBI's table 11.
BACTERIAL_CODE = 11


@dataclass(frozen=True)
class Constraints:
    """What a design avoids. Lengths of 0 turn a constraint off."""

    #: Relative adaptiveness below which a codon isn't used, unless it's the only one.
    rare_codon_threshold: float = 0.1
    gc_min: float = 30.0
    gc_max: float = 70.0
    #: The window the GC share is also kept within, and the share it's kept within there.
    gc_window: int = 50
    gc_window_min: float = 25.0
    gc_window_max: float = 75.0
    #: The longest run of one base allowed, and the longest repeated stretch.
    longest_homopolymer: int = 6
    longest_repeat: int = 15
    #: The longest stem of an inverted repeat allowed, and the loop it may close.
    longest_hairpin_stem: int = 10
    hairpin_loop: int = 30
    #: Restriction enzymes whose recognition sites are kept out, as REBASE names them.
    avoid_enzymes: tuple[str, ...] = ()
    #: Motifs in IUPAC codes kept out, read on both strands.
    avoid_motifs: tuple[str, ...] = ()
    #: Whether the host kind's own motifs are kept out too.
    avoid_host_motifs: bool = True


@dataclass(frozen=True)
class Violation:
    """One constraint a stretch of the sequence breaks, where it is (from 1, inclusive)
    and what it is, in words.

    ``severity`` is how badly it is broken, which lets a repair see that it has got
    closer: a GC share's distance outside the band it must be in, and 1 for a motif, a
    repeat or a hairpin, which are broken or not.
    """

    kind: str
    start: int
    end: int
    detail: str
    severity: float = 1.0
    #: Which way a GC share has to move to meet the constraint: 1 for more G and C, -1
    #: for less, 0 where the constraint isn't about GC at all.
    needs: int = 0

    def value(self) -> dict[str, Any]:
        return {"kind": self.kind, "start": self.start, "end": self.end, "detail": self.detail}


def enzyme_site(name: str) -> str:
    """A restriction enzyme's recognition site, from Biopython's REBASE-derived set."""
    found = getattr(Restriction, name, None)
    site = getattr(found, "site", None)
    if not isinstance(site, str):
        raise Refused(
            f"No restriction enzyme is called {name}. Write it as REBASE does, such as "
            "EcoRI, BamHI, BsaI or BsmBI."
        )
    return site.upper()


def motifs_of(constraints: Constraints, host_kind: str) -> list[tuple[str, str, str]]:
    """Every motif a design keeps out: its IUPAC codes, what it is in words, and the kind
    of constraint it is."""
    out: list[tuple[str, str, str]] = [
        (enzyme_site(name), f"the {name} site", "restriction_site")
        for name in constraints.avoid_enzymes
    ]
    out += [
        (motif.upper(), f"the motif {motif.upper()}", "motif") for motif in constraints.avoid_motifs
    ]
    if constraints.avoid_host_motifs:
        for motif, why in HOST_MOTIFS.get(host_kind, []):
            out.append((motif, why, "host_motif"))
    return out


def motif_hits(bases: str, motifs: Sequence[tuple[str, str, str]]) -> list[Violation]:
    """Where any motif lies, on either strand: a site is a site whichever strand it reads
    on, as an enzyme cuts a duplex."""
    out: list[Violation] = []
    other = reverse_complement(bases)
    length = len(bases)
    for motif, why, kind in motifs:
        pattern = motif_pattern(motif)
        out += [
            Violation(kind, at + 1, at + len(motif), f"{why}, on the given strand")
            for at in _at_every(pattern, bases)
        ]
        if motif.upper() == reverse_complement(motif.upper()):
            continue  # a palindrome reads the same both ways: it's already reported
        out += [
            Violation(
                kind, length - at - len(motif) + 1, length - at, f"{why}, on the other strand"
            )
            for at in _at_every(pattern, other)
        ]
    return out


def _at_every(pattern: re.Pattern[str], bases: str) -> list[int]:
    """Every place the pattern matches, overlapping ones included: two of an enzyme's
    sites that share bases are two sites, and a motif inside a run of itself occurs as
    many times as it fits."""
    out = []
    at = 0
    while True:
        found = pattern.search(bases, at)
        if found is None:
            return out
        out.append(found.start())
        at = found.start() + 1


def homopolymers(bases: str, longest: int) -> list[Violation]:
    """Runs of one base longer than ``longest``."""
    if longest <= 0:
        return []
    out = []
    for found in re.finditer(r"(A+|C+|G+|T+)", bases):
        run = found.group(1)
        if len(run) > longest:
            out.append(
                Violation(
                    "homopolymer",
                    found.start() + 1,
                    found.end(),
                    f"{len(run)} {run[0]}'s in a row, more than {longest}",
                )
            )
    return out


def repeats(bases: str, longest: int) -> list[Violation]:
    """Stretches of ``longest`` bases or more that occur more than once. Each later
    occurrence is reported, so repairing it leaves the first."""
    if longest <= 0 or len(bases) <= longest:
        return []
    seen: dict[str, int] = {}
    out = []
    for at in range(len(bases) - longest + 1):
        word = bases[at : at + longest]
        first = seen.get(word)
        if first is None:
            seen[word] = at
            continue
        out.append(
            Violation(
                "repeat",
                at + 1,
                at + longest,
                f"{longest} bases repeated from {first + 1}",
            )
        )
    return out


def hairpins(bases: str, stem: int, loop: int) -> list[Violation]:
    """Inverted repeats whose two arms are ``stem`` bases or more, closing a loop of up to
    ``loop`` bases: the sequence can fold back on itself there."""
    if stem <= 0:
        return []
    out = []
    length = len(bases)
    for at in range(length - 2 * stem + 1):
        arm = bases[at : at + stem]
        wanted = reverse_complement(arm)
        start = at + stem
        for other in range(start, min(length - stem, at + stem + loop) + 1):
            if bases[other : other + stem] == wanted:
                out.append(
                    Violation(
                        "hairpin",
                        at + 1,
                        other + stem,
                        f"{stem} bases pair with {other + 1}..{other + stem}, "
                        f"a loop of {other - at - stem}",
                    )
                )
                break
    return out


def gc_breaks(bases: str, constraints: Constraints) -> list[Violation]:
    """Where the GC share is outside what's allowed: over the whole sequence, and in any
    window of it."""
    out = []
    whole = gc_content(bases)
    if whole is not None and not constraints.gc_min <= whole <= constraints.gc_max:
        out.append(
            Violation(
                "gc_content",
                1,
                len(bases),
                f"{whole:.1f}% GC overall, outside {constraints.gc_min:g}-{constraints.gc_max:g}%",
                max(constraints.gc_min - whole, whole - constraints.gc_max),
                1 if whole < constraints.gc_min else -1,
            )
        )
    window = constraints.gc_window
    if window <= 0 or len(bases) < window:
        return out
    # Every window of `window` bases, how far outside the band its GC share is. Windows
    # that are out of band and next to one another are one stretch, reported once, with
    # how far out all of them are together: a repair can then see that it has improved
    # the stretch even while another window within it is still the worst of them.
    away = [0.0] * (len(bases) - window + 1)
    for at in range(len(bases) - window + 1):
        share = gc_content(bases[at : at + window]) or 0.0
        away[at] = max(constraints.gc_window_min - share, share - constraints.gc_window_max, 0.0)
    at = 0
    while at < len(away):
        if away[at] <= 0:
            at += 1
            continue
        end = at
        while end + 1 < len(away) and away[end + 1] > 0:
            end += 1
        worst = max(range(at, end + 1), key=lambda i: away[i])
        share = gc_content(bases[worst : worst + window]) or 0.0
        out.append(
            Violation(
                "gc_window",
                at + 1,
                end + window,
                f"{share:.1f}% GC over bases {worst + 1} to {worst + window}, outside "
                f"{constraints.gc_window_min:g}-{constraints.gc_window_max:g}%",
                round(sum(away[at : end + 1]), 6),
                1 if share < constraints.gc_window_min else -1,
            )
        )
        at = end + 1
    return out


def violations(
    bases: str, constraints: Constraints, motifs: Sequence[tuple[str, str, str]]
) -> list[Violation]:
    """Every constraint the bases break, in order along them."""
    found = [
        *motif_hits(bases, motifs),
        *homopolymers(bases, constraints.longest_homopolymer),
        *repeats(bases, constraints.longest_repeat),
        *hairpins(bases, constraints.longest_hairpin_stem, constraints.hairpin_loop),
        *gc_breaks(bases, constraints),
    ]
    return sorted(found, key=lambda v: (v.start, v.end, v.kind))


# --- choosing the codons --------------------------------------------------------------

#: How the codons are chosen before anything is repaired.
STRATEGIES = ("highest_usage", "usage_matched", "harmonized")


def allowed_codons(
    residue: str,
    table: Any,  # noqa: ANN401 - Biopython's CodonTable
    weights: dict[str, float],
    threshold: float,
) -> list[str]:
    """The codons a residue may be written with, the most-used first: those whose relative
    adaptiveness is at least ``threshold``, or the most-used alone if none is."""
    family = [codon for codon in CODONS if residue_of(codon, table) == residue]
    family.sort(key=lambda codon: (-weights[codon], codon))
    kept = [codon for codon in family if weights[codon] >= threshold]
    return kept or family[:1]


def _largest_remainder(total: int, shares: list[float]) -> list[int]:
    """``total`` split in the proportions ``shares``, by the largest-remainder method: no
    randomness, so the same design always gives the same codons."""
    exact = [total * share for share in shares]
    out = [math.floor(value) for value in exact]
    left = total - sum(out)
    order = sorted(
        range(len(shares)),
        key=lambda i: (-(exact[i] - math.floor(exact[i])), -shares[i], i),
    )
    for i in order[:left]:
        out[i] += 1
    return out


def _spread(positions: list[int], codons: list[str], counts: list[int]) -> dict[int, str]:
    """``counts`` of each codon dealt round the positions in turn, so synonyms alternate
    instead of running together: the first position gets the most-used codon, the next the
    next one that is still owed, and so on."""
    left = dict(zip(codons, counts, strict=True))
    out: dict[int, str] = {}
    at = 0
    for position in positions:
        for _ in range(len(codons)):
            codon = codons[at % len(codons)]
            at += 1
            if left[codon]:
                left[codon] -= 1
                out[position] = codon
                break
    return out


def initial_codons(
    protein: str,
    table: Any,  # noqa: ANN401
    host: dict[str, float],
    allowed: dict[int, list[str]],
    strategy: str,
    source_fractions: dict[str, float] | None,
    source_codons: list[str] | None,
) -> list[str]:
    """One codon per residue, before anything is repaired.

    ``highest_usage`` takes each amino acid's most-used codon. ``usage_matched`` uses each
    amino acid's codons in the proportions the host uses them, dealt round the positions
    so synonyms alternate: the gene keeps the host's codon distribution rather than being
    a string of one codon, which is what makes a most-used-codon gene repetitive.
    ``harmonized`` keeps each codon's share within its family as near as it can to the
    share the template's own codon has in the source organism, so that where the template
    paused the new gene pauses too (Angov et al., *PLoS ONE* 3:e2189, 2008).
    """
    if strategy == "highest_usage":
        return [allowed[i][0] for i in range(len(protein))]
    if strategy == "harmonized":
        if source_fractions is None or source_codons is None:
            raise Refused(
                "A harmonized design needs the organism the template's gene is from, so "
                "that each codon's share there can be matched in the host."
            )
        out = []
        for i, codon in enumerate(source_codons):
            wanted = source_fractions[codon]
            out.append(min(allowed[i], key=lambda c: (abs(host[c] - wanted), -host[c], c)))
        return out
    by_residue: dict[str, list[int]] = {}
    for i, residue in enumerate(protein):
        by_residue.setdefault(residue, []).append(i)
    chosen: dict[int, str] = {}
    for positions in by_residue.values():
        codons = allowed[positions[0]]
        shares = [host[codon] for codon in codons]
        total = sum(shares)
        counts = _largest_remainder(len(positions), [share / total for share in shares])
        chosen.update(_spread(positions, codons, counts))
    return [chosen[i] for i in range(len(protein))]


@dataclass
class Design:
    """What a run worked out: the optimized bases, how they compare with the template's,
    and every constraint still broken."""

    bases: str
    protein: str
    source_bases: str
    strategy: str
    codons_changed: int
    source_cai: float | None
    optimized_cai: float | None
    source_gc: float | None
    optimized_gc: float | None
    unmet: list[Violation] = field(default_factory=list)
    changes: list[dict[str, Any]] = field(default_factory=list)

    @property
    def codons(self) -> int:
        return len(self.source_bases) // 3


#: How many codons a repair tries for one violation, and how many substitutions it tries
#: in all: a bound, so a long gene with a GC share out of band can't take all day.
MOST_POSITIONS = 24
MOST_TRIALS = 20_000


def _codon_positions(violation: Violation, length: int) -> list[int]:
    """The codons a violation lies across, by their index in the protein."""
    first = max((violation.start - 1) // 3, 0)
    last = min((violation.end - 1) // 3, length - 1)
    return list(range(first, last + 1))


def _candidates(
    violation: Violation,
    chosen: list[str],
    allowed: dict[int, list[str]],
    kept: set[int],
    needs_gc: int,
) -> list[int]:
    """The codons a repair tries for this violation, at most ``MOST_POSITIONS`` of them.

    A motif, a repeat or a hairpin lies across a few codons, and every one of them is
    tried. A GC share can lie across a whole gene, so there the codons tried are the ones
    whose synonyms can move the GC share furthest the way it has to go: a substitution
    anywhere else cannot help.
    """
    within = [at for at in _codon_positions(violation, len(chosen)) if at not in kept]
    if not needs_gc or len(within) <= MOST_POSITIONS:
        return within[:MOST_POSITIONS]

    def shift(at: int) -> int:
        here = sum(base in "GC" for base in chosen[at])
        moves = [sum(base in "GC" for base in codon) - here for codon in allowed[at]]
        return max((needs_gc * move for move in moves), default=0)

    within.sort(key=lambda at: (-shift(at), at))
    return [at for at in within if shift(at) > 0][:MOST_POSITIONS]


def optimize(
    source: str,
    usage: Usage,
    *,
    table_number: int = 1,
    strategy: str = "highest_usage",
    constraints: Constraints | None = None,
    source_usage: Usage | None = None,
) -> Design:
    """``source``'s codons chosen again for ``usage``'s host, keeping the protein exactly.

    The first codon and a stop codon at the end are kept as the template has them: a
    construct's start and its stop are what the lab put there, and an optimizer has no
    business changing either. Everything between is chosen by ``strategy`` and then
    repaired, codon by codon, until no constraint is broken or no substitution helps.
    """
    constraints = constraints or Constraints()
    if strategy not in STRATEGIES:
        raise Refused(f"There's no {strategy} strategy: choose one of {', '.join(STRATEGIES)}.")
    bases = source.upper().replace("U", "T")
    if len(bases) > LONGEST:
        raise Refused(
            f"The coding region is {len(bases):,} bases, more than the {LONGEST:,} a run "
            "optimizes: optimize a gene at a time."
        )
    table = genetic_code(table_number)
    protein = translate(bases, table)
    if len(protein) < 2:
        raise Refused("A coding sequence to optimize has at least two codons.")
    inner = protein.find("*")
    if 0 <= inner < len(protein) - 1:
        raise Refused(
            f"The coding region has a stop codon at codon {inner + 1} of {len(protein)}, "
            "so it isn't one coding sequence read in one frame: give the region that is."
        )
    weights = usage.weights(table)
    fractions = usage.fractions(table)
    host_kind = "bacterial" if usage.genetic_code == BACTERIAL_CODE else "eukaryotic"
    motifs = motifs_of(constraints, host_kind)
    source_codons = [bases[at : at + 3] for at in range(0, len(bases), 3)]
    allowed = {
        i: allowed_codons(residue, table, weights, constraints.rare_codon_threshold)
        for i, residue in enumerate(protein)
    }
    chosen = initial_codons(
        protein,
        table,
        fractions,
        allowed,
        strategy,
        source_usage.fractions(table) if source_usage else None,
        source_codons if source_usage else None,
    )
    # The start and the stop are the template's.
    chosen[0] = source_codons[0]
    if protein.endswith("*"):
        chosen[-1] = source_codons[-1]
    kept = {0} | ({len(protein) - 1} if protein.endswith("*") else set())
    chosen, unmet = _repair(chosen, allowed, kept, weights, constraints, motifs)
    optimized = "".join(chosen)
    if translate(optimized, table) != protein:
        raise Refused(
            "The optimized codons don't code for the template's protein, so nothing is "
            "registered. This is a defect in the optimizer, not in the sequence."
        )
    changes = [
        {
            "codon": i + 1,
            "residue": protein[i],
            "from": source_codons[i],
            "to": chosen[i],
            "from_weight": round(weights[source_codons[i]], 4),
            "to_weight": round(weights[chosen[i]], 4),
        }
        for i in range(len(protein))
        if chosen[i] != source_codons[i]
    ]
    return Design(
        bases=optimized,
        protein=protein,
        source_bases=bases,
        strategy=strategy,
        codons_changed=len(changes),
        source_cai=cai(bases, weights, table),
        optimized_cai=cai(optimized, weights, table),
        source_gc=gc_content(bases),
        optimized_gc=gc_content(optimized),
        unmet=unmet,
        changes=changes,
    )


def _repair(
    chosen: list[str],
    allowed: dict[int, list[str]],
    kept: set[int],
    weights: dict[str, float],
    constraints: Constraints,
    motifs: Sequence[tuple[str, str, str]],
) -> tuple[list[str], list[Violation]]:
    """Substitutes synonymous codons until nothing is broken or nothing helps.

    One violation at a time, each codon it lies across is tried with each of that
    residue's other allowed codons, and the substitution that leaves the fewest
    violations is taken; where several leave the same number, the codon the host uses
    more is taken, so repairing a constraint costs as little adaptation as it can. A
    violation no substitution helps is left and reported.
    """
    current = list(chosen)
    stuck: set[tuple[str, int, int]] = set()
    trials = 0
    # One substitution per step, and at most this many steps: each step either repairs
    # something or gives that violation up, so it always ends.
    for _ in range(PASSES * len(current) + PASSES):
        bases = "".join(current)
        found = violations(bases, constraints, motifs)
        open_ones = [v for v in found if (v.kind, v.start, v.end) not in stuck]
        if not open_ones:
            return current, found
        violation = open_ones[0]
        before = _badness(found)
        best: tuple[tuple[int, float], float, int, str] | None = None
        for at in _candidates(violation, current, allowed, kept, violation.needs):
            for codon in allowed[at]:
                if codon == current[at]:
                    continue
                trials += 1
                trial = list(current)
                trial[at] = codon
                after = violations("".join(trial), constraints, motifs)
                score = (_badness(after), -weights[codon], at, codon)
                if best is None or score < best:
                    best = score
            if trials > MOST_TRIALS:
                break
        if best is None or best[0] >= before:
            stuck.add((violation.kind, violation.start, violation.end))
            continue
        current[best[2]] = best[3]
        if trials > MOST_TRIALS:
            break
    return current, violations("".join(current), constraints, motifs)


def _badness(found: Sequence[Violation]) -> tuple[int, float]:
    """How broken a sequence is: how many constraints it breaks, and by how much. A
    repair compares this before and after a substitution, so moving a GC share towards
    the band it must be in counts as progress even before it gets there."""
    return (len(found), round(sum(violation.severity for violation in found), 6))


def described(
    usage: Usage,
    strategy: str,
    table_number: int,
    constraints: Constraints,
    source_usage: Usage | None,
) -> str:
    """What a design was optimized with, in words, for the record that keeps it."""
    parts = [
        f"{strategy.replace('_', ' ')} codons for {usage.described}",
        f"translation table {table_number} ({table_name(table_number)})",
        f"no codon below w = {constraints.rare_codon_threshold:g}",
        f"GC {constraints.gc_min:g}-{constraints.gc_max:g}% overall",
    ]
    if source_usage is not None:
        parts.insert(1, f"harmonized from {source_usage.described}")
    if constraints.gc_window:
        parts.append(
            f"{constraints.gc_window_min:g}-{constraints.gc_window_max:g}% over "
            f"{constraints.gc_window} bases"
        )
    if constraints.longest_homopolymer:
        parts.append(f"runs of one base at most {constraints.longest_homopolymer}")
    if constraints.longest_repeat:
        parts.append(f"no repeat of {constraints.longest_repeat} bases")
    if constraints.longest_hairpin_stem:
        parts.append(
            f"no hairpin stem of {constraints.longest_hairpin_stem} bases closing a loop "
            f"of up to {constraints.hairpin_loop}"
        )
    if constraints.avoid_enzymes:
        parts.append(f"without the {', '.join(constraints.avoid_enzymes)} sites")
    if constraints.avoid_motifs:
        parts.append(f"without {', '.join(m.upper() for m in constraints.avoid_motifs)}")
    parts.append(
        "the host's own motifs avoided"
        if constraints.avoid_host_motifs
        else "the host's own motifs allowed"
    )
    return "; ".join(parts)


def counted(violations_found: Iterable[Violation]) -> dict[str, int]:
    """How many of each kind of constraint is still broken."""
    out: dict[str, int] = {}
    for violation in violations_found:
        out[violation.kind] = out.get(violation.kind, 0) + 1
    return out
