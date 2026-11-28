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
its monoisotopic mass from each element's most abundant isotope, as RDKit has them. An
atom a structure labels with its isotope ([13CH], [2H]) weighs that nuclide's mass, as
RDKit has it from AME2020, in both, and is written as itself in its formula:
C4[13C]5H12N2O6.
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
from weights import AVERAGE

RDLogger.DisableLog("rdApp.*")  # type: ignore[attr-defined]

#: The library Iki seeds the registry of monomers from (see data/README.md).
LIBRARY = Path(__file__).resolve().parent.parent / "data" / "monomers.json"


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


def rgroup_smiles(smiles: str, *, isomeric: bool = True) -> str:
    """A monomer's structure, as RDKit's canonical SMILES with each attachment point as
    [*:n]: the same for the same structure however it's drawn. Without its stereochemistry
    if not ``isomeric``."""
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
    return Chem.MolToSmiles(mol, isomericSmiles=isomeric)


def structure_key(m: Monomer) -> str:
    """The SHA-256 of a monomer's polymer type, R-group SMILES and each attachment
    point's cap: one structure's however it's drawn. The caps are in it because a free
    one stays on the molecule (HELM 2.04, 5.2.1.1): a methyl capped H is methane, and
    capped OH methanol."""
    caps = ",".join(f"R{n}={cap}" for n, cap in sorted(structure(m.smiles).cap_groups.items()))
    text = f"{m.polymer_type}:{rgroup_smiles(m.smiles)}:{caps}"
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
    out: list[tuple[tuple[str, int], int, tuple[str, int], int]] = []
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
#: What a sodium salt weighs more than the free acid for each sodium in place of a proton.
SODIUM_FOR_PROTON = AVERAGE["Na"] - AVERAGE["H"]


def acidic_protons(mol: Chem.Mol) -> int:
    """Its phosphates' and phosphorothioates' acidic protons, which its sodium salt has a
    sodium for each of: one for each linkage, two for a terminal phosphate, none for a
    methylphosphonate (nor a morpholino's phosphorodiamidate), and one fewer for each a
    cationic dye took to make it an inner salt."""
    return len(mol.GetSubstructMatches(_ACID, maxMatches=100_000))


def sodium_salt_weight(mol: Chem.Mol) -> float | None:
    """Its average molecular weight as its sodium salt, as marketed oligonucleotides are
    (nusinersen sodium, patisiran sodium): a sodium for each acidic proton. None for a
    molecule with none, which isn't a salt."""
    n = acidic_protons(mol)
    return average_weight(mol) + n * SODIUM_FOR_PROTON if n else None


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


def _token(atom: Chem.Atom) -> str:
    """An atom as its formula writes it: its element, or its isotope if it's labelled."""
    isotope = atom.GetIsotope()
    return f"[{isotope}{atom.GetSymbol()}]" if isotope else atom.GetSymbol()


_TOKEN = re.compile(r"\[(\d+)([A-Z][a-z]?)\]|([A-Z][a-z]?)")


def _element(token: str) -> tuple[str, int]:
    """A formula's token's element, and its isotope's mass number (0 if natural)."""
    m = _TOKEN.fullmatch(token)
    assert m is not None  # noqa: S101 - only tokens _token wrote
    return (m.group(2), int(m.group(1))) if m.group(1) else (m.group(3), 0)


def hill(counts: Counter[str]) -> str:
    """Counts in Hill order: carbon, hydrogen, then the rest alphabetically, each labelled
    isotope after its element's natural atoms (C4[13C]5)."""

    def order(token: str) -> tuple[int, str, int]:
        element, isotope = _element(token)
        return ({"C": 0, "H": 1}.get(element, 2), element, isotope)

    return "".join(
        f"{t}{counts[t] if counts[t] > 1 else ''}" for t in sorted(counts, key=order) if counts[t]
    )


