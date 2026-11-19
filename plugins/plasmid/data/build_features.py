"""Builds features.json, the library of common vector parts the plasmid plugin annotates
plasmids from, out of the INSDC records data/README.md lists, each part the bases of one
of a record's own features.

Run it from this directory, giving the address NCBI asks automated clients for:

    python build_features.py lab@example.com > features.json

It fetches each record from NCBI's E-utilities in GenBank format, takes each part's bases
from the location of the record's own feature, and checks what it writes: every part's
bases are A, C, G or T, no two parts are one sequence, each part's length is the length
its location gives, and each part is found in the record it came from.

Nothing here is Iki's judgement of where a part lies: each part's location is a feature
of a public-domain INSDC record, named in `source`, so the library can be checked against
it. Parts a record annotates only as a point (a rep_origin at one base) are left out.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from typing import Any

EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
COMPLEMENT = str.maketrans("ACGT", "TGCA")

#: Each part: the record it comes from, the location of that record's feature, the name
#: Iki annotates it under, the feature key it's annotated with, its role, and what its
#: presence says about the plasmid (`implies`, a field of `iki/plasmid.Plasmid` and the
#: value to add). `at_least` is the identity a match needs, where the default (0.9, or an
#: exact match under 40 bp) isn't right for it.
PARTS: list[dict[str, Any]] = [
    # pET-28a(+) carrying a terminal deoxynucleotidyl transferase (Cloning vector
    # pET-28a-ZaTdT12): a bacterial T7 expression vector, annotated feature by feature.
    {
        "accession": "OL989372.1",
        "location": "12..467",
        "name": "f1 ori",
        "type": "rep_origin",
        "role": "origin",
        "implies": {"replication_origins": "f1"},
    },
    {
        "accession": "OL989372.1",
        "location": "complement(560..1375)",
        "name": "KanR",
        "label": "KanR (aph(3')-Ia, aminoglycoside phosphotransferase)",
        "type": "CDS",
        "role": "resistance",
        "implies": {"bacterial_resistance": "kanamycin"},
    },
    {
        "accession": "OL989372.1",
        "location": "1497..2085",
        "name": "ori",
        "label": "ori (ColE1/pMB1/pBR322/pUC origin of replication)",
        "type": "rep_origin",
        "role": "origin",
        "implies": {"replication_origins": "pMB1/pUC"},
    },
    {
        "accession": "OL989372.1",
        "location": "complement(2515..2706)",
        "name": "rop",
        "label": "rop (Rop, which keeps the pMB1 origin at a low copy number)",
        "type": "CDS",
        "role": "regulatory",
    },
    {
        "accession": "OL989372.1",
        "location": "complement(3515..4597)",
        "name": "lacI",
        "label": "lacI (the lac repressor)",
        "type": "CDS",
        "role": "regulatory",
    },
    {
        "accession": "OL989372.1",
        "location": "complement(4598..4675)",
        "name": "lacI promoter",
        "type": "promoter",
        "role": "promoter",
        "implies": {"promoters": "lacI"},
    },
    {
        "accession": "OL989372.1",
        "location": "4984..5002",
        "name": "T7 promoter",
        "type": "promoter",
        "role": "promoter",
        "implies": {"promoters": "T7"},
    },
    {
        "accession": "OL989372.1",
        "location": "5003..5027",
        "name": "lac operator",
        "type": "protein_bind",
        "role": "regulatory",
    },
    {
        "accession": "OL989372.1",
        "location": "5083..5100",
        "name": "6xHis",
        "type": "CDS",
        "role": "tag",
    },
    {
        "accession": "OL989372.1",
        "location": "5110..5127",
        "name": "thrombin site",
        "type": "CDS",
        "role": "tag",
    },
    {
        "accession": "OL989372.1",
        "location": "6763..6810",
        "name": "T7 terminator",
        "type": "terminator",
        "role": "terminator",
    },
    # pLenti6-V5-Puro: a third-generation lentiviral Gateway destination vector, with the
    # viral elements, a mammalian marker and the Gateway att sites.
    {
        "accession": "LT009457.1",
        "location": "1..16",
        "name": "cPPT",
        "label": "cPPT (HIV-1 central polypurine tract)",
        "type": "misc_feature",
        "role": "viral",
    },
    {
        "accession": "LT009457.1",
        "location": "17..251",
        "name": "3' LTR",
        "label": "3' LTR (RSV/HIV-1 dU3 long terminal repeat)",
        "type": "LTR",
        "role": "viral",
    },
    {
        "accession": "LT009457.1",
        "location": "323..397",
        "name": "SV40 poly(A) signal",
        "type": "polyA_signal",
        "role": "terminator",
    },
    {
        "accession": "LT009457.1",
        "location": "533..608",
        "name": "SV40 ori",
        "type": "rep_origin",
        "role": "origin",
        "implies": {"replication_origins": "SV40"},
    },
    {
        "accession": "LT009457.1",
        "location": "1412..2272",
        "name": "AmpR",
        "label": "AmpR (bla, beta-lactamase)",
        "type": "CDS",
        "role": "resistance",
        "implies": {"bacterial_resistance": "ampicillin"},
    },
    {
        "accession": "LT009457.1",
        "location": "3307..3390",
        "name": "lac promoter",
        "type": "promoter",
        "role": "promoter",
        "implies": {"promoters": "lac"},
    },
    {
        "accession": "LT009457.1",
        "location": "3454..3470",
        "name": "T3 promoter",
        "type": "promoter",
        "role": "promoter",
        "implies": {"promoters": "T3"},
    },
    {
        "accession": "LT009457.1",
        "location": "3579..3907",
        "name": "5' LTR",
        "label": "5' LTR (RSV/HIV-1 long terminal repeat)",
        "type": "LTR",
        "role": "viral",
    },
    {
        "accession": "LT009457.1",
        "location": "4018..4062",
        "name": "psi",
        "label": "psi (HIV-1 packaging signal)",
        "type": "misc_feature",
        "role": "viral",
    },
    {
        "accession": "LT009457.1",
        "location": "4572..4805",
        "name": "RRE",
        "label": "RRE (HIV-1 Rev response element)",
        "type": "misc_feature",
        "role": "viral",
    },
    {
        "accession": "LT009457.1",
        "location": "5304..5820",
        "name": "CMV promoter",
        "label": "CMV promoter and enhancer (human cytomegalovirus immediate early)",
        "type": "promoter",
        "role": "promoter",
        "implies": {"promoters": "CMV"},
    },
    {
        "accession": "LT009457.1",
        "location": "complement(5937..6061)",
        "name": "attR1",
        "label": "attR1 (Gateway recombination site)",
        "type": "misc_recomb",
        "role": "recombination",
    },
    {
        "accession": "LT009457.1",
        "location": "6170..6829",
        "name": "CmR",
        "label": "CmR (cat, chloramphenicol acetyltransferase)",
        "type": "CDS",
        "role": "resistance",
        "implies": {"bacterial_resistance": "chloramphenicol"},
    },
    {
        "accession": "LT009457.1",
        "location": "7171..7476",
        "name": "ccdB",
        "label": "ccdB (kills cells without a recombined insert)",
        "type": "CDS",
        "role": "regulatory",
    },
    {
        "accession": "LT009457.1",
        "location": "7517..7641",
        "name": "attR2",
        "label": "attR2 (Gateway recombination site)",
        "type": "misc_recomb",
        "role": "recombination",
    },
    {
        "accession": "LT009457.1",
        "location": "7694..7735",
        "name": "V5 tag",
        "type": "CDS",
        "role": "tag",
    },
    {
        "accession": "LT009457.1",
        "location": "7768..8050",
        "name": "SV40 early promoter",
        "type": "promoter",
        "role": "promoter",
        "implies": {"promoters": "SV40 early"},
    },
    {
        "accession": "LT009457.1",
        "location": "8159..8761",
        "name": "PuroR",
        "label": "PuroR (pac, puromycin N-acetyltransferase)",
        "type": "CDS",
        "role": "selection",
        "implies": {"selection_markers": "puromycin"},
    },
    # pBR322, the vector most of these descend from: its tetracycline resistance.
    {
        "accession": "J01749.1",
        "location": "86..1276",
        "name": "TetR",
        "label": "TetR (tet, tetracycline efflux protein)",
        "type": "CDS",
        "role": "resistance",
        "implies": {"bacterial_resistance": "tetracycline"},
    },
    # pcDNA3.1(+): the neo gene, which selects with kanamycin in E. coli and with
    # neomycin or G418 in mammalian cells.
    {
        "accession": "MN996867.1",
        "location": "2136..2930",
        "name": "NeoR/KanR",
        "label": "NeoR/KanR (nptII, neomycin phosphotransferase II)",
        "type": "CDS",
        "role": "selection",
        "implies": {
            "bacterial_resistance": "kanamycin",
            "selection_markers": "neomycin_g418",
        },
    },
    # Saccharomyces cerevisiae URA3, the marker of the pRS41x yeast shuttle vectors.
    {
        "accession": "K02207.1",
        "location": "223..1026",
        "name": "URA3",
        "label": "URA3 (orotidine-5'-phosphate decarboxylase)",
        "type": "CDS",
        "role": "selection",
        "implies": {"selection_markers": "ura3"},
    },
    # The yeast centromere and origin a pRS41x vector keeps one copy per cell by.
    {
        "accession": "FJ457000.1",
        "location": "4357..4475",
        "name": "CEN6",
        "label": "CEN6 (Saccharomyces cerevisiae centromere 6)",
        "type": "centromere",
        "role": "origin",
        "implies": {"replication_origins": "CEN6"},
    },
    {
        "accession": "FJ457000.1",
        "location": "4488..4861",
        "name": "ARSH4",
        "label": "ARSH4 (Saccharomyces cerevisiae autonomously replicating sequence)",
        "type": "rep_origin",
        "role": "origin",
        "implies": {"replication_origins": "ARSH4"},
    },
]

#: Where each record is from, and what it is, for data/README.md and each part's `source`.
RECORDS = {
    "OL989372.1": "Cloning vector pET-28a-ZaTdT12",
    "LT009457.1": "Lentiviral vector pLenti6-V5-Puro",
    "J01749.1": "Cloning vector pBR322",
    "MN996867.1": "Cloning vector pcDNA3.1(+)",
    "K02207.1": "Saccharomyces cerevisiae URA3 gene",
    "FJ457000.1": "Yeast shuttle vector pRS416-10xGly",
}

#: The library's version. Raise it whenever a part changes, so a plasmid's annotation
#: says which library found it.
VERSION = "1.0"


def fetch(accession: str, email: str) -> str:
    """``accession``'s record, in GenBank format, from NCBI's E-utilities."""
    query = urllib.parse.urlencode(
        {
            "db": "nuccore",
            "id": accession,
            "rettype": "gbwithparts",
            "retmode": "text",
            "tool": "iki-plasmid",
            "email": email,
        }
    )
    with urllib.request.urlopen(f"{EFETCH}?{query}", timeout=120) as answer:  # noqa: S310
        return answer.read().decode("utf-8")


def bases_of(record: str) -> str:
    """A GenBank record's bases, upper case."""
    out, reading = [], False
    for line in record.splitlines():
        if line.startswith("ORIGIN"):
            reading = True
        elif line.startswith("//"):
            break
        elif reading:
            out.append(re.sub(r"[^A-Za-z]", "", line))
    return "".join(out).upper()


