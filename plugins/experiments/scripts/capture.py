"""Copy the revisions the worker actually read into an experiment's signed evidence."""

import json
import os
from pathlib import Path
from typing import Any


def capture(run: dict[str, Any]) -> dict[str, Any]:
    experiment = run["inputs"]["experiment"][0]
    results = run["inputs"].get("results", [])
    values = experiment["values"]
    if not (results or values.get("observations") or values.get("attachments")):
        raise ValueError(
            "Select results, enter a dated observation or attach evidence before capture."
        )

    flow = run["settings"]["review_flow"]
    if flow not in {"research", "regulated_qc"}:
        raise ValueError("Choose a supported experiment review flow in the plugin settings.")
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
        "apiVersion": "iki/v1",
        "kind": "RunOutput",
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


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    try:
        output = capture(run)
    except ValueError as error:
        print(f"::error {error}", flush=True)
        raise SystemExit(1) from error
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")


if __name__ == "__main__":
    main()
