"""Import native proteomics reports while preserving their identification levels."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import iki_action as iki

from iki_runtime import text

MISSING = {"", "null", "na", "nan", "n/a"}
PREVIEW_ROWS = 12
MZTAB_LEVELS = {"PRT": "protein_group", "PEP": "peptide", "PSM": "spectrum_match"}
MZTAB_HEADERS = {"PRH": "PRT", "PEH": "PEP", "PSH": "PSM"}
MZTAB_DECOY_COLUMN = re.compile(r"opt_(?:global_)?cv_(?:MS:1002217|PRIDE:0000303)(?:_|$)", re.I)
#: The supported reports: each format names one tool's export and the table form it is read in.
FORMATS = {
    "mztab_1_0": "HUPO-PSI mzTab 1.0 proteomics (1.0.0, or the PRIDE 1.0 rc5 export)",
    "maxquant_txt": "MaxQuant txt table: proteinGroups.txt, peptides.txt or evidence.txt",
    "diann_tsv": "DIA-NN main report as tab-separated text",
    "fragpipe_tsv": "FragPipe protein or peptide TSV, single or combined",
}
PARQUET = b"PAR1"


@contextmanager
def lines_at(path: Path) -> Iterator[Iterable[str]]:
    with path.open("rb") as source:
        start = source.read(4)
    if start == PARQUET:
        raise ValueError(
            "This is a Parquet file. Import the report as tab-separated text: convert a "
            "Parquet main report to TSV with its column names unchanged."
        )
    with text.open_text(path) as source:
        yield source


def distinct_columns(header: list[str]) -> bool:
    return bool(header) and "" not in header and len(header) == len(set(header))


def table(lines: Iterable[str]) -> tuple[list[str], Iterator[dict[str, str]]]:
    reader = csv.reader(lines, delimiter="\t")
    header = next(reader, [])
    if not distinct_columns(header):
        raise ValueError("The first row must name distinct, nonempty report columns.")

    def rows() -> Iterator[dict[str, str]]:
        for number, cells in enumerate(reader, 2):
            if not cells:
                continue
            if len(cells) != len(header):
                raise ValueError(f"Row {number} has {len(cells)} columns, expected {len(header)}.")
            yield dict(zip(header, cells, strict=True))

    return header, rows()


def accession_list(*values: str) -> list[str]:
    accessions = {}
    for value in values:
        for item in re.split(r"[;,]", value):
            token = item.strip()
            if token.lower() in MISSING:
                continue
            fields = token.split("|")
            accession = fields[1] if len(fields) == 3 and fields[0] in {"sp", "tr"} else token
            accessions[accession] = None
    return list(accessions)


def numeric(
    row: dict[str, str], columns: Iterable[str], *, keep_nan: bool = False
) -> dict[str, float | str | None]:
    result: dict[str, float | str | None] = {}
    for column in columns:
        if column not in row:
            continue
        raw = row[column].strip()
        if keep_nan and raw.lower() == "nan":
            result[column] = "NaN"
            continue
        if raw.lower() in MISSING:
            result[column] = None
            continue
        try:
            number = float(raw)
        except ValueError:
            raise ValueError(f"Column {column!r} has a nonnumeric value.") from None
        # mzTab can explicitly report non-finite quantities. JSON retains the notation.
        result[column] = number if math.isfinite(number) else raw
    return result


def hit(
    level: str,
    accessions: list[str],
    row: dict[str, str],
    quantities: Iterable[str],
    scores: Iterable[str] = (),
    *,
    keep_nan: bool = False,
    **details: object,
) -> dict[str, Any]:
    return {
        "level": level,
        "accessions": accessions,
        "quantities": numeric(row, quantities, keep_nan=keep_nan),
        "scores": numeric(row, scores, keep_nan=keep_nan),
        **details,
    }


def mztab_decoy(row: dict[str, str]) -> bool | None:
    flags = {
        value.strip().lower() for column, value in row.items() if MZTAB_DECOY_COLUMN.match(column)
    } - MISSING
    if not flags:
        return None
    if not flags <= {"0", "1", "false", "true"}:
        raise ValueError("An mzTab decoy flag must be 0, 1, true or false.")
    return bool(flags & {"1", "true"})


def mztab_rows(lines: Iterable[str], about: dict[str, str]) -> Iterator[dict[str, Any]]:
    # Proteomics mzTab 1.0, https://github.com/HUPO-PSI/mzTab.
    version = ""
    headers = {}
    for number, line in enumerate(lines, 1):
        cells = line.rstrip("\r\n").split("\t")
        tag = cells[0]
        if tag == "MTD" and len(cells) >= 3 and cells[1] == "mzTab-version":
            version = cells[2]
        if tag in MZTAB_HEADERS:
            header = cells[1:]
            required = {"accession", "sequence"} if tag != "PRH" else {"accession"}
            if not distinct_columns(header) or not required <= set(header):
                raise ValueError(f"Invalid mzTab {tag} header on line {number}.")
            headers[MZTAB_HEADERS[tag]] = header
        if tag not in MZTAB_LEVELS:
            continue
        if version not in {"1.0.0", "1.0 rc5"}:
            raise ValueError(
                "The importer expects proteomics mzTab 1.0.0 or the PRIDE 1.0 rc5 export."
            )
        about["table"] = f"mzTab {version}"
        header = headers.get(tag)
        if not header or len(header) != len(cells) - 1:
            raise ValueError(f"mzTab row {number} does not match its section header.")
        row = dict(zip(header, cells[1:], strict=True))
        prefix = "protein" if tag == "PRT" else "peptide"
        yield hit(
            MZTAB_LEVELS[tag],
            accession_list(row["accession"], row.get("ambiguity_members", "")),
            row,
            (name for name in header if name.startswith(f"{prefix}_abundance_")),
            (
                name
                for name in header
                if name.startswith(("best_search_engine_score", "search_engine_score"))
            ),
            sequence=row.get("sequence"),
            reported_id=row.get("PSM_ID"),
            spectrum=row.get("spectra_ref"),
            modifications=row.get("modifications"),
            mztab_version=version,
            keep_nan=True,
            decoy=mztab_decoy(row),
        )


def maxquant_rows(
    header: list[str], rows: Iterable[dict[str, str]], about: dict[str, str]
) -> Iterator[dict[str, Any]]:
    # Released MaxQuant tables documentation accompanies this public dataset:
    # MassIVE MSV000088694, quant/Maxquant/BulkSet/txt/tables.pdf.
    columns = set(header)
    # MaxQuant's other tables also have Sequence, Proteins and Raw file columns, but their
    # rows aren't peptides or precursors: each is one MS/MS scan, one MS1 feature or one
    # modified form of a peptide. Read as evidence.txt, every scan would count as a
    # precursor.
    other = next(
        (
            (table, unit)
            for table, unit, found in (
                ("msms.txt or msmsScans.txt", "MS/MS scan", "Scan number" in columns),
                ("allPeptides.txt", "MS1 feature", "MSMS Scan Numbers" in columns),
                (
                    "modificationSpecificPeptides.txt",
                    "modified form of a peptide",
                    "Evidence IDs" in columns
                    and ("Peptide ID" in columns or "Raw file" in columns),
                ),
            )
            if found
        ),
        None,
    )
    if other is not None:
        table, unit = other
        raise ValueError(
            f"This is MaxQuant's {table}, one row per {unit}. Choose proteinGroups.txt, "
            "peptides.txt or evidence.txt."
        )
    protein = "Protein IDs" in header and "Sequence" not in header
    accession = "Protein IDs" if protein else "Proteins"
    if accession not in header or (not protein and "Sequence" not in header):
        raise ValueError("Choose MaxQuant proteinGroups.txt, peptides.txt or evidence.txt.")
    about["table"] = (
        "proteinGroups.txt"
        if protein
        else ("evidence.txt" if "Raw file" in header else "peptides.txt")
    )
    quantities = [
        key
        for key in header
        if key == "Intensity" or key.startswith(("Intensity ", "LFQ intensity", "iBAQ", "Ratio "))
    ]
    for row in rows:
        flags = [
            key
            for key in ("Reverse", "Potential contaminant", "Only identified by site")
            if row.get(key) == "+"
        ]
        identification = hit(
            "protein_group" if protein else ("precursor" if "Raw file" in header else "peptide"),
            accession_list(row[accession]),
            row,
            quantities,
            ("Score", "PEP"),
            sequence=row.get("Sequence"),
            modified_sequence=row.get("Modified sequence"),
            run=row.get("Raw file"),
            reported_id=row.get("id"),
            flags=flags,
            decoy="Reverse" in flags,
        )
        # Zero is MaxQuant's missing-value sentinel for abundance. Ratios can be zero.
        for column, value in identification["quantities"].items():
            if value == 0 and column.startswith(("Intensity", "LFQ intensity", "iBAQ")):
                identification["quantities"][column] = None
        yield identification


def diann_rows(
    header: list[str], rows: Iterable[dict[str, str]], about: dict[str, str]
) -> Iterator[dict[str, Any]]:
    # DIA-NN report field definitions: https://github.com/vdemichev/DiaNN.
    if not {"Run", "Protein.Group", "Stripped.Sequence", "Precursor.Id"} <= set(header):
        raise ValueError(
            "Choose a DIA-NN main TSV report with Run, Protein.Group and precursor columns."
        )
    about["table"] = "main report"
    # DIA-NN 1.8 reports PG.Quantity and PG.Normalised; 2.x reports PG.TopN instead.
    quantities = ("PG.MaxLFQ", "PG.Normalised", "PG.Quantity", "PG.TopN")
    groups: dict[tuple[str, str, str], dict[str, float | str | None]] = {}
    for row in rows:
        context = {"run": row["Run"], "channel": row.get("Channel") or ""}
        accessions = accession_list(row["Protein.Group"])
        is_decoy = row.get("Decoy", "").lower() in {"1", "true"}
        yield hit(
            "precursor",
            accessions,
            row,
            ("Precursor.Quantity", "Precursor.Normalised"),
            ("Q.Value", "Global.Q.Value"),
            sequence=row["Stripped.Sequence"],
            modified_sequence=row.get("Modified.Sequence"),
            precursor=row["Precursor.Id"],
            decoy=is_decoy,
            **context,
        )
        if is_decoy or not accessions:
            continue
        key = (row["Protein.Group"], context["run"], context["channel"])
        group = hit(
            "protein_group",
            accessions,
            row,
            quantities,
            ("PG.Q.Value", "Global.PG.Q.Value"),
            decoy=False,
            **context,
        )
        values = group["quantities"]
        if key in groups:
            if groups[key] != values:
                raise ValueError(
                    "DIA-NN repeats a protein group and run with different quantities."
                )
            continue
        if len(groups) >= 500_000:
            raise ValueError(
                "Split this DIA-NN report into datasets with fewer protein group/run pairs."
            )
        groups[key] = values
        yield group


def fragpipe_rows(
    header: list[str], rows: Iterable[dict[str, str]], about: dict[str, str]
) -> Iterator[dict[str, Any]]:
    # Column definitions: https://fragpipe.nesvilab.org/docs/tutorial_fragpipe_outputs.html
    if "Spectrum" in header or {"Charge", "M/Z"} <= set(header):
        raise ValueError(
            "Choose a FragPipe protein or peptide TSV. PSM and ion tables are unsupported."
        )
    if {"Modified Sequence", "Charges"} <= set(header):
        # combined_modified_peptide.tsv: one row per modified form of each peptide.
        raise ValueError(
            "This is FragPipe's modified peptide table, one row per modified form of a "
            "peptide. Choose combined_peptide.tsv, peptide.tsv or a protein table."
        )
    if "Protein ID" not in header:
        raise ValueError("Choose a FragPipe protein or peptide TSV with the Protein ID column.")
    sequence = next((key for key in ("Peptide Sequence", "Peptide") if key in header), None)
    about["table"] = "peptide" if sequence else "protein"
    quantities = [
        key
        for key in header
        if key.endswith(("Intensity", "Spectral Count")) or key == "Spectral Count"
    ]
    for row in rows:
        yield hit(
            "peptide" if sequence else "protein_group",
            accession_list(
                row["Protein ID"],
                row.get("Mapped Proteins", ""),
                row.get("Indistinguishable Proteins", ""),
            ),
            row,
            quantities,
            ("Protein Probability", "PeptideProphet Probability", "Probability"),
            sequence=row[sequence] if sequence else None,
            modified_sequence=row.get("Modified Sequence"),
            modifications=row.get("Assigned Modifications"),
        )


def parsed(
    format_: str, source: Iterable[str], about: dict[str, str] | None = None
) -> Iterator[dict[str, Any]]:
    """The report's identifications. ``about`` gets the table the report turned out to be."""
    if format_ not in FORMATS:
        raise ValueError(f"Choose one of the supported formats: {', '.join(FORMATS)}.")
    about = {} if about is None else about
    if format_ == "mztab_1_0":
        yield from mztab_rows(source, about)
    else:
        header, rows = table(source)
        adapter = {
            "maxquant_txt": maxquant_rows,
            "diann_tsv": diann_rows,
            "fragpipe_tsv": fragpipe_rows,
        }[format_]
        yield from adapter(header, rows, about)


