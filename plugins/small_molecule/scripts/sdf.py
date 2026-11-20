"""The small_molecule plugin's SDF files: `import` registers each record of one as a
compound (and, with `batches`, a batch of it), `export` writes compounds to one.

Importing, each record's structure is standardised and identified as `chemistry` says,
as a SMILES line is, and registered; one already in Iki is reused. Its name is the SD
field the run names, or the record's title (its first line). SD fields that are a
compound's fields (or, with batches, a batch's) are kept, matched by name whatever
their case, spaces and underscores, and by the names suppliers and PubChem give them
(`FIELDS`); the run says which others it left out. A record's names and IDs are its
material's, so a compound that isn't that material (a salt's parent, drawn so or made a
salt by the record's Salt field, with batches or without) isn't given them. Each records
which file and record it came from, with the record's SHA-256.

A record that can't be registered fails the import, which names each such record and
says why, so nothing is half imported; or, with `on_error: skip`, is left out, saying
so. Files are read a record at a time, so they may hold a million; past a thousand
compounds, without batches, they go in a creates file (ADR 0056).

Exporting, each compound is written with 2D coordinates RDKit computes, its name (or
HRID) as its title, and its values as SD fields.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from chemistry import (
    LIMITS,
    MAX_SYNONYMS,
    SALTS,
    Creates,
    Messages,
    Registered,
    UnreadableError,
    batch_values,
    cas_number,
    compound_values,
    cut,
    fail,
    from_smiles,
    given_as,
    labelled_salt,
    name_left_out,
    rdkit_messages,
    run_input,
    standardise,
    stripped,
    unstated_equivalents,
    weight_withheld,
    whole_salts,
    why_not,
    write_output,
)
from rdkit import Chem
from rdkit.Chem import rdDepictor

if TYPE_CHECKING:
    from collections.abc import Iterator

MAX_RECORDS = 1_000_000
#: With batches, every create stays in output.json, two a record.
MAX_RECORDS_WITH_BATCHES = 5_000
NAMED = 20
MAX_FIELD = 10_000
SDF = "compounds.sdf"
#: The fields that say which structure a record is, as other systems know it.
IDS = {"cas_number": "CAS number", "pubchem_cid": "PubChem CID", "chembl_id": "ChEMBL ID"}


def _key(name: str) -> str:
    """An SD field's name as it's matched: lower case, letters and digits only."""
    return re.sub(r"[^a-z0-9%]", "", name.lower())


#: Each field an SD field can fill, by the names it goes by.
FIELDS: dict[str, tuple[str, str]] = {
    _key(alias): (kind, name)
    for kind, name, aliases in (
        ("compound", "synonyms", ["synonyms", "synonym", "PUBCHEM_IUPAC_NAME"]),
        ("compound", "description", ["description", "comment", "comments"]),
        ("compound", "pubchem_cid", ["pubchem_cid", "cid", "PUBCHEM_COMPOUND_CID"]),
        ("compound", "chembl_id", ["chembl_id", "chembl"]),
        ("compound", "cas_number", ["cas_number", "cas", "casrn", "cas_rn", "cas_no"]),
        ("batch", "purity", ["purity", "purity %", "purity_percent"]),
        ("batch", "amount", ["amount", "amount_mg", "mass", "mass_mg", "quantity"]),
        ("batch", "supplier", ["supplier", "vendor", "manufacturer", "source"]),
        (
            "batch",
            "catalogue_number",
            [
                "catalogue_number",
                "catalog_number",
                "catalogue_no",
                "catalog_no",
                "cat_no",
                "catalog_id",
                "product_number",
            ],
        ),
        ("batch", "supplier_lot", ["supplier_lot", "lot", "lot_number", "lot_no", "batch_number"]),
        ("batch", "received", ["received", "received_date", "date_received", "received_on"]),
        ("batch", "salt", ["salt", "salt_form"]),
        ("batch", "salt_equivalents", ["salt_equivalents", "salt_eq", "salt_ratio"]),
    )
    for alias in aliases
}
#: What salts are often written as.
SALT_NAMES = {
    "hcl": "hydrochloride",
    "hbr": "hydrobromide",
    "hi": "hydroiodide",
    "na": "sodium",
    "k": "potassium",
    "li": "lithium",
    "ca": "calcium",
    "mg": "magnesium",
    "zn": "zinc",
    "tfa": "trifluoroacetate",
    "msoh": "mesylate",
    "methanesulfonate": "mesylate",
    "tsoh": "tosylate",
    "ptsa": "tosylate",
    "besilate": "besylate",
    "mesilate": "mesylate",
    "tosilate": "tosylate",
    "nh4": "ammonium",
}
#: What a Salt field says when the material is its compound, not a salt of it.
NO_SALT = {"", "-", "n/a", "none", "free base", "freebase", "free acid", "free-base", "neutral"}
#: The batch's fields that say what its material is, so a record's names and IDs are a
#: salt's: read whether or not the run makes batches.
MATERIAL = {"salt", "salt_equivalents"}
AMOUNT = re.compile(r"^([0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)\s*(mg|g|ug|µg|μg|kg)?$")
#: The amount's fields whose names give its unit, so a bare number in them is in mg. In
#: others (Amount, Mass, Quantity) a bare number could be anything, a molecular mass say.
IN_MG = {_key("amount_mg"), _key("mass_mg")}
GRAMS = {"mg": 1.0, "g": 1_000.0, "ug": 0.001, "µg": 0.001, "μg": 0.001, "kg": 1_000_000.0}


@dataclass
class Record:
    number: int
    title: str
    molblock: str
    fields: dict[str, str]
    text: str
    warnings: list[str] = field(default_factory=list)


# --- Reading -------------------------------------------------------------------------------------


def records(path: Path) -> Iterator[Record]:
    """Each record of an SDF file, as it's read: its molblock and its SD fields."""
    number = 0
    lines: list[str] = []
    with path.open("rb") as given:
        for count, raw in enumerate(given):
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError:
                try:
                    line = raw.decode("latin-1")
                except UnicodeDecodeError:  # pragma: no cover - latin-1 reads any byte
                    raise UnreadableError(f"Line {count + 1} isn't text.") from None
            if count == 0:
                line = line.removeprefix("﻿")
            if line.rstrip("\r\n") == "$$$$":
                number += 1
                yield _record(number, lines)
                lines = []
            else:
                lines.append(line)
    if any(line.strip() for line in lines):
        number += 1
        yield _record(number, lines)


