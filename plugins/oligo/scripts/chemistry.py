"""An oligonucleotide's chemistry, from its HELM and its monomers' structures.

Each monomer is a structure with its attachment points (R1, R2, R3), each capped as its
monomer library caps it: a hydrogen ([H:1]) or a hydroxyl ([OH:3]). HELM says which join:
along a polymer, each backbone monomer's R2 to the next one's R1 (a sugar's 3′ to its
phosphate, the phosphate to the next sugar's 5′); a branch's R1 to the R3 of the backbone
monomer it hangs from (a base to its sugar's C1′); and each connection's two. A joined
attachment point loses both caps, and RDKit's `molzip` makes the bond, keeping each
atom's stereochemistry; one left free keeps its cap.

The molecule is the free acid, as the WHO's INN lists and vendors give it: every
phosphate and phosphorothioate protonated. A cationic dye (Cy3, Cy5) makes it an inner
salt, one phosphate giving up its proton, so the molecule is neutral.

Its average molecular weight is from CIAAW's abridged standard atomic weights (2021),
its monoisotopic mass from each element's most abundant isotope, as RDKit has them.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any

from helm import Helm, HelmError, Node, Polymer, node_of
from rdkit import Chem, RDLogger
from rdkit.Chem import Descriptors, rdCIPLabeler

RDLogger.DisableLog("rdApp.*")

#: The library Iki seeds the registry of monomers from (see data/README.md).
LIBRARY = Path(__file__).resolve().parent.parent / "data" / "monomers.json"

#: CIAAW's abridged standard atomic weights (Prohaska et al., Pure Appl. Chem. 94, 573,
#: 2022; the table at ciaaw.org), for the elements oligonucleotides have.
ATOMIC_WEIGHTS = {
    "H": 1.0080,
    "B": 10.81,
    "C": 12.011,
    "N": 14.007,
    "O": 15.999,
    "F": 18.998,
    "Na": 22.990,
    "P": 30.974,
    "S": 32.06,
    "Cl": 35.45,
    "Br": 79.904,
    "I": 126.90,
}

ROLES = ("sugar", "base", "linker", "modifier")


@dataclass(frozen=True)
class Monomer:
    """A monomer as the registry has it."""

    polymer_type: str
    symbol: str
    smiles: str
    name: str = ""
    role: str = "modifier"
    natural_analogue: str | None = None
    #: Its molar extinction coefficient at 260 nm, L/(mol·cm), for a modifier that has
    #: one curated: a dye's. None when it isn't known.
    extinction_260: float | None = None
    #: Its registry entry's ID, when it's one.
    id: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.polymer_type, self.symbol)

    @property
    def label(self) -> str:
        return f"[{self.symbol}]" if len(self.symbol) > 1 else self.symbol


def library() -> dict[tuple[str, str], Monomer]:
    """The bundled library, by (polymer type, symbol)."""
    found = json.loads(LIBRARY.read_text(encoding="utf-8"))
    out = {}
    for m in found["monomers"]:
        monomer = Monomer(
            polymer_type=m["polymer_type"],
            symbol=m["symbol"],
            smiles=m["smiles"],
            name=m["name"],
            role=m["role"],
            natural_analogue=m.get("natural_analogue"),
            extinction_260=m.get("extinction_260"),
        )
        out[monomer.key] = monomer
    return out


def from_entity(entity: Mapping[str, Any]) -> Monomer:
    """A Monomer registry entry, as a run reads it."""
    v = entity["values"]
    extinction = v.get("extinction_260")
    return Monomer(
        polymer_type=v["polymer_type"],
        symbol=v["symbol"],
        smiles=v["smiles"],
        name=v.get("name") or v["symbol"],
        role=v.get("role") or "modifier",
        natural_analogue=v.get("natural_analogue"),
        extinction_260=extinction["value"] if isinstance(extinction, dict) else extinction,
        id=entity.get("id"),
    )


# --- A monomer's structure ---------------------------------------------------------------


@dataclass(frozen=True)
class Structure:
    """A monomer's structure read: the molecule, and each attachment point's cap atom."""

    mol: Chem.Mol
    #: R number to the index of its cap atom.
    caps: dict[int, int]
    #: R number to its cap: "H" or "OH".
    cap_groups: dict[int, str]


_CAP = re.compile(r"\[(\*|H|OH):(\d+)\]")


def structure(smiles: str) -> Structure:
    """A monomer's SMILES, with each attachment point's cap atom mapped ([H:1], [OH:3]),
    read. Raises ValueError saying what's wrong."""
    if "*" in smiles:
        raise ValueError(
            "its attachment points must be capped as they are when free, [H:1] for a "
            "hydrogen or [OH:1] for a hydroxyl, not drawn as [*:1]"
        )
    mol = Chem.MolFromSmiles(smiles, sanitize=False)
    if mol is None:
        raise ValueError("its SMILES can't be read")
    try:
        Chem.SanitizeMol(mol)
    except Exception as error:
        raise ValueError(f"its SMILES isn't a valid structure: {error}") from None
    caps: dict[int, int] = {}
    groups: dict[int, str] = {}
    for atom in mol.GetAtoms():
        n = atom.GetAtomMapNum()
        if not n:
            continue
        if n in caps:
            raise ValueError(f"it has two R{n}")
        if atom.GetDegree() != 1:
            raise ValueError(f"its R{n} cap is bonded to {atom.GetDegree()} atoms, not one")
        if atom.GetSymbol() == "H":
            groups[n] = "H"
        elif atom.GetSymbol() == "O" and atom.GetTotalNumHs() == 1:
            groups[n] = "OH"
        else:
            raise ValueError(f"its R{n} cap is {atom.GetSymbol()}: Iki reads H and OH caps")
        caps[n] = atom.GetIdx()
    if not caps:
        raise ValueError("it has no attachment points: map each cap atom, as [H:1]")
    if sorted(caps) != list(range(1, len(caps) + 1)):
        raise ValueError("its attachment points must be R1, R2 and so on, with none missed")
    return Structure(mol, caps, groups)


