"""The small_molecule plugin's chemistry, which its actions share: reading structures,
standardising them, identifying them, their descriptors, salts, and writing a run's output.

Standardising is RDKit's MolStandardize, as `rdMolStandardize.Cleanup` does it: hydrogens
made implicit, bonds to metals broken (the metal a cation, the atom it was bonded to an
anion), functional groups drawn in their usual form (nitro as N(=O)=O charge-separated,
and so on), and charges moved to the most acidic sites. A compound is that structure,
whole, unless the run asks for its parent: then fragments that are known salts or
solvents (`SALTS`, `SOLVENTS`) are taken off, of what's left the largest fragment is
kept (RDKit's LargestFragmentChooser), and it's neutralised where it can be (RDKit's
Uncharger). What was taken off is the batch's salt form.

A compound's identity is its standard InChIKey, from the InChI Trust's library, which
RDKit's wheels carry, so the same structure drawn another way is the same compound.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from rdkit import Chem, rdBase
from rdkit.Chem import QED, Crippen, Descriptors, rdinchi
from rdkit.Chem import rdMolDescriptors as Calc
from rdkit.Chem.MolStandardize import rdMolStandardize

if TYPE_CHECKING:
    from collections.abc import Iterator

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
WATER = 18.015


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


def _why(messages: list[str], fallback: str) -> str:
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
            raise UnreadableError(f"{smiles!r} isn't SMILES RDKit can read: {_why(said, 'syntax')}")
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
    """A fragment's canonical SMILES, neutralised where it can be (a metal stays an ion)."""
    uncharged = rdMolStandardize.Uncharger().uncharge(mol)
    if uncharged.GetNumAtoms() == 1:
        atom = uncharged.GetAtomWithIdx(0)
        if atom.GetSymbol() in METAL_CHARGE:
            return atom.GetSymbol()
    return str(Chem.MolToSmiles(uncharged))


def _known() -> dict[str, tuple[str, Counterion]]:
    found: dict[str, tuple[str, Counterion]] = {}
    for kind, table in (("salt", SALTS), ("solvate", SOLVENTS)):
        for counterion in table.values():
            key = counterion.metal or _neutral(Chem.MolFromSmiles(counterion.adds))
            found[key] = (kind, counterion)
    return found


KNOWN = _known()


def contribution(counterion: Counterion) -> float:
    """What one equivalent adds to the parent's formula weight, in g/mol."""
    if counterion.metal:
        table = Chem.GetPeriodicTable()
        hydrogen = table.GetAtomicWeight("H")
        metal = table.GetAtomicWeight(counterion.metal)
        return float(metal - METAL_CHARGE[counterion.metal] * hydrogen)
    return float(Descriptors.MolWt(Chem.MolFromSmiles(counterion.adds)))


def formula_weight(
    parent_weight: float,
    salt: str | None,
    salt_equivalents: float | None,
    solvate: str | None,
    solvate_equivalents: float | None,
) -> float:
    """A batch's formula weight: its parent's, with its salt and solvate."""
    weight = parent_weight
    if salt:
        weight += contribution(SALTS[salt]) * (
            1.0 if salt_equivalents is None else salt_equivalents
        )
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
    return round(count / parents, 3)


def standardise(mol: Chem.Mol, parent: bool) -> Registered:
    """The compound a structure is, standardised; with `parent`, its parent, saying what
    salts and solvents were taken off it."""
    with rdkit_messages():
        cleaned = rdMolStandardize.Cleanup(mol)
    found = _parent(cleaned) if parent else Registered(cleaned, "", "", "")
    with rdkit_messages():
        found.as_drawn = Chem.MolToSmiles(cleaned)
    with rdkit_messages():
        smiles = Chem.MolToSmiles(found.mol)
    if len(smiles) > LIMITS["smiles"]:
        raise UnreadableError(f"its SMILES is over {LIMITS['smiles']:,} characters")
    inchi, key, warnings = identify(found.mol)
    found.smiles, found.inchi, found.inchikey = smiles, inchi, key
    found.warnings.extend(f"InChI: {w}." for w in warnings if w != "Omitted undefined stereo")
    return found


def _parent(cleaned: Chem.Mol) -> Registered:
    fragments = list(Chem.GetMolFrags(cleaned, asMols=True))
    taken: dict[tuple[str, str], int] = {}
    rest: list[Chem.Mol] = []
    for fragment in fragments:
        known = KNOWN.get(_neutral(fragment))
        if known is None:
            rest.append(fragment)
        else:
            kind, counterion = known
            taken[kind, counterion.name] = taken.get((kind, counterion.name), 0) + 1
    warnings: list[str] = []
    if not rest:
        # Nothing but salts or solvents (sodium chloride, say): it is what it is.
        return Registered(cleaned, "", "", "", warnings=warnings)
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
    found = Registered(neutral, "", "", "", warnings=warnings)
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
    return found


# --- Descriptors ---------------------------------------------------------------------------------


def stereocentres(mol: Chem.Mol) -> tuple[int, int]:
    """Its tetrahedral stereocentres, and how many its structure leaves undefined."""
    centres = Chem.FindMolChiralCenters(mol, includeUnassigned=True, useLegacyImplementation=False)
    return len(centres), sum(label == "?" for _, label in centres)


def descriptors(mol: Chem.Mol) -> dict[str, Any]:
    """Its descriptors, as the Compound type's computed fields hold them."""
    weight = Descriptors.MolWt(mol)
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
        "molecular_formula": Calc.CalcMolFormula(mol),
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


def compound_values(registered: Registered, name: str = "") -> dict[str, Any]:
    values: dict[str, Any] = {
        "smiles": registered.smiles,
        "inchikey": registered.inchikey,
        "inchi": registered.inchi,
    }
    if name:
        values["name"] = cut(name, LIMITS["name"])
    return values


def batch_values(registered: Registered) -> dict[str, Any]:
    """A batch's salt form, from what was taken off its compound, and its formula weight."""
    values: dict[str, Any] = {}
    if registered.as_drawn:
        values["structure_as_drawn"] = registered.as_drawn
    for name in ("salt", "salt_equivalents", "solvate", "solvate_equivalents"):
        if getattr(registered, name) is not None:
            values[name] = getattr(registered, name)
    values["formula_weight"] = round(
        formula_weight(
            Descriptors.MolWt(registered.mol),
            registered.salt,
            registered.salt_equivalents,
            registered.solvate,
            registered.solvate_equivalents,
        ),
        3,
    )
    return values


# --- The run's output ----------------------------------------------------------------------------


def run_input() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    return loaded


def fail(why: str) -> None:
    print(f"::error {why}", flush=True)
    sys.exit(1)


def write_output(output: dict[str, Any]) -> None:
    text = json.dumps({"apiVersion": "iki/v1", "kind": "RunOutput", **output})
    if len(text.encode("utf-8")) > MAX_OUTPUT_BYTES:
        fail("What it registers is over the 2.5 MB a run's output holds: give it fewer at once.")
    Path(os.environ["IKI_OUTPUT"]).write_text(text, encoding="utf-8")


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
