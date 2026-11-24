"""Archive previously reviewed files or a reference alignment for a single plasmid prep."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def archive(run: dict[str, Any]) -> dict[str, Any]:
    prep = run["inputs"]["prep"][0]
    plasmids = prep.get("included", {}).get("plasmid", [])
    if len(plasmids) != 1:
        raise ValueError("The prep must have one pinned plasmid project record.")
    plasmid = plasmids[0]
    sequence = plasmid["values"].get("sequence")
    if not sequence or not prep["values"].get("plasmid"):
        raise ValueError("The prep must identify a registered plasmid with a DNA sequence.")

    params = run["params"]
    reason = (params.get("override_reason") or "").strip()
    report = params.get("report")
    alignments = run["inputs"].get("alignment", [])
    if not report and not alignments:
        raise ValueError("Select an alignment or an externally reviewed report for this prep.")
    flow = run.get("settings", {}).get("verification_flow")
    if flow not in {"research", "regulated_qc"}:
        raise ValueError("The deployment must select a plasmid verification review flow.")

    # Registry-valued references hold an entry UUID. The included entity, independently
    # pinned by the server, identifies a project record and its revision at archival
    # launch. These IDs differ for registered types (ADRs 0105 and 0115).
    values = {
        "prep": prep["id"],
        "plasmid": prep["values"]["plasmid"],
        "plasmid_record_id": plasmid["id"],
        "plasmid_revision": plasmid["revision"],
        "outcome": params["outcome"],
        "review_flow": flow,
    }
    derived_from = [prep["id"]]
    if alignments:
        alignment = alignments[0]
        if alignment["values"].get("reference") != sequence:
            raise ValueError("The alignment reference must be the prep plasmid's registered DNA.")
        sample = alignment["values"].get("sample")
        if sample is not None and sample != prep["id"]:
            raise ValueError("The alignment names a different physical prep.")
        verdict = alignment["values"].get("verdict")
        if params["outcome"] == "match" and verdict in {"differs", "incomplete"} and not reason:
            raise ValueError(
                "Give a reason to record a match over differing or incomplete evidence."
            )
        aligned = alignment["values"].get("aligned")
        if not aligned or aligned not in run["files"]:
            raise ValueError("The alignment must have a pinned aligned FASTA file.")
        if not run["files"][aligned]["size"]:
            raise ValueError("The pinned aligned FASTA file is empty.")
        values.update(alignment=alignment["id"], alignment_revision=alignment["revision"])
        derived_from.append(alignment["id"])
    if report:
        if report not in run["files"]:
            raise ValueError("The report must be a file supplied to this run.")
        if not run["files"][report]["size"]:
            raise ValueError("The verification report is empty.")
        # Existing file UUIDs can be attached directly from run params (ADR 0177).
        values["report"] = report
    if reason:
        values["override_reason"] = reason
    if params.get("notes"):
        values["notes"] = params["notes"]
    return {
        "apiVersion": "iki/v1",
        "kind": "RunOutput",
        "creates": [
            {"type": "iki/lots.PlasmidVerification", "values": values, "derived_from": derived_from}
        ],
    }


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    try:
        result = archive(run)
    except ValueError as error:
        print(f"::error {error}", flush=True)
        raise SystemExit(1) from None
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(result), encoding="utf-8")


if __name__ == "__main__":
    main()