def rgroup_smiles(smiles: str) -> str:
    """A monomer's structure, as RDKit's canonical SMILES with each attachment point as
    [*:n]: the same for the same structure however it's drawn."""
    s = structure(smiles)
    rw = Chem.RWMol(s.mol)
    for n, index in s.caps.items():
        atom = rw.GetAtomWithIdx(index)
        atom.SetAtomicNum(0)
        atom.SetNoImplicit(True)
        atom.SetNumExplicitHs(0)
        atom.SetFormalCharge(0)
        atom.SetAtomMapNum(n)
    mol = rw.GetMol()
    Chem.SanitizeMol(mol)
    return Chem.MolToSmiles(mol)


def structure_key(m: Monomer) -> str:
    """The SHA-256 of a monomer's polymer type and R-group SMILES: one structure's
    however it's drawn."""
    text = f"{m.polymer_type}:{rgroup_smiles(m.smiles)}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def free(smiles: str) -> Chem.Mol:
    """A monomer with its caps, as it is unjoined."""
    mol = Chem.Mol(structure(smiles).mol)
    for atom in mol.GetAtoms():
        atom.SetAtomMapNum(0)
    return Chem.RemoveHs(mol)


# --- Building the molecule -----------------------------------------------------------------


@dataclass
class Built:
    """An oligonucleotide built: its molecule, and what each monomer of its HELM is."""

    mol: Chem.Mol
    helm: Helm
    monomers: dict[tuple[str, str], Monomer]

    def of(self, polymer: Polymer, node: Node) -> Monomer:
        return self.monomers[(polymer.type, node.id)]


class MissingError(HelmError):
    """Monomers its HELM names that aren't known: their (polymer type, symbol)."""

    def __init__(self, missing: list[tuple[str, str]]) -> None:
        self.missing = missing
        names = ", ".join(f"{t} [{s}]" for t, s in missing)
        super().__init__(f"Its HELM names monomers the registry hasn't got: {names}.")


def bonds(helm: Helm) -> list[tuple[tuple[str, int], int, tuple[str, int], int]]:
    """Each bond HELM makes: (polymer, node index) and R number at each end."""
    out = []
    for p in helm.polymers:
        backbone = [n for n in p.nodes if n.branch_of is None]
        out.extend(
            ((p.id, before.index), 2, (p.id, after.index), 1)
            for before, after in pairwise(backbone)
        )
        out.extend(
            ((p.id, n.branch_of), 3, (p.id, n.index), 1) for n in p.nodes if n.branch_of is not None
        )
    for c in helm.connections:
        if c.pair:
            continue  # hydrogen bonds: no atoms join
        a_polymer, a = node_of(helm, c.one)
        b_polymer, b = node_of(helm, c.other)
        out.append(
            (
                (a_polymer.id, a.index),
                int(c.one.attachment[1:]),
                (b_polymer.id, b.index),
                int(c.other.attachment[1:]),
            )
        )
    return out


