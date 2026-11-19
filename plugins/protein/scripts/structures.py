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

Each protein's organism is by its NCBI Taxonomy ID, with the name the file gives, if any
(ADR 0140). A ModelCIF file's target takes its UniProt entry, gene and organism each from
one of its references, never mixed: the primary one, or else the UniProt one's entry and
the organism they all agree on (see `targets`).

A structure determined by several methods (a joint X-ray and neutron structure) keeps each
method's resolution, from its own refinement; its method and resolution are the first's.

A PDB ID is its four characters (1CRN) or wwPDB's extended form, pdb_ and eight letters or
digits (pdb_00001crn, as `_database_2` gives it, or pdb_1001ba3c). An extended ID of an
entry that has a four-character one is the same entry, so it's kept as that (1CRN),
whichever was given.

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
#: wwPDB's extended PDB ID, upper-cased, as the structure's `pdb_id` field stores it: pdb_
#: and eight letters or digits, as the PDBx/mmCIF dictionary gives it (pdb_[a-z0-9]{8}):
#: pdb_1001ba3c, as OneDep issues them once the four-character IDs run out. An entry with
#: a four-character ID has pdb_0000 before it (pdb_00001abc is 1ABC).
EXTENDED_PDB_ID = re.compile(r"^PDB_[0-9A-Z]{8}$")
UNIPROT = re.compile(
    r"^([OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2})(-[0-9]+)?$"
)
RESIDUES = re.compile(r"^[ACDEFGHIKLMNPQRSTVWYUOBZJX]+$")
#: A name an organism's scientific_name takes (iki/core.Organism): one line, from a letter or
#: a digit, or NCBI's [ for a genus it hasn't placed ([Clostridium] scindens) or ' for an
#: informal name ('Nostoc azollae' 0708).
SCIENTIFIC_NAME = re.compile(r"[A-Za-z0-9\['][^\t\r\n]{0,254}")
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


@dataclass
class Source:
    """An organism a file says an entity's residues are from: its NCBI Taxonomy ID and
    name, as the file gives them (either may be missing), and the residues it's for."""

    taxon: str
    name: str
    spans: list[Span] = field(default_factory=list)

    @property
    def shown(self) -> str:
        return self.name or f"NCBI taxon {self.taxon}"

    def same(self, taxon: str, name: str) -> bool:
        """Whether a row naming `taxon` and `name` is this organism: the same ID, or, where
        either has none, the same name."""
        if taxon and self.taxon:
            return taxon == self.taxon
        return bool(name) and name == self.name


def _grouped(found: list[tuple[str, str, Span]]) -> list[Source]:
    """Rows of (taxon, name, span) as the organisms they name, each once."""
    out: list[Source] = []
    for taxon, name, span in found:
        if not taxon and not name:
            continue
        source = next((s for s in out if s.same(taxon, name)), None)
        if source is None:
            source = Source(taxon, name)
            out.append(source)
        source.taxon, source.name = source.taxon or taxon, source.name or name
        source.spans.append(span)
    return out


@dataclass
class Target:
    """What a ModelCIF file's references (`_ma_target_ref_db_details`) say of one target:
    each of its parts from one reference, never mixed from several."""

    accession: str = ""
    gene: str = ""
    source: Source | None = None


def _reference(row: dict[str, str]) -> str:
    named = {"UNP": "UniProt"}.get(row["db_name"], row["db_name"] or "a database")
    taxon = f", taxon {row['ncbi_taxonomy_id']}" if row["ncbi_taxonomy_id"] else ""
    return f"{named} {row['db_accession'] or '?'}{taxon}"


