"""Checks a numbering-tools image: ANARCI, with the image's hmmscan and germlines, numbers
trastuzumab's variable domains (PDB 1N8Z) as IMGT does, and finds their nearest human
germlines.

A regression test of the nearest human germline V genes ANARCI computes for them,
IGHV3-66*01 (82.5% of the V region the same) and IGKV1-39*01 (87.0%): an image without
hmmscan, or without IMGT's germlines, fails it. They're the nearest, not trastuzumab's
history: Carter et al. (PNAS 89:4285-4289, 1992) humanized 4D5 onto human consensus
frameworks (VH subgroup III, VL kappa subgroup I), not onto particular germline genes.

    docker run --rm --network none -v "$PWD/plugins/antibody/images/numbering-tools:/check:ro" \
        IMAGE python -I /check/check.py
"""

from __future__ import annotations

import sys

from anarci import anarci

VH = (
    "EVQLVESGGGLVQPGGSLRLSCAASGFNIKDTYIHWVRQAPGKGLEWVARIYPTNGYTRYADSVKGRFTISADTSKNTAYLQMNSLR"
    "AEDTAVYYCSRWGGDGFYAMDYWGQGTLVTVSS"
)
VL = (
    "DIQMTQSPSSLSASVGDRVTITCRASQDVNTAVAWYQQKPGKAPKLLIYSASFLYSGVPSRFSGSRSGTDFTLTISSLQPEDFATYY"
    "CQQHYTTPPTFGQGTKVEIK"
)
#: IMGT's CDR3: positions 105 to 117.
EXPECTED = {
    "vh": ("H", "IGHV3-66*01", "SRWGGDGFYAMDY"),
    "vl": ("K", "IGKV1-39*01", "QQHYTTPPT"),
}


def main() -> None:
    numbered, details, _ = anarci(
        [("vh", VH), ("vl", VL)], scheme="imgt", assign_germline=True, allowed_species=["human"]
    )
    for i, name in enumerate(EXPECTED):
        if not numbered[i]:
            sys.exit(f"ANARCI found no variable domain in trastuzumab's {name}")
        positions, _, _ = numbered[i][0]
        found = details[i][0]
        cdr3 = "".join(aa for (n, _), aa in positions if 105 <= n <= 117 and aa != "-")
        v_gene = found["germlines"]["v_gene"][0][1]
        got = (found["chain_type"], v_gene, cdr3)
        if got != EXPECTED[name]:
            sys.exit(f"trastuzumab's {name}: expected {EXPECTED[name]}, ANARCI gave {got}")
        identity = found["germlines"]["v_gene"][1]
        print(f"{name}: {got[0]} chain, {v_gene} ({identity:.1%}), CDR3 {cdr3}")


if __name__ == "__main__":
    main()
