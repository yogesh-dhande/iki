"""Checks a structure-tools image: mkdssp assigns an ideal alpha helix as one, with the
Chemical Component Dictionary the image carries, and the plugin's assign_secondary_structure
puts each letter on its own residue of a PDB-format file.

The helix is built here from ideal backbone geometry (Engh & Huber bond lengths and
angles, phi -57.8, psi -47.0), so no PDB entry is copied. Its middle residue is
selenomethionine (MSE), which mkdssp refuses unless the dictionary describes it: a
dictionary of the standard amino acids alone would fail this check.

Then the plugin's own script (scripts/secondary.py, mounted at /scripts) annotates the
helix as PDB files a lab uploads: numbered from 101, with an insertion code (104A), and
with two residues missing, with and without SEQRES. Each residue's letter must be the one
mkdssp gives when it reads the PDB file itself, residue by residue (its classic output
names each by chain, number and insertion code), and a residue SEQRES has but the file
doesn't must be `?`.

    docker run --rm -v "$PWD/plugins/protein/images/structure-tools:/check:ro" \
        -v "$PWD/plugins/protein/scripts:/scripts:ro" IMAGE python -I /check/check.py
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


def classic(given: Path) -> dict[tuple[str, int, str], str]:
    """What mkdssp assigns each residue of a PDB file it reads itself, by chain, number and
    insertion code, from its classic output (`.` for none, as the plugin writes `-`)."""
    out = given.with_suffix(".dssp")
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
    return {
        (line[11], int(line[5:10]), line[10].strip()): line[16].strip() or "-"
        for line in lines[start + 1 :]
        if line[13] != "!"
    }


def renumbered(text: str, number: dict[int, tuple[int, str] | None]) -> str:
    """The PDB file with each residue renumbered (number, insertion code), or left out."""
    lines = []
    for line in text.splitlines():
        if line.startswith(("ATOM", "HETATM")):
            found = number[int(line[22:26])]
            if found is None:
                continue
            line = f"{line[:22]}{found[0]:4d}{found[1] or ' '}{line[27:]}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def through_the_plugin(tmp: Path) -> list[str]:
    """The plugin's annotation of PDB files a lab uploads, checked against mkdssp's own."""
    sys.path.insert(0, "/scripts")
    import secondary
    import structures

    text = pdb(helix())
    bare = "\n".join(line for line in text.splitlines() if not line.startswith("SEQRES"))
    # 1 to 20 as 101 to 120, but the fifth is 104A: 101, 102, 103, 104, 104A, 105, ...
    offset = {n: (100 + n, "") if n < 5 else (99 + n, "A" if n == 5 else "") for n in range(21)}
    gap = {n: None if n in (5, 6) else (n, "") for n in range(21)}
    cases = {
        "numbered from 101, with 104A, without SEQRES": renumbered(bare, offset),
        "numbered from 101, with 104A, with SEQRES": renumbered(text, offset),
        "missing 5 and 6, without SEQRES": renumbered(bare, gap),
        "missing 5 and 6, with SEQRES": renumbered(text, gap),
    }
    said = []
    for n, (case, content) in enumerate(cases.items()):
        given = tmp / f"upload{n}.pdb"
        given.write_text(content, encoding="ascii")
        read = structures.read(given, given.stem, "")
        (protein,) = read.proteins
        entity = {"hrid": f"PST-{n}", "values": {"chain_ids": read.values["chain_ids"]}}
        values, _ = secondary.annotate(entity, {"path": str(given)}, tmp)
        (letters,) = values["secondary_structure"]
        mine = classic(given)
        model = structures.gemmi.read_pdb(str(given))
        expected = "".join(
            mine.get((chain.name, r.seqid.num, r.seqid.icode.strip()), "?")
            for chain in model[0]
            for r in chain
        )
        modelled = letters.replace("?", "")
        if len(letters) != len(protein.values["sequence"]) or modelled != expected:
            sys.exit(
                f"{case}: the plugin assigned {letters!r} to {protein.values['sequence']}, "
                f"and mkdssp, reading the file itself, {expected!r}"
            )
        if "SEQRES" in content and "missing" in case and letters[4:6] != "??":
            sys.exit(f"{case}: residues 5 and 6 have no coordinates, but got {letters!r}")
        said.append(f"{case}: {letters}")
    return said


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        given = Path(tmp, "helix.pdb")
        given.write_text(pdb(helix()), encoding="ascii")
        assigned = "".join(classic(given).values())
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
    if not Path("/scripts/secondary.py").exists():
        sys.exit("The plugin's scripts aren't at /scripts: mount plugins/protein/scripts there.")
    with tempfile.TemporaryDirectory() as tmp:
        for line in through_the_plugin(Path(tmp)):
            print(f"The plugin's DSSP of the helix as a PDB file {line}")


if __name__ == "__main__":
    main()