def _record(number: int, lines: list[str]) -> Record:
    text = "".join(lines) + "$$$$\n"
    end = next((i for i, line in enumerate(lines) if line.startswith("M  END")), None)
    if end is None:
        return Record(number, lines[0].strip() if lines else "", "", {}, text)
    molblock = "".join(lines[: end + 1])
    fields: dict[str, str] = {}
    name: str | None = None
    value: list[str] = []
    for line in lines[end + 1 :]:
        header = re.match(r"^>.*?<([^>]+)>", line)
        if header is not None:
            if name is not None:
                fields[name] = "\n".join(value).strip()
            name, value = header[1].strip(), []
        elif name is not None:
            if line.strip():
                value.append(line.rstrip("\r\n"))
            else:
                fields[name] = "\n".join(value).strip()
                name, value = None, []
    if name is not None:
        fields[name] = "\n".join(value).strip()
    return Record(number, lines[0].strip(), molblock, fields, text)


def structure(record: Record) -> Chem.Mol:
    if not record.molblock:
        raise UnreadableError("it has no structure (no M  END line)")
    with rdkit_messages() as said:
        mol = Chem.MolFromMolBlock(record.molblock, sanitize=False, removeHs=False)
        if mol is None:
            raise UnreadableError(f"its structure can't be read: {why_not(said, 'unreadable')}")
        try:
            Chem.SanitizeMol(mol)
        except Exception as error:  # RDKit raises its own kinds, each saying why
            raise UnreadableError(f"its structure isn't valid: {error}") from None
        Chem.AssignChiralTypesFromBondDirs(mol)
        Chem.AssignStereochemistry(mol, cleanIt=True, force=True)
        mol = Chem.RemoveHs(mol)
    if mol.GetNumAtoms() == 0:
        raise UnreadableError("its structure has no atoms")
    return mol


