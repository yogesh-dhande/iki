"""The protein plugin's structures: `fetch` gets PDB entries by ID from RCSB, `file` reads
a structure file the run was given.

Each structure is registered with the proteins its chains are, linked to them, in one
run: all or nothing. A structure file is read with gemmi, as mmCIF (it starts with
`data_`) or PDB format:

- the structure's name (its PDB ID, or the file's name), title, method, resolution,
  first release and chains;
- for each protein in it (an mmCIF entity of type polypeptide(L)), its full sequence
  (SEQRES, or the residues it has where a file gives none), name, organism, gene and
  UniProt accession. Chains of the same protein refer to the same Protein, which is
  reused if Iki already has its sequence.

Other polymers (DNA, RNA, D-peptides) and ligands are left out, and the run says so.

Fetching downloads `<ID>.cif.gz` from files.rcsb.org, up to 100 entries a run, and
records the entry's revision, when it was fetched and the file's SHA-256. An entry the
PDB doesn't have (or has made obsolete) fails the run, which names it.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import gemmi

DOWNLOAD = "https://files.rcsb.org/download/{}.cif.gz"
ENTRY = "https://www.rcsb.org/structure/{}"
PDB_ID = re.compile(r"^[0-9][A-Z0-9]{3}$")
UNIPROT = re.compile(
    r"^([OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2})(-[0-9]+)?$"
)
RESIDUES = re.compile(r"^[ACDEFGHIKLMNPQRSTVWYUOBZJX]+$")
MAX_ENTRIES = 100
#: The largest structure file it reads, unpacked.
MAX_BYTES = 1024**3
ATTEMPTS = 4
PAUSE = 0.2
METHODS = {
    "X-RAY DIFFRACTION": "xray",
    "FIBER DIFFRACTION": "xray",
    "POWDER DIFFRACTION": "xray",
    "SOLUTION NMR": "nmr",
    "SOLID-STATE NMR": "nmr",
    "ELECTRON MICROSCOPY": "cryo_em",
    "ELECTRON CRYSTALLOGRAPHY": "electron_crystallography",
    "NEUTRON DIFFRACTION": "neutron",
    "THEORETICAL MODEL": "predicted",
}
LIMITS = {"name": 255, "description": 10_000, "organism": 200, "gene": 100}


class UnreadableError(Exception):
    """A structure that can't be imported, and why."""


@dataclass
class Chains:
    """One protein in a structure, and the chains that are it."""

    key: str
    values: dict[str, Any]
    ids: list[str]


@dataclass
class Read:
    """A structure file, as read."""

    label: str
    values: dict[str, Any]
    proteins: list[Chains]
    #: The entry's latest revision, such as 4.3.
    revision: str = ""
    notes: list[str] = field(default_factory=list)


# --- Reading -------------------------------------------------------------------------------------


def _text(block: gemmi.cif.Block, tag: str) -> str:
    """A single value, unquoted; empty for CIF's unknown (?) and not-applicable (.)."""
    raw = block.find_value(tag)
    if raw is None or raw in ("?", "."):
        return ""
    return str(gemmi.cif.as_string(raw)).strip()


def rows(block: gemmi.cif.Block, category: str, tags: list[str]) -> list[dict[str, str]]:
    """A category's rows, as tag → unquoted value, with optional tags marked `?`."""
    table = block.find(category, tags)
    names = [t.lstrip("?") for t in tags]
    out = []
    for row in table:
        values = {}
        for i, name in enumerate(names):
            raw = row[i] if row.has(i) else "?"
            values[name] = "" if raw in ("?", ".") else str(gemmi.cif.as_string(raw)).strip()
        out.append(values)
    return out


def _is_mmcif(path: Path) -> bool:
    with path.open("rb") as given:
        for raw in given:
            line = raw.strip()
            if line and not line.startswith(b"#"):
                return line.lower().startswith(b"data_")
    raise UnreadableError("It's empty.")


