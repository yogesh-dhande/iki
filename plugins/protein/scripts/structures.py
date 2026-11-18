"""The protein plugin's structures: `fetch` gets PDB entries by ID from RCSB, `alphafold`
AlphaFold DB's predictions of UniProt entries, and `file` reads a structure file the run
was given.

Each structure is registered with the proteins its chains are, linked to them, in one
run: all or nothing. A structure file is read with gemmi, as mmCIF (it starts with
`data_`) or PDB format:

- the structure's name (its PDB ID, or the file's name), title, method, resolution,
  chains and, for a PDB entry, when it was first released (its first revision's date);
- for each protein in it (an mmCIF entity of type polypeptide(L)), its full sequence
  (SEQRES, or the residues it has where a file gives none), name, organism, gene and
  UniProt accession. Chains of the same protein refer to the same Protein, which is
  reused if Iki already has its sequence. A chimeric chain, whose segments are
  different UniProt entries or come from different organisms (such as 2RH1's
  β2-adrenergic receptor with T4 lysozyme in its third loop), is none of them: it gets
  no accession, organism or gene, and its description says which residues are which.

A PDB ID is its four characters (1CRN) or wwPDB's extended form (pdb_00001crn, as
`_database_2` gives it). An extended ID of an entry that has a four-character one is the
same entry, so it's kept as that (1CRN), whichever was given.

Other polymers (DNA, RNA, D-peptides) and ligands are left out, and the run says so.

The structure's origin says how the file says it was made (ADR 0106): an experiment, by
`_exptl`'s method (EXPDTA in PDB format), or a computed model, by THEORETICAL MODEL or
ModelCIF's categories, with the software it names and, for a model, its global quality
metrics, such as a mean pLDDT. A file that says nothing gets nothing.

Fetching downloads `<ID>.cif.gz` from files.rcsb.org, up to 100 entries a run, and
records the entry's revision, when it was fetched and the file's SHA-256. An entry the
PDB doesn't have (or has made obsolete) fails the run, which names it.

Fetching from AlphaFold DB asks its API (alphafold.ebi.ac.uk/api/prediction/<accession>)
for each accession's models, up to 100 accessions a run, and downloads each model's
latest mmCIF from alphafold.ebi.ac.uk/files, recording the model's ID, its version, when
it was fetched and the file's SHA-256. An accession AlphaFold DB has no model of fails
the run, which names it.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
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
PREDICTION = "https://alphafold.ebi.ac.uk/api/prediction/{}"
#: Where AlphaFold DB keeps its files, where a model's mmCIF must be.
FILES = "https://alphafold.ebi.ac.uk/files/"
AF_ENTRY = "https://alphafold.ebi.ac.uk/entry/{}"
#: A model's ID: AF-P69905-F1 for AlphaFold's, AF-0000000365840314 for others' it holds.
MODEL_ID = re.compile(r"^AF-[A-Z0-9]{1,40}(-F[0-9]{1,4})?$")
#: Naming the plugin, as AlphaFold DB's file server refuses some clients' own (curl's).
USER_AGENT = "iki-protein"
PDB_ID = re.compile(r"^[0-9][A-Z0-9]{3}$")
#: wwPDB's extended PDB ID, upper-cased, as the structure's `pdb_id` field stores it: pdb_,
#: five digits and three letters or digits, as wwPDB's FAQ gives it (pdb_00001abc). An entry
#: with a four-character ID has pdb_0000 before it.
EXTENDED_PDB_ID = re.compile(r"^PDB_[0-9]{4}[0-9][A-Z0-9]{3}$")
UNIPROT = re.compile(
    r"^([OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2})(-[0-9]+)?$"
)
RESIDUES = re.compile(r"^[ACDEFGHIKLMNPQRSTVWYUOBZJX]+$")
MAX_ENTRIES = 100
#: The largest structure file it reads, unpacked.
MAX_BYTES = 1024**3
#: The largest answer it reads from AlphaFold DB's API.
MAX_ANSWER = 16 * 1024**2
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
#: wwPDB's experimental methods (`_exptl.method`); its other, THEORETICAL MODEL, is computed.
EXPERIMENTAL = frozenset(
    {
        "X-RAY DIFFRACTION",
        "FIBER DIFFRACTION",
        "POWDER DIFFRACTION",
        "NEUTRON DIFFRACTION",
        "ELECTRON MICROSCOPY",
        "ELECTRON CRYSTALLOGRAPHY",
        "SOLUTION NMR",
        "SOLID-STATE NMR",
        "SOLUTION SCATTERING",
        "INFRARED SPECTROSCOPY",
        "EPR",
        "FLUORESCENCE TRANSFER",
    }
)
COMPUTED = "THEORETICAL MODEL"
#: The most tools, and details, an origin's `made` holds.
MAX_TOOLS = 20
MAX_DETAILS = 20
LIMITS = {"name": 255, "description": 10_000, "gene": 100}


class UnreadableError(Exception):
    """A structure that can't be imported, and why."""


