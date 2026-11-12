"""Checks a structure-tools image: mkdssp assigns an ideal alpha helix as one, with the
Chemical Component Dictionary the image carries.

The helix is built here from ideal backbone geometry (Engh & Huber bond lengths and
angles, phi -57.8, psi -47.0), so no PDB entry is copied. Its middle residue is
selenomethionine (MSE), which mkdssp refuses unless the dictionary describes it: a
dictionary of the standard amino acids alone would fail this check.

    docker run --rm -v "$PWD/plugins/protein/images/structure-tools:/check:ro" \
        IMAGE python -I /check/check.py
"""

from __future__ import annotations

import math
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

LENGTH = 20
PHI, PSI, OMEGA = -57.8, -47.0, 180.0
# Bond lengths (Å) and angles (degrees).
N_CA, CA_C, C_N, C_O = 1.458, 1.525, 1.329, 1.231
N_CA_C, CA_C_N, C_N_CA, CA_C_O = 111.2, 116.2, 121.7, 120.5


def place(
    a: np.ndarray, b: np.ndarray, c: np.ndarray, bond: float, angle: float, torsion: float
) -> np.ndarray:
    """The atom bonded to c, `bond` from it, at `angle` b-c-d and dihedral `torsion` a-b-c-d."""
    angle, torsion = math.radians(angle), math.radians(torsion)
    bc = (c - b) / np.linalg.norm(c - b)
    n = np.cross(b - a, bc)
    n /= np.linalg.norm(n)
    m = np.array([bc, np.cross(n, bc), n]).T
    d = np.array(
        [
            -bond * math.cos(angle),
            bond * math.sin(angle) * math.cos(torsion),
            bond * math.sin(angle) * math.sin(torsion),
        ]
    )
    return c + m @ d


def helix() -> list[tuple[str, dict[str, np.ndarray]]]:
    n = np.array([0.0, 0.0, 0.0])
    ca = np.array([N_CA, 0.0, 0.0])
    angle = math.radians(180 - N_CA_C)
    c = ca + CA_C * np.array([math.cos(angle), math.sin(angle), 0.0])
    residues = []
    for i in range(LENGTH):
        next_n = place(n, ca, c, C_N, CA_C_N, PSI)
        o = place(n, ca, c, C_O, CA_C_O, PSI + 180)
        name = "MSE" if i == LENGTH // 2 else "ALA"
        residues.append((name, {"N": n, "CA": ca, "C": c, "O": o}))
        next_ca = place(ca, c, next_n, N_CA, C_N_CA, OMEGA)
        next_c = place(c, next_n, next_ca, CA_C, N_CA_C, PHI)
        n, ca, c = next_n, next_ca, next_c
    return residues


def pdb(residues: list[tuple[str, dict[str, np.ndarray]]]) -> str:
    lines = []
    serial = 0
    for number, (name, atoms) in enumerate(residues, start=1):
        record = "HETATM" if name == "MSE" else "ATOM  "
        for atom, (x, y, z) in atoms.items():
            serial += 1
            lines.append(
                f"{record}{serial:5d}  {atom:<3} {name} A{number:4d}    "
                f"{x:8.3f}{y:8.3f}{z:8.3f}  1.00 20.00           {atom[0]}"
            )
    seqres = " ".join(name for name, _ in residues)
    header = [
        f"SEQRES   1 A {LENGTH:4d}  {seqres[:51]}",
        f"SEQRES   2 A {LENGTH:4d}  {seqres[52:]}",
    ]
    return "\n".join(
        [
            "HEADER    DE NOVO PROTEIN                         01-JAN-26   XXXX",
            *header,
            *lines,
            "TER",
            "END",
            "",
        ]
    )


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        given, out = Path(tmp, "helix.pdb"), Path(tmp, "helix.dssp")
        given.write_text(pdb(helix()), encoding="ascii")
        done = subprocess.run(  # noqa: S603 - mkdssp from the image, on a file made here
            ["mkdssp", "--output-format", "dssp", str(given), str(out)],  # noqa: S607 - the image's
            capture_output=True,
            text=True,
            check=False,
        )
        if done.returncode != 0 or not out.exists():
            sys.exit(f"mkdssp failed ({done.returncode}):\n{done.stderr[-2000:]}")
        lines = out.read_text(encoding="ascii").splitlines()
        start = next(i for i, line in enumerate(lines) if line.startswith("  #  RESIDUE"))
        assigned = "".join(line[16] for line in lines[start + 1 :] if line[13] != "!")
    # DSSP leaves a helix's first and last residues out of it.
    if len(assigned) != LENGTH or assigned[1:-1].strip("H") or "H" * (LENGTH - 4) not in assigned:
        sys.exit(f"expected an alpha helix, mkdssp assigned {assigned!r}")
    version = subprocess.run(
        ["mkdssp", "--version"],  # noqa: S607 - the image's
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    print(f"{version}: an ideal {LENGTH}-residue helix with MSE assigned {assigned!r}")


if __name__ == "__main__":
    main()
