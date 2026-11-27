"""A plugin's writer under Wasmtime (ADR 0230): the engine, its limits, and the ABI's calls.

Only the sandbox's child process (:mod:`iki_spec.writers.child`) runs a writer, one export
per process. This module is what it runs: an engine with fuel and none of the proposals a
writer doesn't need, a store with a memory cap and one instance, no imports, and a call
that writes the request into the module's memory, runs it, and reads back the bytes and
notes, refusing anything outside its limits.

Fuel makes a writer's work deterministic: the same call runs out at the same instruction
on every host, so a writer that's too slow for one record is too slow everywhere, and the
file it writes doesn't depend on the machine.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import wasmtime

from iki_spec.writers.check import ABI, ABI_VERSION
from iki_spec.writers.limits import (
    DEFAULT_LIMITS,
    FUEL_BASE,
    FUEL_PER_BYTE,
    MAX_MEMORY,
    MAX_NOTE_CHARS,
    MAX_NOTES,
    MAX_OUTPUT,
    MAX_REQUEST,
    Limits,
)

__all__ = [
    "DEFAULT_LIMITS",
    "FUEL_BASE",
    "FUEL_PER_BYTE",
    "MAX_MEMORY",
    "MAX_NOTES",
    "MAX_NOTE_CHARS",
    "MAX_OUTPUT",
    "MAX_REQUEST",
    "Call",
    "Instance",
    "Limits",
    "WriterFailed",
    "check_exports",
    "engine",
]

#: The Wasm stack: enough for any writer's recursion, little enough to stop a runaway.
STACK = 512 * 1024

#: What the entries of :data:`ENTRIES` are called in the ABI.
ENTRIES = {"begin": "iki_begin", "record": "iki_record", "end": "iki_end"}


class WriterFailed(Exception):  # noqa: N818 - a failure the caller reports as such
    """The writer trapped, ran out of fuel or memory, or broke the ABI or a limit. ``code``
    says which; the message says so in words, without any of the values."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def engine() -> wasmtime.Engine:
    """The sandbox's engine, as Wasmtime's deterministic-execution guide sets one up: fuel,
    canonical NaNs, no copy-on-write memory initialisation (so instantiating costs the same
    fuel on every host), and every proposal a writer doesn't need off, which also turns off
    the code paths behind most of 2026's advisories. A precompiled module only loads into an
    engine with the same settings, which Wasmtime checks."""
    config = wasmtime.Config()
    config.consume_fuel = True
    config.cranelift_nan_canonicalization = True
    config.memory_init_cow = False
    config.parallel_compilation = False
    config.max_wasm_stack = STACK
    config.wasm_threads = False
    config.shared_memory = False
    config.wasm_simd = False
    config.wasm_relaxed_simd = False
    config.wasm_gc = False
    config.wasm_function_references = False
    config.wasm_component_model = False
    config.wasm_memory64 = False
    config.wasm_multi_memory = False
    config.wasm_exceptions = False
    config.wasm_stack_switching = False
    config.wasm_tail_call = False
    config.wasm_wide_arithmetic = False
    config.wasm_custom_page_sizes = False
    return wasmtime.Engine(config)


def check_exports(module: wasmtime.Module) -> list[str]:
    """What's wrong with a compiled module's imports and the types of its exports, against
    ABI 1; empty when it's a writer."""
    problems = [
        f"It imports {i.module}.{i.name}: a writer may import nothing." for i in module.imports
    ]
    exports = {e.name: e.type for e in module.exports}
    if not isinstance(exports.get("memory"), wasmtime.MemoryType):
        problems.append("It doesn't export its memory as `memory`.")
    for name, (params, results) in ABI.items():
        found = exports.get(name)
        if not isinstance(found, wasmtime.FuncType):
            problems.append(f"It doesn't export the function `{name}`.")
            continue
        got = (tuple(str(p) for p in found.params), tuple(str(r) for r in found.results))
        if got != (params, results):
            problems.append(
                f"`{name}` takes {got[0]} and returns {got[1]}, not {params} → {results}."
            )
    return problems


@dataclass(frozen=True)
class Call:
    """What one call wrote: written (0) or refused (1), the bytes it adds, its notes, and
    the fuel it used."""

    status: int
    output: bytes
    notes: list[dict[str, Any]]
    fuel: int


_TRAPS = {
    wasmtime.TrapCode.OUT_OF_FUEL: ("out_of_fuel", "it ran out of fuel"),
    wasmtime.TrapCode.STACK_OVERFLOW: ("stack_overflow", "its stack overflowed"),
    wasmtime.TrapCode.UNREACHABLE: ("panicked", "it stopped (a panic, or memory it couldn't get)"),
    wasmtime.TrapCode.MEMORY_OUT_OF_BOUNDS: ("trapped", "it read or wrote outside its memory"),
}