@dataclass
class Chains:
    """One protein in a structure, and the chains that are it."""

    key: str
    values: dict[str, Any]
    ids: list[str]
    #: Its organism's NCBI Taxonomy ID and name, if the file says (ADR 0140).
    organism: tuple[int, str] | None = None


@dataclass
class Read:
    """A structure file, as read."""

    label: str
    values: dict[str, Any]
    proteins: list[Chains]
    #: The entry's latest revision, such as 4.3.
    revision: str = ""
    notes: list[str] = field(default_factory=list)
    #: How the file says the structure was made, if it does (ADR 0106).
    made: dict[str, Any] | None = None


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


def _number(text: str) -> float | None:
    """A CIF number, as CIF writes them: 2.10, or 2.10(5) with its standard uncertainty in
    brackets, which is left off. None for one that isn't a number."""
    value = gemmi.cif.as_number(text) if text else math.nan
    return value if math.isfinite(value) else None


def pdb_id(text: str) -> str:
    """The PDB ID `text` is, as Iki keeps it, or empty: 1CRN for 1crn and pdb_00001crn."""
    word = text.strip().upper()
    if EXTENDED_PDB_ID.match(word) and word.startswith("PDB_0000") and PDB_ID.match(word[8:]):
        return word[8:]
    return word if PDB_ID.match(word) or EXTENDED_PDB_ID.match(word) else ""


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
    # Each residue's place in its entity's sequence (label_seq_id): without it, mkdssp
    # numbers residues by their authors' numbers, and a chain numbered from 101, or with
    # an insertion code (52A), or a gap where a file has no SEQRES, comes back with its
    # secondary structure on the wrong residues, or none.
    structure.assign_label_seq_id()
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


def strands(block: gemmi.cif.Block) -> dict[str, str]:
    """Each polymer entity's chain IDs, for a file whose `_entity_poly` doesn't list them
    (as some models' don't): from its residues' scheme, or else its asyms'."""
    out: dict[str, dict[str, None]] = {}
    for row in rows(block, "_pdbx_poly_seq_scheme.", ["entity_id", "pdb_strand_id"]):
        out.setdefault(row["entity_id"], {})[row["pdb_strand_id"]] = None
    if not out:
        for row in rows(block, "_struct_asym.", ["entity_id", "id"]):
            out.setdefault(row["entity_id"], {})[row["id"]] = None
    return {entity: ",".join(c for c in chains if c) for entity, chains in out.items()}


def _organism(taxon: str, name: str) -> tuple[int, str] | None:
    """An organism by its NCBI Taxonomy ID, the first if a file gives several."""
    first = taxon.split(",")[0].strip()
    if not first.isdigit() or not 0 < int(first) < 10**10:
        return None
    return int(first), " ".join(name.split(",")[0].split())


