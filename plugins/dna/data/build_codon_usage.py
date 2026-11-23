"""Builds codon_usage.json, the codon usage tables the DNA plugin optimizes a coding
sequence for, out of the RefSeq coding sequences of the reference assemblies
data/README.md lists.

Run it from this directory:

    python build_codon_usage.py > codon_usage.json

It fetches each assembly's ``_cds_from_genomic.fna.gz`` from NCBI's genomes FTP site,
counts the codons of the coding sequences it keeps, and writes the counts: 64 integers
per organism, with the assembly they were counted from, how many coding sequences and
how many codons went into them, and the translation table they are read by.

With ``--from <directory>`` it reads each assembly's file from there, by the name the
URL ends in, instead of fetching it: about 60 MB is fetched otherwise, and a change to
what is counted can be checked without fetching it all again.

Nothing here is a number Iki made up, and nothing is copied from another database's
table: every count can be had again from the assembly it names. A codon usage table is
counts, not frequencies, so a reader can see how much evidence is behind each one and
work out RSCU (Sharp, Tuohy and Mosurski, Nucleic Acids Res 14:5125, 1986) or relative
adaptiveness (Sharp and Li, Nucleic Acids Res 15:1281, 1987) themselves.

What is counted, and what is left out:

- A coding sequence NCBI marks ``pseudo`` or ``partial`` is left out: its codons are not
  a gene's codons as it is read.
- A coding sequence whose length is not a multiple of three, or which has a base other
  than A, C, G or T, is left out: its reading frame is not settled.
- **One coding sequence per gene**, the longest, so that a gene with many annotated
  transcripts counts once. A genome-wide table counted transcript by transcript is a
  table of the splicing, not of the coding: human genes carry from 1 to over 50
  annotated transcripts each.
- Every codon of a kept coding sequence is counted, its stop codon included, so the
  table says which stop codon an organism uses.
"""

from __future__ import annotations

import gzip
import json
import re
import sys
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: NCBI's genomes site, where each assembly's coding sequences are.
GENOMES = "https://ftp.ncbi.nlm.nih.gov/genomes/all"
BASES = frozenset("ACGT")
#: The 64 codons, in NCBI's order (first base T, C, A, G, then the second, then the
#: third), as its translation tables are written.
ORDER = [a + b + c for a in "TCAG" for b in "TCAG" for c in "TCAG"]


@dataclass(frozen=True)
class Organism:
    """One organism's table: what to fetch it from, and how its codons are read."""

    key: str
    name: str
    #: The strain or isolate the assembly is of, where that is part of what it is.
    strain: str
    common: str
    taxid: int
    accession: str
    assembly: str
    #: NCBI's translation table its nuclear genes are read by (1 standard, 11 bacterial).
    genetic_code: int
    #: Why a lab expresses a gene in it, in a few words.
    note: str

    @property
    def url(self) -> str:
        digits = self.accession.split("_")[1].split(".")[0]
        parts = "/".join(digits[i : i + 3] for i in (0, 3, 6))
        stem = f"{self.accession}_{self.assembly}"
        return f"{GENOMES}/GCF/{parts}/{stem}/{stem}_cds_from_genomic.fna.gz"


#: The hosts a lab expresses a gene in, each as its RefSeq reference assembly. Add one by
#: adding its assembly here, not by writing counts in by hand.
ORGANISMS = [
    Organism(
        key="e_coli_k12",
        name="Escherichia coli",
        strain="K-12 substr. MG1655",
        common="E. coli",
        taxid=511145,
        accession="GCF_000005845.2",
        assembly="ASM584v2",
        genetic_code=11,
        note="The usual bacterial host for recombinant protein.",
    ),
    Organism(
        key="b_subtilis_168",
        name="Bacillus subtilis",
        strain="subsp. subtilis str. 168",
        common="B. subtilis",
        taxid=224308,
        accession="GCF_000009045.1",
        assembly="ASM904v1",
        genetic_code=11,
        note="A Gram-positive host, used where a protein is secreted.",
    ),
    Organism(
        key="s_cerevisiae_s288c",
        name="Saccharomyces cerevisiae",
        strain="S288C",
        common="Budding yeast",
        taxid=559292,
        accession="GCF_000146045.2",
        assembly="R64",
        genetic_code=1,
        note="The reference yeast, and a eukaryotic expression host.",
    ),
    Organism(
        key="k_phaffii_gs115",
        name="Komagataella phaffii",
        strain="GS115",
        common="Pichia pastoris",
        taxid=644223,
        accession="GCF_000027005.1",
        assembly="ASM2700v1",
        genetic_code=1,
        note="A methylotrophic yeast used for secreted protein at scale.",
    ),
    Organism(
        key="h_sapiens",
        name="Homo sapiens",
        strain="",
        common="Human",
        taxid=9606,
        accession="GCF_000001405.40",
        assembly="GRCh38.p14",
        genetic_code=1,
        note="Human cells, and the codons a human gene is read with.",
    ),
    Organism(
        key="c_griseus",
        name="Cricetulus griseus",
        strain="",
        common="Chinese hamster (CHO)",
        taxid=10029,
        accession="GCF_003668045.3",
        assembly="CriGri-PICRH-1.0",
        genetic_code=1,
        note="CHO cells, which most therapeutic antibodies are made in.",
    ),
    Organism(
        key="s_frugiperda",
        name="Spodoptera frugiperda",
        strain="",
        common="Fall armyworm (Sf9)",
        taxid=7108,
        accession="GCF_023101765.2",
        assembly="AGI-APGP_CSIRO_Sfru_2.0",
        genetic_code=1,
        note="Sf9 and Sf21 cells, the baculovirus expression host.",
    ),
    Organism(
        key="d_melanogaster",
        name="Drosophila melanogaster",
        strain="",
        common="Fruit fly",
        taxid=7227,
        accession="GCF_000001215.4",
        assembly="Release_6_plus_ISO1_MT",
        genetic_code=1,
        note="Fly cells and transgenes.",
    ),
    Organism(
        key="a_thaliana",
        name="Arabidopsis thaliana",
        strain="",
        common="Thale cress",
        taxid=3702,
        accession="GCF_000001735.4",
        assembly="TAIR10.1",
        genetic_code=1,
        note="The reference plant, and plant transgenes.",
    ),
]