def label(record: Record, name: str) -> str:
    return f"Record {record.number}" + (f" ({name})" if name else "")


# --- Values --------------------------------------------------------------------------------------


def _salt(text: str) -> str | None:
    """The salt a Salt field names, or None when it says there's none (`NO_SALT`): "N/A"
    isn't Na."""
    if " ".join(text.lower().split()) in NO_SALT:
        return None
    word = re.sub(r"[^a-z0-9]", "", text.lower())
    found = SALT_NAMES.get(word, word)
    if found not in SALTS:
        raise UnreadableError(
            f"its salt {text!r} isn't one Iki knows: give one of {', '.join(sorted(SALTS))}"
        )
    return found


def _number(text: str, what: str, low: float, high: float) -> float:
    try:
        value = float(text.strip().removesuffix("%").strip())
    except ValueError:
        raise UnreadableError(f"its {what} {text!r} isn't a number") from None
    if not low <= value <= high:
        raise UnreadableError(f"its {what} {text!r} isn't between {low:g} and {high:g}")
    return value


def _amount(text: str, *, bare: bool) -> float | None:
    """A mass, in mg; without a unit, in mg if its field's name says so (`bare`), else None."""
    found = AMOUNT.match(text.strip())
    if found is None:
        raise UnreadableError(f"its amount {text!r} isn't a mass, such as 25 mg")
    if found[2] is None and not bare:
        return None
    return float(found[1]) * GRAMS[found[2] or "mg"]


def _date(text: str) -> str:
    try:
        return date.fromisoformat(text.strip()[:10]).isoformat()
    except ValueError:
        raise UnreadableError(
            f"its received date {text!r} isn't a date, such as 2026-11-16"
        ) from None


def field_values(record: Record, batches: bool) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """The compound's and the batch's values its SD fields give, and the fields left out.
    Without batches, the batch's are only its salt form (`MATERIAL`), which says what the
    material is, and they're left out too."""
    compound: dict[str, Any] = {}
    batch: dict[str, Any] = {}
    left: list[str] = []
    for sd_name, text in record.fields.items():
        found = FIELDS.get(_key(sd_name))
        if found is None or not text or (found[0] == "batch" and not batches):
            left.append(sd_name)
            if found is None or not text or found[1] not in MATERIAL:
                continue
        name = found[1]
        text = text[:MAX_FIELD]
        if name == "synonyms":
            names = [cut(s, LIMITS["name"]) for s in re.split(r"[;\n|]", text) if s.strip()]
            compound["synonyms"] = list(dict.fromkeys(compound.get("synonyms", []) + names))[
                :MAX_SYNONYMS
            ]
        elif name == "description":
            compound["description"] = text[: LIMITS["description"]]
        elif name == "pubchem_cid":
            if not re.match(r"^[1-9][0-9]{0,19}$", text.strip()):
                raise UnreadableError(f"its PubChem CID {text!r} isn't one, such as 2244")
            compound["pubchem_cid"] = text.strip()
        elif name == "chembl_id":
            chembl = text.strip().upper()
            if not re.match(r"^CHEMBL[1-9][0-9]{0,13}$", chembl):
                raise UnreadableError(f"its ChEMBL ID {text!r} isn't one, such as CHEMBL25")
            compound["chembl_id"] = chembl
        elif name == "cas_number":
            cas = cas_number(text)
            if cas is None:
                record.warnings.append(
                    f"Its CAS number {text!r} isn't one (or its check digit is wrong), so it "
                    "was left out."
                )
            else:
                compound["cas_number"] = cas
        elif name == "purity":
            batch["purity"] = _number(text, "purity", 0, 100)
        elif name == "amount":
            amount = _amount(text, bare=_key(sd_name) in IN_MG)
            if amount is None:
                record.warnings.append(
                    f"Its {sd_name} field, {text.strip()!r}, has no unit, so it wasn't taken as "
                    "its batch's amount: a bare number there could be its molecular mass. Give "
                    "the amount with its unit, such as 25 mg, or in an Amount_mg field."
                )
            else:
                batch["amount"] = amount
        elif name == "received":
            batch["received"] = _date(text)
        elif name == "salt":
            salt = _salt(text)
            if salt is not None:
                batch["salt"] = salt
        elif name == "salt_equivalents":
            batch["salt_equivalents"] = _number(text, "salt equivalents", 0, 10)
        else:
            batch[name] = cut(text, 200 if name == "supplier" else 100)
    return compound, batch, left


