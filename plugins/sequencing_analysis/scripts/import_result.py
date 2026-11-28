"""Archive one sample's results from an existing VCF, count matrix or AIRR rearrangement TSV."""

from __future__ import annotations

import csv
import gzip
import json
import os
import re
import sys
import zlib
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

PREVIEW_ROWS = 12
FORMATS = ("vcf", "counts", "airr")
VCF_COLUMNS = ["#CHROM", "POS", "ID", "REF", "ALT", "QUAL", "FILTER", "INFO"]
FEATURECOUNTS = ["Geneid", "Chr", "Start", "End", "Strand", "Length"]
AIRR_COLUMNS = [
    "sequence_id",
    "locus",
    "productive",
    "v_call",
    "d_call",
    "j_call",
    "duplicate_count",
]
IG_LOCI = {"IGH", "IGK", "IGL"}


@contextmanager
def report_lines(path: Path) -> Iterator[Iterable[str]]:
    with path.open("rb") as source:
        compressed = source.peek(2)[:2] == b"\x1f\x8b"
    with (
        gzip.open(path, "rt", encoding="utf-8-sig", newline="")
        if compressed
        else path.open(encoding="utf-8-sig", newline="")
    ) as source:
        yield source


def choose_column(names: list[str], choice: str, label: str) -> str:
    if not names or "" in names or len(set(names)) != len(names):
        raise ValueError(f"The {label} headings must be distinct and nonempty.")
    if not choice and len(names) == 1:
        return names[0]
    if choice not in names:
        raise ValueError(f"Choose a {label} heading present in the report.")
    return choice


def tsv_rows(lines: Iterable[str]) -> Iterator[list[str]]:
    for line in lines:
        if line.rstrip("\r\n"):
            yield line.rstrip("\r\n").split("\t")


def width(row: list[str], header: list[str], number: int) -> None:
    if len(row) != len(header):
        raise ValueError(f"Row {number} has {len(row)} columns, expected {len(header)}.")


def add_row(row: dict[str, Any], preview: list[dict[str, Any]], output: csv._writer) -> None:
    output.writerow(row.values())
    if len(preview) < PREVIEW_ROWS:
        preview.append(row)


def variant_rows(lines: Iterable[str], selector: str, output: csv._writer) -> dict[str, Any]:
    # VCF 4 specification: https://samtools.github.io/hts-specs/VCFv4.5.pdf
    rows = tsv_rows(lines)
    declaration = next(rows, [""])
    if len(declaration) != 1 or not re.fullmatch(r"##fileformat=VCFv4\.[0-5]", declaration[0]):
        raise ValueError("Expected a VCF 4.x fileformat declaration.")
    header = next((row for row in rows if not row[0].startswith("##")), [])
    if header[:8] != VCF_COLUMNS or (len(header) > 8 and header[8] != "FORMAT"):
        raise ValueError("The VCF is missing its #CHROM header or the fixed columns are invalid.")
    sample = choose_column(header[9:], selector, "VCF sample") if len(header) > 8 else ""
    if selector and not sample:
        raise ValueError("This VCF has no genotype sample columns.")
    sample_index = header.index(sample, 9) if sample else None
    output.writerow([*VCF_COLUMNS, *([header[8], sample] if sample else [])])
    preview: list[dict[str, Any]] = []
    variants = 0
    sites = 0
    untyped = 0
    for number, row in enumerate(rows, 1):
        width(row, header, number)
        if any(not value for value in row) or not re.fullmatch(r"[0-9]+", row[1]):
            raise ValueError(f"VCF site {number} has an empty value or invalid position.")
        alternatives = row[4].split(",")
        genuine = {
            i
            for i, allele in enumerate(alternatives, 1)
            if allele not in {".", "<NON_REF>", "<*>", "*"}
        }
        sites += 1
        selected = row[:8]
        if sample_index is not None:
            keys = row[8].split(":")
            if not keys or keys[0] != "GT":
                untyped += 1
                continue
            alleles = re.split(r"[|/]", row[sample_index].split(":")[0])
            # VCF 4.4+ permits a leading phasing separator.
            if alleles and alleles[0] == "":
                alleles = alleles[1:]
            if not alleles or any(
                allele != "." and not re.fullmatch(r"[0-9]+", allele) for allele in alleles
            ):
                raise ValueError(f"VCF site {number} has an invalid genotype.")
            try:
                called = {int(allele) for allele in alleles if allele != "."}
            except ValueError:
                raise ValueError(f"VCF site {number} has an invalid genotype.") from None
            if any(index < 0 or index > len(alternatives) for index in called):
                raise ValueError(f"VCF site {number} has an allele outside the ALT list.")
            if not called:
                untyped += 1
                continue
            if not genuine.intersection(called):
                continue
            selected.extend([row[8], row[sample_index]])
        elif not genuine:
            continue
        variants += 1
        output.writerow(selected)
        if len(preview) < PREVIEW_ROWS:
            preview.append(
                {
                    "Contig": row[0],
                    "Position": row[1],
                    "Reference": row[3],
                    "Alternate alleles": row[4],
                    **(
                        {"Genotype": row[sample_index].split(":")[0]}
                        if sample_index is not None
                        else {}
                    ),
                }
            )
    if sample:
        projection = (
            f"Sites where {sample}'s genotype calls an alternate allele. Calls of only the "
            "reference or a symbolic allele (<NON_REF>, <*> or *), and sites without a "
            "genotype call, are left out."
        )
        metrics = {"VCF sites": sites, "Variant sites": variants, "Sites without a call": untyped}
    else:
        projection = (
            "Sites with an alternate allele other than a symbolic reference "
            "(<NON_REF>, <*>, * or .)."
        )
        metrics = {"VCF sites": sites, "Variant sites": variants}
    return {
        "selector": sample,
        "projection": projection,
        "rows": variants,
        "metrics": metrics,
        "preview": preview,
    }


