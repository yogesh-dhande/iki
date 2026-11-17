"""Predict an antibody's structure with ImmuneBuilder (BSD-3-Clause; its weights CC BY 4.0).

ABodyBuilder2 models the variable domains of a heavy and a light chain (an IgG, a Fab or an
scFv); NanoBodyBuilder2 a single domain (a VHH). Each runs four models and takes the one
closest to the others, refines it with OpenMM and writes it as PDB, with each residue's
estimated error (the four models' spread, in Å) as its B-factor.

The antibody's chains are what its input includes (ADR 0105): their sequences as of launch.
It reads the run input from $IKI_INPUT, reports progress with `::progress` lines, and writes
its results to $IKI_OUTPUT, as every action's command does.
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path
from typing import Any

#: Where the image keeps the weights, checked against their SHA-256 when it was built.
WEIGHTS = Path(os.environ.get("IMMUNEBUILDER_WEIGHTS", "/opt/immunebuilder/weights"))
#: CDR-H3 in IMGT numbering, which ImmuneBuilder's models use.
H3 = range(105, 118)


def chains(antibody: dict[str, Any]) -> tuple[dict[str, str], list[str], list[str]]:
    """What to model: sequences by chain ID (H, L), the protein each chain ID is, in order."""
    included = antibody.get("included") or {}
    heavy = (included.get("heavy_chain") or [None])[0]
    light = (included.get("light_chain") or [None])[0]
    if heavy is None:
        raise ValueError("its heavy chain wasn't given to the run")
    h = heavy["values"]["sequence"].removesuffix("*").upper()
    form = antibody["values"].get("format")
    if form == "vhh":
        return {"H": h}, [heavy["id"]], ["H"]
    if form == "scfv" or light is None:
        # One chain with both domains: each is found in it by its type.
        return {"H": h, "L": h}, [heavy["id"], heavy["id"]], ["H", "L"]
    return (
        {"H": h, "L": light["values"]["sequence"].removesuffix("*").upper()},
        [heavy["id"], light["id"]],
        ["H", "L"],
    )


def errors(model: Any, numbered: dict[str, list[Any]]) -> tuple[float, float | None]:  # noqa: ANN401
    """The mean estimated error over every residue, and over CDR-H3's, in Å."""
    each = model.error_estimates.mean(0).sqrt().cpu().numpy()
    mean = float(each.mean())
    h3 = [
        float(each[i]) for i, ((number, _), _) in enumerate(numbered.get("H", [])) if number in H3
    ]
    return mean, (sum(h3) / len(h3) if h3 else None)


def cpus(quota: Path = Path("/sys/fs/cgroup/cpu.max")) -> int:
    """The CPUs the run may use: its container's quota (cgroup v2), not the machine's, which
    os.cpu_count() gives; more threads than that only wait their turn."""
    try:
        limit, period = quota.read_text().split()
        if limit != "max":
            return max(1, math.ceil(int(limit) / int(period)))
    except (OSError, ValueError):
        pass
    return max(1, len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else 1)


def predict(run: dict[str, Any]) -> dict[str, Any]:
    import torch
    from ImmuneBuilder import ABodyBuilder2, NanoBodyBuilder2

    torch.set_num_threads(cpus())
    (antibody,) = run["inputs"]["antibody"]
    label = _label(antibody)
    sequences, proteins, ids = chains(antibody)
    single = list(sequences) == ["H"]
    name = "NanoBodyBuilder2" if single else "ABodyBuilder2"
    print(f"Modelling {label} with {name}", flush=True)
    print("::progress 0.1 Loading the models", flush=True)
    builder = (NanoBodyBuilder2 if single else ABodyBuilder2)(weights_dir=str(WEIGHTS))
    print("::progress 0.3 Predicting", flush=True)
    model = builder.predict(sequences)
    mean, h3 = errors(model, model.numbered_sequences)
    print("::progress 0.6 Refining with OpenMM", flush=True)
    out = Path(os.environ["IKI_OUT_DIR"]) / f"{antibody['hrid']}.pdb"
    model.save(str(out))
    print(f"Mean estimated error {mean:.2f} Å" + (f", CDR-H3 {h3:.2f} Å" if h3 else ""))
    values: dict[str, Any] = {
        "structure": {"$file": out.name, "media_type": "chemical/x-pdb"},
        "name": f"{label}, predicted by {name}",
        "method": "predicted",
        "chains": proteins,
        "chain_ids": ids,
        "predicted_error": round(mean, 3),
        "cdr_h3_predicted_error": round(h3, 3) if h3 is not None else None,
    }
    return {
        "creates": [{"type": "iki/protein.ProteinStructure", "values": values}],
        "metrics": {
            "predicted_error": round(mean, 3),
            **({"cdr_h3_predicted_error": round(h3, 3)} if h3 is not None else {}),
        },
    }


def _label(antibody: dict[str, Any]) -> str:
    return str(antibody["values"].get("name") or antibody["hrid"])


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    (antibody,) = run["inputs"]["antibody"]
    try:
        output = predict(run)
    except (AssertionError, ValueError) as error:
        # ImmuneBuilder's own checks, such as a chain that isn't the type it's given as,
        # and ours: what's wrong with the antibody, said as the run's error.
        why = (str(error).splitlines() or ["it couldn't be modelled"])[0].rstrip(".:")
        print(f"::error {_label(antibody)}: {why}.", flush=True)
        sys.exit(1)
    document = {"apiVersion": "iki/v1", "kind": "RunOutput", **output}
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(document), encoding="utf-8")


if __name__ == "__main__":
    main()