def block_of(path: Path) -> gemmi.cif.Block:
    """The file as an mmCIF block: read as it is, or made from a PDB file's records."""
    try:
        if _is_mmcif(path):
            return gemmi.cif.read(str(path)).sole_block()
        structure = gemmi.read_pdb(str(path))
    except (RuntimeError, ValueError) as why:
        raise UnreadableError(f"It isn't a structure gemmi can read: {why}") from None
    if len(structure) == 0:
        raise UnreadableError("It has no models: is it a PDB or mmCIF file?")
    # setup_entities(), with sequences filled in before chains of the same one are merged.
    structure.add_entity_types()
    structure.assign_subchains()
    structure.ensure_entities()
    for entity in structure.entities:
        if entity.entity_type == gemmi.EntityType.Polymer and not entity.full_sequence:
            # No SEQRES (as predictions often have): the residues it has.
            chain = next(
                (c for c in structure[0] if c.get_polymer().subchain_id() in entity.subchains),
                None,
            )
            if chain is not None:
                entity.full_sequence = [r.name for r in chain.get_polymer()]
    structure.deduplicate_entities()
    return structure.make_mmcif_document().sole_block()


def _one_letter(names: list[str]) -> str:
    letters = []
    for name in names:
        found = gemmi.find_tabulated_residue(name)
        code = found.one_letter_code.upper() if found is not None else ""
        letters.append(code if code.strip() and code != "?" else "X")
    return "".join(letters)


def sequences(block: gemmi.cif.Block) -> dict[str, str]:
    """Each polymer entity's full sequence, by its ID: the canonical one-letter code the
    file gives, or from its residues, first of each where several are possible."""
    out: dict[str, str] = {}
    for row in rows(block, "_entity_poly.", ["entity_id", "?pdbx_seq_one_letter_code_can"]):
        code = re.sub(r"\s", "", row["pdbx_seq_one_letter_code_can"]).upper()
        if code:
            out[row["entity_id"]] = code
    residues: dict[str, dict[str, str]] = {}
    for row in rows(block, "_entity_poly_seq.", ["entity_id", "num", "mon_id"]):
        residues.setdefault(row["entity_id"], {}).setdefault(row["num"], row["mon_id"])
    for entity, by_number in residues.items():
        if entity not in out:
            ordered = sorted(by_number.items(), key=lambda item: int(item[0] or 0))
            out[entity] = _one_letter([name for _, name in ordered])
    return out


def _by_entity(block: gemmi.cif.Block, category: str, tag: str) -> dict[str, str]:
    return {
        row["entity_id"]: row[tag.lstrip("?")]
        for row in rows(block, category, ["entity_id", tag])
        if row[tag.lstrip("?")]
    }


def _fit(values: dict[str, Any], label: str, notes: list[str]) -> dict[str, Any]:
    out = {}
    for name, value in values.items():
        limit = LIMITS.get(name)
        if isinstance(value, str) and limit is not None and len(value) > limit:
            notes.append(f"{label}: its {name} was cut to {limit} characters.")
            value = value[:limit]
        if value not in (None, "", []):
            out[name] = value
    return out


def _chain_order(chain: str) -> tuple[int, str]:
    return (len(chain), chain)


