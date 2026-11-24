"""Import native proteomics reports while preserving their identification levels."""

from __future__ import annotations

import csv
import gzip
import json
import math
import os
import re
import zlib
from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

MISSING = {"", "null", "na", "nan", "n/a"}
PREVIEW_ROWS = 12
MZTAB_LEVELS = {"PRT": "protein_group", "PEP": "peptide", "PSM": "spectrum_match"}
MZTAB_HEADERS = {"PRH": "PRT", "PEH": "PEP", "PSH": "PSM"}


@contextmanager
def lines_at(path: Path) -> Iterator[Iterable[str]]:
    with path.open("rb") as source:
        compressed = source.peek(2)[:2] == b"\x1f\x8b"
    with (
        gzip.open(path, "rt", encoding="utf-8-sig", newline="")
        if compressed
        else path.open(encoding="utf-8-sig", newline="")
    ) as source:
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


def numeric(row: dict[str, str], columns: Iterable[str]) -> dict[str, float | str | None]:
    result: dict[str, float | str | None] = {}
    for column in columns:
        if column not in row:
            continue
        raw = row[column].strip()
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
    **details: object,
) -> dict[str, Any]:
    return {
        "level": level,
        "accessions": accessions,
        "quantities": numeric(row, quantities),
        "scores": numeric(row, scores),
        **details,
    }


def mztab_rows(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
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
        )


def maxquant_rows(header: list[str], rows: Iterable[dict[str, str]]) -> Iterator[dict[str, Any]]:
    # Released MaxQuant tables documentation accompanies this public dataset:
    # MassIVE MSV000088694, quant/Maxquant/BulkSet/txt/tables.pdf.
    protein = "Protein IDs" in header and "Sequence" not in header
    accession = "Protein IDs" if protein else "Proteins"
    if accession not in header or (not protein and "Sequence" not in header):
        raise ValueError("Choose MaxQuant proteinGroups.txt, peptides.txt or evidence.txt.")
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
        yield hit(
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


def diann_rows(header: list[str], rows: Iterable[dict[str, str]]) -> Iterator[dict[str, Any]]:
    # DIA-NN report field definitions: https://github.com/vdemichev/DiaNN.
    if not {"Run", "Protein.Group", "Stripped.Sequence", "Precursor.Id"} <= set(header):
        raise ValueError(
            "Choose a DIA-NN main TSV report with Run, Protein.Group and precursor columns."
        )
    quantities = ("PG.MaxLFQ", "PG.Normalised", "PG.Quantity")
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
        if not accessions:
            continue
        key = (row["Protein.Group"], context["run"], context["channel"])
        group = hit(
            "protein_group",
            accessions,
            row,
            quantities,
            ("PG.Q.Value", "Global.PG.Q.Value"),
            decoy=is_decoy,
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


def fragpipe_rows(header: list[str], rows: Iterable[dict[str, str]]) -> Iterator[dict[str, Any]]:
    # Column definitions: https://fragpipe.nesvilab.org/docs/tutorial_fragpipe_outputs.html
    if "Protein ID" not in header:
        raise ValueError("Choose a FragPipe protein or peptide TSV with the Protein ID column.")
    sequence = next((key for key in ("Peptide Sequence", "Peptide") if key in header), None)
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
            ("Protein Probability", "PeptideProphet Probability"),
            sequence=row[sequence] if sequence else None,
            modified_sequence=row.get("Modified Sequence"),
            modifications=row.get("Assigned Modifications"),
        )


def parsed(format_: str, source: Iterable[str]) -> Iterator[dict[str, Any]]:
    if format_ == "mztab":
        yield from mztab_rows(source)
    else:
        header, rows = table(source)
        adapter = {"maxquant": maxquant_rows, "diann": diann_rows, "fragpipe": fragpipe_rows}[
            format_
        ]
        yield from adapter(header, rows)


def identity(record: dict[str, Any]) -> str:
    return str(record.get("entry_id") or record["id"])


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
    with artifact.open("w", encoding="utf-8") as output:
        for report_id in report_ids:
            file = run["files"][report_id]
            counts: Counter[str] = Counter()
            with lines_at(Path(file["path"])) as source:
                try:
                    for row in parsed(format_, source):
                        parents = [
                            by_accession[accession]
                            for accession in row["accessions"]
                            if accession in by_accession and not row.get("decoy")
                        ]
                        row["registered_proteins"] = [identity(protein) for protein in parents]
                        selected_proteins.update(
                            {identity(protein): protein for protein in parents}
                        )
                        row["report"] = report_id
                        counts[row["level"]] += 1
                        output.write(json.dumps(row, allow_nan=False) + "\n")
                        if len(preview) < PREVIEW_ROWS:
                            preview.append(row)
                except (ValueError, csv.Error) as exc:
                    raise ValueError(f"{file['name']}: {exc}") from None
            if not counts:
                raise ValueError(f"{file['name']} contains no recognized identification rows.")
            totals.update(counts)
            summaries.append(
                {
                    "file": report_id,
                    "name": file["name"],
                    "sha256": file["sha256"],
                    "counts": counts,
                }
            )
    values = dataset["values"]
    return {
        "apiVersion": "iki/v1",
        "kind": "RunOutput",
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


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    output = Path(os.environ["IKI_OUTPUT"])
    artifacts = Path(os.environ["IKI_OUT_DIR"])
    try:
        document = result(run, artifacts / "identifications.jsonl")
    except (EOFError, OSError, ValueError, zlib.error) as error:
        print(f"::error {error}", flush=True)
        raise SystemExit(1) from None
    output.write_text(json.dumps(document, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    main()
