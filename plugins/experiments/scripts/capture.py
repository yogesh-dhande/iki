"""Copy the revisions the worker actually read into an experiment's signed evidence."""

from typing import Any

import iki_action as iki


def capture(run: dict[str, Any]) -> dict[str, Any]:
    experiment = run["inputs"]["experiment"][0]
    results = run["inputs"].get("results", [])
    values = experiment["values"]
    if not (results or values.get("observations") or values.get("attachments")):
        raise ValueError(
            "Select results, enter a dated observation or attach evidence before capture."
        )

    # The project's experiment-review choice, or the deployment's, pinned when the run began.
    flow = run["settings"]["review_flow"]
    if flow not in {"author_signed", "research", "regulated_qc"}:
        raise ValueError("Choose a supported experiment review flow in the plugin settings.")
    if values.get("review_flow") == "regulated_qc" and flow != "regulated_qc":
        # A record captured for Regulated QC finishes that review (ADR 0240, ADR 0247): it
        # may carry a signed submission already, which no laxer flow may close.
        print(
            "It was captured for Regulated QC, so it stays in that review: the project's "
            f"{flow.replace('_', ' ')} flow is for records not captured for it yet.",
            flush=True,
        )
        flow = "regulated_qc"
    pins = []
    for result in results:
        record_id = result["id"]
        if record_id == experiment["id"]:
            raise ValueError("An experiment cannot be its own result.")
        pin = {"result": record_id, "record_id": record_id, "record_revision": result["revision"]}
        entry_id = result.get("entry_id")
        entry_revision = result.get("entry_revision")
        if (entry_id is None) != (entry_revision is None):
            raise ValueError("This run lacks the complete registry pin. Launch a fresh capture.")
        if entry_id is not None:
            pin.update(entry_id=entry_id, entry_revision=entry_revision)
        pins.append(pin)
    return {
        "updates": {
            "experiment": [
                {
                    "entity": experiment["id"],
                    "require_unchanged": True,
                    "values": {
                        "result_pins": pins,
                        "review_flow": flow,
                        "capture_id": run["run"]["id"],
                    },
                }
            ]
        },
    }


if __name__ == "__main__":
    iki.main(capture, refuse=ValueError)
