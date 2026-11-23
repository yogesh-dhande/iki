"""Run the packaged routine cloning-vector library matcher as an Iki action."""

from __future__ import annotations

import json
import os
from functools import cache
from pathlib import Path
from typing import Any

import wasmtime

MODULE = Path(__file__).resolve().parent.parent / "data" / "dna-annotation.wasm"
MAX_BASES = 16 * 1024 * 1024  # The matcher accepts up to 16 MiB of bases.
MAX_INPUT = MAX_BASES + 1024  # Leave room for the JSON envelope.
# A maximum-length circular duplex of N bases took 4.45 billion instructions under
# Wasmtime 50.0.1. Allow headroom for other inputs while bounding each sequence.
FUEL = 6_000_000_000
MEMORY = 256 * 1024 * 1024  # Per sequence, below the environment's 512 MiB allowance.


class AnnotationError(ValueError):
    """A sequence or its annotations cannot be processed."""


@cache
def _module() -> tuple[wasmtime.Engine, wasmtime.Module]:
    config = wasmtime.Config()
    config.consume_fuel = True
    engine = wasmtime.Engine(config)
    return engine, wasmtime.Module.from_file(engine, str(MODULE))


def annotate(bases: str, *, circular: bool, double_stranded: bool) -> dict[str, Any]:
    """Return the matcher's features and the identity of the library used to find them."""
    if len(bases) > MAX_BASES:
        raise AnnotationError("The feature library accepts up to 16 MiB of bases per sequence.")
    if not bases.isascii():
        raise AnnotationError("Use IUPAC DNA bases to search the feature library.")
    request = {"bases": bases, "circular": circular, "double_stranded": double_stranded}
    payload = json.dumps(request).encode()
    if len(payload) > MAX_INPUT:
        raise AnnotationError("The feature library request is too large.")
    engine, module = _module()
    with wasmtime.Store(engine) as store:
        store.set_limits(memory_size=MEMORY)
        store.set_fuel(FUEL)
        exports = wasmtime.Instance(store, module, []).exports(store)
        memory = exports["memory"]
        allocate = exports["iki_input"]
        run = exports["iki_annotate_dna_sequence"]
        output = exports["iki_output"]
        output_len = exports["iki_output_len"]
        if not (
            isinstance(memory, wasmtime.Memory)
            and isinstance(allocate, wasmtime.Func)
            and isinstance(run, wasmtime.Func)
            and isinstance(output, wasmtime.Func)
            and isinstance(output_len, wasmtime.Func)
        ):
            raise RuntimeError("The packaged DNA library matcher has an incompatible ABI.")
        try:
            at = allocate(store, len(payload))
            if not at:
                raise AnnotationError("The feature library request is too large.")
            memory.write(store, payload, at)
            run(store)
        except wasmtime.Trap as error:
            raise AnnotationError(
                "The feature library search exceeded its runtime limits."
            ) from error
        start = output(store)
        length = output_len(store)
        answer: dict[str, Any] = json.loads(memory.read(store, start, start + length))
    if "error" in answer:
        raise AnnotationError(answer["error"])
    return answer


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    (sequence,) = run["inputs"]["sequence"]
    values = sequence["values"]
    hrid = sequence["hrid"]
    try:
        result = annotate(
            values["bases"],
            circular=values.get("topology") == "circular",
            double_stranded=values.get("strandedness", "double") == "double",
        )
    except AnnotationError as error:
        print(f"::error {hrid}: {error}", flush=True)
        raise SystemExit(1) from error
    features = result["features"]
    print(f"{hrid}: {len(features)} exact library matches")
    update = {
        "entity": sequence["id"],
        "values": {
            "library_features": features,
            "library_annotated_with": result["annotated_with"],
        },
    }
    output = {"apiVersion": "iki/v1", "kind": "RunOutput", "updates": {"sequence": [update]}}
    Path(os.environ["IKI_OUTPUT"]).write_text(
        json.dumps(output, separators=(",", ":")), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
