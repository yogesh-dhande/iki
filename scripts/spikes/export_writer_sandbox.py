# ruff: noqa: S101, S603, T201 - a pytest spike: asserts, its own child processes, measurements printed
"""Spike for ADR 0230: what wasmtime-py does with a plugin's export writer.

Not product code and not collected by the test suite (the name doesn't start with
``test_``). It checks the claims ADR 0230 makes about the runtime, so they rest on a
measurement rather than on documentation alone. Run it on both supported Pythons, with
the wasmtime-py the server pins and the current release:

    uv run --no-project --python 3.12 --with wasmtime==51.0.0 --with pytest \
        pytest -q -s scripts/spikes/export_writer_sandbox.py
    uv run --no-project --python 3.14 --with wasmtime==50.0.1 --with pytest \
        pytest -q -s scripts/spikes/export_writer_sandbox.py

The writer modules are written in the WebAssembly text format, so the spike needs no Rust
toolchain and commits no module: each one is compiled from the text here when it runs.
They follow the ABI ADR 0230 proposes: ``iki_input`` hands out a buffer for a JSON
request, ``iki_begin``/``iki_record``/``iki_end`` return 0 (written) or 1 (refused), and
``iki_output``/``iki_notes`` with their ``_len`` give the call's bytes and its notes.

Delete it once the implementation's own tests cover each point.
"""

from __future__ import annotations

import hashlib
import json
import statistics
import subprocess
import sys
import textwrap
import threading
import time
from importlib.metadata import version
from pathlib import Path

import pytest
import wasmtime

#: The exports every writer must have, and their signatures: (params, results).
ABI: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "iki_input": (("i32",), ("i32",)),
    "iki_begin": ((), ("i32",)),
    "iki_record": ((), ("i32",)),
    "iki_end": ((), ("i32",)),
    "iki_output": ((), ("i32",)),
    "iki_output_len": ((), ("i32",)),
    "iki_notes": ((), ("i32",)),
    "iki_notes_len": ((), ("i32",)),
}
MiB = 1024 * 1024
MEMORY_CAP = 64 * MiB
FUEL_PER_CALL = 200_000_000

NOTES = json.dumps([{"code": "spike", "message": "A note on what the file can't hold."}])


def _escape(text: str) -> str:
    return "".join(f"\\{b:02x}" for b in text.encode())


# A writer that writes "> " + the request + "\n" for each record, and the same note each
# time. The request goes at 64 KiB, the output straight after it.
ECHO = f"""
(module
  (memory (export "memory") 1)
  (global $in_len (mut i32) (i32.const 0))
  (global $out_len (mut i32) (i32.const 0))
  (global $notes_len (mut i32) (i32.const 0))
  (data (i32.const 0) "{_escape(NOTES)}")
  (func (export "iki_input") (param $n i32) (result i32)
    (local $need i32)
    ;; 64 KiB of notes and globals, the request, then its copy and 3 bytes.
    (local.set $need (i32.add (i32.const 65539) (i32.mul (local.get $n) (i32.const 2))))
    (block $done
      (loop $more
        (br_if $done (i32.ge_u (i32.mul (memory.size) (i32.const 65536)) (local.get $need)))
        (if (i32.eq (memory.grow (i32.const 1)) (i32.const -1)) (then unreachable))
        (br $more)))
    (global.set $in_len (local.get $n))
    (i32.const 65536))
  (func $none (result i32)
    (global.set $out_len (i32.const 0))
    (global.set $notes_len (i32.const 0))
    (i32.const 0))
  (func (export "iki_begin") (result i32) (call $none))
  (func (export "iki_end") (result i32) (call $none))
  (func (export "iki_record") (result i32)
    (local $out i32)
    (local.set $out (i32.add (i32.const 65536) (global.get $in_len)))
    (i32.store8 (local.get $out) (i32.const 62))
    (i32.store8 (i32.add (local.get $out) (i32.const 1)) (i32.const 32))
    (memory.copy (i32.add (local.get $out) (i32.const 2)) (i32.const 65536) (global.get $in_len))
    (i32.store8 (i32.add (i32.add (local.get $out) (i32.const 2)) (global.get $in_len))
      (i32.const 10))
    (global.set $out_len (i32.add (global.get $in_len) (i32.const 3)))
    (global.set $notes_len (i32.const {len(NOTES.encode())}))
    (i32.const 0))
  (func (export "iki_output") (result i32) (i32.add (i32.const 65536) (global.get $in_len)))
  (func (export "iki_output_len") (result i32) (global.get $out_len))
  (func (export "iki_notes") (result i32) (i32.const 0))
  (func (export "iki_notes_len") (result i32) (global.get $notes_len)))
"""