def targets(block: gemmi.cif.Block, label: str) -> tuple[dict[str, Target], list[str]]:
    """Each ModelCIF target's UniProt entry, gene and organism, and notes. A reference
    marked primary (`is_primary` yes, ModelCIF's main input) gives all of them. Without
    one, the UniProt entry and its gene are the UniProt reference's, if there's one, and
    the organism is the one every reference that names one agrees on: references that
    disagree, such as a UniProt and an OrthoDB entry of different strains, leave it out,
    saying so."""
    found: dict[str, list[dict[str, str]]] = {}
    for row in rows(
        block,
        "_ma_target_ref_db_details.",
        [
            "target_entity_id",
            "?db_name",
            "?db_accession",
            "?ncbi_taxonomy_id",
            "?organism_scientific",
            "?gene_name",
            "?is_primary",
        ],
    ):
        found.setdefault(row["target_entity_id"], []).append(row)
    out: dict[str, Target] = {}
    notes: list[str] = []
    for entity, given in found.items():
        primary = [r for r in given if r["is_primary"].lower() == "yes"]
        if len(primary) == 1 or len(given) == 1:
            (row,) = primary or given
            accession = row["db_accession"] if row["db_name"] == "UNP" else ""
            source = None
            if row["ncbi_taxonomy_id"] or row["organism_scientific"]:
                source = Source(row["ncbi_taxonomy_id"], row["organism_scientific"], [None])
            out[entity] = Target(accession, row["gene_name"], source)
            continue
        target = Target()
        uniprot = [r for r in given if r["db_name"] == "UNP" and UNIPROT.match(r["db_accession"])]
        entries = list(dict.fromkeys(r["db_accession"] for r in uniprot))
        disagree: list[str] = []
        if len(entries) == 1:
            target.accession, target.gene = entries[0], uniprot[0]["gene_name"]
        elif entries:
            disagree.append("UniProt entry")
        named = [r for r in given if r["ncbi_taxonomy_id"]]
        if len({r["ncbi_taxonomy_id"] for r in named}) == 1:
            row = named[0]
            target.source = Source(row["ncbi_taxonomy_id"], row["organism_scientific"], [None])
        elif named:
            disagree.append("organism")
        out[entity] = target
        if disagree:
            notes.append(
                f"{label}: entity {entity}'s references disagree "
                f"({'; '.join(_reference(r) for r in given)}) and none is marked primary, so "
                f"its protein has no {' or '.join(disagree)} from them."
            )
    return out, notes


def _organisms(block: gemmi.cif.Block) -> dict[str, list[Source]]:
    """Each entity's source organisms, with the residues each is for: from where the gene
    was cloned (`_entity_src_gen`), or else the natural source or the synthetic one. A
    chimera has a row for each of its segments."""
    out: dict[str, list[Source]] = {}
    for category, name_tag, taxon_tag in (
        ("_entity_src_gen.", "pdbx_gene_src_scientific_name", "pdbx_gene_src_ncbi_taxonomy_id"),
        ("_entity_src_nat.", "pdbx_organism_scientific", "pdbx_ncbi_taxonomy_id"),
        ("_pdbx_entity_src_syn.", "organism_scientific", "ncbi_taxonomy_id"),
    ):
        found: dict[str, list[tuple[str, str, Span]]] = {}
        for row in rows(
            block,
            category,
            [
                "entity_id",
                f"?{name_tag}",
                f"?{taxon_tag}",
                "?pdbx_beg_seq_num",
                "?pdbx_end_seq_num",
            ],
        ):
            span = _span(row["pdbx_beg_seq_num"], row["pdbx_end_seq_num"])
            found.setdefault(row["entity_id"], []).append((row[taxon_tag], row[name_tag], span))
        for entity, given in found.items():
            grouped = _grouped(given)
            if grouped:
                out.setdefault(entity, grouped)
    return out


def _accessions(block: gemmi.cif.Block) -> dict[str, dict[str, list[Span]]]:
    """Each entity's UniProt entries, with the residues each is aligned to: the PDB's
    references (`_struct_ref`, aligned by `_struct_ref_seq`). wwPDB gives a chimera a
    reference for each segment (2RH1: P07550, P00720, P07550)."""
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
    return out


#: Where an experiment's resolution is: its refinement's, or its reconstruction's (cryo-EM),
#: or else its data's.
RESOLUTION_TAGS = (
    "_refine.ls_d_res_high",
    "_em_3d_reconstruction.resolution",
    "_reflns.d_resolution_high",
)


def _first_value(block: gemmi.cif.Block, tag: str) -> str:
    """A tag's value, its first row's in a loop; empty for CIF's unknown (?) and . ."""
    category, _, item = tag.rpartition(".")
    found = rows(block, f"{category}.", [item])
    return next((r[item] for r in found if r[item]), "")


def _described_method(method: str) -> str:
    """NEUTRON DIFFRACTION as neutron diffraction, X-RAY DIFFRACTION as X-ray diffraction."""
    return method.lower().replace("x-ray", "X-ray")