def build(helm: Helm, monomers: Mapping[tuple[str, str], Monomer]) -> Built:
    """The molecule ``helm`` is, from its monomers' structures. Raises HelmError saying
    what can't be built."""
    missing = sorted(
        {(p.type, n.id) for p in helm.polymers for n in p.nodes} - set(monomers),
    )
    if missing:
        raise MissingError(missing)
    pieces: dict[tuple[str, int], Structure] = {}
    for p in helm.polymers:
        for n in p.nodes:
            m = monomers[(p.type, n.id)]
            try:
                pieces[(p.id, n.index)] = structure(m.smiles)
            except ValueError as why:
                raise HelmError(
                    f"The monomer {m.label}'s structure can't be used: {why}."
                ) from None
    used: set[tuple[tuple[str, int], int]] = set()
    labels: dict[tuple[tuple[str, int], int], int] = {}
    for label, (a, ra, b, rb) in enumerate(bonds(helm), start=1):
        for piece, r in ((a, ra), (b, rb)):
            m = monomers[(_type(helm, piece[0]), _node(helm, piece).id)]
            if r not in pieces[piece].caps:
                raise HelmError(
                    f"{piece[0]}'s monomer {piece[1] + 1}, {m.label}, has no R{r} to join: "
                    f"it has {', '.join(f'R{x}' for x in sorted(pieces[piece].caps))}."
                )
            if (piece, r) in used:
                raise HelmError(
                    f"{piece[0]}'s monomer {piece[1] + 1}, {m.label}, joins two bonds at R{r}."
                )
            used.add((piece, r))
            labels[(piece, r)] = label
    combined: Chem.Mol | None = None
    for piece, s in pieces.items():
        rw = Chem.RWMol(s.mol)
        for r, index in s.caps.items():
            atom = rw.GetAtomWithIdx(index)
            if (piece, r) in labels:
                # The cap's place becomes the bond's: its atom, and so its neighbours'
                # order, which their stereochemistry is read by, stay as they were.
                atom.SetAtomicNum(0)
                atom.SetNoImplicit(True)
                atom.SetNumExplicitHs(0)
                atom.SetFormalCharge(0)
                atom.SetAtomMapNum(labels[(piece, r)])
            else:
                atom.SetAtomMapNum(0)
        mol = rw.GetMol()
        combined = mol if combined is None else Chem.CombineMols(combined, mol)
    assert combined is not None  # noqa: S101 - HELM has a polymer
    zipped = Chem.molzip(combined)
    zipped = Chem.RemoveHs(zipped)
    Chem.SanitizeMol(zipped)
    return Built(_inner_salt(zipped), helm, dict(monomers))


def _type(helm: Helm, polymer: str) -> str:
    return helm.polymer(polymer).type


def _node(helm: Helm, piece: tuple[str, int]) -> Node:
    return helm.polymer(piece[0]).nodes[piece[1]]


_ACID = Chem.MolFromSmarts("[OX2H1,SX2H1][P]")


def _inner_salt(mol: Chem.Mol) -> Chem.Mol:
    """A cationic molecule with its charge taken by its phosphates' protons, one each."""
    charge = Chem.GetFormalCharge(mol)
    if charge <= 0:
        return mol
    rw = Chem.RWMol(mol)
    for match in mol.GetSubstructMatches(_ACID)[:charge]:
        atom = rw.GetAtomWithIdx(match[0])
        atom.SetFormalCharge(-1)
        atom.SetNumExplicitHs(0)
        atom.SetNoImplicit(True)
    out = rw.GetMol()
    Chem.SanitizeMol(out)
    return out


# --- What's computed --------------------------------------------------------------------


def formula(mol: Chem.Mol) -> str:
    """Its formula in Hill order: carbon, hydrogen, then the rest alphabetically."""
    counts = atom_counts(mol)
    order = [e for e in ("C", "H") if e in counts] + sorted(e for e in counts if e not in "CH")
    text = "".join(f"{e}{counts[e] if counts[e] > 1 else ''}" for e in order)
    charge = Chem.GetFormalCharge(mol)
    if charge:
        text += f"{'+' if charge > 0 else '-'}{abs(charge) if abs(charge) > 1 else ''}"
    return text


def atom_counts(mol: Chem.Mol) -> Counter[str]:
    counts: Counter[str] = Counter()
    for atom in mol.GetAtoms():
        counts[atom.GetSymbol()] += 1
        counts["H"] += atom.GetTotalNumHs()
    return counts