# The ABI's other exports, for modules that differ only in iki_record.
_REST = """
  (memory (export "memory") 1)
  (func (export "iki_input") (param i32) (result i32) (i32.const 1024))
  (func (export "iki_begin") (result i32) (i32.const 0))
  (func (export "iki_end") (result i32) (i32.const 0))
  (func (export "iki_output") (result i32) (i32.const 0))
  (func (export "iki_output_len") (result i32) (i32.const 0))
  (func (export "iki_notes") (result i32) (i32.const 0))
  (func (export "iki_notes_len") (result i32) (i32.const 0))
"""
SPINS = f'(module {_REST} (func (export "iki_record") (result i32) (loop $l (br $l)) unreachable))'
# Grows a page at a time, as an allocator does, and traps when it's refused, as Rust's
# allocator aborts. Before trapping it records how far it got, which the test reads.
HOARDS = f"""(module {_REST}
  (global (export "pages") (mut i32) (i32.const 0))
  (func (export "iki_record") (result i32)
    (loop $more
      (if (i32.eq (memory.grow (i32.const 1)) (i32.const -1)) (then unreachable))
      (br $more))
    (i32.const 0)))"""
RECURSES = f"""(module {_REST}
  (func $deeper (param i64) (result i64) (call $deeper (i64.add (local.get 0) (i64.const 1))))
  (func (export "iki_record") (result i32) (drop (call $deeper (i64.const 0))) (i32.const 0)))"""
# One memory.fill over 48 MiB: one instruction that touches every byte.
FILLS = f"""(module
  (memory (export "memory") 769)
  (func (export "iki_input") (param i32) (result i32) (i32.const 1024))
  (func (export "iki_begin") (result i32) (i32.const 0))
  (func (export "iki_end") (result i32) (i32.const 0))
  (func (export "iki_output") (result i32) (i32.const 0))
  (func (export "iki_output_len") (result i32) (i32.const 0))
  (func (export "iki_notes") (result i32) (i32.const 0))
  (func (export "iki_notes_len") (result i32) (i32.const 0))
  (func (export "iki_record") (result i32)
    (memory.fill (i32.const 0) (i32.const 7) (i32.const {48 * MiB}))
    (i32.const 0)))"""
# What a writer that wanted the network or the filesystem would have to import.
IMPORTS_WASI = f"""(module
  (import "wasi_snapshot_preview1" "fd_write" (func (param i32 i32 i32 i32) (result i32)))
  {_REST}
  (func (export "iki_record") (result i32) (i32.const 0)))"""


def sandbox_engine() -> wasmtime.Engine:
    """The engine ADR 0230 configures: fuel, deterministic floats, and none of the
    proposals a writer doesn't need (each of this year's advisories needed one of them,
    a WASI import, or a host function)."""
    config = wasmtime.Config()
    config.consume_fuel = True
    config.cranelift_nan_canonicalization = True
    # Wasmtime's determinism guide: CoW initialisation can change the fuel instantiation
    # takes from host to host.
    config.memory_init_cow = False
    config.wasm_threads = False
    config.wasm_relaxed_simd = False
    config.wasm_gc = False
    config.wasm_component_model = False
    config.wasm_memory64 = False
    config.wasm_multi_memory = False
    config.wasm_exceptions = False
    config.wasm_stack_switching = False
    config.max_wasm_stack = 512 * 1024
    return wasmtime.Engine(config)


ENGINE = sandbox_engine()


def compile_writer(wat: str) -> wasmtime.Module:
    return wasmtime.Module(ENGINE, wasmtime.wat2wasm(wat))