def read(path: Path, label: str, prefix: str) -> Read:
    """The structure in a file, with the proteins its chains are. Keys start with
    `prefix`, so several structures' creates can go in one output."""
    block = block_of(path)
    notes: list[str] = []
    entry = _text(block, "_entry.id").upper()
    pdb_id = entry if PDB_ID.match(entry) else ""
    method_names = [r["method"].upper() for r in rows(block, "_exptl.", ["method"])]
    method = next((METHODS.get(m, "other") for m in method_names), "")
    resolution = next(
        (
            _text(block, tag)
            for tag in (
                "_refine.ls_d_res_high",
                "_em_3d_reconstruction.resolution",
                "_reflns.d_resolution_high",
            )
            if _text(block, tag)
        ),
        "",
    )
    revisions = rows(
        block,
        "_pdbx_audit_revision_history.",
        ["major_revision", "minor_revision", "revision_date"],
    )
    found = sequences(block)
    names = {
        r["id"]: r["pdbx_description"]
        for r in rows(block, "_entity.", ["id", "?pdbx_description"])
        if r["pdbx_description"]
    }
    organisms = {
        **_by_entity(block, "_pdbx_entity_src_syn.", "?organism_scientific"),
        **_by_entity(block, "_entity_src_nat.", "?pdbx_organism_scientific_name"),
        **_by_entity(block, "_entity_src_gen.", "?pdbx_gene_src_scientific_name"),
    }
    genes = _by_entity(block, "_entity_src_gen.", "?pdbx_gene_src_gene")
    accessions = {
        r["entity_id"]: r["pdbx_db_accession"]
        for r in rows(block, "_struct_ref.", ["entity_id", "db_name", "?pdbx_db_accession"])
        if r["db_name"] == "UNP" and UNIPROT.match(r["pdbx_db_accession"])
    }
    shown = pdb_id or label
    proteins: list[Chains] = []
    for row in rows(block, "_entity_poly.", ["entity_id", "type", "?pdbx_strand_id"]):
        entity, kind = row["entity_id"], row["type"].lower()
        chains = sorted(
            (c.strip() for c in row["pdbx_strand_id"].split(",") if c.strip()),
            key=_chain_order,
        )
        listed = ", ".join(chains) or "?"
        if kind != "polypeptide(l)":
            notes.append(f"{shown}: chain {listed} is {kind}, not a protein, so it was left out.")
            continue
        sequence = found.get(entity, "")
        if not sequence or not RESIDUES.match(sequence):
            notes.append(f"{shown}: chain {listed} has no sequence it could read: left out.")
            continue
        many = "Chains" if len(chains) > 1 else "Chain"
        description = f"{many} {listed} of {shown}."
        gene = genes.get(entity, "").split(",")[0].strip()
        values = {
            "name": names.get(entity, "") or f"{shown} chain {chains[0] if chains else entity}",
            "description": description,
            "sequence": sequence,
            "organism": organisms.get(entity, ""),
            "gene": gene,
            "uniprot_accession": accessions.get(entity, ""),
        }
        proteins.append(
            Chains(f"{prefix}{entity}", _fit(values, f"{shown} chain {listed}", notes), chains)
        )
    if not proteins:
        raise UnreadableError("It has no protein chains.")
    pairs = sorted(
        ((chain, p) for p in proteins for chain in p.ids), key=lambda pair: _chain_order(pair[0])
    )
    released = min((r["revision_date"] for r in revisions if r["revision_date"]), default="")
    structure = {
        "name": pdb_id or label,
        "description": _text(block, "_struct.title"),
        "pdb_id": pdb_id,
        "method": method,
        "resolution": float(resolution) if resolution else None,
        "chains": [{"$create": p.key} for _, p in pairs],
        "chain_ids": [chain for chain, _ in pairs],
        "released": released,
    }
    latest = max(
        (
            (int(r["major_revision"] or 0), int(r["minor_revision"] or 0))
            for r in revisions
            if r["major_revision"]
        ),
        default=None,
    )
    return Read(
        label,
        _fit(structure, shown, notes),
        proteins,
        f"{latest[0]}.{latest[1]}" if latest else "",
        notes,
    )


# --- Output --------------------------------------------------------------------------------------


def creates(structure: Read, file: Any, origin: dict[str, Any]) -> list[dict[str, Any]]:  # noqa: ANN401 - a file ID or a $file
    """The proteins, then the structure that refers to them."""
    out: list[dict[str, Any]] = [
        {"type": "protein.Protein", "key": p.key, "values": p.values, "origin": origin}
        for p in structure.proteins
    ]
    out.append(
        {
            "type": "protein.ProteinStructure",
            "values": {"structure": file, **structure.values},
            "origin": origin,
        }
    )
    return out


