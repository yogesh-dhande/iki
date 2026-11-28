"""The small_molecule plugin's chemistry, which its actions share: reading structures,
standardising them, identifying them, their descriptors, salts, and writing a run's output.

Standardising is RDKit's MolStandardize, as `rdMolStandardize.Cleanup` does it: hydrogens
made implicit, bonds to metals broken (the metal a cation, the atom it was bonded to an
anion), functional groups drawn in their usual form (nitro as N(=O)=O charge-separated,
and so on), and charges moved to the most acidic sites. A compound is that structure,
whole, unless the run asks for its parent: then fragments that are known salts or
solvents (`SALTS`, `SOLVENTS`) are taken off, of what's left the largest fragment is
kept (RDKit's LargestFragmentChooser), and it's neutralised where it can be (RDKit's
Uncharger). What was taken off is the batch's salt form. A salt or solvent is known by
its constitution, whatever configuration its stereocentres are drawn with: (S)-malate is
malate, and D-, L- and meso-tartrate are each tartrate. A part that's neither is left
out, and a batch of it has no formula weight, since what it adds isn't known.

A compound's identity is its standard InChIKey, from the InChI Trust's library, which
RDKit's wheels carry, so the same structure drawn another way is the same compound.

Its Molfile is its structure laid out in 2D (`molfile`), which Iki writes its Molfile and
SDF exports from (ADR 0229). Layouts run in a process of their own (`Layouts`), so one
that takes too long or too much memory is given up on, and the rest go on.
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import os
import re
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from rdkit import Chem, rdBase
from rdkit.Chem import QED, Crippen, Descriptors, rdDepictor, rdinchi
from rdkit.Chem import rdMolDescriptors as Calc
from rdkit.Chem.MolStandardize import rdMolStandardize
from weights import AVERAGE

if TYPE_CHECKING:
    from collections.abc import Iterator
    from multiprocessing.connection import Connection
    from multiprocessing.process import BaseProcess

#: As the fields take them.
LIMITS = {"name": 255, "description": 10_000, "smiles": 20_000, "inchi": 20_000}
MAX_SYNONYMS = 200
INCHIKEY = re.compile(r"^[A-Z]{14}-[A-Z]{8}SA-[A-Z]$")
CAS = re.compile(r"^([1-9][0-9]{1,6})-([0-9]{2})-([0-9])$")
#: Up to these many (and bytes of them), creates go in output.json; past them, in a file.
INLINE_CREATES = 1_000
INLINE_BYTES = 1_000_000
#: output.json's own limits (run-output/v1).
MAX_CREATES = 10_000
MAX_OUTPUT_BYTES = 2_500_000
CREATES_FILE = "creates.ndjson"
MAX_MESSAGES = 99


class UnreadableError(Exception):
    """A structure, line or record that can't be registered, and why."""


# --- RDKit's messages ----------------------------------------------------------------------------