class Instance:
    """One export's writer: a store of its own, capped, with nothing imported."""

    def __init__(
        self,
        module: wasmtime.Module,
        store_engine: wasmtime.Engine,
        memory: int,
        limits: Limits = DEFAULT_LIMITS,
    ) -> None:
        self.limits = limits
        if not 0 < memory <= MAX_MEMORY:
            raise WriterFailed("limits", f"A writer's memory is at most {MAX_MEMORY:,} bytes.")
        if problems := check_exports(module):
            raise WriterFailed("abi", " ".join(problems))
        self.store = wasmtime.Store(store_engine)
        self.store.set_limits(memory_size=memory, instances=1, tables=1, memories=1)
        # Instantiating runs its start function and data initialisation, which use fuel.
        self.store.set_fuel(limits.fuel_base)
        try:
            instance = wasmtime.Instance(self.store, module, [])
        except wasmtime.Trap as trap:
            raise _failed(trap) from None
        except wasmtime.WasmtimeError as error:
            raise WriterFailed("instantiate", f"It couldn't start: {error}") from None
        exports = instance.exports(self.store)
        memory_export = exports["memory"]
        assert isinstance(memory_export, wasmtime.Memory)  # noqa: S101 - check_exports
        self.memory = memory_export
        self.functions: dict[str, wasmtime.Func] = {}
        for name in ABI:
            function = exports[name]
            assert isinstance(function, wasmtime.Func)  # noqa: S101 - check_exports
            self.functions[name] = function
        self.store.set_fuel(limits.fuel_base)
        abi = self._run("iki_abi")
        if abi != ABI_VERSION:
            raise WriterFailed(
                "abi", f"It speaks ABI {abi}; this Iki runs writers of ABI {ABI_VERSION}."
            )

    def _run(self, name: str, *args: int) -> int:
        try:
            result = self.functions[name](self.store, *args)
        except wasmtime.Trap as trap:
            raise _failed(trap) from None
        except wasmtime.WasmtimeError as error:
            raise WriterFailed("trapped", f"It stopped: {error}") from None
        if not isinstance(result, int):
            raise WriterFailed("abi", f"`{name}` returned no number.")
        return result

    def _read(self, start: int, length: int, limit: int, what: str) -> bytes:
        size = self.memory.data_len(self.store)
        if length < 0 or start < 0 or length > limit or start + length > size:
            raise WriterFailed(
                "output_limit" if length > limit else "abi",
                f"Its {what} is over the limit of {limit:,} bytes."
                if length > limit
                else f"Its {what} lies outside its memory.",
            )
        return bytes(self.memory.read(self.store, start, start + length))

    def call(self, entry: str, request: bytes) -> Call:
        """Runs ``begin``, ``record`` or ``end`` on ``request``, canonical JSON. Raises
        :class:`WriterFailed`."""
        limits = self.limits
        if len(request) > limits.max_request:
            raise WriterFailed(
                "request_limit", f"The request is over the limit of {limits.max_request:,} bytes."
            )
        fuel = limits.fuel_base + limits.fuel_per_byte * len(request)
        self.store.set_fuel(fuel)
        at = self._run("iki_input", len(request))
        size = self.memory.data_len(self.store)
        if at < 0 or at + len(request) > size:
            raise WriterFailed("abi", "`iki_input` gave a buffer outside its memory.")
        self.memory.write(self.store, request, at)
        status = self._run(ENTRIES[entry])
        if status not in (0, 1):
            raise WriterFailed("abi", f"`{ENTRIES[entry]}` returned {status}, not 0 or 1.")
        output = b""
        if status == 0:
            output = self._read(
                self._run("iki_output"), self._run("iki_output_len"), limits.max_output, "output"
            )
        raw = self._read(
            self._run("iki_notes"),
            self._run("iki_notes_len"),
            limits.max_notes * (limits.max_note_chars * 4 + 256),
            "notes",
        )
        used = fuel - self.store.get_fuel()
        return Call(status, output, _notes(raw, refused=status == 1, limits=limits), used)


def _failed(trap: wasmtime.Trap) -> WriterFailed:
    code, words = _TRAPS.get(trap.trap_code, ("trapped", "it trapped"))  # type: ignore[arg-type]
    return WriterFailed(code, f"The writer failed: {words}.")


def _notes(raw: bytes, *, refused: bool, limits: Limits = DEFAULT_LIMITS) -> list[dict[str, Any]]:
    """The notes as the ABI has them, checked: a list of ``{code, message, role?}``."""
    if not raw:
        notes: Any = []
    else:
        try:
            notes = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise WriterFailed("notes", "Its notes aren't JSON.") from None
    if not isinstance(notes, list) or len(notes) > limits.max_notes:
        raise WriterFailed("notes", f"Its notes aren't a list of at most {limits.max_notes:,}.")
    out = []
    for note in notes:
        if (
            not isinstance(note, dict)
            or not isinstance(note.get("code"), str)
            or not isinstance(note.get("message"), str)
            or not isinstance(note.get("role", ""), str)
            or set(note) - {"code", "message", "role"}
        ):
            raise WriterFailed("notes", "A note isn't {code, message, role?}, each a string.")
        if len(note["message"]) > limits.max_note_chars or len(note["code"]) > 64:
            raise WriterFailed("notes", f"A note is over {limits.max_note_chars} characters.")
        out.append({k: note[k] for k in ("code", "message", "role") if k in note})
    if refused and (len(out) != 1 or out[0]["code"] != "refused"):
        raise WriterFailed("notes", "A refusal's notes are one note, coded `refused`.")
    return out