def count_rows(lines: Iterable[str], selector: str, output: csv._writer) -> dict[str, Any]:
    # Subread Users Guide, featureCounts output: https://subread.sourceforge.net/SubreadUsersGuide.pdf
    rows = (row for row in tsv_rows(lines) if not row[0].startswith("#"))
    header = next(rows, [])
    if not header or header[0].lower() not in {"", "geneid", "gene_id"}:
        raise ValueError("The count table must start with gene identifiers.")
    start = len(FEATURECOUNTS) if header[: len(FEATURECOUNTS)] == FEATURECOUNTS else 1
    sample = choose_column(header[start:], selector, "count sample")
    index = header.index(sample, start)
    output.writerow(["Gene ID", "Raw count"])
    observed = set()
    preview: list[dict[str, Any]] = []
    total = 0
    for number, row in enumerate(rows, 1):
        width(row, header, number)
        gene = row[0]
        count = row[index]
        if not gene or gene in observed:
            raise ValueError(f"Count row {number} has an empty or duplicate gene identifier.")
        if not re.fullmatch(r"[0-9]{1,18}", count):
            raise ValueError(f"Count row {number} needs a nonnegative integer raw count.")
        observed.add(gene)
        value = int(count)
        total += value
        add_row({"Gene ID": gene, "Raw count": value}, preview, output)
    if not observed:
        raise ValueError("The count table has no gene rows.")
    return {
        "selector": sample,
        "projection": f"Every gene's raw integer count in the {sample} column.",
        "rows": len(observed),
        "metrics": {"Genes": len(observed), "Total raw count": total},
        "preview": preview,
    }