def average_weight(mol: Chem.Mol) -> float:
    """Its average molecular weight, g/mol, from CIAAW's abridged atomic weights."""
    counts = atom_counts(mol)
    unknown = sorted(set(counts) - set(ATOMIC_WEIGHTS))
    if unknown:
        raise HelmError(f"It has {', '.join(unknown)}, whose atomic weight Iki doesn't hold.")
    return sum(ATOMIC_WEIGHTS[e] * n for e, n in counts.items())


def monoisotopic_mass(mol: Chem.Mol) -> float:
    return float(Descriptors.ExactMolWt(mol))


# --- Nucleotides --------------------------------------------------------------------------


@dataclass
class Nucleotide:
    """A unit of an RNA polymer: its sugar, base and linker, or a modifier in the chain."""

    position: int  # from 1, 5′ to 3′
    sugar: Monomer | None = None
    base: Monomer | None = None
    linker: Monomer | None = None
    others: list[Monomer] = field(default_factory=list)

    def letter(self) -> str:
        """Its base's natural analogue's letter, or N if it hasn't one."""
        if self.base is None:
            return ""
        analogue = (self.base.natural_analogue or "").upper()
        return analogue if analogue in ("A", "C", "G", "T", "U") else "N"


def strand_polymer(helm: Helm) -> Polymer:
    """The oligonucleotide's one strand: its one RNA polymer."""
    strands = [p for p in helm.polymers if p.type == "RNA"]
    if len(strands) != 1:
        raise HelmError(
            "An oligonucleotide is one strand: its HELM has "
            f"{len(strands)} RNA polymers. Register each strand on its own, and a duplex "
            "of the two."
        )
    other = [p.id for p in helm.polymers if p.type not in ("RNA", "CHEM")]
    if other:
        raise HelmError(
            f"It has {', '.join(other)}: an oligonucleotide's HELM has its strand and the "
            "chemical groups on it (CHEM). A peptide conjugate is a conjugate of the two."
        )
    return strands[0]


def nucleotides(built: Built) -> list[Nucleotide]:
    p = strand_polymer(built.helm)
    units: dict[int, Nucleotide] = {}
    for n in p.nodes:
        unit = units.setdefault(n.unit, Nucleotide(n.unit + 1))
        m = built.of(p, n)
        slot = m.role if m.role in ("sugar", "base", "linker") else None
        if slot and getattr(unit, slot) is None:
            setattr(unit, slot, m)
        else:
            unit.others.append(m)
    return [units[k] for k in sorted(units)]


def ends(built: Built) -> dict[str, list[Monomer]]:
    """The chemical groups (CHEM) on the strand, by where they're joined: 5′, 3′, or
    internal."""
    p = strand_polymer(built.helm)
    first, last = p.nodes[0].index if p.nodes else 0, max((n.index for n in p.nodes), default=0)
    backbone = [n.index for n in p.nodes if n.branch_of is None]
    out: dict[str, list[Monomer]] = {"5′": [], "3′": [], "internal": []}
    for c in built.helm.connections:
        if c.pair:
            continue
        for mine, theirs in ((c.one, c.other), (c.other, c.one)):
            if mine.polymer != p.id or not theirs.polymer.startswith("CHEM"):
                continue
            chem_polymer, chem_node = node_of(built.helm, theirs)
            m = built.of(chem_polymer, chem_node)
            _, node = node_of(built.helm, mine)
            if node.index == (backbone[0] if backbone else first) and mine.attachment == "R1":
                out["5′"].append(m)
            elif node.index == (backbone[-1] if backbone else last) and mine.attachment == "R2":
                out["3′"].append(m)
            else:
                out["internal"].append(m)
    return out


def bases(built: Built) -> str:
    """Its bases' natural analogues, 5′ to 3′: what a base sequence search reads."""
    return "".join(n.letter() for n in nucleotides(built))


# Natural nucleotides, by their structure, so a lab's own copy of one is read as it.
_NATURAL: dict[str, tuple[str, str]] = {}


def _natural() -> dict[str, tuple[str, str]]:
    """Each natural monomer's R-group SMILES: what it is, and DNA or RNA for a sugar."""
    if not _NATURAL:
        lib = library()
        for symbol, what in {
            "dR": ("sugar", "DNA"),
            "R": ("sugar", "RNA"),
            "P": ("linker", "phosphate"),
            "sP": ("linker", "phosphorothioate"),
            "A": ("base", "A"),
            "C": ("base", "C"),
            "G": ("base", "G"),
            "T": ("base", "T"),
            "U": ("base", "U"),
        }.items():
            _NATURAL[rgroup_smiles(lib[("RNA", symbol)].smiles)] = what
    return _NATURAL