def at(bases: str, location: str) -> str:
    """The bases at an INSDC location of one span, reverse-complemented if it's on the
    complement strand."""
    reverse = location.startswith("complement(")
    inner = location[len("complement(") : -1] if reverse else location
    start, _, end = inner.partition("..")
    got = bases[int(start) - 1 : int(end)]
    return got.translate(COMPLEMENT)[::-1] if reverse else got


def length_of(location: str) -> int:
    inner = location.removeprefix("complement(").removesuffix(")")
    start, _, end = inner.partition("..")
    return int(end) - int(start) + 1


def main() -> None:
    if len(sys.argv) != 2 or "@" not in sys.argv[1]:
        raise SystemExit("Usage: python build_features.py <your email for NCBI>")
    email = sys.argv[1]
    records: dict[str, str] = {}
    for accession in RECORDS:
        records[accession] = bases_of(fetch(accession, email))
        sys.stderr.write(f"{accession}: {len(records[accession])} bp\n")
        time.sleep(0.5)  # NCBI asks for at most three requests a second.

    parts, seen = [], {}
    for part in PARTS:
        accession = part["accession"]
        bases = at(records[accession], part["location"])
        if set(bases) - set("ACGT"):
            odd = sorted(set(bases) - set("ACGT"))
            raise SystemExit(f"{part['name']}: {odd} isn't A, C, G or T")
        if len(bases) != length_of(part["location"]):
            raise SystemExit(f"{part['name']}: {len(bases)} bp, not {length_of(part['location'])}")
        if bases in seen:
            raise SystemExit(f"{part['name']} and {seen[bases]} are one sequence")
        other = bases.translate(COMPLEMENT)[::-1]
        if bases not in records[accession] and other not in records[accession]:
            raise SystemExit(f"{part['name']} isn't in {accession}")
        seen[bases] = part["name"]
        out = {
            "name": part["name"],
            "label": part.get("label", part["name"]),
            "type": part["type"],
            "role": part["role"],
            "bases": bases,
            "source": {
                "database": "INSDC",
                "accession": accession,
                "record": RECORDS[accession],
                "location": part["location"],
            },
        }
        if "implies" in part:
            out["implies"] = part["implies"]
        if "at_least" in part:
            out["at_least"] = part["at_least"]
        parts.append(out)

    json.dump(
        {
            "version": VERSION,
            "licence": (
                "The sequences are from INSDC records (GenBank), which NCBI places no "
                "restrictions on the use or distribution of. Each part names the record "
                "and the feature it is."
            ),
            "records": RECORDS,
            "parts": parts,
        },
        sys.stdout,
        indent=2,
        ensure_ascii=False,
    )
    sys.stdout.write("\n")
    sys.stderr.write(f"{len(parts)} parts\n")


if __name__ == "__main__":
    main()