def check_writer(module: wasmtime.Module) -> list[str]:
    """What's wrong with a module as a writer: an import of any kind, or an ABI export
    missing or of the wrong type. Empty when it's a writer."""
    problems = [f"imports {i.module}.{i.name}" for i in module.imports]
    exports = {e.name: e.type for e in module.exports}
    if not isinstance(exports.get("memory"), wasmtime.MemoryType):
        problems.append("exports no memory")
    for name, (params, results) in ABI.items():
        found = exports.get(name)
        if not isinstance(found, wasmtime.FuncType):
            problems.append(f"exports no function {name}")
            continue
        got = (tuple(str(p) for p in found.params), tuple(str(r) for r in found.results))
        if got != (params, results):
            problems.append(f"{name} is {got}, not {(params, results)}")
    return problems


class Writer:
    """One export's instance: a store of its own, capped, with no imports."""

    def __init__(self, module: wasmtime.Module, memory: int = MEMORY_CAP) -> None:
        if problems := check_writer(module):
            raise ValueError("; ".join(problems))
        self.store = wasmtime.Store(ENGINE)
        self.store.set_limits(memory_size=memory, instances=1, tables=1, memories=1)
        self.store.set_fuel(FUEL_PER_CALL)
        self.instance = wasmtime.Instance(self.store, module, [])
        self.exports = self.instance.exports(self.store)
        self.fuel_used = 0

    def call(self, entry: str, request: dict[str, object]) -> tuple[int, bytes, list[object]]:
        store, ex = self.store, self.exports
        payload = json.dumps(request, sort_keys=True).encode()
        store.set_fuel(FUEL_PER_CALL)
        at = ex["iki_input"](store, len(payload))
        ex["memory"].write(store, payload, at)
        status = ex[entry](store)
        self.fuel_used += FUEL_PER_CALL - store.get_fuel()
        out_at, out_len = ex["iki_output"](store), ex["iki_output_len"](store)
        notes_at, notes_len = ex["iki_notes"](store), ex["iki_notes_len"](store)
        memory = ex["memory"]
        out = bytes(memory.read(store, out_at, out_at + out_len))
        notes = json.loads(memory.read(store, notes_at, notes_at + notes_len) or b"[]")
        return status, out, notes


RECORD = {
    "hrid": "PLT-000042",
    "name": "Assay plate 42",
    "revision": 3,
    "revised_at": "2026-11-20T09:14:03Z",
    "roles": {"volume": {"value": 2.5, "unit": "nL"}, "well": "A01"},
}


def write_one(module: wasmtime.Module) -> tuple[bytes, list[object], int]:
    writer = Writer(module)
    parts, notes = [], []
    for entry, request in (
        ("iki_begin", {"abi": 1, "writer": "spike", "records": 1}),
        ("iki_record", RECORD),
        ("iki_end", {}),
    ):
        status, out, said = writer.call(entry, request)
        assert status == 0
        parts.append(out)
        notes.extend(said)
    return b"".join(parts), notes, writer.fuel_used


def test_a_writer_writes_bytes_and_notes() -> None:
    content, notes, fuel = write_one(compile_writer(ECHO))
    assert content.startswith(b"> {") and content.endswith(b"}\n")
    assert json.loads(content[2:]) == RECORD
    assert notes == json.loads(NOTES)
    print(f"\n  echo writer: {len(content)} bytes, {fuel} fuel for begin/record/end")


def test_same_input_and_module_give_the_same_bytes_and_fuel_in_two_processes() -> None:
    here = Path(__file__)
    child = textwrap.dedent(f"""
        import hashlib, importlib.util, sys
        spec = importlib.util.spec_from_file_location("spike", {str(here)!r})
        spike = importlib.util.module_from_spec(spec); spec.loader.exec_module(spike)
        content, notes, fuel = spike.write_one(spike.compile_writer(spike.ECHO))
        print(hashlib.sha256(content).hexdigest(), fuel)
    """)
    runs = {
        subprocess.run(
            [sys.executable, "-c", child], capture_output=True, text=True, check=True
        ).stdout.strip()
        for _ in range(2)
    }
    content, _, fuel = write_one(compile_writer(ECHO))
    runs.add(f"{hashlib.sha256(content).hexdigest()} {fuel}")
    assert len(runs) == 1, runs


def test_a_writer_that_never_stops_runs_out_of_fuel_after_exactly_its_budget() -> None:
    writer = Writer(compile_writer(SPINS))
    started = time.perf_counter()
    with pytest.raises(wasmtime.Trap) as trapped:
        writer.call("iki_record", RECORD)
    took = time.perf_counter() - started
    assert trapped.value.trap_code == wasmtime.TrapCode.OUT_OF_FUEL
    assert writer.store.get_fuel() == 0
    print(f"\n  {FUEL_PER_CALL:,} fuel of a tight loop: {took * 1000:.0f} ms")