#: A gene's name in a CDS header, which one coding sequence per gene is chosen by.
GENE = re.compile(r"\[(?:gene|locus_tag)=([^\]]+)\]")
#: What NCBI marks a coding sequence it doesn't vouch for the whole reading frame of.
REFUSED = ("[pseudo=true]", "[partial=true]", "[exception=", "[transl_except=")


def records(text: str) -> list[tuple[str, str]]:
    """The FASTA records of a coding-sequence file: each header and its bases."""
    out: list[tuple[str, str]] = []
    header, bases = "", []
    for line in text.splitlines():
        if line.startswith(">"):
            if header:
                out.append((header, "".join(bases)))
            header, bases = line, []
        elif header:
            bases.append(line.strip().upper())
    if header:
        out.append((header, "".join(bases)))
    return out


def longest_per_gene(found: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """One coding sequence per gene, the longest: a gene with many annotated transcripts
    counts once, so the table is of the coding and not of the splicing."""
    best: dict[str, tuple[str, str]] = {}
    for index, (header, bases) in enumerate(found):
        named = GENE.search(header)
        # A record with no gene or locus tag is its own gene: it has nothing to share with.
        gene = named.group(1) if named else f"#{index}"
        if gene not in best or len(bases) > len(best[gene][1]):
            best[gene] = (header, bases)
    return list(best.values())


def fetched(organism: Organism, directory: str | None) -> bytes:
    """One assembly's coding sequences, from NCBI or from a directory already holding
    the file the URL names."""
    if directory is not None:
        path = Path(directory) / organism.url.rsplit("/", 1)[1]
        sys.stderr.write(f"{organism.name}: reading {path}\n")
        return path.read_bytes()
    sys.stderr.write(f"{organism.name}: fetching {organism.url}\n")
    with urllib.request.urlopen(organism.url) as answer:  # noqa: S310 - the URL above
        found: bytes = answer.read()
    return found


def counted(organism: Organism, directory: str | None = None) -> dict[str, Any]:
    """One organism's codon counts, from the coding sequences of its assembly."""
    text = gzip.decompress(fetched(organism, directory)).decode("ascii", "replace")
    kept = longest_per_gene(
        [
            (header, bases)
            for header, bases in records(text)
            if not any(mark in header for mark in REFUSED)
            and len(bases) >= 3
            and len(bases) % 3 == 0
            and not set(bases) - BASES
        ]
    )
    counts: Counter[str] = Counter()
    for _, bases in kept:
        counts.update(bases[at : at + 3] for at in range(0, len(bases), 3))
    total = sum(counts.values())
    sys.stderr.write(f"{organism.name}: {len(kept):,} coding sequences, {total:,} codons\n")
    if len(kept) < 1000 or total < 300_000:
        raise SystemExit(
            f"{organism.name} gave only {len(kept):,} coding sequences and {total:,} "
            "codons: too few for a table. Check the assembly."
        )
    unseen = [codon for codon in ORDER if not counts[codon]]
    if unseen:
        raise SystemExit(f"{organism.name} never uses {', '.join(unseen)}: check the file.")
    return {
        "key": organism.key,
        "name": organism.name,
        "strain": organism.strain,
        "common": organism.common,
        "taxid": organism.taxid,
        "assembly": organism.accession,
        "assembly_name": organism.assembly,
        "source": organism.url,
        "genetic_code": organism.genetic_code,
        "note": organism.note,
        "coding_sequences": len(kept),
        "codons": total,
        "counts": {codon: counts[codon] for codon in ORDER},
    }


def main() -> None:
    arguments = sys.argv[1:]
    directory = None
    if arguments[:1] == ["--from"] and len(arguments) == 2:
        directory = arguments[1]
    elif arguments:
        raise SystemExit("Usage: python build_codon_usage.py [--from <directory>]")
    table = {
        "version": "1.0",
        "licence": (
            "Counted from the RefSeq coding sequences of the assemblies each organism "
            "names. NCBI places no restrictions on the use or distribution of the data "
            "in its sequence databases."
        ),
        "counted": (
            "One coding sequence per gene, the longest; a sequence NCBI marks pseudo or "
            "partial, or whose length is not a multiple of three, or which has a base "
            "other than A, C, G or T, is left out. Every codon of a kept sequence is "
            "counted, its stop codon included."
        ),
        "organisms": [counted(organism, directory) for organism in ORGANISMS],
    }
    json.dump(table, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