def exclusion_of(row: dict[str, Any]) -> str:
    if row.get("decoy"):
        return "decoy"
    flags = row.get("flags", ())
    if "Potential contaminant" in flags:
        return "contaminant"
    if "Only identified by site" in flags:
        return "site_only"
    return ""


def mztab_identification(row: dict[str, Any], exclusion: str) -> tuple[str, ...] | None:
    # mzTab 1.0 sections 6.4.2 and 6.5.2 repeat an identification for each accession.
    level = row["level"]
    if level == "spectrum_match":
        psm_id = row["reported_id"]
        if psm_id and psm_id.lower() not in MISSING:
            return level, exclusion, psm_id
    if level == "peptide":
        sequence = row["sequence"]
        if sequence and sequence.lower() not in MISSING:
            return level, exclusion, sequence, row.get("modifications") or ""
    return None


#: The most distinct spectrum matches and peptides one mzTab report may have. It fits the
#: environment's memory with room to spare: an uncompressed report of the largest size
#: the action takes has at most about 6 million rows; only a compressed one can exceed it.
MAX_IDENTIFICATIONS = 10_000_000


class Seen:
    """The identifications a report has had, as a set of fixed-size digests in one flat,
    open-addressed table: 12 bytes a slot, so millions of spectrum matches fit in a few
    hundred MiB where tuples of strings took over a GiB. 96-bit BLAKE2b digests make a
    collision between two of ten million keys about a one-in-10^15 chance."""

    WIDTH = 12
    EMPTY = bytes(WIDTH)

    def __init__(self) -> None:
        self.bits = 16
        self.slots = bytearray(self.WIDTH << self.bits)
        self.count = 0

    def _place(self, slots: bytearray, bits: int, digest: bytes) -> bool:
        width, mask = self.WIDTH, (1 << bits) - 1
        at = int.from_bytes(digest[:8], "little") & mask
        while True:
            start = at * width
            held = slots[start : start + width]
            if held == self.EMPTY:
                slots[start : start + width] = digest
                return True
            if held == digest:
                return False
            at = (at + 1) & mask

    def add(self, key: tuple[str, ...]) -> bool:
        """Whether the key is new, adding it."""
        digest = hashlib.blake2b("\x1f".join(key).encode(), digest_size=self.WIDTH).digest()
        if digest == self.EMPTY:
            digest = b"\x01" + digest[1:]
        if not self._place(self.slots, self.bits, digest):
            return False
        self.count += 1
        if self.count > MAX_IDENTIFICATIONS:
            raise ValueError(
                f"It has more than {MAX_IDENTIFICATIONS:,} spectrum matches and peptides, more "
                "than Iki reads from one report: import the search's runs as separate datasets."
            )
        if self.count * 10 > 6 << self.bits:
            self._grow()
        return True

    def _grow(self) -> None:
        bits, width = self.bits + 1, self.WIDTH
        slots = bytearray(width << bits)
        old = memoryview(self.slots)
        for start in range(0, len(old), width):
            digest = bytes(old[start : start + width])
            if digest != self.EMPTY:
                self._place(slots, bits, digest)
        old.release()
        self.slots, self.bits = slots, bits


