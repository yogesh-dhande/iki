"""Link one rearrangement of an imported AIRR repertoire to a registered antibody."""

from __future__ import annotations

import gzip
import json
import os
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

IG_LOCI = {"IGH", "IGK", "IGL"}
#: The row's columns the link keeps, when the report has them (AIRR Rearrangement schema:
#: https://docs.airr-community.org/en/stable/datarep/rearrangements.html).
KEPT = [
    "productive",
    "v_call",
    "d_call",
    "j_call",
    "c_call",
    "duplicate_count",
    "junction_aa",
    "cdr3_aa",
]


def rows(path: Path) -> Iterator[dict[str, str]]:
    with path.open("rb") as source:
        compressed = source.peek(2)[:2] == b"\x1f\x8b"
    with (
        gzip.open(path, "rt", encoding="utf-8-sig", newline="")
        if compressed
        else path.open(encoding="utf-8-sig", newline="")
    ) as source:
        lines = (line.rstrip("\r\n") for line in source)
        header = next((line.split("\t") for line in lines if line), [])
        if "sequence_id" not in header or len(set(header)) != len(header):
            raise ValueError("The AIRR report has no distinct sequence_id column.")
        for line in lines:
            if line:
                cells = line.split("\t")
                if len(cells) != len(header):
                    raise ValueError("An AIRR row's columns don't match its header.")
                yield dict(zip(header, cells, strict=True))


def locus_of(row: dict[str, str]) -> str | None:
    # Locus is nullable in AIRR. The V and J calls identify it when both are present and
    # every call shares one immunoglobulin prefix.
    if row.get("locus"):
        return row["locus"]
    prefixes = {
        call.strip()[:3]
        for field in ("v_call", "j_call")
        for call in (row.get(field) or "").split(",")
        if call.strip()
    }
    if row.get("v_call") and row.get("j_call") and len(prefixes) == 1:
        return prefixes.pop()
    return None


def link(run: dict[str, Any]) -> dict[str, Any]:
    result = run["inputs"]["result"][0]
    antibody = run["inputs"]["antibody"][0]
    values = result["values"]
    sequence_id = run["params"]["sequence_id"].strip()
    curator = run["run"].get("requested_by")
    if not curator:
        raise ValueError("Linking a rearrangement requires an authenticated run requester.")
    if values.get("format") != "airr":
        raise ValueError("Choose a result imported with Import AIRR rearrangements.")
    report = run["files"].get(values.get("report") or "")
    if report is None or report["sha256"] != values.get("report_sha256"):
        raise ValueError("The AIRR result's report doesn't match the SHA-256 it recorded.")
    repertoire = (values.get("summary") or {}).get("selector") or ""
    found = [
        row
        for row in rows(Path(report["path"]))
        if row["sequence_id"] == sequence_id and (row.get("repertoire_id") or "") == repertoire
    ]
    if len(found) != 1:
        where = f"repertoire {repertoire}" if repertoire else "the imported rearrangements"
        raise ValueError(f"{sequence_id!r} isn't exactly one row of {where}.")
    row = found[0]
    locus = locus_of(row)
    if locus not in IG_LOCI:
        raise ValueError(f"{sequence_id!r} has no immunoglobulin locus (IGH, IGK or IGL).")
    pins = ("id", "revision", "entry_id", "entry_revision")
    pinned = [{key: record[key] for key in pins if key in record} for record in (result, antibody)]
    return {
        "apiVersion": "iki/v1",
        "kind": "RunOutput",
        "creates": [
            {
                "type": "iki/antibody_rearrangements.AntibodyRearrangement",
                "derived_from": [result["id"], antibody["id"]],
                "values": {
                    "antibody": antibody.get("entry_id") or antibody["id"],
                    "result": result["id"],
                    "sequence_id": sequence_id,
                    "repertoire_id": repertoire or None,
                    "locus": locus,
                    "rearrangement": {key: row[key] for key in KEPT if row.get(key)},
                    "curator": curator,
                    "inputs": {
                        "result": pinned[0],
                        "antibody": pinned[1],
                        "report_sha256": report["sha256"],
                    },
                },
            }
        ],
    }


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    try:
        output = link(run)
    except (ValueError, OSError, EOFError, zlib.error) as error:
        print(f"::error {error}", flush=True)
        raise SystemExit(1) from None
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")


if __name__ == "__main__":
    main()