def _salt_form(registered: Registered, given: dict[str, Any]) -> list[str]:
    """Settles the record's salt form from what its structure shows and what its Salt and
    Salt_eq fields say, which must agree, and what to say of it on its batch. A salt is
    counted once: from the structure when it shows one, else from the fields, which then
    make the material a salt of the compound (`salt_from_field`), so its names and IDs
    aren't the compound's, with a batch or without one."""
    drawn = registered.salt
    salt, each = given.get("salt"), given.get("salt_equivalents")
    if salt and drawn and salt != drawn:
        raise UnreadableError(f"its salt field says {salt}, but its structure is a {drawn} salt")
    if each is not None and drawn:
        _agree(each, drawn, registered.salt_equivalents or 0.0)
    if drawn or not (salt or each is not None):
        return []
    whole = whole_salts(registered.mol)
    if whole:
        # Registered whole, the compound is the salt already: the fields add nothing.
        if salt and salt not in whole:
            raise UnreadableError(
                f"its salt field says {salt}, but its structure, registered whole, is a "
                f"{', '.join(sorted(whole))} salt"
            )
        named = salt or (next(iter(whole)) if len(whole) == 1 else None)
        if each is not None and named is not None:
            _agree(each, named, whole[named])
        if not salt:
            return []
        return [
            f"Its salt field says {salt}, which its structure, registered whole, already has, "
            "so its batch doesn't add it again. Register its parent to record the salt on its "
            "batch."
        ]
    # A part its parent left out may be a salt drawn with an isotope (lithium-6): the
    # material has it, as that isotope, so a field naming it doesn't add it again.
    labelled = {
        part: found.name
        for part in registered.left_out
        if (found := labelled_salt(from_smiles(part))) is not None
    }
    if salt and labelled:
        if salt not in labelled.values():
            raise UnreadableError(
                f"its salt field says {salt}, but its structure is a "
                f"{', '.join(sorted(set(labelled.values())))} salt"
            )
        parts = ", ".join(p for p, name in labelled.items() if name == salt)
        return [
            f"Its salt field says {salt}, which its structure has already, drawn as {parts}, "
            "so its batch doesn't add it again."
        ]
    if not salt:
        return []
    # A salt only its field gives: the material is that salt of the structure drawn. A
    # structure of one part (benzoic acid, say, though benzoate is a salt Iki knows) is the
    # parent its field's salt is of.
    registered.salt = salt
    registered.salt_from_field = True
    if each is not None:
        registered.salt_equivalents = each
        return []
    balance = unstated_equivalents(registered.mol, salt)
    registered.salt_equivalents = balance.equivalents
    said = f"Its salt field says {salt} but not how many for each of its compound. "
    if balance.equivalents is None:
        return [
            f"Its batch has no formula weight: {said[0].lower()}{said[1:]}{balance.why}: give "
            f"its Salt_eq field, {balance.example}."
        ]
    return [f"{said}{balance.why}."] if balance.why else []


def _agree(given: float, salt: str, each: float) -> None:
    if abs(given - each) > 0.01:
        raise UnreadableError(
            f"its salt equivalents field says {given:g}, but its structure has {each:g} "
            f"{salt} for each parent"
        )


# --- Importing -----------------------------------------------------------------------------------