class _Collected(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        text = re.sub(r"^\[\d\d:\d\d:\d\d\] ", "", record.getMessage()).strip()
        if text:
            self.messages.append(text)


_LOG = logging.getLogger("rdkit")
_LOG.propagate = False
_LOG.setLevel(logging.WARNING)
rdBase.LogToPythonLogger()


@contextmanager
def rdkit_messages() -> Iterator[list[str]]:
    """What RDKit says while the block runs, instead of printing it."""
    handler = _Collected()
    _LOG.addHandler(handler)
    try:
        yield handler.messages
    finally:
        _LOG.removeHandler(handler)


def why_not(messages: list[str], fallback: str) -> str:
    """RDKit's first message about what went wrong, without its echo of the input."""
    for message in messages:
        if message.startswith("SMILES Parse Error: check for mistakes"):
            return message.removeprefix("SMILES Parse Error: ")
    for message in messages:
        cut = re.sub(r" for input: '.*'$", "", message).removeprefix("SMILES Parse Error: ")
        if cut and not cut.startswith("Failed parsing SMILES"):
            return cut
    return fallback


def from_smiles(smiles: str) -> Chem.Mol:
    """The molecule a SMILES gives, sanitised, or an UnreadableError that says why."""
    if len(smiles) > LIMITS["smiles"]:
        raise UnreadableError(f"its SMILES is over {LIMITS['smiles']:,} characters")
    with rdkit_messages() as said:
        mol = Chem.MolFromSmiles(smiles, sanitize=False)
        if mol is None:
            raise UnreadableError(
                f"{smiles!r} isn't SMILES RDKit can read: {why_not(said, 'syntax')}"
            )
        try:
            Chem.SanitizeMol(mol)
        except Exception as error:  # RDKit raises its own kinds, each saying why
            raise UnreadableError(f"{smiles!r} isn't a valid structure: {error}") from None
        # As MolFromSmiles does when it sanitises: stereo that the structure can't have
        # (a "centre" with two of the same neighbour) is cleared, so InChI sees what is.
        Chem.AssignStereochemistry(mol, cleanIt=True, force=True)
    if mol.GetNumAtoms() == 0:
        raise UnreadableError("it has no atoms")
    return mol


# --- Salts and solvents --------------------------------------------------------------------------


@dataclass(frozen=True)
class Counterion:
    #: The batch's `salt` or `solvate` value.
    name: str
    #: What a salt adds to the neutral parent per equivalent, as SMILES of what's added
    #: whole (an acid or base), or a metal that takes a hydrogen's place (or two).
    adds: str = ""
    metal: str = ""


#: Common pharmaceutical salt forms. Acids and organic bases form a salt with the parent
#: whole (an amine's hydrochloride is the amine plus HCl); a metal takes the place of
#: the acid's hydrogen (a sodium salt is the acid, less H, plus Na).
SALTS = {
    c.name: c
    for c in (
        Counterion("hydrochloride", adds="Cl"),
        Counterion("hydrobromide", adds="Br"),
        Counterion("hydroiodide", adds="I"),
        Counterion("sulfate", adds="O=S(=O)(O)O"),
        Counterion("phosphate", adds="O=P(O)(O)O"),
        Counterion("nitrate", adds="O=[N+]([O-])O"),
        Counterion("mesylate", adds="CS(=O)(=O)O"),
        Counterion("esylate", adds="CCS(=O)(=O)O"),
        Counterion("besylate", adds="O=S(=O)(O)c1ccccc1"),
        Counterion("tosylate", adds="Cc1ccc(S(=O)(=O)O)cc1"),
        Counterion("acetate", adds="CC(=O)O"),
        Counterion("trifluoroacetate", adds="O=C(O)C(F)(F)F"),
        Counterion("formate", adds="O=CO"),
        Counterion("oxalate", adds="O=C(O)C(=O)O"),
        Counterion("maleate", adds="O=C(O)/C=C\\C(=O)O"),
        Counterion("fumarate", adds="O=C(O)/C=C/C(=O)O"),
        Counterion("succinate", adds="O=C(O)CCC(=O)O"),
        # L-tartaric acid, (2R,3R), as most tartrates are; D- and meso- are tartrates too.
        Counterion("tartrate", adds="O=C(O)[C@H](O)[C@@H](O)C(=O)O"),
        Counterion("citrate", adds="O=C(O)CC(O)(CC(=O)O)C(=O)O"),
        Counterion("lactate", adds="CC(O)C(=O)O"),
        Counterion("malate", adds="O=C(O)CC(O)C(=O)O"),
        Counterion("benzoate", adds="O=C(O)c1ccccc1"),
        Counterion("sodium", metal="Na"),
        Counterion("potassium", metal="K"),
        Counterion("lithium", metal="Li"),
        Counterion("calcium", metal="Ca"),
        Counterion("magnesium", metal="Mg"),
        Counterion("zinc", metal="Zn"),
        Counterion("ammonium", adds="N"),
        Counterion("meglumine", adds="CNC[C@H](O)[C@@H](O)[C@H](O)[C@H](O)CO"),
        Counterion("tromethamine", adds="NC(CO)(CO)CO"),
    )
}
#: Solvents a batch may be a solvate of, by what's added whole per equivalent.
SOLVENTS = {
    c.name: c
    for c in (
        Counterion("water", adds="O"),
        Counterion("methanol", adds="CO"),
        Counterion("ethanol", adds="CCO"),
        Counterion("isopropanol", adds="CC(C)O"),
        Counterion("acetone", adds="CC(C)=O"),
        Counterion("acetonitrile", adds="CC#N"),
        Counterion("ethyl_acetate", adds="CCOC(C)=O"),
        Counterion("dichloromethane", adds="ClCCl"),
        Counterion("dimethyl_sulfoxide", adds="CS(C)=O"),
    )
}
#: The charges each metal takes in a salt.
METAL_CHARGE = {"Na": 1, "K": 1, "Li": 1, "Ca": 2, "Mg": 2, "Zn": 2}


def _neutral(mol: Chem.Mol) -> str:
    """A fragment's canonical SMILES, neutralised where it can be (a metal stays an ion,
    known by its symbol; one drawn as an isotope, such as lithium-6, is its own)."""
    uncharged = rdMolStandardize.Uncharger().uncharge(mol)
    if uncharged.GetNumAtoms() == 1:
        atom = uncharged.GetAtomWithIdx(0)
        if atom.GetSymbol() in METAL_CHARGE and not atom.GetIsotope():
            return atom.GetSymbol()
    return str(Chem.MolToSmiles(uncharged))


def _constitution(mol: Chem.Mol) -> str:
    """What a salt or solvent is known by: its neutral canonical SMILES (`_neutral`), with
    its stereocentres' configurations taken off. A counterion is the same salt whichever
    enantiomer, or diastereomer, it is: sunitinib's (S)-malate is a malate, and a meso-
    tartrate a tartrate. A double bond's configuration is kept, since it tells salts
    apart: maleate is cis, fumarate trans. So is an isotope: heavy water isn't water, nor
    lithium-6 lithium, whose standard atomic weight is mostly lithium-7's."""
    plain = Chem.RWMol(mol)
    for atom in plain.GetAtoms():
        atom.SetChiralTag(Chem.ChiralType.CHI_UNSPECIFIED)
    plain.SetStereoGroups([])
    return _neutral(plain.GetMol())


def _known() -> dict[str, tuple[str, Counterion]]:
    found: dict[str, tuple[str, Counterion]] = {}
    for kind, table in (("salt", SALTS), ("solvate", SOLVENTS)):
        for counterion in table.values():
            key = counterion.metal or _constitution(Chem.MolFromSmiles(counterion.adds))
            found[key] = (kind, counterion)
    return found


KNOWN = _known()


def known_part(fragment: Chem.Mol) -> tuple[str, Counterion] | None:
    """The salt or solvent a part of a structure is, if Iki knows it: ("salt", its
    counterion) or ("solvate", its solvent)."""
    return KNOWN.get(_constitution(fragment))


def labelled_salt(fragment: Chem.Mol) -> Counterion | None:
    """The salt Iki knows a part is, whether or not it's drawn with an isotope: lithium-6
    is lithium, and a deuterated mesylate a mesylate. Iki doesn't weigh them as the salt
    (`_constitution`), but a structure with one has that salt already."""
    plain = Chem.RWMol(fragment)
    for atom in plain.GetAtoms():
        atom.SetIsotope(0)
    with rdkit_messages():
        unlabelled = Chem.RemoveHs(plain.GetMol())
    known = known_part(unlabelled)
    return known[1] if known is not None and known[0] == "salt" else None


def whole_salts(mol: Chem.Mol) -> dict[str, float]:
    """The salts a structure registered whole has, by name, isotope-labelled ones too
    (`labelled_salt`), with how many of each for each parent. A structure of one part,
    once its solvents are set aside (acetic acid with its water), is a parent, not a salt,
    though it be an acid Iki knows as a counterion."""
    parts = [
        f
        for f in Chem.GetMolFrags(mol, asMols=True)
        if (known_part(f) or ("salt",))[0] != "solvate"
    ]
    if len(parts) < 2:
        return {}
    named = [labelled_salt(f) for f in parts]
    found: dict[str, float] = {}
    for name in {c.name for c in named if c is not None}:
        others = [f for f, c in zip(parts, named, strict=True) if c is None or c.name != name]
        if not others:
            continue
        combined = others[0]
        for other in others[1:]:
            combined = Chem.CombineMols(combined, other)
        key = _neutral(rdMolStandardize.LargestFragmentChooser().choose(combined))
        parents = sum(_neutral(f) == key for f in others)
        found[name] = _equivalents(sum(c is not None and c.name == name for c in named), parents)
    return found


#: Acid groups whose hydrogen a metal can take the place of, as SMARTS, with or without
#: it: a carboxylic acid, a sulfonic acid or sulfate's, each OH of a phosphorus acid, and
#: a tetrazole's NH (a carboxylic acid's isostere, as in losartan).
ACID_GROUPS = {
    ("a carboxylic acid", "carboxylic acids"): "[CX3](=O)[OX2H1,OX1-]",
    ("a sulfonic or sulfuric acid", "sulfonic or sulfuric acids"): "[SX4](=O)(=O)[OX2H1,OX1-]",
    ("a phosphorus acid's OH", "phosphorus acids' OHs"): "[PX4](=O)[OX2H1,OX1-]",
    ("a tetrazole", "tetrazoles"): "[$([nH1,n-]1nnnc1),$([nH1,n-]1nncn1)]",
}
_ACID_PATTERNS = {names: Chem.MolFromSmarts(smarts) for names, smarts in ACID_GROUPS.items()}
_NUMBERS = {1: "one", 2: "two", 3: "three"}


def acid_groups(mol: Chem.Mol) -> list[tuple[str, str]]:
    """Its acid groups (`ACID_GROUPS`), one (singular, plural) name for each."""
    return [
        names for names, pattern in _ACID_PATTERNS.items() for _ in mol.GetSubstructMatches(pattern)
    ]


@dataclass(frozen=True)
class Balance:
    """How many of a salt there are for each parent when nothing says: `equivalents`, or
    None when that isn't known; why, as a sentence (empty when it's the usual one); and
    an example to give when it isn't known."""

    equivalents: float | None
    why: str = ""
    example: str = ""


def unstated_equivalents(parent: Chem.Mol, salt: str) -> Balance:
    """How many of a salt there are for each parent when nothing says.

    A salt is one for each parent unless it says otherwise, as a hydrochloride or a
    sodium salt is. But a metal of charge 2 takes the place of two hydrogens, so one for
    each would take two from each parent: from acetic acid, which has one, that's no
    compound at all. Calcium acetate is one calcium for two acetates. So it's as charge
    balance says: 1/2 for a parent with one acid group, the only ratio that balances. A
    parent with more (folinic acid's two, which calcium takes both of, or one each of two)
    or none Iki counts (esomeprazole's, a benzimidazole's NH) doesn't say, so the number
    isn't known, and nor is the formula weight.
    """
    counterion = SALTS[salt]
    charge = METAL_CHARGE.get(counterion.metal, 1)
    if charge == 1:
        return Balance(1.0)
    name = counterion.name
    groups = acid_groups(parent)
    example = f"such as {1 / charge:g} for one {name} for {_NUMBERS[charge]} of it"
    if len(groups) == 1:
        each = 1 / charge
        return Balance(
            each,
            f"{name.capitalize()} is {charge}+, and its compound has one acid group to give a "
            f"hydrogen for it ({groups[0][0]}), so by charge balance it's {each:g}: one "
            f"{name} for {_NUMBERS[charge]} of it",
        )
    if not groups:
        return Balance(
            None,
            f"{name.capitalize()} is {charge}+, and Iki finds no acid group in its compound "
            "to give a hydrogen for it, so charge balance doesn't say how many",
            example,
        )
    kinds = list(dict.fromkeys(groups))
    if len(kinds) == 1:
        listed = kinds[0][1]
    else:
        each_kind = [k[0] if groups.count(k) == 1 else f"{groups.count(k)} {k[1]}" for k in kinds]
        listed = ", ".join(each_kind[:-1]) + " and " + each_kind[-1]
    return Balance(
        None,
        f"{name.capitalize()} is {charge}+, and its compound has {len(groups)} acid groups "
        f"({listed}), so charge balance doesn't say how many",
        f"such as 1 for one {name} for each of it",
    )


def average_weight(mol: Chem.Mol) -> float:
    """Its average molecular weight, g/mol, as RDKit's Descriptors.MolWt weighs it but by
    CIAAW's standard atomic weights, the table every Iki plugin weighs by (weights.py): each
    atom, and the hydrogens it carries, its element's; an atom drawn as an isotope, that
    nuclide's mass (RDKit's); a dummy atom (*), nothing."""
    table = Chem.GetPeriodicTable()
    total = 0.0
    for atom in mol.GetAtoms():
        number, isotope, symbol = atom.GetAtomicNum(), atom.GetIsotope(), atom.GetSymbol()
        if isotope:
            total += table.GetMassForIsotope(number, isotope)
        elif number:
            if symbol not in AVERAGE:
                raise ValueError(
                    f"{symbol} has no standard atomic weight, so its molecular weight isn't "
                    f"known: draw it as the isotope it is, such as [99{symbol}]."
                )
            total += AVERAGE[symbol]
        total += atom.GetTotalNumHs() * AVERAGE["H"]
    return total


def contribution(counterion: Counterion) -> float:
    """What one equivalent adds to the parent's formula weight, in g/mol."""
    if counterion.metal:
        metal = AVERAGE[counterion.metal]
        return float(metal - METAL_CHARGE[counterion.metal] * AVERAGE["H"])
    return average_weight(Chem.MolFromSmiles(counterion.adds))


def formula_weight(
    parent: Chem.Mol,
    salt: str | None,
    salt_equivalents: float | None,
    solvate: str | None,
    solvate_equivalents: float | None,
) -> float | None:
    """A batch's formula weight: its parent's, with its salt and solvate; or None, when
    its salt's equivalents aren't given and nothing says how many (`unstated_equivalents`).

    A salt's contribution is what it adds to a neutral parent. A parent that keeps a charge
    (a quaternary ammonium, which no proton can neutralise) is already the salt's ion, so
    each unit of its charge stands for a hydrogen the salt doesn't add: choline chloride
    is choline's cation and Cl, not choline's cation and HCl.
    """
    weight = average_weight(parent)
    if salt:
        if salt_equivalents is None:
            salt_equivalents = unstated_equivalents(parent, salt).equivalents
            if salt_equivalents is None:
                return None
        weight += contribution(SALTS[salt]) * salt_equivalents
        weight -= Chem.GetFormalCharge(parent) * AVERAGE["H"]
    if solvate:
        each = contribution(SOLVENTS[solvate])
        weight += each * (1.0 if solvate_equivalents is None else solvate_equivalents)
    return weight


# --- Standardising and identifying ---------------------------------------------------------------


@dataclass
class Registered:
    """A structure as Iki registers it: the compound, and what was taken off it."""

    mol: Chem.Mol
    smiles: str
    inchi: str
    inchikey: str
    #: The salt and solvate taken off it, with how many of each for one parent.
    salt: str | None = None
    salt_equivalents: float | None = None
    solvate: str | None = None
    solvate_equivalents: float | None = None
    #: The structure as it was given, standardised, whole.
    as_drawn: str = ""
    warnings: list[str] = field(default_factory=list)
    #: With the parent: its parts that are neither it nor a salt or solvent Iki knows,
    #: which were left out, as SMILES; and how many parents it has.
    left_out: list[str] = field(default_factory=list)
    parents: int = 1
    #: Whether its salt is one a field gave (an SD file's Salt), not one its structure shows:
    #: the material is that salt of the structure drawn.
    salt_from_field: bool = False


def identify(mol: Chem.Mol) -> tuple[str, str, list[str]]:
    """Its standard InChI and InChIKey, and what the InChI library warned of."""
    inchi, code, message, _log, _aux = rdinchi.MolToInchi(mol)
    if code not in (0, 1) or not inchi:
        raise UnreadableError(f"InChI can't represent it: {message or 'no InChI'}")
    key = rdinchi.InchiToInchiKey(inchi)
    if not key or not INCHIKEY.match(key):
        raise UnreadableError("InChI gave it no standard InChIKey")
    warnings = [message] if code == 1 and message else []
    return inchi, key, warnings


def _equivalents(count: int, parents: int) -> float:
    # Kept whole, not rounded: a citrate of three amines is a third of one each, and
    # 0.333 would put the formula weight out in its second decimal.
    return count / parents


#: What each kind of enhanced stereo group means: of one centre, and of several. A group
#: says nothing of proportions, and its members are enantiomers only when nothing else in
#: the structure is chiral (`_members`): so not a racemate, which IUPAC keeps for an
#: equimolar pair of enantiomers.
_GROUP_KINDS = {
    Chem.StereoGroupType.STEREO_AND: (
        "AND",
        "both configurations together",
        "both of the group's configurations are present",
    ),
    Chem.StereoGroupType.STEREO_OR: (
        "OR",
        "one configuration, not known which",
        "not which of the group's two configurations it is",
    ),
}
#: A single bond's configuration as an atropisomer's axis (RDKit's, from a wedge beside it).
_AXIAL = (Chem.BondStereo.STEREOATROPCW, Chem.BondStereo.STEREOATROPCCW)


def _axis(mol: Chem.Mol, bond: Chem.Bond) -> str:
    """A stereogenic axis, as the run names it: C11-C12, its atoms numbered from 1."""
    ends = (bond.GetBeginAtom(), bond.GetEndAtom())
    return "-".join(f"{a.GetSymbol()}{a.GetIdx() + 1}" for a in ends)


def _members(mol: Chem.Mol, atoms: list[int], bonds: list[int]) -> str:
    """What the two configurations of one AND group's centre or axis are of each other:
    enantiomers when nothing else in the structure is chiral; epimers (diastereomers) when
    the rest of its stereochemistry is defined; and not known when some of it isn't."""
    grouped = {a.GetIdx() for g in mol.GetStereoGroups() for a in g.GetAtoms()}
    grouped_bonds = {b.GetIdx() for g in mol.GetStereoGroups() for b in g.GetBonds()}
    centres = [
        i
        for i, _ in Chem.FindMolChiralCenters(
            mol, includeUnassigned=True, useLegacyImplementation=False
        )
        if i not in atoms
    ]
    axes = [
        b.GetIdx() for b in mol.GetBonds() if b.GetStereo() in _AXIAL and b.GetIdx() not in bonds
    ]
    if not centres and not axes:
        return " (its two enantiomers)"
    defined = all(
        i not in grouped and mol.GetAtomWithIdx(i).GetChiralTag() != Chem.ChiralType.CHI_UNSPECIFIED
        for i in centres
    ) and not grouped_bonds.intersection(axes)
    if defined:
        return (
            " (two epimers, diastereomers of each other, as the rest of its stereochemistry "
            "is defined)"
        )
    return ""


def enhanced_stereo(mol: Chem.Mol) -> tuple[Chem.Mol, list[str]]:
    """The structure with its enhanced stereochemistry as a standard InChIKey can hold it,
    and what was changed.

    A V3000 molfile or CXSMILES can mark stereocentres, or an atropisomer's axis, as an AND
    group (both configurations) or an OR group (one, not known which). A standard InChI
    can't, so it would give the single stereoisomer drawn, and a mixture of the two would
    be one of them. A group of one centre or one axis says no more than it undefined, as a
    racemate drawn without wedges says, so it's registered undefined. A group of several
    also says how they relate (cis, not trans), which nothing in a standard InChIKey holds,
    so it can't be registered.

    Nor can an axis drawn with its configuration (by a wedge beside it, as RDKit reads
    one): a standard InChI doesn't record an atropisomer's axis, a single bond's, so (R)-
    and (S)-BINOL, or sotorasib and its atropisomer, would have one InChIKey, and be one
    compound. (It does record an allene's, which `_lost_stereo` keeps from being lost.)
    """
    groups = list(mol.GetStereoGroups())
    relative = [g for g in groups if g.GetGroupType() in _GROUP_KINDS]
    warnings: list[str] = []
    changed = Chem.RWMol(mol)
    for group in relative:
        kind, meaning, together = _GROUP_KINDS[group.GetGroupType()]
        atoms = [a.GetIdx() for a in group.GetAtoms()]
        bonds = [b.GetIdx() for b in group.GetBonds()]
        named = ", ".join(
            [f"{mol.GetAtomWithIdx(i).GetSymbol()}{i + 1}" for i in atoms]
            + [f"the axis {_axis(mol, mol.GetBondWithIdx(i))}" for i in bonds]
        )
        if len(atoms) + len(bonds) > 1:
            raise UnreadableError(
                f"its stereocentres {named} are an {kind} group: how they relate to each "
                f"other is known, but {together}. A standard InChIKey can't record that: it "
                "would be the single stereoisomer drawn. Draw them without wedges "
                "(undefined), and record what's known as the compound's stereochemistry"
            )
        if kind == "AND":
            meaning += _members(mol, atoms, bonds)
        if atoms:
            changed.GetAtomWithIdx(atoms[0]).SetChiralTag(Chem.ChiralType.CHI_UNSPECIFIED)
            what = f"Its stereocentre {named} is"
        else:
            changed.GetBondWithIdx(bonds[0]).SetStereo(Chem.BondStereo.STEREONONE)
            what = f"Its stereogenic axis {named.removeprefix('the axis ')} (an atropisomer's) is"
        warnings.append(
            f"{what} in an {kind} group, {meaning}, which a standard InChIKey can't record, "
            "so it was registered undefined."
        )
    axes = [b for b in changed.GetBonds() if b.GetStereo() in _AXIAL]
    if axes:
        named = ", ".join(_axis(mol, b) for b in axes)
        one = len(axes) == 1
        raise UnreadableError(
            f"its bond{'' if one else 's'} {named} {'is a' if one else 'are'} stereogenic "
            f"ax{'is' if one else 'es'} drawn with {'its' if one else 'their'} configuration "
            "(an atropisomer). A standard InChI doesn't record an atropisomer's axis, so both "
            "atropisomers would have one InChIKey, and be one compound. Draw the axis without "
            "a wedge beside it (undefined), say which atropisomer it is in its name, and "
            "record what's known as the compound's stereochemistry"
        )
    if not relative:
        return mol, warnings
    changed.SetStereoGroups([g for g in groups if g.GetGroupType() not in _GROUP_KINDS])
    Chem.AssignStereochemistry(changed, cleanIt=True, force=True)
    return changed.GetMol(), warnings


def standardise(mol: Chem.Mol, parent: bool) -> Registered:
    """The compound a structure is, standardised; with `parent`, its parent, saying what
    salts and solvents were taken off it."""
    with rdkit_messages():
        cleaned = rdMolStandardize.Cleanup(mol)
    cleaned, stereo = enhanced_stereo(cleaned)
    found = _parent(cleaned) if parent else Registered(cleaned, "", "", "")
    found.warnings[:0] = stereo
    with rdkit_messages():
        found.as_drawn = Chem.MolToSmiles(cleaned)
    with rdkit_messages():
        smiles = Chem.MolToSmiles(found.mol)
    if len(smiles) > LIMITS["smiles"]:
        raise UnreadableError(f"its SMILES is over {LIMITS['smiles']:,} characters")
    inchi, key, warnings = identify(found.mol)
    _lost_stereo(found.mol, smiles, inchi)
    found.smiles, found.inchi, found.inchikey = smiles, inchi, key
    found.warnings.extend(f"InChI: {w}." for w in warnings if w != "Omitted undefined stereo")
    return found


def _lost_stereo(mol: Chem.Mol, smiles: str, inchi: str) -> None:
    """Refuses a structure whose standard InChI has stereochemistry its SMILES can't hold,
    so the compound's SMILES, which its descriptors, depiction and exports are made from,
    always gives its InChIKey.

    That's a cumulene's: an allene with two different groups at each end is axially
    chiral (penta-2,3-diene's two enantiomers), and one of three double bonds is E or Z.
    InChI reads either from a drawing's coordinates, but no SMILES RDKit reads or writes
    holds it, so both would be one SMILES, which InChI reads as neither."""
    if "/t" not in inchi and "/b" not in inchi:
        return  # it has no stereochemistry to lose
    if identify(from_smiles(smiles))[0] == inchi:
        return
    meet = [
        f"{a.GetSymbol()}{a.GetIdx() + 1}"
        for a in mol.GetAtoms()
        if sum(b.GetBondType() == Chem.BondType.DOUBLE for b in a.GetBonds()) == 2
    ]
    where = (
        f"its configuration at {', '.join(meet)}, where its double bonds meet (an allene's "
        "or cumulene's), is"
        if meet
        else "its configuration is"
    )
    raise UnreadableError(
        f"{where} in its standard InChI, from its drawing's coordinates, but no SMILES holds "
        "it, so its compound's SMILES would be another stereoisomer's too. Draw it without "
        "its configuration (in 2D, without wedges), say which it is in its name, and record "
        "what's known as the compound's stereochemistry"
    )


def _parent(cleaned: Chem.Mol) -> Registered:
    fragments = list(Chem.GetMolFrags(cleaned, asMols=True))
    taken: dict[tuple[str, str], int] = {}
    rest: list[Chem.Mol] = []
    salts: list[Chem.Mol] = []
    for fragment in fragments:
        known = known_part(fragment)
        if known is None:
            rest.append(fragment)
        else:
            kind, counterion = known
            taken[kind, counterion.name] = taken.get((kind, counterion.name), 0) + 1
            if kind == "salt":
                salts.append(fragment)
    warnings: list[str] = []
    if not rest:
        # Nothing but salts or solvents. With its solvents set aside, one compound, drawn
        # once or more, is the parent, though it's an acid Iki knows as a counterion:
        # acetic acid drawn with its water. Anything else (sodium chloride, say) is what it is.
        if not salts or len({_neutral(f) for f in salts}) > 1:
            return Registered(cleaned, "", "", "", warnings=warnings)
        rest = salts
        taken = {k: n for k, n in taken.items() if k[0] == "solvate"}
    combined = rest[0]
    for fragment in rest[1:]:
        combined = Chem.CombineMols(combined, fragment)
    largest = rdMolStandardize.LargestFragmentChooser().choose(combined)
    neutral = rdMolStandardize.Uncharger().uncharge(largest)
    key = _neutral(largest)
    parents = sum(_neutral(f) == key for f in rest)
    others = sorted({str(Chem.MolToSmiles(f)) for f in rest if _neutral(f) != key})
    if others:
        warnings.append(
            "It has other parts, which aren't salts or solvents Iki knows, so only its "
            f"largest part was kept: {', '.join(others)} left out."
        )
    found = Registered(neutral, "", "", "", warnings=warnings, left_out=others, parents=parents)
    for kind in ("salt", "solvate"):
        names = sorted(name for k, name in taken if k == kind)
        if len(names) > 1:
            raise UnreadableError(
                f"it has more than one {kind} ({', '.join(names)}), and a batch records one"
            )
        if names:
            equivalents = _equivalents(taken[kind, names[0]], parents)
            if kind == "salt":
                found.salt, found.salt_equivalents = names[0], equivalents
            else:
                found.solvate, found.solvate_equivalents = names[0], equivalents
    if not others:
        # Everything drawn is the parent, its salt and its solvent, so the formula weight
        # from them must be the drawn structure's, for each parent. With parts left out,
        # there's no formula weight to check (`weight_withheld` says so).
        drawn = average_weight(cleaned) / parents
        computed = formula_weight(
            neutral, found.salt, found.salt_equivalents, found.solvate, found.solvate_equivalents
        )
        if computed is not None and abs(drawn - computed) > 0.01:
            found.warnings.append(
                f"Its formula weight from its parent, salt and solvent is {computed:.3f} g/mol, "
                f"but its structure as drawn weighs {drawn:.3f} g/mol for each parent: its "
                "charges may not balance."
            )
    return found


# --- Descriptors ---------------------------------------------------------------------------------


def stereocentres(mol: Chem.Mol) -> tuple[int, int]:
    """Its tetrahedral stereocentres, and how many its structure leaves undefined."""
    centres = Chem.FindMolChiralCenters(mol, includeUnassigned=True, useLegacyImplementation=False)
    return len(centres), sum(label == "?" for _, label in centres)


def descriptors(mol: Chem.Mol) -> dict[str, Any]:
    """Its descriptors, as the Compound type's computed fields hold them."""
    weight = average_weight(mol)
    logp = Crippen.MolLogP(mol)
    centres, undefined = stereocentres(mol)
    violations = sum(
        (
            weight > 500,
            logp > 5,
            Calc.CalcNumLipinskiHBD(mol) > 5,
            Calc.CalcNumLipinskiHBA(mol) > 10,
        )
    )
    try:
        qed: float | None = round(QED.qed(mol), 3)
    except Exception:  # QED fails on what it has no properties for, such as a lone ion
        qed = None
    values: dict[str, Any] = {
        # An atom drawn as an isotope is written as it ([13C], D for 2H), as its masses
        # count it: heavy water is D2O, not H2O.
        "molecular_formula": Calc.CalcMolFormula(
            mol, separateIsotopes=True, abbreviateHIsotopes=True
        ),
        "molecular_weight": round(weight, 3),
        "exact_mass": round(Descriptors.ExactMolWt(mol), 5),
        "formal_charge": Chem.GetFormalCharge(mol),
        "heavy_atoms": mol.GetNumHeavyAtoms(),
        "logp": round(logp, 3),
        "tpsa": round(Calc.CalcTPSA(mol), 2),
        "hbd": Calc.CalcNumHBD(mol),
        "hba": Calc.CalcNumHBA(mol),
        "rotatable_bonds": Calc.CalcNumRotatableBonds(mol),
        "rings": Calc.CalcNumRings(mol),
        "aromatic_rings": Calc.CalcNumAromaticRings(mol),
        "fraction_csp3": round(Calc.CalcFractionCSP3(mol), 3),
        "stereocentres": centres,
        "undefined_stereocentres": undefined,
        "lipinski_violations": violations,
    }
    if qed is not None:
        values["qed"] = qed
    return values


# --- Molfiles -----------------------------------------------------------------------------------

#: How long one layout may take, by CoordGen and then by RDKit's own depictor. A drug's
#: takes a few hundredths of a second; a 200-residue peptide's, several seconds.
LAYOUT_SECONDS = 60.0
#: How long the layout process may take to start: to import RDKit.
STARTING_SECONDS = 60.0


def molfile(mol: Chem.Mol, coordgen: bool = True) -> str:
    """Its structure as an MDL Molfile, as RDKit writes one: laid out in 2D by CoordGen
    (Schrödinger's, which RDKit carries), or with `coordgen` false by RDKit's own
    depictor; kekulized; its stereocentres wedged and a double bond whose configuration
    isn't given drawn crossed, so the Molfile is the compound's stereoisomer. V2000,
    unless it has more than 999 atoms or bonds, which V2000's counts line can't hold:
    then RDKit writes V3000. Its title, its first line, is left empty: Iki writes the
    record's name there when it exports it. The same structure gives the same Molfile,
    with the same RDKit.

    CoordGen puts every atom at one point for some structures, such as a peptide of 250
    residues; RDKit's own depictor lays those out instead."""
    laid = Chem.Mol(mol)
    laid.SetProp("_Name", "")
    rdDepictor.SetPreferCoordGen(coordgen)
    rdDepictor.Compute2DCoords(laid)
    if coordgen and _collapsed(laid):
        rdDepictor.SetPreferCoordGen(False)
        rdDepictor.Compute2DCoords(laid)
    return str(Chem.MolToMolBlock(laid, kekulize=True))


def _collapsed(mol: Chem.Mol) -> bool:
    """Whether a layout put two of its atoms at one point."""
    at = {(round(x, 3), round(y, 3)) for x, y, _ in mol.GetConformer().GetPositions()}
    return len(at) < mol.GetNumAtoms()


def read_back(block: str) -> str | None:
    """The standard InChIKey of the structure a Molfile holds, if it holds one."""
    with rdkit_messages():
        mol = Chem.MolFromMolBlock(block)
    if mol is None:
        return None
    try:
        return identify(mol)[1]
    except UnreadableError:
        return None


def _lay_out(conn: Connection) -> None:
    """The layout process: each SMILES it's sent, laid out as `molfile` lays it out."""
    with suppress(OSError):
        # On Linux: a layout over the memory a run has stops this process, not the run.
        Path("/proc/self/oom_score_adj").write_text("1000")
    conn.send("ready")
    while True:
        try:
            smiles, coordgen = conn.recv()
        except EOFError:
            return
        try:
            conn.send(molfile(from_smiles(smiles), coordgen))
        except Exception:  # RDKit raises its own kinds; the run says it has no Molfile
            conn.send(None)


class Layouts:
    """Compounds' Molfiles (`molfile`), laid out in a process of their own, so a structure
    CoordGen can't lay out in time, or in the memory a run has, is laid out by RDKit's own
    depictor instead, and one neither can is given up on while the rest go on. CoordGen
    hasn't laid out a cycloalkane's ring of 42 carbons after minutes, which RDKit's
    depictor lays out at once, and takes a gigabyte over a PEG of 1,000 units."""

    def __init__(self, seconds: float = LAYOUT_SECONDS) -> None:
        self.seconds = seconds
        self._context = multiprocessing.get_context("spawn")
        self._process: BaseProcess | None = None
        self._conn: Connection | None = None

    def __enter__(self) -> Layouts:
        return self

    def __exit__(self, *_: object) -> None:
        self._stop()

    def molfile(self, smiles: str, until: float) -> str | None:
        """Its Molfile, laid out by CoordGen or else by RDKit's own depictor, each in at most
        `seconds`, and neither once it's `until` (as `time.monotonic` tells it)."""
        for coordgen in (True, False):
            left = min(self.seconds, until - time.monotonic())
            if left <= 0:
                return None
            found = self._ask(smiles, coordgen, left)
            if found is not None:
                return found
        return None

    def _ask(self, smiles: str, coordgen: bool, seconds: float) -> str | None:
        conn = self._conn or self._start()
        if conn is None:
            return None
        try:
            conn.send((smiles, coordgen))
        except OSError:  # it was stopped while it waited
            self._stop()
            return None
        if conn.poll(seconds):
            try:
                found: str | None = conn.recv()
            except EOFError:  # it was stopped, over the memory a run has
                pass
            else:
                return found
        self._stop()
        return None

    def _start(self) -> Connection | None:
        ours, theirs = self._context.Pipe()
        process = self._context.Process(target=_lay_out, args=(theirs,), daemon=True)
        process.start()
        theirs.close()
        self._process, self._conn = process, ours
        try:
            if ours.poll(STARTING_SECONDS) and ours.recv() == "ready":
                return ours
        except EOFError:
            pass
        self._stop()
        return None

    def _stop(self) -> None:
        if self._process is not None:
            self._process.kill()
            self._process.join()
        if self._conn is not None:
            self._conn.close()
        self._process = self._conn = None


# --- Values --------------------------------------------------------------------------------------


def cas_number(text: str) -> str | None:
    """`text` if it's a CAS Registry Number whose check digit is right."""
    found = CAS.match(text.strip())
    if found is None:
        return None
    digits = (found[1] + found[2])[::-1]
    if sum(i * int(d) for i, d in enumerate(digits, start=1)) % 10 != int(found[3]):
        return None
    return text.strip()


def cut(text: str, limit: int) -> str:
    """One line of text, at most `limit` characters."""
    return " ".join(text.split())[:limit]


def stripped(registered: Registered) -> bool:
    """Whether the compound registered isn't the material given, but its parent: salts,
    solvents or other parts taken off, or neutralised; or a salt of it its fields give."""
    return registered.as_drawn != registered.smiles or registered.salt_from_field


def given_as(registered: Registered, batches: bool = True) -> str:
    """What a name or an ID given with a stripped structure (`stripped`) is of, as the run
    says it: its structure as drawn, or the salt its fields give (its batch's, with
    batches)."""
    if registered.salt_from_field:
        if not batches:
            return f"a {registered.salt} salt's, by its salt field"
        return f"its batch's, a {registered.salt} salt by its salt field"
    return "its structure's as drawn"


def compound_values(registered: Registered, name: str = "") -> dict[str, Any]:
    """A compound's values, named `name` unless that's the name of what was drawn and the
    compound is its parent: "aspirin sodium" isn't aspirin's name."""
    values: dict[str, Any] = {
        "smiles": registered.smiles,
        "inchikey": registered.inchikey,
        "inchi": registered.inchi,
    }
    if name and not stripped(registered):
        values["name"] = cut(name, LIMITS["name"])
    return values


def name_left_out(registered: Registered, name: str, batches: bool) -> str | None:
    """What to say when `name` is what was drawn, not its parent's, so its compound
    isn't given it."""
    if not name or not stripped(registered):
        return None
    return (
        f"Its name, {cut(name, LIMITS['name'])}, is {given_as(registered, batches)}, not its "
        f"{'compound' if registered.salt_from_field else 'parent'}'s, so its compound wasn't "
        "given it" + ("; its batch was." if batches else ".")
    )


def weight_withheld(registered: Registered) -> str | None:
    """What to say of a batch whose structure has parts left out: it has no formula weight,
    since those parts are in the material but what they add to it isn't known. A weight
    without them would be too low, and a dose weighed out by it too little compound."""
    if not registered.left_out:
        return None
    drawn = average_weight(Chem.MolFromSmiles(registered.as_drawn)) / registered.parents
    one = len(registered.left_out) == 1
    return (
        f"Its batch has no formula weight: {', '.join(registered.left_out)} "
        f"{'is' if one else 'are'} part of the material, but not a salt or solvent Iki "
        f"knows, so what {'it adds' if one else 'they add'} isn't known. Its structure as "
        f"drawn weighs {drawn:.3f} g/mol for each parent."
    )


def batch_values(registered: Registered) -> dict[str, Any]:
    """A batch's salt form, from what was taken off its compound, and its formula weight,
    unless parts of it that aren't salts or solvents were left out (`weight_withheld`), or
    how many of its salt there are isn't known (`unstated_equivalents`)."""
    values: dict[str, Any] = {}
    if registered.as_drawn:
        values["structure_as_drawn"] = registered.as_drawn
    for name in ("salt", "salt_equivalents", "solvate", "solvate_equivalents"):
        if getattr(registered, name) is not None:
            values[name] = getattr(registered, name)
    if registered.left_out:
        return values
    weight = formula_weight(
        registered.mol,
        registered.salt,
        registered.salt_equivalents,
        registered.solvate,
        registered.solvate_equivalents,
    )
    if weight is not None:
        values["formula_weight"] = round(weight, 3)
    return values


# --- The run's output ----------------------------------------------------------------------------


class Creates:
    """Where the creates go: output.json while they're few and small, then a creates
    file (``creates_file``, one per line), which a run may fill with millions. Creates
    that refer to others (batches to their compounds) must stay in output.json."""

    def __init__(self, inline: bool = False) -> None:
        self.inline = inline
        self.held: list[str] = []
        self.size = 0
        self.count = 0
        self.file: TextIO | None = None

    def add(self, create: dict[str, Any]) -> None:
        self.count += 1
        line = json.dumps(create, separators=(",", ":"))
        if self.file is None:
            self.held.append(line)
            self.size += len(line)
            if self.inline:
                if self.count > MAX_CREATES:
                    raise UnreadableError(
                        f"That's more than the {MAX_CREATES:,} entities a run registers "
                        "with batches: split it."
                    )
                return
            if len(self.held) <= INLINE_CREATES and self.size <= INLINE_BYTES:
                return
            out = Path(os.environ["IKI_OUT_DIR"]) / CREATES_FILE
            self.file = out.open("w", encoding="utf-8")
            self.file.writelines(f"{held}\n" for held in self.held)
            self.held = []
            return
        self.file.write(f"{line}\n")

    def output(self) -> dict[str, Any]:
        if self.file is None:
            return {"creates": [json.loads(line) for line in self.held]}
        self.file.close()
        return {"creates_file": {"$file": CREATES_FILE}}


class Messages:
    """A run's messages, at most a hundred, saying how many more there were."""

    def __init__(self) -> None:
        self.held: list[dict[str, str]] = []
        self.more = 0

    def add(self, level: str, text: str, entity: str | None = None) -> None:
        if len(self.held) >= MAX_MESSAGES:
            self.more += 1
            return
        message = {"level": level, "text": text[:2000]}
        if entity is not None:
            message["entity"] = entity
        self.held.append(message)

    def output(self) -> list[dict[str, str]]:
        if self.more:
            return [*self.held, {"level": "warning", "text": f"And {self.more:,} more."}]
        return self.held