def formula(mol: Chem.Mol) -> str:
    """Its formula in Hill order: carbon, hydrogen, then the rest alphabetically."""
    text = hill(atom_counts(mol))
    charge = Chem.GetFormalCharge(mol)
    if charge:
        text += f"{'+' if charge > 0 else '-'}{abs(charge) if abs(charge) > 1 else ''}"
    return text


def atom_counts(mol: Chem.Mol) -> Counter[str]:
    """Its atoms by element, or isotope where one is labelled, hydrogens included."""
    counts: Counter[str] = Counter()
    for atom in mol.GetAtoms():
        counts[_token(atom)] += 1
        counts["H"] += atom.GetTotalNumHs()
    return counts


def average_weight(mol: Chem.Mol) -> float:
    """Its average molecular weight, g/mol, from CIAAW's standard atomic weights (the table
    every Iki plugin weighs by, weights.py), and each labelled isotope's nuclide mass."""
    table = Chem.GetPeriodicTable()
    terms: list[float] = []
    unknown = set()
    for token, n in atom_counts(mol).items():
        element, isotope = _element(token)
        if isotope:
            terms.append(table.GetMassForIsotope(element, isotope) * n)
        elif element in AVERAGE:
            terms.append(AVERAGE[element] * n)
        else:
            unknown.add(element)
    if unknown:
        named = ", ".join(sorted(unknown))
        raise HelmError(
            f"It has {named}, which has no standard atomic weight: label it with its isotope."
        )
    return sum(terms)


def monoisotopic_mass(mol: Chem.Mol) -> float:
    return float(Descriptors.ExactMolWt(mol))  # type: ignore[attr-defined]


# --- Nucleotides --------------------------------------------------------------------------


@dataclass
class Nucleotide:
    """A unit of an RNA polymer: its sugar, base and linker, or a modifier in the chain."""

    position: int  # from 1, 5′ to 3′
    sugar: Monomer | None = None
    base: Monomer | None = None
    linker: Monomer | None = None
    others: list[Monomer] = field(default_factory=list)

    @property
    def nucleotide(self) -> bool:
        """It's a nucleotide: it has a sugar. One without a base is an abasic site; a unit
        without a sugar, such as a 5′ phosphate, isn't a nucleotide."""
        return self.sugar is not None

    def letter(self) -> str:
        """Its base's natural analogue's letter, N if it hasn't one, - for an abasic site,
        and nothing for a unit that isn't a nucleotide."""
        if self.base is None:
            return "-" if self.nucleotide else ""
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


def check_joined(helm: Helm) -> None:
    """Every polymer is bonded to the strand, directly or through others: an
    oligonucleotide is one molecule. Raises HelmError naming one that isn't."""
    strand = strand_polymer(helm).id
    reached, edges = {strand}, [c for c in helm.connections if not c.pair]
    grew = True
    while grew:
        grew = False
        for c in edges:
            ends = {c.one.polymer, c.other.polymer}
            if ends & reached and not ends <= reached:
                reached |= ends
                grew = True
    apart = [p.id for p in helm.polymers if p.id not in reached]
    if apart:
        raise HelmError(
            f"{_and(apart)} {'is' if len(apart) == 1 else 'are'}n't joined to the strand: an "
            "oligonucleotide is one molecule, so connect each chemical group to it, as "
            f"{strand},{apart[0]},1:R1-1:R1 joins one to its 5′ end."
        )


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def ring(helm: Helm) -> bool:
    """The strand is bonded to itself: a ring, such as a cyclic oligonucleotide."""
    strand = strand_polymer(helm).id
    return any(not c.pair and c.one.polymer == c.other.polymer == strand for c in helm.connections)


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
#: Its linkers, by their R-group SMILES without stereochemistry: a phosphorothioate is one
#: whether its phosphorus is Rp, Sp or undefined.
_LINKERS: dict[str, tuple[str, str]] = {}


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
            smiles = lib[("RNA", symbol)].smiles
            _NATURAL[rgroup_smiles(smiles)] = what
            if what[0] == "linker":
                _LINKERS[rgroup_smiles(smiles, isomeric=False)] = what
    return _NATURAL