def _named(record: Record, name_field: str) -> str:
    """The record's name: the SD field the run names, or else its title."""
    if not name_field:
        return cut(record.title, LIMITS["name"])
    found = next((v for k, v in record.fields.items() if _key(k) == _key(name_field)), "")
    return cut(found.split("\n")[0], LIMITS["name"])


def _creates(
    record: Record, name: str, file_id: str, parent: bool, batches: bool
) -> tuple[list[dict[str, Any]], Registered, list[str]]:
    """What a record registers: its compound, and with batches its batch; its
    standardised structure; and the SD fields it left out."""
    compound, batch, left = field_values(record, batches)
    registered = standardise(structure(record), parent)
    # Its salt form first: a salt its Salt field gives makes the material that salt, not
    # the compound, so the record's names and IDs aren't the compound's.
    said = _salt_form(registered, batch)
    left_out = name_left_out(registered, name, batches)
    if left_out:
        record.warnings.append(left_out)
    if stripped(registered):
        # The record's names and IDs are its material's, such as a salt's CAS number or
        # synonyms: the compound registered isn't that material, so they aren't its.
        theirs = [IDS[k] for k in IDS if compound.pop(k, None) is not None]
        synonyms = compound.pop("synonyms", None)
        if synonyms:
            theirs.append("synonyms")
        if theirs:
            many = len(theirs) > 1 or theirs == ["synonyms"]
            listed = ", ".join(theirs[:-1]) + " and " + theirs[-1] if len(theirs) > 1 else theirs[0]
            record.warnings.append(
                f"Its {listed} {'are' if many else 'is'} "
                f"{given_as(registered, batches)}, not its "
                f"{'compound' if registered.salt_from_field else 'parent'}'s, so "
                f"{'they were' if many else 'it was'} left out."
            )
    origin = {
        "source": "SDF file",
        "id": cut(name or f"Record {record.number}", 255),
        "file": file_id,
        "sha256": hashlib.sha256(record.text.encode("utf-8")).hexdigest(),
    }
    made: dict[str, Any] = {
        "type": "iki/small_molecule.Compound",
        "values": {**compound_values(registered, name), **compound},
        "origin": origin,
    }
    if not batches:
        return [made], registered, left
    key = f"record-{record.number}"
    named = {"name": name} if name else {}
    record.warnings.extend(said)
    withheld = weight_withheld(registered)
    if withheld:
        record.warnings.append(withheld)
    rest = {k: v for k, v in batch.items() if k not in MATERIAL}
    lot = {
        "type": "iki/small_molecule.CompoundBatch",
        "values": {"compound": {"$create": key}, **named, **batch_values(registered), **rest},
        "origin": origin,
    }
    return [{**made, "key": key}, lot], registered, left


