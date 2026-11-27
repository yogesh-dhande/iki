"""Checks a structure-tools image: ImmuneBuilder, with the image's weights and no network,
models trastuzumab's variable domains (PDB 1N8Z) with ABodyBuilder2, and the GFP enhancer
nanobody's (PDB 3K1K) with NanoBodyBuilder2, refining both with OpenMM.

An image without the weights fails this (ImmuneBuilder would try to download them), as
does one whose OpenMM or PDBFixer can't refine, or whose ANARCI can't number the chains.

    docker run --rm --network none -v "$PWD/plugins/ab_predict/images/structure-tools:/check:ro" \
        IMAGE python -I /check/check.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from ImmuneBuilder import ABodyBuilder2, NanoBodyBuilder2

WEIGHTS = os.environ["IMMUNEBUILDER_WEIGHTS"]
VH = (
    "EVQLVESGGGLVQPGGSLRLSCAASGFNIKDTYIHWVRQAPGKGLEWVARIYPTNGYTRYADSVKGRFTISADTSKNTAYLQMNSLR"
    "AEDTAVYYCSRWGGDGFYAMDYWGQGTLVTVSS"
)
VL = (
    "DIQMTQSPSSLSASVGDRVTITCRASQDVNTAVAWYQQKPGKAPKLLIYSASFLYSGVPSRFSGSRSGTDFTLTISSLQPEDFATYY"
    "CQQHYTTPPTFGQGTKVEIK"
)
ENHANCER = (
    "QVQLVESGGALVQPGGSLRLSCAASGFPVNRYSMRWYRQAPGKEREWVAGMSSAGDRSSYEDSVKGRFTISRDDARNTVYLQMNSLK"
    "PEDTAVYYCNVNVGFEYWGQGTQVTVSS"
)
#: A loose bound: a model this uncertain over every residue has gone wrong.
MAX_ERROR = 2.0


def residues(pdb: Path) -> dict[str, int]:
    """Each chain's residue count, from the CA atoms."""
    counts: dict[str, int] = {}
    for line in pdb.read_text().splitlines():
        if line.startswith("ATOM") and line[12:16].strip() == "CA":
            counts[line[21]] = counts.get(line[21], 0) + 1
    return counts


def model(name: str, builder: object, sequences: dict[str, str], out: Path) -> None:
    predicted = builder.predict(sequences)  # type: ignore[attr-defined]
    error = float(predicted.error_estimates.mean(0).sqrt().mean())
    predicted.save(str(out))
    got = residues(out)
    if set(got) != set(sequences):
        sys.exit(f"{name}: expected chains {sorted(sequences)}, the model has {sorted(got)}")
    if error > MAX_ERROR:
        sys.exit(f"{name}: mean estimated error {error:.2f} Å, over {MAX_ERROR} Å")
    if "REMARK" not in out.read_text()[:200]:
        sys.exit(f"{name}: the model wasn't written as ImmuneBuilder writes it")
    shown = ", ".join(f"{chain} {n} residues" for chain, n in sorted(got.items()))
    print(f"{name}: {shown}, mean estimated error {error:.2f} Å")


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        out = Path(directory)
        model(
            "trastuzumab (ABodyBuilder2)",
            ABodyBuilder2(weights_dir=WEIGHTS),
            {"H": VH, "L": VL},
            out / "fv.pdb",
        )
        model(
            "GFP enhancer (NanoBodyBuilder2)",
            NanoBodyBuilder2(weights_dir=WEIGHTS),
            {"H": ENHANCER},
            out / "vhh.pdb",
        )


if __name__ == "__main__":
    main()