def natural(m: Monomer | None) -> tuple[str, str] | None:
    if m is None:
        return None
    try:
        found = _natural().get(rgroup_smiles(m.smiles))
        if found is None and m.role == "linker":
            found = _LINKERS.get(rgroup_smiles(m.smiles, isomeric=False))
    except ValueError:
        return None
    return found


def modified(m: Monomer | None) -> bool:
    """Whether it isn't natural DNA or RNA: a phosphorothioate is a modification, though
    nearest-neighbour parameters read it as the phosphate it replaces."""
    found = natural(m)
    return m is not None and (found is None or found[1] == "phosphorothioate")


#: What a nucleotide without a base is.
ABASIC = "Abasic site"


def modifications(built: Built) -> list[str]:
    """What isn't natural DNA or RNA, by monomer, with how many: "2′-O-methoxyethyl ribose
    (18)", "5′ 6-FAM"."""
    counts: Counter[str] = Counter()
    for n in nucleotides(built):
        if n.nucleotide and n.base is None:
            counts[ABASIC] += 1
        for m in [n.sugar, n.base, n.linker, *n.others]:
            if m is not None and modified(m):
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
    if ring(built.helm):
        return Extinction(
            None,
            "Not computed: its strand is joined to itself, a ring, and nearest-neighbour "
            "parameters describe a linear strand, with two ends.",
        )
    for n in units:
        sugar, base, linker = natural(n.sugar), natural(n.base), natural(n.linker)
        if n.nucleotide and n.base is None:
            # Its neighbours aren't neighbours: no doublet describes them.
            unknown[f"an {ABASIC.lower()}"] += 1
        if n.sugar is None or n.base is None:
            # A unit that isn't a nucleotide (a 5′ phosphate, or a modifier in the chain),
            # or an abasic site.
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
    """Each nucleotide's sugar, base and linker, 5′ to 3′, numbered from 1, and the groups
    at each end: what the record page draws. A unit that isn't a nucleotide, such as a 5′
    phosphate, is drawn at the end it's at, or with the nucleotide before it."""
    units = nucleotides(built)
    chemical = ends(built)
    five: list[Monomer] = list(chemical["5′"])
    three: list[Monomer] = []
    drawn: list[dict[str, Any]] = []
    for n in units:
        parts = [m for m in (n.sugar, n.base, n.linker, *n.others) if m is not None]
        if n.nucleotide:
            drawn.append(
                {
                    "position": len(drawn) + 1,
                    "base": n.letter() or None,
                    "sugar": _brief(n.sugar),
                    "nucleobase": _brief(n.base),
                    "linker": _brief(n.linker),
                    "others": [_brief(m) for m in n.others],
                }
            )
            three = []
        elif not drawn:
            five.extend(parts)
        else:
            drawn[-1]["others"].extend(_brief(m) for m in parts)
            three.extend(parts)
    # A unit after the last nucleotide is at the 3′ end, not with it.
    if three:
        drawn[-1]["others"] = drawn[-1]["others"][: -len(three)]
    groups = {"5′": five, "3′": [*three, *chemical["3′"]], "internal": chemical["internal"]}
    return {
        "nucleotides": drawn,
        "ends": {where: [_brief(m) for m in found] for where, found in groups.items() if found},
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


_COUNTED = re.compile(r"(\[\d+[A-Z][a-z]?\]|[A-Z][a-z]?)(\d*)")


def add_formulas(*formulas: str) -> str:
    """Formulas added, in Hill order: two strands' as their duplex's, each labelled
    isotope kept as itself."""
    counts: Counter[str] = Counter()
    for f in formulas:
        if not re.fullmatch(r"(?:(?:\[\d+[A-Z][a-z]?\]|[A-Z][a-z]?)\d*)+", f):
            raise ValueError(f"{f} isn't a neutral formula")
        for token, n in _COUNTED.findall(f):
            counts[token] += int(n or 1)
    return hill(counts)