def identity(record: dict[str, Any]) -> str:
    return str(iki.ref(record))


def result(run: dict[str, Any], artifact: Path) -> dict[str, Any]:
    params = run["params"]
    dataset = run["inputs"]["dataset"][0]
    proteins = run["inputs"].get("proteins", [])
    by_accession = {}
    for protein in proteins:
        accession = protein["values"].get("uniprot_accession")
        if not accession or accession in by_accession:
            raise ValueError("Select protein records with distinct UniProt accessions.")
        by_accession[accession] = protein
    report_ids = params["reports"]
    if not report_ids or len(report_ids) != len(set(report_ids)):
        raise ValueError("Choose one or more distinct report files.")
    checksums = {run["files"][report_id]["sha256"] for report_id in report_ids}
    if len(checksums) != len(report_ids):
        raise ValueError("The selected reports include duplicate file content.")
    format_ = params["format"]
    selected_proteins = {}
    summaries = []
    preview = []
    totals: Counter[str] = Counter()
    excluded_totals: defaultdict[str, Counter[str]] = defaultdict(Counter)
    with artifact.open("w", encoding="utf-8") as output:
        for report_id in report_ids:
            file = run["files"][report_id]
            counts: Counter[str] = Counter()
            excluded: defaultdict[str, Counter[str]] = defaultdict(Counter)
            seen = Seen()
            # The preview's own rows, by identification: a repeat adds its accessions.
            previewed: dict[tuple[str, ...], dict[str, Any]] = {}
            about: dict[str, str] = {}
            try:
                with lines_at(Path(file["path"])) as source:
                    for row in parsed(format_, source, about):
                        exclusion = exclusion_of(row)
                        parents = [
                            by_accession[accession]
                            for accession in row["accessions"]
                            if accession in by_accession and not exclusion
                        ]
                        row["registered_proteins"] = [identity(protein) for protein in parents]
                        selected_proteins.update(
                            {identity(protein): protein for protein in parents}
                        )
                        row["report"] = report_id
                        # The artifact retains each source row, including evidence per accession.
                        output.write(json.dumps(row, allow_nan=False) + "\n")
                        key = (
                            mztab_identification(row, exclusion) if format_ == "mztab_1_0" else None
                        )
                        if key is not None and not seen.add(key):
                            earlier = previewed.get(key)
                            if earlier is not None:
                                for field in ("accessions", "registered_proteins"):
                                    earlier[field] = list(
                                        dict.fromkeys(earlier[field] + row[field])
                                    )
                            continue
                        counter = excluded[exclusion] if exclusion else counts
                        counter[row["level"]] += 1
                        if len(preview) < PREVIEW_ROWS:
                            preview.append(row)
                            if key is not None:
                                previewed[key] = row
            except (ValueError, csv.Error) as exc:
                raise ValueError(f"{file['name']}: {exc}") from None
            if not counts and not excluded:
                raise ValueError(f"{file['name']} contains no recognized identification rows.")
            totals.update(counts)
            for exclusion, counter in excluded.items():
                excluded_totals[exclusion].update(counter)
            summaries.append(
                {
                    "file": report_id,
                    "name": file["name"],
                    "sha256": file["sha256"],
                    "table": about.get("table"),
                    "counts": counts,
                    "excluded_counts": excluded,
                }
            )
    values = dataset["values"]
    return {
        "creates": [
            {
                "type": "iki/proteomics.ProteomicsResult",
                "derived_from": [
                    dataset["id"],
                    *(protein["id"] for protein in selected_proteins.values()),
                ],
                "values": {
                    "dataset": dataset["id"],
                    "dataset_revision": dataset["revision"],
                    "raw_files": values.get("raw_files") or [],
                    "archive": values.get("archive"),
                    "proteins": list(selected_proteins),
                    "reports": report_ids,
                    "format": format_,
                    "version": params["version"],
                    "summary": {
                        "counts": totals,
                        "excluded_counts": excluded_totals,
                        "sources": summaries,
                        "preview": preview,
                        "unmatched_accessions": [
                            accession
                            for accession, protein in by_accession.items()
                            if identity(protein) not in selected_proteins
                        ],
                    },
                    "review_flow": run.get("settings", {}).get("review_flow", "research"),
                },
            }
        ],
    }


def import_report(run: iki.Run) -> dict[str, Any]:
    return result(run, run.out_dir / "identifications.jsonl")


if __name__ == "__main__":
    iki.main(import_report, refuse=(ValueError, *text.UNREADABLE))