def natural(m: Monomer | None) -> tuple[str, str] | None:
    if m is None:
        return None
    try:
        return _natural().get(rgroup_smiles(m.smiles))
    except ValueError:
        return None


def modified(m: Monomer | None) -> bool:
    """Whether it isn't natural DNA or RNA: a phosphorothioate is a modification, though
    nearest-neighbour parameters read it as the phosphate it replaces."""
    found = natural(m)
    return m is not None and (found is None or found[1] == "phosphorothioate")


def modifications(built: Built) -> list[str]:
    """What isn't natural DNA or RNA, by monomer, with how many: "2′-O-methoxyethyl ribose
    (18)", "5′ 6-FAM"."""
    counts: Counter[str] = Counter()
    for n in nucleotides(built):
        for m in [n.sugar, n.base, n.linker, *n.others]:
            if modified(m):
                counts[m.name] += 1
    out = [f"{name} ({count})" for name, count in sorted(counts.items())]
    for where, groups in ends(built).items():
        out.extend(f"{where} {m.name}" if where != "internal" else m.name for m in groups)
    return out


# --- Extinction at 260 nm ------------------------------------------------------------------

#: Nearest-neighbour molar extinction coefficients at 260 nm, L/(mol·cm), for the whole
#: doublet 5′→3′: single-stranded DNA from Cantor, Warshaw & Shapiro (Biopolymers 9, 1059,
#: 1970), and RNA from Puglisi & Tinoco (Methods Enzymol. 180, 304, 1989), as tabulated
#: per doublet (twice their per-nucleotide values).
DOUBLETS = {
    "DNA": {
        "AA": 27400, "AC": 21200, "AG": 25000, "AT": 22800,
        "CA": 21200, "CC": 14600, "CG": 18000, "CT": 15200,
        "GA": 25200, "GC": 17600, "GG": 21600, "GT": 20000,
        "TA": 23400, "TC": 16200, "TG": 19000, "TT": 16800,
    },
    "RNA": {
        "AA": 27400, "AC": 21000, "AG": 25000, "AU": 24000,
        "CA": 21000, "CC": 14200, "CG": 17800, "CU": 16200,
        "GA": 25200, "GC": 17400, "GG": 21600, "GU": 21200,
        "UA": 24600, "UC": 17200, "UG": 20000, "UU": 19600,
    },
}  # fmt: skip
#: Each nucleotide's own, from the same sources.
SINGLES = {
    "DNA": {"A": 15400, "C": 7400, "G": 11500, "T": 8700},
    "RNA": {"A": 15400, "C": 7200, "G": 11500, "U": 9900},
}


def nearest_neighbour(sequence: str, kind: str) -> int:
    """ε260 of a single strand of natural DNA or RNA by nearest neighbours: its doublets'
    sum less its inner nucleotides' (Cantor, Warshaw & Shapiro, 1970)."""
    if not sequence:
        return 0
    if len(sequence) == 1:
        return SINGLES[kind][sequence]
    pairs = sum(DOUBLETS[kind][sequence[i : i + 2]] for i in range(len(sequence) - 1))
    return pairs - sum(SINGLES[kind][c] for c in sequence[1:-1])


@dataclass(frozen=True)
class Extinction:
    value: int | None
    note: str


