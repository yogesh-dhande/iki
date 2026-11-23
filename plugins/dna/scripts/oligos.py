"""The packaged Rust module this plugin's primer checks run in.

How warm a primer melts in its buffer, and what it folds into or pairs with, is one
implementation, compiled to WebAssembly from the same Rust the server and the web app run
(ADR 0201), so a temperature or a free energy is the same wherever it is worked out and
whoever asks. This module loads it from the action archive and calls it.

Each call gets a store of its own with a budget of fuel and a cap on memory, so no oligo
can hold a run up however long it is or however many it is given.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

import wasmtime

MODULE = Path(__file__).resolve().parent.parent / "data" / "oligo-checks.wasm"
#: The most a request may be, matching the module's own limit.
MAX_REQUEST = 64 * 1024 * 1024
#: Instructions one call may take. Folding is quadratic in an oligo's length and the
#: module accepts 500 oligos of at most 200 bases; a full batch of the longest took about
#: 30 billion under Wasmtime 50.0.1, so this leaves room while still bounding every call.
FUEL = 200_000_000_000
#: Per call, below the environment's 512 MiB allowance.
MEMORY = 256 * 1024 * 1024


class ModuleError(RuntimeError):
    """The packaged module isn't there, doesn't load, or has the wrong shape."""


@cache
def _module() -> tuple[wasmtime.Engine, wasmtime.Module]:
    """Compile the pinned module once per process, for a batch of records."""
    if not MODULE.is_file():
        raise ModuleError(
            f"The packaged checks module is missing: {MODULE}. "
            "Run scripts/build-oligo-checks.wasm.sh."
        )
    config = wasmtime.Config()
    config.consume_fuel = True
    engine = wasmtime.Engine(config)
    try:
        return engine, wasmtime.Module.from_file(engine, str(MODULE))
    except wasmtime.WasmtimeError as error:
        raise ModuleError(f"The packaged checks module doesn't load: {error}") from error


def call(export: str, request: dict[str, Any]) -> dict[str, Any]:
    """One of the module's exports, with a JSON request: its JSON answer.

    Raises :class:`ModuleError` where the module can't be run or runs out of its budget,
    and returns the answer as it is otherwise -- including an ``error`` key, which each
    caller turns into its own kind of refusal.
    """
    payload = json.dumps(request, separators=(",", ":")).encode()
    if len(payload) > MAX_REQUEST:
        raise ModuleError("The request is larger than the checks module accepts.")
    engine, module = _module()
    with wasmtime.Store(engine) as store:
        store.set_limits(memory_size=MEMORY)
        store.set_fuel(FUEL)
        exports = wasmtime.Instance(store, module, []).exports(store)
        memory = exports["memory"]
        allocate = exports["iki_input"]
        run = exports[export]
        output = exports["iki_output"]
        output_len = exports["iki_output_len"]
        if not (
            isinstance(memory, wasmtime.Memory)
            and isinstance(allocate, wasmtime.Func)
            and isinstance(run, wasmtime.Func)
            and isinstance(output, wasmtime.Func)
            and isinstance(output_len, wasmtime.Func)
        ):
            raise ModuleError("The packaged checks module has an incompatible ABI.")
        try:
            at = allocate(store, len(payload))
            if not at:
                raise ModuleError("The request is larger than the checks module accepts.")
            memory.write(store, payload, at)
            run(store)
            start = output(store)
            length = output_len(store)
            answer: dict[str, Any] = json.loads(memory.read(store, start, start + length))
        except wasmtime.Trap as error:
            raise ModuleError(
                "The checks module exceeded its runtime limits: give fewer oligos, or shorter ones."
            ) from error
    return answer