def _by_entity(block: gemmi.cif.Block, category: str, tag: str) -> dict[str, str]:
    return {
        row["entity_id"]: row[tag.lstrip("?")]
        for row in rows(block, category, ["entity_id", tag])
        if row[tag.lstrip("?")]
    }


def _said(text: str) -> str:
    """A name or version as the file gives it, in one line, as long as an origin takes."""
    return " ".join(text.split())[:100].strip()


def tools(block: gemmi.cif.Block) -> list[dict[str, str]]:
    """The software the file says made it: in a ModelCIF file, its software groups', which
    its modelling used (not those that only annotated it, such as DSSP); otherwise each
    `_software` lists. A version's `v` is left off: AlphaFold v2.0 is AlphaFold 2.0."""
    grouped = {r["software_id"] for r in rows(block, "_ma_software_group.", ["software_id"])}
    out: list[dict[str, str]] = []
    for row in rows(block, "_software.", ["name", "?version", "?pdbx_ordinal"]):
        if grouped and row["pdbx_ordinal"] not in grouped:
            continue
        tool = {"name": _said(row["name"])}
        version = _said(re.sub(r"^[vV](?=[0-9])", "", row["version"]))
        if version:
            tool["version"] = version
        if tool["name"] and tool not in out:
            out.append(tool)
    return out[:MAX_TOOLS]


def quality(block: gemmi.cif.Block) -> dict[str, float]:
    """A model's global quality metrics (ModelCIF's `_ma_qa_metric_global`), by name: a
    pLDDT's mean as `mean_plddt`, others as their names in lower case, such as `ptm`."""
    metrics = {r["id"]: r for r in rows(block, "_ma_qa_metric.", ["id", "name", "?type"])}
    first = next((r["model_id"] for r in rows(block, "_ma_model_list.", ["model_id"])), "")
    out: dict[str, float] = {}
    for row in rows(block, "_ma_qa_metric_global.", ["metric_id", "metric_value", "?model_id"]):
        metric = metrics.get(row["metric_id"])
        if metric is None or (first and row["model_id"] not in ("", first)):
            continue
        if metric["type"] == "pLDDT":
            name = "mean_plddt"
        else:
            name = re.sub(r"[^a-z0-9]+", "_", metric["name"].lower()).strip("_")[:63]
        try:
            value = float(row["metric_value"])
        except ValueError:
            continue
        if re.match(r"^[a-z]", name) and name not in out and math.isfinite(value):
            out[name] = value
    return dict(list(out.items())[:MAX_DETAILS])


def made(block: gemmi.cif.Block) -> dict[str, Any] | None:
    """How the file says its structure was made, read from it, never guessed (ADR 0106):
    `_exptl`'s method, an experiment's or THEORETICAL MODEL, or, in a ModelCIF file without
    one, its model's type; the software it names; and a model's global quality metrics,
    such as its mean pLDDT. None for a file that doesn't say, or names a method that isn't
    wwPDB's."""
    methods = list(dict.fromkeys(_said(r["method"]) for r in rows(block, "_exptl.", ["method"])))
    methods = [m for m in methods if m]
    models = rows(block, "_ma_model_list.", ["model_id", "?model_type"])
    if methods:
        named = {m.upper() for m in methods}
        if named <= EXPERIMENTAL:
            kind = "experimental"
        elif named == {COMPUTED}:
            kind = "computational"
        else:
            return None
        method = ", ".join(methods)
    elif models:
        kind, method = "computational", models[0]["model_type"]
    else:
        return None
    found: dict[str, Any] = {"kind": kind}
    if _said(method):
        found["method"] = _said(method)
    software = tools(block)
    if software:
        found["software"] = software
    details = quality(block) if models else {}
    if details:
        found["details"] = details
    return found


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


#: Residues of an entity's sequence, from and to (from 1), or None where a file doesn't say.
Span = tuple[int, int] | None


def _span(begin: str, end: str) -> Span:
    try:
        first, last = int(begin), int(end)
    except ValueError:
        return None
    return (first, last) if 0 < first <= last else None


