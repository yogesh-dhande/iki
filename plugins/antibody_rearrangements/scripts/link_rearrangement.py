"""Link one rearrangement of an imported AIRR repertoire to a registered antibody."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import iki_action as iki

from iki_runtime import airr, text

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


def productive_of(row: dict[str, str], sequence_id: str) -> bool | None:
    """The row's `productive`: True if its V(D)J is predicted to be productive, None when
    the report leaves it empty (AIRR Rearrangement schema: a boolean, written T or F, and
    null as an empty string). TRUE and FALSE, as some tools write them, are read too."""
    value = (row.get("productive") or "").strip()
    if not value:
        return None
    read = {"t": True, "true": True, "f": False, "false": False}.get(value.lower())
    if read is None:
        raise ValueError(
            f"{sequence_id!r}'s productive is {value!r}, not T, F or empty, as AIRR writes it."
        )
    return read


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
        for row in airr.read(Path(report["path"]))
        if row["sequence_id"] == sequence_id and (row.get("repertoire_id") or "") == repertoire
    ]
    if len(found) != 1:
        where = f"repertoire {repertoire}" if repertoire else "the imported rearrangements"
        raise ValueError(f"{sequence_id!r} isn't exactly one row of {where}.")
    row = found[0]
    locus = airr.locus(row)
    if locus not in IG_LOCI:
        raise ValueError(f"{sequence_id!r} has no immunoglobulin locus (IGH, IGK or IGL).")
    # A rearrangement predicted not to be productive (out of frame, or with a stop codon)
    # can't encode a chain, unless the call is wrong: then the curator says why.
    reason = (run["params"].get("nonproductive_reason") or "").strip() or None
    productive = productive_of(row, sequence_id)
    messages = []
    if productive is False and reason is None:
        raise ValueError(
            f"{sequence_id!r} is predicted not to be productive (productive F: out of frame, "
            "or with a stop codon), so it can't encode a chain: link a productive "
            "rearrangement, or, if this one's call is wrong, give why it encodes the chain."
        )
    if productive is not False and reason is not None:
        raise ValueError(
            f"{sequence_id!r} isn't called non-productive, so there's nothing to give a "
            "reason for: leave it out."
        )
    if productive is None:
        messages.append(
            {
                "level": "warning",
                "text": f"The report doesn't say whether {sequence_id!r} is productive: check "
                "that it's in frame and without a stop codon, since only a productive "
                "rearrangement encodes a chain.",
            }
        )
    pins = ("id", "revision", "entry_id", "entry_revision")
    pinned = [{key: record[key] for key in pins if key in record} for record in (result, antibody)]
    return {
        "creates": [
            {
                "type": "iki/antibody_rearrangements.AntibodyRearrangement",
                "derived_from": [result["id"], antibody["id"]],
                "values": {
                    "antibody": iki.ref(antibody),
                    "result": result["id"],
                    "sequence_id": sequence_id,
                    "repertoire_id": repertoire or None,
                    "locus": locus,
                    "rearrangement": {key: row[key] for key in KEPT if row.get(key)},
                    "curator": curator,
                    "nonproductive_reason": reason,
                    "inputs": {
                        "result": pinned[0],
                        "antibody": pinned[1],
                        "report_sha256": report["sha256"],
                    },
                },
            }
        ],
        "messages": messages,
    }


if __name__ == "__main__":
    iki.main(link, refuse=(ValueError, *text.UNREADABLE))