def write(made: list[dict[str, Any]], notes: list[str], count: int) -> None:
    messages = [{"level": "info", "text": note[:2000]} for note in notes[:99]]
    if len(notes) > 99:
        messages.append({"level": "info", "text": f"And {len(notes) - 99} more notes."})
    output = {
        "apiVersion": "iki/v1",
        "kind": "RunOutput",
        "creates": made,
        "metrics": {"structures": count},
        "messages": messages,
    }
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as given:
        for chunk in iter(lambda: given.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _summary(structure: Read) -> str:
    values = structure.values
    chains = ", ".join(values.get("chain_ids", []))
    method = values.get("method", "")
    resolution = f" at {values['resolution']} Å" if "resolution" in values else ""
    return f"{values['name']}: {method}{resolution}, chains {chains}"


# --- A file --------------------------------------------------------------------------------------


def from_file(run: dict[str, Any]) -> None:
    file_id = run["params"]["file"]
    given = run["files"][file_id]
    path = Path(given["path"])
    name = str(given["name"])
    try:
        structure = read(path, Path(name).stem[:255] or "structure", "")
    except UnreadableError as why:
        print(f"::error {name}: {why}", flush=True)
        sys.exit(1)
    print(_summary(structure), flush=True)
    origin = {
        "source": "mmCIF file" if _is_mmcif(path) else "PDB file",
        "id": name[:255],
        "file": file_id,
        "sha256": _sha256(path),
    }
    write(creates(structure, file_id, origin), structure.notes, 1)


# --- The PDB -------------------------------------------------------------------------------------


def pdb_ids(text: str) -> list[str]:
    """The PDB IDs in `text`, in order, each once, upper-cased."""
    found = list(dict.fromkeys(w.upper() for w in re.split(r"[\s,;]+", text) if w))
    if not found:
        raise UnreadableError("Give at least one PDB ID, such as 4HHB.")
    for word in found:
        if not PDB_ID.match(word):
            raise UnreadableError(f"{word} isn't a PDB ID, such as 4HHB.")
    if len(found) > MAX_ENTRIES:
        raise UnreadableError(
            f"That's {len(found):,} entries, more than the {MAX_ENTRIES} one run fetches: "
            "split them."
        )
    return found


def download(pdb_id: str, to: Path) -> None:
    """The entry's mmCIF, unpacked into `to`."""
    request = urllib.request.Request(  # noqa: S310 - a fixed https URL
        DOWNLOAD.format(pdb_id), headers={"User-Agent": "iki-protein"}
    )
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with (
                urllib.request.urlopen(request, timeout=300) as response,  # noqa: S310
                gzip.GzipFile(fileobj=response) as unpacked,
                to.open("wb") as out,
            ):
                copied = 0
                while chunk := unpacked.read(1024 * 1024):
                    copied += len(chunk)
                    if copied > MAX_BYTES:
                        raise UnreadableError(f"{pdb_id}'s mmCIF is over 1 GiB.")
                    out.write(chunk)
            return
        except urllib.error.HTTPError as error:
            error.close()
            if error.code == 404:
                raise UnreadableError(
                    f"The PDB has no entry {pdb_id}: it may have been made obsolete. "
                    "Search for it at https://www.rcsb.org."
                ) from None
            if attempt < ATTEMPTS and (error.code == 429 or error.code >= 500):
                time.sleep(2**attempt)
                continue
            raise UnreadableError(f"RCSB answered {error.code} {error.reason}.") from None
        except (urllib.error.URLError, TimeoutError, EOFError, gzip.BadGzipFile) as error:
            if attempt < ATTEMPTS:
                time.sleep(2**attempt)
                continue
            reason = getattr(error, "reason", error)
            raise UnreadableError(f"Couldn't fetch {pdb_id} from RCSB: {reason}.") from None
    raise AssertionError("unreachable")


def fetch(run: dict[str, Any]) -> None:
    out_dir = Path(os.environ["IKI_OUT_DIR"])
    made: list[dict[str, Any]] = []
    notes: list[str] = []
    try:
        asked = pdb_ids(run["params"]["pdb_ids"])
        for done, pdb_id in enumerate(asked, start=1):
            if done > 1:
                time.sleep(PAUSE)
            path = out_dir / f"{pdb_id}.cif"
            download(pdb_id, path)
            retrieved = datetime.now(UTC).isoformat(timespec="seconds")
            try:
                structure = read(path, pdb_id, f"{pdb_id}-")
            except UnreadableError as why:
                raise UnreadableError(f"{pdb_id}: {why}") from None
            origin: dict[str, Any] = {
                "source": "RCSB PDB",
                "id": pdb_id,
                "url": ENTRY.format(pdb_id),
                "retrieved_at": retrieved,
                "sha256": _sha256(path),
            }
            if structure.revision:
                origin["version"] = f"revision {structure.revision}"
            file = {"$file": path.name, "media_type": "chemical/x-mmcif"}
            made.extend(creates(structure, file, origin))
            notes.extend(structure.notes)
            print(_summary(structure))
            print(f"::progress {done / len(asked):.2f} {done} of {len(asked)}", flush=True)
    except UnreadableError as why:
        print(f"::error {why}", flush=True)
        sys.exit(1)
    write(made, notes, len(asked))


def main() -> None:
    kind = sys.argv[1]
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    {"fetch": fetch, "file": from_file}[kind](run)


if __name__ == "__main__":
    main()