def _spans(spans: list[Span]) -> str:
    """Spans as a list: 8–237 and 398–500."""
    shown = [f"{a}–{b}" if a != b else str(a) for a, b in sorted(s for s in spans if s)]
    return shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + " and " + shown[-1]


def _sourced(sources: dict[str, list[Span]], what: str) -> str:
    """Each source with the residues it's for: UniProt P07550 (residues 8–237 and
    398–500) and P00720 (residues 238–397)."""
    parts = [
        f"{name} (residues {_spans(spans)})" if any(spans) else name
        for name, spans in sources.items()
    ]
    listed = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return f"{what} {listed}"


def _organisms(
    block: gemmi.cif.Block, targets: list[dict[str, str]]
) -> dict[str, dict[str, list[Span]]]:
    """Each entity's source organisms, with the residues each is for: from where the gene
    was cloned (`_entity_src_gen`), or else the natural source, the synthetic one, or a
    ModelCIF file's target. A chimera has a row for each of its segments."""
    out: dict[str, dict[str, list[Span]]] = {}
    for category, tag in (
        ("_entity_src_gen.", "pdbx_gene_src_scientific_name"),
        ("_entity_src_nat.", "pdbx_organism_scientific_name"),
        ("_pdbx_entity_src_syn.", "organism_scientific"),
    ):
        found: dict[str, dict[str, list[Span]]] = {}
        for row in rows(
            block, category, ["entity_id", f"?{tag}", "?pdbx_beg_seq_num", "?pdbx_end_seq_num"]
        ):
            if row[tag]:
                spans = found.setdefault(row["entity_id"], {}).setdefault(row[tag], [])
                spans.append(_span(row["pdbx_beg_seq_num"], row["pdbx_end_seq_num"]))
        for entity, named in found.items():
            out.setdefault(entity, named)
    for target in targets:
        if target["organism_scientific"]:
            out.setdefault(target["target_entity_id"], {target["organism_scientific"]: [None]})
    return out


def _accessions(
    block: gemmi.cif.Block, targets: list[dict[str, str]]
) -> dict[str, dict[str, list[Span]]]:
    """Each entity's UniProt entries, with the residues each is aligned to: the PDB's
    references (`_struct_ref`, aligned by `_struct_ref_seq`), or a ModelCIF file's target.
    wwPDB gives a chimera a reference for each segment (2RH1: P07550, P00720, P07550)."""
    aligned: dict[str, list[Span]] = {}
    for row in rows(block, "_struct_ref_seq.", ["ref_id", "?seq_align_beg", "?seq_align_end"]):
        span = _span(row["seq_align_beg"], row["seq_align_end"])
        if span not in aligned.setdefault(row["ref_id"], []):
            aligned[row["ref_id"]].append(span)
    out: dict[str, dict[str, list[Span]]] = {}
    for row in rows(block, "_struct_ref.", ["entity_id", "db_name", "?id", "?pdbx_db_accession"]):
        accession = row["pdbx_db_accession"]
        if row["db_name"] == "UNP" and UNIPROT.match(accession):
            spans = out.setdefault(row["entity_id"], {}).setdefault(accession, [])
            spans.extend(s for s in aligned.get(row["id"], [None]) if s not in spans)
    for target in targets:
        accession = target["db_accession"]
        if target["db_name"] == "UNP" and UNIPROT.match(accession):
            out.setdefault(target["target_entity_id"], {accession: [None]})
    return out