def rearrangement_rows(lines: Iterable[str], selector: str, output: csv._writer) -> dict[str, Any]:
    # AIRR Rearrangement schema: https://docs.airr-community.org/en/stable/datarep/rearrangements.html
    rows = tsv_rows(lines)
    header = next(rows, [])
    if len(set(header)) != len(header) or not {
        "sequence_id",
        "productive",
        "v_call",
        "j_call",
    } <= set(header):
        raise ValueError("Expected AIRR sequence_id, productive, v_call and j_call columns.")
    selected = selector or None
    output.writerow(AIRR_COLUMNS)
    seen: set[str] = set()
    preview: list[dict[str, Any]] = []
    productive = 0
    for number, cells in enumerate(rows, 1):
        width(cells, header, number)
        row = dict(zip(header, cells, strict=True))
        repertoire = row.get("repertoire_id", "")
        if selector and repertoire != selector:
            continue
        if selected is None:
            selected = repertoire
        if repertoire != selected:
            raise ValueError("The AIRR file has multiple repertoires. Choose a repertoire ID.")
        sequence_id = row["sequence_id"]
        if not sequence_id or sequence_id in seen:
            raise ValueError(f"AIRR row {number} has an empty or duplicate sequence_id.")
        seen.add(sequence_id)
        if row["productive"] not in {"T", "F", ""}:
            raise ValueError(f"AIRR row {number} must use T, F or empty for productive.")
        productive += row["productive"] == "T"
        duplicates = row.get("duplicate_count") or ""
        if duplicates and not re.fullmatch(r"[0-9]{1,18}", duplicates):
            raise ValueError(f"AIRR row {number} has an invalid duplicate_count.")
        # Locus is nullable in AIRR. The supplied V and J gene calls can
        # identify it when both have the same unambiguous immunoglobulin prefix.
        if not row.get("locus"):
            prefixes = {
                call.strip()[:3]
                for field in ("v_call", "j_call")
                for call in row[field].split(",")
                if call.strip()
            }
            if row["v_call"] and row["j_call"] and len(prefixes) == 1 and prefixes <= IG_LOCI:
                row["locus"] = prefixes.pop()
        add_row({name: row.get(name, "") for name in AIRR_COLUMNS}, preview, output)
    if not seen:
        raise ValueError("The selected AIRR repertoire has no rearrangements.")
    return {
        "selector": selected,
        "projection": (
            f"Every rearrangement in repertoire {selected}."
            if selected
            else "Every rearrangement: the file names no repertoire."
        ),
        "rows": len(seen),
        "metrics": {"Rearrangements": len(seen), "Productive": productive},
        "preview": preview,
    }


def result(run: dict[str, Any], artifact: Path, format_: str) -> dict[str, Any]:
    if format_ not in FORMATS:
        raise ValueError(f"Unknown report format {format_!r}.")
    params = run["params"]
    inputs = run["inputs"]
    library = inputs["library"][0]
    sequencing_run = next(iter(inputs.get("run", [])), None)
    material = next(iter(inputs.get("material", [])), None)
    reference = (params.get("reference") or "").strip()
    column = (params.get("column") or "").strip()
    repertoire = (params.get("repertoire_id") or "").strip()
    if format_ in {"vcf", "counts"} and not reference:
        raise ValueError("Enter the reference build and version used for this report.")
    file_id = params["report"]
    report = run["files"][file_id]
    with (
        report_lines(Path(report["path"])) as lines,
        artifact.open("w", encoding="utf-8", newline="") as out,
    ):
        writer = csv.writer(out, delimiter="\t")
        if format_ == "vcf":
            summary = variant_rows(lines, column, writer)
        elif format_ == "counts":
            summary = count_rows(lines, column, writer)
        else:
            summary = rearrangement_rows(lines, repertoire, writer)
    parents = [row for row in (library, sequencing_run, material) if row]
    return {
        "apiVersion": "iki/v1",
        "kind": "RunOutput",
        "creates": [
            {
                "type": "iki/sequencing_analysis.SequencingAnalysisResult",
                "derived_from": [row["id"] for row in parents],
                "values": {
                    "library": library["id"],
                    "sample": library["values"]["sample"],
                    "run": sequencing_run["id"] if sequencing_run else None,
                    "material": (material.get("entry_id") or material["id"]) if material else None,
                    "report": file_id,
                    "report_sha256": report["sha256"],
                    "format": format_,
                    "summary": summary,
                    "pipeline_context": {
                        "pipeline": params["tool"],
                        "version": params["tool_version"],
                        "reference": reference,
                        "records": [
                            {"id": row["id"], "revision": row["revision"]} for row in parents
                        ],
                    },
                },
            }
        ],
    }


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    output = Path(os.environ["IKI_OUTPUT"])
    artifacts = Path(os.environ["IKI_OUT_DIR"])
    try:
        document = result(run, artifacts / "selected.tsv", sys.argv[1] if len(sys.argv) > 1 else "")
    except (ValueError, OSError, EOFError, zlib.error) as error:
        print(f"::error {error}", flush=True)
        raise SystemExit(1) from None
    output.write_text(json.dumps(document, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    main()