def test_memory_stops_growing_at_the_cap_and_the_writer_traps() -> None:
    module = compile_writer(HOARDS)
    writer = Writer(module, memory=16 * MiB)
    with pytest.raises(wasmtime.Trap) as trapped:
        writer.call("iki_record", RECORD)
    assert trapped.value.trap_code == wasmtime.TrapCode.UNREACHABLE
    assert writer.exports["memory"].size(writer.store) * 65536 == 16 * MiB


def test_a_module_declaring_more_memory_than_the_cap_does_not_instantiate() -> None:
    with pytest.raises(wasmtime.WasmtimeError):
        Writer(compile_writer(FILLS), memory=16 * MiB)


def test_runaway_recursion_is_a_trap_not_a_crash() -> None:
    writer = Writer(compile_writer(RECURSES))
    with pytest.raises(wasmtime.Trap) as trapped:
        writer.call("iki_record", RECORD)
    assert trapped.value.trap_code == wasmtime.TrapCode.STACK_OVERFLOW
    # The host is fine, and the next export gets a fresh instance that works.
    assert write_one(compile_writer(ECHO))[0]


def test_bulk_memory_costs_fuel_per_byte() -> None:
    """Wasmtime charges memory.fill (and memory.copy) a unit of fuel per byte, so a writer
    can't buy a long run with a few bulk instructions: fuel bounds them too."""
    writer = Writer(compile_writer(FILLS))
    started = time.perf_counter()
    writer.call("iki_record", RECORD)
    took = time.perf_counter() - started
    record_fuel = writer.fuel_used
    print(f"\n  one 48 MiB memory.fill: {record_fuel} fuel, {took * 1000:.1f} ms")
    assert record_fuel >= 48 * MiB


def test_imports_are_refused_before_anything_runs() -> None:
    module = compile_writer(IMPORTS_WASI)
    assert check_writer(module) == ["imports wasi_snapshot_preview1.fd_write"]
    with pytest.raises(ValueError, match="imports"):
        Writer(module)
    # Nor could the host link it by accident: with nothing to import, it can't start.
    store = wasmtime.Store(ENGINE)
    with pytest.raises(wasmtime.WasmtimeError):
        wasmtime.Instance(store, module, [])


def test_a_call_releases_the_gil() -> None:
    """Matters only for alternative (a), a writer in the server's own threads."""
    ticks = 0
    stop = threading.Event()

    def count() -> None:
        nonlocal ticks
        while not stop.is_set():
            ticks += 1

    writer = Writer(compile_writer(SPINS))
    counter = threading.Thread(target=count)
    counter.start()
    try:
        with pytest.raises(wasmtime.Trap):
            writer.call("iki_record", RECORD)
    finally:
        stop.set()
        counter.join()
    print(f"\n  another thread counted to {ticks:,} during the call")
    assert ticks > 100_000


def test_a_fresh_process_per_export_starts_quickly_from_precompiled_code(
    tmp_path: Path,
) -> None:
    """The cost ADR 0230 accepts for one process per export: start Python, load
    wasmtime, map the precompiled module, write one record."""
    native = tmp_path / "echo.cwasm"
    native.write_bytes(compile_writer(ECHO).serialize())
    here = Path(__file__)
    child = textwrap.dedent(f"""
        import importlib.util
        spec = importlib.util.spec_from_file_location("spike", {str(here)!r})
        spike = importlib.util.module_from_spec(spec); spec.loader.exec_module(spike)
        import wasmtime
        module = wasmtime.Module.deserialize_file(spike.ENGINE, {str(native)!r})
        content, notes, fuel = spike.write_one(module)
        assert content
    """)
    times = []
    for _ in range(7):
        started = time.perf_counter()
        subprocess.run([sys.executable, "-I", "-c", child], check=True)
        times.append(time.perf_counter() - started)
    print(
        f"\n  process per export (Python {sys.version_info.major}.{sys.version_info.minor}, "
        f"wasmtime {version('wasmtime')}): "
        f"median {statistics.median(times) * 1000:.0f} ms, max {max(times) * 1000:.0f} ms"
    )
    assert statistics.median(times) < 1.0