def resolutions(
    block: gemmi.cif.Block, methods: list[str], shown: str, notes: list[str]
) -> tuple[float | None, list[dict[str, Any]]]:
    """The structure's resolution, and, if it was determined by several methods (a joint
    X-ray and neutron structure, say), each one's, from its own refinement
    (`_refine.pdbx_refine_id`): its resolution is then its first method's, as the file lists
    them. A value that isn't a number, or that no method can be told to be from, is left
    out, saying so."""
    if len(methods) <= 1:
        given = next((v for v in (_first_value(block, t) for t in RESOLUTION_TAGS) if v), "")
        resolution = _number(given)
        if given and resolution is None:
            notes.append(f"{shown}: its resolution, {given}, isn't a number, so it was left out.")
        return resolution, []
    refined: dict[str, str] = {}
    for row in rows(block, "_refine.", ["ls_d_res_high", "?pdbx_refine_id"]):
        if row["ls_d_res_high"]:
            refined.setdefault(row["pdbx_refine_id"].upper(), row["ls_d_res_high"])
    reconstructed = _first_value(block, "_em_3d_reconstruction.resolution")
    each: list[dict[str, Any]] = []
    said: list[str] = []
    for method in methods:
        given = refined.get(method, "")
        if not given and method == "ELECTRON MICROSCOPY":
            given = reconstructed
        value = _number(given)
        if given and value is None:
            notes.append(
                f"{shown}: its {_described_method(method)} resolution, {given}, isn't a "
                "number, so it was left out."
            )
        each.append({"method": METHODS.get(method, "other"), "resolution": value})
        said.append(
            f"{_described_method(method)}"
            + (f" ({value:g} Å)" if value is not None else " (no resolution given)")
        )
    unplaced = sorted(set(refined) - set(methods))
    if unplaced:
        listed = ", ".join(refined[m] for m in unplaced)
        notes.append(
            f"{shown}: its refinement at {listed} Å doesn't say which of its methods it's "
            "for, so it was left out."
        )
    notes.append(
        f"{shown} was determined by {' and '.join(said)}: its method and resolution are the "
        "first's, as the file lists them, and its resolutions give each."
    )
    return each[0]["resolution"], each


def read(path: Path, label: str, prefix: str) -> Read:
    """The structure in a file, with the proteins its chains are. Keys start with
    `prefix`, so several structures' creates can go in one output."""
    block = block_of(path)
    notes: list[str] = []
    entry = pdb_id(_text(block, "_entry.id"))
    method_names = list(
        dict.fromkeys(
            r["method"].upper() for r in rows(block, "_exptl.", ["method"]) if r["method"]
        )
    )
    method = next((METHODS.get(m, "other") for m in method_names), "")
    if not method and rows(block, "_ma_model_list.", ["model_id"]):
        method = "predicted"  # a ModelCIF file's model, without _exptl
    shown = entry or label
    resolution, by_method = resolutions(block, method_names, shown, notes)
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
    # A ModelCIF file's targets: the sequence database records it modelled. The PDB's own
    # categories, where a file has them, come first.
    modelled, said = targets(block, shown)
    notes.extend(said)
    organisms = _organisms(block)
    accessions = _accessions(block)
    for entity, target in modelled.items():
        if target.source is not None:
            organisms.setdefault(entity, [target.source])
        if target.accession and UNIPROT.match(target.accession):
            accessions.setdefault(entity, {target.accession: [None]})
    genes = {
        **{entity: target.gene for entity, target in modelled.items()},
        **_by_entity(block, "_entity_src_gen.", "?pdbx_gene_src_gene"),
    }
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
        sources = organisms.get(entity, [])
        organism = {s.shown: s.spans for s in sources}
        if len(accession) > 1 or len(sources) > 1:
            # A chimera: no one entry, organism or gene is the whole of it.
            parts = [
                _sourced(found, what)
                for found, what in ((accession, "UniProt"), (organism, "from"))
                if found
            ]
            description = f"{many} {listed} of {shown}, a chimera of {'; '.join(parts)}."
            notes.append(
                f"{shown}: chain {listed} is a chimera of "
                f"{' and '.join(accession or organism)}, so its protein has no one UniProt "
                "accession, organism or gene: its description says which residues are which."
            )
            accession, sources, gene = {}, [], ""
        values = {
            "name": names.get(entity, "") or f"{shown} chain {chains[0] if chains else entity}",
            "description": description,
            "sequence": sequence,
            "gene": gene,
            "uniprot_accession": next(iter(accession), ""),
        }
        # Its organism is a record, by its NCBI Taxonomy ID (ADR 0140), with the name the
        # file gives it, if any; a chimera's has none.
        source = None
        if sources:
            (only,) = sources
            source = _organism(only.taxon, only.name)
            if source is None:
                notes.append(
                    f"{shown} chain {listed}: its organism, {only.shown[:200]}, has no NCBI "
                    "Taxonomy ID, so it's left out."
                )
            elif source[1] and not SCIENTIFIC_NAME.fullmatch(source[1]):
                notes.append(
                    f"{shown} chain {listed}: its organism's name, {source[1][:200]}, isn't "
                    "one an organism's scientific name can be, so its organism has only its "
                    f"NCBI Taxonomy ID, {source[0]}."
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
        "resolutions": by_method,
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
    if SCIENTIFIC_NAME.fullmatch(name):
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