def import_file(run: dict[str, Any]) -> dict[str, Any]:
    params = run.get("params") or {}
    file_id = params["file"]
    given = run["files"][file_id]
    name_field = (params.get("name_field") or "").strip()
    parent = bool(params.get("parent", False))
    batches = bool(params.get("batches", False))
    skip = params.get("on_error", "fail") == "skip"
    size = max(int(given.get("size") or 0), 1)
    limit = MAX_RECORDS_WITH_BATCHES if batches else MAX_RECORDS
    creates = Creates(inline=batches)
    messages = Messages()
    problems: list[str] = []
    left_out: dict[str, int] = {}
    named_missing = 0
    count = 0
    read = 0
    for record in records(Path(given["path"])):
        count += 1
        read += len(record.text)
        if count > limit:
            raise UnreadableError(
                f"It holds more than the {limit:,} records one run imports"
                + (" with batches" if batches else "")
                + ": split it."
            )
        name = _named(record, name_field)
        named_missing += bool(name_field) and not name
        try:
            made, registered, left = _creates(record, name, file_id, parent, batches)
        except UnreadableError as why:
            problems.append(f"{label(record, name)}: {why}.")
            if skip:
                messages.add("warning", f"{label(record, name)} was left out: {why}.")
            continue
        for sd_name in left:
            if name_field and _key(sd_name) == _key(name_field):
                continue  # it's the name
            left_out[sd_name] = left_out.get(sd_name, 0) + 1
        for create in made:
            creates.add(create)
        for warning in [*record.warnings, *registered.warnings]:
            messages.add("warning", f"{label(record, name)}: {warning}")
        if count <= 1000:
            print(f"{label(record, name)}: {registered.inchikey} {registered.smiles}")
        if count % 1000 == 0:
            print(f"::progress {min(read / size, 1.0):.2f} {count:,} records", flush=True)
    if not count:
        raise UnreadableError("It holds no records.")
    if count > 1000:
        print(f"… and {count - 1000:,} more records.")
    if problems and not skip:
        more = f" And {len(problems) - NAMED:,} more." if len(problems) > NAMED else ""
        raise UnreadableError(
            f"{len(problems):,} of {count:,} records can't be imported, so none was. "
            + " ".join(problems[:NAMED])
            + more
        )
    if len(problems) == count:
        raise UnreadableError(
            "None of its records could be imported. " + " ".join(problems[:NAMED])
        )
    if named_missing:
        messages.add(
            "warning",
            f"{named_missing:,} records have no {name_field} field, so they have no name.",
        )
    if left_out:
        fields = ", ".join(f"{k} ({n:,})" for k, n in sorted(left_out.items()))
        messages.add(
            "info",
            "These SD fields aren't fields of compounds"
            + (" or batches" if batches else "")
            + f", so they weren't kept: {fields}.",
        )
    return {
        **creates.output(),
        "metrics": {"records": count, "skipped": len(problems)},
        "messages": messages.output(),
    }


# --- Exporting -----------------------------------------------------------------------------------

#: What each compound's SD fields are, in order: its HRID, then its values.
EXPORTED = (
    "name",
    "smiles",
    "inchikey",
    "inchi",
    "synonyms",
    "molecular_formula",
    "molecular_weight",
    "exact_mass",
    "formal_charge",
    "logp",
    "tpsa",
    "hbd",
    "hba",
    "rotatable_bonds",
    "aromatic_rings",
    "fraction_csp3",
    "stereocentres",
    "undefined_stereocentres",
    "qed",
    "lipinski_violations",
    "pubchem_cid",
    "chembl_id",
    "cas_number",
)


def export(run: dict[str, Any]) -> dict[str, Any]:
    entities = run["inputs"]["compounds"]
    out = Path(os.environ["IKI_OUT_DIR"]) / SDF
    messages = Messages()
    written = 0
    with Chem.SDWriter(str(out)) as writer:
        writer.SetKekulize(True)
        for done, entity in enumerate(entities, start=1):
            values = entity["values"]
            try:
                mol = from_smiles(values["smiles"])
            except UnreadableError as why:
                messages.add("warning", f"{entity['hrid']} was left out: {why}.", entity["id"])
                continue
            rdDepictor.SetPreferCoordGen(True)
            rdDepictor.Compute2DCoords(mol)
            mol.SetProp("_Name", values.get("name") or entity["hrid"])
            mol.SetProp("hrid", entity["hrid"])
            for name in EXPORTED:
                value = values.get(name)
                if value in (None, "", []):
                    continue
                mol.SetProp(name, "; ".join(value) if isinstance(value, list) else str(value))
            writer.write(mol)
            written += 1
            if done % 1000 == 0:
                print(f"::progress {done / len(entities):.2f} {done:,} written", flush=True)
    print(f"Wrote {written:,} compounds to {SDF}.")
    if not written:
        fail("None of the compounds could be written.")
    return {"metrics": {"compounds": written}, "messages": messages.output()}


def main() -> None:
    kind = sys.argv[1]
    run = run_input()
    if kind == "export":
        write_output(export(run))
        return
    given = run["files"][run["params"]["file"]]
    try:
        output = import_file(run)
    except UnreadableError as why:
        fail(f"{given['name']}: {why}")
        return
    write_output(output)


if __name__ == "__main__":
    main()
