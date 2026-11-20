"""Builds monomers.json, the library the oligo plugin seeds its registry of monomers from,
from the Pistoia Alliance's HELM monomer sets, with the corrections data/README.md lists.

Run it from this directory with RDKit, given a checkout of HELMMonomerSets at commit
fecc972f4707bc4e0cc34b7de3713cdba559e177 (9 December 2021; MIT licence):

    git clone https://github.com/PistoiaHELM/HELMMonomerSets
    git -C HELMMonomerSets checkout fecc972f4707bc4e0cc34b7de3713cdba559e177
    python build_monomers.py HELMMonomerSets > monomers.json

It checks every structure it writes: each reads, its attachment points are capped as
HELM caps them, and no two monomers of a polymer type are one structure.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from rdkit import Chem

COMMIT = "fecc972f4707bc4e0cc34b7de3713cdba559e177"
LIB2 = f"HELMMonomerSets monomerLib2.0 (Pistoia Alliance, commit {COMMIT[:7]}, MIT)"
CORE = f"HELMMonomerSets HELMCoreLibrary (Pistoia Alliance, commit {COMMIT[:7]}, MIT)"
IONIS = f"HELMMonomerSets Ionis library (Pistoia Alliance, commit {COMMIT[:7]}, MIT)"

#: From monomerLib2.0, as it gives them: the monomers its own examples and the HELM
#: specification use. (symbol, role, name, modification label)
FROM_LIB2 = [
    ("R", "sugar", "Ribose"),
    ("dR", "sugar", "2′-Deoxyribose"),
    ("mR", "sugar", "2′-O-Methylribose"),
    ("fR", "sugar", "2′-Fluoro-2′-deoxyribose"),
    ("MOE", "sugar", "2′-O-Methoxyethylribose"),
    ("LR", "sugar", "LNA (2′-O,4′-C-methylene ribose)"),
    ("eR", "sugar", "ENA (2′-O,4′-C-ethylene ribose)"),
    ("P", "linker", "Phosphate"),
    ("sP", "linker", "Phosphorothioate"),
    ("A", "base", "Adenine"),
    ("C", "base", "Cytosine"),
    ("G", "base", "Guanine"),
    ("T", "base", "Thymine"),
    ("U", "base", "Uracil"),
    ("5meC", "base", "5-Methylcytosine"),
]

#: From HELMCoreLibrary, rebuilt from its molfiles: its SMILES for these give the other
#: stereoisomer or none (see data/README.md). (symbol, role, name)
FROM_CORE = [
    ("cet", "sugar", "cEt, (S)-constrained ethyl (2′-O,4′-C-[(S)-ethylidene] ribose)"),
    ("Rsp", "linker", "Phosphorothioate, Rp"),
    ("Ssp", "linker", "Phosphorothioate, Sp"),
]

#: Chemical groups (CHEM), from the Ionis library with the corrections data/README.md
#: lists, each joined through its R1 to a phosphate of the strand's. (symbol, name, its
#: corrected SMILES or None for the library's own, ε260 and where it's from)
NO_CHROMOPHORE = "No chromophore that absorbs at 260 nm: saturated chains and amides."
FROM_IONIS: list[tuple[str, str, str | None, float, str]] = [
    (
        "5FAM",
        "6-FAM (6-carboxyfluorescein on a C6 amino linker)",
        None,
        20960,
        "IDT, 5′ 6-FAM (/56-FAM/): ε260 20,960",
    ),
    ("BHQ1", "BHQ-1 (Black Hole Quencher 1)", None, 8000, "IDT, 3′ BHQ-1 (/3BHQ_1/): ε260 8,000"),
    (
        "BHQ2",
        "BHQ-2 (Black Hole Quencher 2)",
        "C1=CC(=CC=C1N=NC2=CC(=C(C=C2OC)N=NC3=CC=C(C=C3)N(CCO[H:1])CCO[H:2])OC)[N+](=O)[O-]",
        8000,
        "IDT, 3′ BHQ-2 (/3BHQ_2/): ε260 8,000",
    ),
    (
        "5Cy5",
        "Cy5 (indodicarbocyanine, N,N′-di(3-hydroxypropyl))",
        "C=1C=CC=C2[N+](=C(C(C21)(C)C)C=CC=CC=C3N(C=4C(C3(C)C)=CC=CC4)CCCO[H:1])CCCO",
        10000,
        "IDT, 5′ Cy5 (/5Cy5/): ε260 10,000",
    ),
    (
        "5BioTEG",
        "Biotin-TEG (biotin on a triethylene glycol linker)",
        "N(C(CCCC[C@@H]1SC[C@@H]2NC(=O)N[C@H]12)=O)CCOCCOCCOCCO[H:1]",
        0,
        "Glen Research: biotin doesn't absorb at 260 nm",
    ),
    ("nC6o", "Amino C6 (6-aminohexyl)", None, 0, NO_CHROMOPHORE),
    (
        "cholTEG",
        "Cholesterol-TEG (cholesteryl carbamate on a triethylene glycol linker)",
        "O(CCOCCOCCO[H:1])CCCNC(O[C@H]1CC[C@@]2(C)C(=CC[C@H]3[C@@H]4CC[C@H]([C@H](C)CCCC(C)C)"
        "[C@@]4(C)CC[C@@H]32)C1)=O",
        0,
        NO_CHROMOPHORE + " Cholesterol's one C=C absorbs below 210 nm.",
    ),
    (
        "3GN3C12hp",
        "Triantennary GalNAc (GalNAc3, as L96), on a hydroxyprolinol linker",
        # Its constitution, with each GalNAc's stereochemistry (N-acetyl-beta-D-
        # galactosamine) and its prolinol's (trans-4-hydroxy-L-prolinol, (2S,4R)), as
        # EMA's assessment report for Leqvio describes them.
        "O(CC(NC(CCCCCCCCCCC(N1C[C@H](O)C[C@H]1CO[H:1])=O)=O)"
        "(COCCC(=O)NCCCNC(CCCCO[C@@H]2O[C@H](CO)[C@H](O)[C@H](O)[C@H]2NC(C)=O)=O)"
        "COCCC(NCCCNC(CCCCO[C@@H]3O[C@H](CO)[C@H](O)[C@H](O)[C@H]3NC(C)=O)=O)=O)"
        "CCC(NCCCNC(CCCCO[C@@H]4O[C@H](CO)[C@H](O)[C@H](O)[C@H]4NC(C)=O)=O)=O",
        0,
        NO_CHROMOPHORE,
    ),
    ("SpC3", "C3 spacer (propane-1,3-diol)", None, 0, NO_CHROMOPHORE),
]

#: Curated by Iki: Cy3, which the Ionis library has only on another linker. IDT's 5′
#: Cy3 is Cy5's structure with one CH=CH fewer in its polymethine chain.
CURATED: list[tuple[str, str, str, float, str]] = [
    ("5Cy3", "Cy3 (indocarbocyanine, N,N′-di(3-hydroxypropyl))",
     "C=1C=CC=C2[N+](=C(C(C21)(C)C)C=CC=C3N(C=4C(C3(C)C)=CC=CC4)CCCO[H:1])CCCO",
     4900, "IDT, 5′ Cy3 (/5Cy3/): ε260 4,900"),
]  # fmt: skip


def from_molfile(entry: dict[str, Any]) -> str:
    """A HELMCoreLibrary monomer from its molfile, with its R atoms (written as aliases on
    carbons, or by M RGP) capped as its rgroups say."""
    lines = entry["molfile"].splitlines()
    labels: dict[int, int] = {}
    for i, line in enumerate(lines):
        if line.startswith("A  ") and re.fullmatch(r"R\d", lines[i + 1].strip()):
            labels[int(line.split()[1]) - 1] = int(lines[i + 1].strip()[1])
        if line.startswith("M  RGP"):
            p = line.split()[3:]
            for a, r in zip(p[0::2], p[1::2], strict=True):
                labels[int(a) - 1] = int(r)
    mol = Chem.MolFromMolBlock(entry["molfile"], sanitize=False, removeHs=False)
    caps = {r["label"]: r["capGroupName"] for r in entry["rgroups"]}
    rw = Chem.RWMol(mol)
    for index, r in labels.items():
        atom = rw.GetAtomWithIdx(index)
        cap = caps[f"R{r}"]
        atom.SetAtomicNum({"H": 1, "OH": 8}[cap])
        atom.SetAtomMapNum(r)
        atom.SetIsotope(0)
        atom.SetNoImplicit(cap == "H")
        atom.SetNumExplicitHs(0)
    out = rw.GetMol()
    Chem.SanitizeMol(out)
    Chem.AssignChiralTypesFromBondDirs(out)
    Chem.AssignStereochemistry(out, cleanIt=True, force=True)
    return Chem.MolToSmiles(out)


def check(smiles: str) -> str:
    """The structure's R-group SMILES, raising if it doesn't read or its caps aren't H or
    OH."""
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise SystemExit(f"Unreadable: {smiles}")
    rw = Chem.RWMol(mol)
    for atom in rw.GetAtoms():
        if atom.GetAtomMapNum():
            if atom.GetSymbol() not in ("H", "O") or atom.GetDegree() != 1:
                raise SystemExit(f"Cap isn't H or OH: {smiles}")
            atom.SetAtomicNum(0)
            atom.SetNoImplicit(True)
            atom.SetNumExplicitHs(0)
    return Chem.MolToSmiles(rw.GetMol())


def main(root: Path) -> None:
    def load(path: str) -> dict[str, dict[str, Any]]:
        found = json.loads((root / path).read_text(encoding="utf-8"))
        return {(m["polymerType"], m["symbol"]): m for m in found}

    lib2 = load("monomerLib2.0 library/monomerLib2.0.json")
    core = load("HELMCoreLibrary/HELMCoreLibrary.json")
    ionis = load("Ionis library/Ionis.json")
    out: list[dict[str, Any]] = []
    for symbol, role, name in FROM_LIB2:
        m = lib2[("RNA", symbol)]
        out.append(
            {
                "polymer_type": "RNA",
                "symbol": symbol,
                "name": name,
                "role": role,
                "natural_analogue": m["naturalAnalog"],
                "smiles": m["smiles"],
                "source": LIB2,
            }
        )
    for symbol, role, name in FROM_CORE:
        m = core[("RNA", symbol)]
        out.append(
            {
                "polymer_type": "RNA",
                "symbol": symbol,
                "name": name,
                "role": role,
                "natural_analogue": m["naturalAnalog"].upper(),
                "smiles": from_molfile(m),
                "source": f"{CORE}, from its molfile",
            }
        )
    for symbol, name, smiles, epsilon, why in FROM_IONIS:
        m = ionis[("CHEM", symbol)]
        out.append(
            {
                "polymer_type": "CHEM",
                "symbol": symbol,
                "name": name,
                "role": "modifier",
                "natural_analogue": None,
                "smiles": smiles or m["smiles"],
                "source": IONIS + (", corrected" if smiles else ""),
                "extinction_260": epsilon,
                "extinction_source": why,
            }
        )
    for symbol, name, smiles, epsilon, why in CURATED:
        out.append(
            {
                "polymer_type": "CHEM",
                "symbol": symbol,
                "name": name,
                "role": "modifier",
                "natural_analogue": None,
                "smiles": smiles,
                "source": "Iki, from the Ionis library's 5Cy5 with one CH=CH fewer",
                "extinction_260": epsilon,
                "extinction_source": why,
            }
        )
    seen: dict[tuple[str, str], str] = {}
    for m in out:
        key = (m["polymer_type"], check(m["smiles"]))
        if key in seen:
            raise SystemExit(f"{m['symbol']} is {seen[key]}'s structure")
        seen[key] = m["symbol"]
    json.dump(
        {
            "about": (
                "The monomers Iki's oligo plugin seeds its registry from. Built by "
                "build_monomers.py; see README.md for sources and corrections."
            ),
            "commit": COMMIT,
            "monomers": out,
        },
        sys.stdout,
        indent=1,
        ensure_ascii=False,
    )
    sys.stdout.write("\n")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