def read(path: Path, label: str, prefix: str) -> Read:
    """The structure in a file, with the proteins its chains are. Keys start with
    `prefix`, so several structures' creates can go in one output."""
    block = block_of(path)
    notes: list[str] = []
    entry = pdb_id(_text(block, "_entry.id"))
    method_names = [r["method"].upper() for r in rows(block, "_exptl.", ["method"])]
    method = next((METHODS.get(m, "other") for m in method_names), "")
    if not method and rows(block, "_ma_model_list.", ["model_id"]):
        method = "predicted"  # a ModelCIF file's model, without _exptl
    given = next(
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
    resolution = _number(given)
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
    # A ModelCIF file's targets: the sequence database records it modelled.
    targets = rows(
        block,
        "_ma_target_ref_db_details.",
        ["target_entity_id", "?db_name", "?db_accession", "?organism_scientific", "?gene_name"],
    )
    organisms = _organisms(block, targets)
    # Each entity's NCBI Taxonomy ID, which names its organism (ADR 0140).
    taxa = {
        **{
            r["target_entity_id"]: r["ncbi_taxonomy_id"]
            for r in rows(
                block, "_ma_target_ref_db_details.", ["target_entity_id", "?ncbi_taxonomy_id"]
            )
            if r["ncbi_taxonomy_id"]
        },
        **_by_entity(block, "_pdbx_entity_src_syn.", "?ncbi_taxonomy_id"),
        **_by_entity(block, "_entity_src_nat.", "?pdbx_ncbi_taxonomy_id"),
        **_by_entity(block, "_entity_src_gen.", "?pdbx_gene_src_ncbi_taxonomy_id"),
    }
    genes = {
        **{t["target_entity_id"]: t["gene_name"] for t in targets},
        **_by_entity(block, "_entity_src_gen.", "?pdbx_gene_src_gene"),
    }
    accessions = _accessions(block, targets)
    shown = entry or label
    if given and resolution is None:
        notes.append(f"{shown}: its resolution, {given}, isn't a number, so it was left out.")
    proteins: list[Chains] = []
    listed_elsewhere = strands(block)
    for row in rows(block, "_entity_poly.", ["entity_id", "type", "?pdbx_strand_id"]):
        entity, kind = row["entity_id"], row["type"].lower()
        chains = sorted(
            (
                c.strip()
                for c in (
                    row["pdbx_strand_id"] or listed_elsewhere.get(row["entity_id"], "")
                ).split(",")
                if c.strip()
            ),
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
        accession = accessions.get(entity, {})
        organism = organisms.get(entity, {})
        if len(accession) > 1 or len(organism) > 1:
            # A chimera: no one entry, organism or gene is the whole of it.
            said = [
                _sourced(found, what)
                for found, what in ((accession, "UniProt"), (organism, "from"))
                if found
            ]
            description = f"{many} {listed} of {shown}, a chimera of {'; '.join(said)}."
            notes.append(
                f"{shown}: chain {listed} is a chimera of "
                f"{' and '.join(accession or organism)}, so its protein has no one UniProt "
                "accession, organism or gene: its description says which residues are which."
            )
            accession, organism, gene = {}, {}, ""
        values = {
            "name": names.get(entity, "") or f"{shown} chain {chains[0] if chains else entity}",
            "description": description,
            "sequence": sequence,
            "gene": gene,
            "uniprot_accession": next(iter(accession), ""),
        }
        # Its organism is a record, by its NCBI Taxonomy ID (ADR 0140); a chimera's has none.
        named = next(iter(organism), "")
        source = _organism(taxa.get(entity, ""), named) if named else None
        if named and source is None:
            notes.append(
                f"{shown} chain {listed}: its organism, {named[:200]}, has no NCBI "
                "Taxonomy ID, so it's left out."
            )
        proteins.append(
            Chains(
                f"{prefix}{entity}",
                _fit(values, f"{shown} chain {listed}", notes),
                chains,
                source,
            )
        )
    if not proteins:
        raise UnreadableError("It has no protein chains.")
    pairs = sorted(
        ((chain, p) for p in proteins for chain in p.ids), key=lambda pair: _chain_order(pair[0])
    )
    # A PDB entry's first revision is its release. Another file's revisions, such as an
    # AlphaFold DB model's, are its own, not a PDB entry's.
    released = min((r["revision_date"] for r in revisions if r["revision_date"]), default="")
    structure = {
        "name": entry or label,
        "description": _text(block, "_struct.title"),
        "pdb_id": entry,
        "method": method,
        "resolution": resolution,
        "chains": [{"$create": p.key} for _, p in pairs],
        "chain_ids": [chain for chain, _ in pairs],
        "released": released if entry else "",
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
        made(block),
    )


# --- Output --------------------------------------------------------------------------------------


def creates(structure: Read, file: Any, origin: dict[str, Any]) -> list[dict[str, Any]]:  # noqa: ANN401 - a file ID or a $file
    """The proteins, then the structure that refers to them. The structure's origin says
    how the file says it was made; the proteins' don't, as their sequences weren't."""
    out: list[dict[str, Any]] = []
    for p in structure.proteins:
        values = p.values
        if p.organism is not None:
            taxon, name = p.organism
            out.append(organism_create(taxon, name))
            values = {**values, "organism": {"$create": f"taxon:{taxon}"}}
        out.append(
            {"type": "iki/protein.Protein", "key": p.key, "values": values, "origin": origin}
        )
    out.append(
        {
            "type": "iki/protein.ProteinStructure",
            "values": {"structure": file, **structure.values},
            "origin": {**origin, "made": structure.made} if structure.made else origin,
        }
    )
    return out


def organism_create(taxon: int, name: str) -> dict[str, Any]:
    """The organism's create (ADR 0140): its entry is found by its ID, or made with it."""
    values: dict[str, Any] = {"taxon_id": taxon}
    if re.fullmatch(r"[A-Za-z0-9][^\t\r\n]{0,254}", name):
        values["scientific_name"] = name
    # A name only fills an entry that has none: an old file may give a taxon's old name.
    fills = ["scientific_name"] if "scientific_name" in values else []
    return {
        "type": "iki/core.Organism",
        "key": f"taxon:{taxon}",
        "values": values,
        **({"fills": fills} if fills else {}),
    }


def once(made: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each organism once, where it's first needed: several structures may share one."""
    seen: set[str] = set()
    out = []
    for create in made:
        if create["type"] == "iki/core.Organism":
            if create["key"] in seen:
                continue
            seen.add(create["key"])
        out.append(create)
    return out


def write(made: list[dict[str, Any]], notes: list[str], count: int) -> None:
    messages = [{"level": "info", "text": note[:2000]} for note in notes[:99]]
    if len(notes) > 99:
        messages.append({"level": "info", "text": f"And {len(notes) - 99} more notes."})
    output = {
        "apiVersion": "iki/v1",
        "kind": "RunOutput",
        "creates": once(made),
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
    """The PDB IDs in `text`, in order, each once, as Iki keeps them: 4HHB for 4hhb and its
    extended form, pdb_00004hhb."""
    words = [w for w in re.split(r"[\s,;]+", text) if w]
    if not words:
        raise UnreadableError("Give at least one PDB ID, such as 4HHB.")
    for word in words:
        if not pdb_id(word):
            raise UnreadableError(f"{word.upper()} isn't a PDB ID, such as 4HHB or pdb_00004hhb.")
    found = list(dict.fromkeys(pdb_id(w) for w in words))
    if len(found) > MAX_ENTRIES:
        raise UnreadableError(
            f"That's {len(found):,} entries, more than the {MAX_ENTRIES} one run fetches: "
            "split them."
        )
    return found


def download(url: str, to: Path, label: str, source: str, missing: str, gunzip: bool) -> None:
    """The file at `url` into `to`, unpacked if `gunzip`, asking again while `source` is
    busy. `missing` is what to say if it has no such file."""
    request = urllib.request.Request(  # noqa: S310 - an https URL of the action's host
        url, headers={"User-Agent": USER_AGENT}
    )
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with (
                urllib.request.urlopen(request, timeout=300) as response,  # noqa: S310
                to.open("wb") as out,
            ):
                given = gzip.GzipFile(fileobj=response) if gunzip else response
                copied = 0
                while chunk := given.read(1024 * 1024):
                    copied += len(chunk)
                    if copied > MAX_BYTES:
                        raise UnreadableError(f"{label}'s mmCIF is over 1 GiB.")
                    out.write(chunk)
            return
        except urllib.error.HTTPError as error:
            error.close()
            if error.code == 404:
                raise UnreadableError(missing) from None
            if attempt < ATTEMPTS and (error.code == 429 or error.code >= 500):
                time.sleep(2**attempt)
                continue
            raise UnreadableError(f"{source} answered {error.code} {error.reason}.") from None
        except (urllib.error.URLError, TimeoutError, EOFError, gzip.BadGzipFile) as error:
            if attempt < ATTEMPTS:
                time.sleep(2**attempt)
                continue
            reason = getattr(error, "reason", error)
            raise UnreadableError(f"Couldn't fetch {label} from {source}: {reason}.") from None
    raise AssertionError("unreachable")


def fetch(run: dict[str, Any]) -> None:
    out_dir = Path(os.environ["IKI_OUT_DIR"])
    made: list[dict[str, Any]] = []
    notes: list[str] = []
    try:
        asked = pdb_ids(run["params"]["pdb_ids"])
        for done, entry in enumerate(asked, start=1):
            if done > 1:
                time.sleep(PAUSE)
            path = out_dir / f"{entry}.cif"
            missing = (
                f"The PDB has no entry {entry}: it may have been made obsolete. "
                "Search for it at https://www.rcsb.org."
            )
            download(DOWNLOAD.format(entry), path, entry, "RCSB", missing, gunzip=True)
            retrieved = datetime.now(UTC).isoformat(timespec="seconds")
            try:
                structure = read(path, entry, f"{entry}-")
            except UnreadableError as why:
                raise UnreadableError(f"{entry}: {why}") from None
            origin: dict[str, Any] = {
                "source": "RCSB PDB",
                "id": entry,
                "url": ENTRY.format(entry),
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


# --- AlphaFold DB --------------------------------------------------------------------------------


def uniprot_accessions(text: str) -> list[str]:
    """The UniProt accessions in `text`, in order, each once, upper-cased."""
    found = list(dict.fromkeys(w.upper() for w in re.split(r"[\s,;]+", text) if w))
    if not found:
        raise UnreadableError("Give at least one UniProt accession, such as P69905.")
    for word in found:
        if UNIPROT.match(word) and "-" in word:
            raise UnreadableError(
                "AlphaFold DB predicts UniProt's canonical sequences: give "
                f"{word.split('-')[0]}, not the isoform {word}."
            )
        if not UNIPROT.match(word):
            raise UnreadableError(f"{word} isn't a UniProt accession, such as P69905.")
    if len(found) > MAX_ENTRIES:
        raise UnreadableError(
            f"That's {len(found):,} accessions, more than the {MAX_ENTRIES} one run fetches: "
            "split them."
        )
    return found


def _answer(accession: str) -> bytes:
    """AlphaFold DB's API's answer for the UniProt entry, asking again while it's busy."""
    request = urllib.request.Request(  # noqa: S310 - a fixed https URL
        PREDICTION.format(accession),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
                data: bytes = response.read(MAX_ANSWER + 1)
        except urllib.error.HTTPError as error:
            error.close()
            if error.code in (400, 404):
                raise UnreadableError(
                    f"AlphaFold DB has no prediction of {accession}. Search for it at "
                    "https://alphafold.ebi.ac.uk."
                ) from None
            if attempt < ATTEMPTS and (error.code == 429 or error.code >= 500):
                time.sleep(2**attempt)
                continue
            raise UnreadableError(f"AlphaFold DB answered {error.code} {error.reason}.") from None
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt < ATTEMPTS:
                time.sleep(2**attempt)
                continue
            reason = getattr(error, "reason", error)
            raise UnreadableError(f"Couldn't reach AlphaFold DB: {reason}.") from None
        if len(data) > MAX_ANSWER:
            raise UnreadableError(f"AlphaFold DB's answer for {accession} is over 16 MiB.")
        return data
    raise AssertionError("unreachable")


def predictions(accession: str) -> list[dict[str, Any]]:
    """AlphaFold DB's models of the UniProt entry, as its API lists them, each with its ID,
    latest version and mmCIF file, checked."""
    try:
        answer = json.loads(_answer(accession))
    except ValueError:
        raise UnreadableError(f"AlphaFold DB's answer for {accession} isn't JSON.") from None
    if not isinstance(answer, list) or not answer:
        raise UnreadableError(f"AlphaFold DB has no prediction of {accession}.")
    for model in answer:
        given = model if isinstance(model, dict) else {}
        model_id, version, url = (
            given.get(k) for k in ("modelEntityId", "latestVersion", "cifUrl")
        )
        if (
            not isinstance(model_id, str)
            or not MODEL_ID.match(model_id)
            or type(version) is not int
            or version < 1
            or not isinstance(url, str)
            or not url.startswith(FILES)
        ):
            raise UnreadableError(
                f"AlphaFold DB's answer for {accession} doesn't give a model's ID, version "
                "and mmCIF file as Iki reads them."
            )
    return answer


def _described(model: dict[str, Any], accession: str) -> str:
    """What the model is of, and what predicted it, as AlphaFold DB's API says."""
    name, tool = model.get("uniprotDescription"), model.get("toolUsed")
    text = accession
    if isinstance(name, str) and name.strip():
        text = f"{' '.join(name.split())} ({accession})"
    if isinstance(tool, str) and tool.strip():
        text += f", predicted by {' '.join(tool.split())}"
    return f"{text}, from AlphaFold DB."[: LIMITS["description"]]


def fetch_predicted(run: dict[str, Any]) -> None:
    out_dir = Path(os.environ["IKI_OUT_DIR"])
    made: list[dict[str, Any]] = []
    notes: list[str] = []
    count = 0
    try:
        asked = uniprot_accessions(run["params"]["accessions"])
        for done, accession in enumerate(asked, start=1):
            if done > 1:
                time.sleep(PAUSE)
            for model in predictions(accession):
                model_id, version = model["modelEntityId"], model["latestVersion"]
                time.sleep(PAUSE)
                path = out_dir / f"{model_id}-model_v{version}.cif"
                missing = f"AlphaFold DB has no mmCIF file of {model_id}, version {version}."
                download(model["cifUrl"], path, model_id, "AlphaFold DB", missing, gunzip=False)
                retrieved = datetime.now(UTC).isoformat(timespec="seconds")
                try:
                    structure = read(path, model_id, f"{model_id}-")
                except UnreadableError as why:
                    raise UnreadableError(f"{model_id}: {why}") from None
                structure.values.setdefault("description", _described(model, accession))
                origin: dict[str, Any] = {
                    "source": "AlphaFold DB",
                    "id": model_id,
                    "version": str(version),
                    "url": AF_ENTRY.format(accession),
                    "retrieved_at": retrieved,
                    "sha256": _sha256(path),
                }
                file = {"$file": path.name, "media_type": "chemical/x-mmcif"}
                made.extend(creates(structure, file, origin))
                notes.extend(structure.notes)
                count += 1
                plddt = ((structure.made or {}).get("details") or {}).get("mean_plddt")
                print(_summary(structure) + (f", mean pLDDT {plddt}" if plddt else ""))
            print(f"::progress {done / len(asked):.2f} {done} of {len(asked)}", flush=True)
    except UnreadableError as why:
        print(f"::error {why}", flush=True)
        sys.exit(1)
    write(made, notes, count)


def main() -> None:
    kind = sys.argv[1]
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    {"fetch": fetch, "file": from_file, "alphafold": fetch_predicted}[kind](run)


if __name__ == "__main__":
    main()