def extinction(built: Built) -> Extinction:
    """ε260 by nearest neighbours where its parameters describe the strand: natural DNA
    or natural RNA, with phosphates or phosphorothioates, and chemical groups whose own
    ε260 is known. Otherwise none, saying what the parameters don't describe."""
    units = nucleotides(built)
    kinds: set[str] = set()
    letters = []
    unknown: Counter[str] = Counter()
    thio = 0
    added = 0.0
    for n in units:
        sugar, base, linker = natural(n.sugar), natural(n.base), natural(n.linker)
        if n.sugar is None or n.base is None:
            # A unit that isn't a nucleotide: a 5′ phosphate, or a modifier in the chain.
            for m in [n.sugar, n.base, n.linker, *n.others]:
                if m is not None and natural(m) is None:
                    unknown[m.name] += 1
            continue
        if sugar is None:
            unknown[n.sugar.name] += 1
        else:
            kinds.add(sugar[1])
        if base is None:
            unknown[n.base.name] += 1
        else:
            letters.append(base[1])
        if n.linker is not None:
            if linker is None:
                unknown[n.linker.name] += 1
            elif linker[1] == "phosphorothioate":
                thio += 1
        for m in n.others:
            unknown[m.name] += 1
    for groups in ends(built).values():
        for m in groups:
            if m.extinction_260 is None:
                unknown[m.name] += 1
            else:
                added += m.extinction_260
    for p in built.helm.polymers:
        if p.type == "CHEM" and not any(
            c for c in built.helm.connections if p.id in (c.one.polymer, c.other.polymer)
        ):
            unknown[f"{p.id}, not joined to the strand"] += 1
    if len(kinds) > 1:
        return Extinction(
            None,
            "Not computed: it mixes DNA and RNA nucleotides, and nearest-neighbour "
            "parameters describe only one or the other.",
        )
    if unknown:
        named = ", ".join(f"{name} ({count})" for name, count in sorted(unknown.items()))
        return Extinction(
            None,
            "Not computed: no nearest-neighbour parameters describe "
            f"{named}. Measure it, or use the extinction coefficient its supplier gives.",
        )
    kind = kinds.pop() if kinds else "DNA"
    sequence = "".join(letters)
    if any(c not in SINGLES[kind] for c in sequence):
        wrong = "U" if kind == "DNA" else "T"
        return Extinction(
            None,
            f"Not computed: it has {wrong} on {kind} sugars, which the "
            f"{kind} parameters don't describe.",
        )
    value = nearest_neighbour(sequence, kind) + round(added)
    source = "Cantor, Warshaw & Shapiro (1970)" if kind == "DNA" else "Puglisi & Tinoco (1989)"
    note = f"Single strand, by nearest neighbours: {kind} parameters from {source}."
    if added:
        note += " Its dyes' own ε260 is added, as their suppliers give it."
    if thio:
        note += (
            f" Its {thio} phosphorothioate links are read as phosphates: the parameters "
            "were measured on phosphodiesters."
        )
    return Extinction(value, note)


# --- Positions, for the record page ------------------------------------------------------


def _brief(m: Monomer | None) -> dict[str, Any] | None:
    if m is None:
        return None
    return {"symbol": m.symbol, "name": m.name, "modified": modified(m)}


def positions(built: Built) -> dict[str, Any]:
    """Each nucleotide's sugar, base and linker, 5′ to 3′, and the groups at each end:
    what the record page draws."""
    return {
        "nucleotides": [
            {
                "position": n.position,
                "base": n.letter() or None,
                "sugar": _brief(n.sugar),
                "nucleobase": _brief(n.base),
                "linker": _brief(n.linker),
                "others": [_brief(m) for m in n.others],
            }
            for n in nucleotides(built)
        ],
        "ends": {
            where: [_brief(m) for m in groups] for where, groups in ends(built).items() if groups
        },
    }


def stereo_labels(mol: Chem.Mol) -> list[str]:
    """Each phosphorus's CIP label, by RDKit's new labeler (Hanson et al., 2018): R, S, or
    empty where it isn't defined."""
    labelled = Chem.Mol(mol)
    rdCIPLabeler.AssignCIPLabels(labelled)
    return [
        a.GetProp("_CIPCode") if a.HasProp("_CIPCode") else ""
        for a in labelled.GetAtoms()
        if a.GetSymbol() == "P"
    ]


def unique_keys(helm: Helm) -> list[tuple[str, str]]:
    """Each monomer its HELM names, by (polymer type, symbol), in the order it first
    names them."""
    seen: dict[tuple[str, str], None] = {}
    for p in helm.polymers:
        for n in p.nodes:
            seen.setdefault((p.type, n.id), None)
    return list(seen)


_ELEMENT = re.compile(r"([A-Z][a-z]?)(\d*)")


def add_formulas(*formulas: str) -> str:
    """Formulas added, in Hill order: two strands' as their duplex's."""
    counts: Counter[str] = Counter()
    for f in formulas:
        if not re.fullmatch(r"(?:[A-Z][a-z]?\d*)+", f):
            raise ValueError(f"{f} isn't a neutral formula")
        for element, n in _ELEMENT.findall(f):
            counts[element] += int(n or 1)
    order = [e for e in ("C", "H") if e in counts] + sorted(e for e in counts if e not in "CH")
    return "".join(f"{e}{counts[e] if counts[e] > 1 else ''}" for e in order)
