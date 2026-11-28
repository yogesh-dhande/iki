"""Writers in WebAssembly text, for tests of the sandbox (ADR 0230): one that works, and one
for each way a writer can go wrong. Each is the ABI's exports and one ``iki_record``.

    compiled(ECHO)   # the module's bytes
"""

from __future__ import annotations

import json

import wasmtime

MiB = 1024 * 1024
NOTE = {"code": "echoed", "message": "A note on what the file can't hold."}
NOTES = json.dumps([NOTE], separators=(",", ":"))
REFUSAL = json.dumps([{"code": "refused", "message": "Not this one."}], separators=(",", ":"))


def _escape(text: str) -> str:
    return "".join(f"\\{b:02x}" for b in text.encode())


def compiled(wat: str) -> bytes:
    return bytes(wasmtime.wat2wasm(wat))


# Writes "> " + the request + "\n" for each record, with the same note each time; begin
# and end write nothing. The request goes at 64 KiB, its copy straight after it.
ECHO = f"""
(module
  (memory (export "memory") 1)
  (global $in_len (mut i32) (i32.const 0))
  (global $out_len (mut i32) (i32.const 0))
  (global $notes_len (mut i32) (i32.const 0))
  (data (i32.const 0) "{_escape(NOTES)}")
  (func (export "iki_abi") (result i32) (i32.const 1))
  (func (export "iki_input") (param $n i32) (result i32)
    (local $need i32)
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

#: The ABI's exports but ``iki_record``, for writers that differ only in it.
REST = """
  (memory (export "memory") 1)
  (func (export "iki_abi") (result i32) (i32.const 1))
  (func (export "iki_input") (param i32) (result i32) (i32.const 1024))
  (func (export "iki_begin") (result i32) (i32.const 0))
  (func (export "iki_end") (result i32) (i32.const 0))
  (func (export "iki_output") (result i32) (i32.const 0))
  (func (export "iki_output_len") (result i32) (i32.const 0))
  (func (export "iki_notes") (result i32) (i32.const 0))
  (func (export "iki_notes_len") (result i32) (i32.const 0))
"""


def recording(body: str, *, rest: str = REST, extra: str = "") -> str:
    return f'(module {rest} {extra} (func (export "iki_record") (result i32) {body}))'


#: Never stops.
SPINS = recording("(loop $l (br $l)) unreachable")
#: Grows a page at a time, as an allocator does, and traps when it's refused, as Rust's
#: allocator aborts.
HOARDS = recording(
    "(loop $more (if (i32.eq (memory.grow (i32.const 1)) (i32.const -1)) (then unreachable))"
    " (br $more)) (i32.const 0)"
)
#: Recurses without end.
RECURSES = recording(
    "(drop (call $deeper (i64.const 0))) (i32.const 0)",
    extra="(func $deeper (param i64) (result i64)"
    " (call $deeper (i64.add (local.get 0) (i64.const 1))))",
)
#: Panics, as a Rust writer's `panic = "abort"` does.
PANICS = recording("unreachable")
#: Refuses every record, as the ABI says a refusal is.
REFUSES = recording(
    "(i32.const 1)",
    rest=REST.replace(
        '(func (export "iki_notes_len") (result i32) (i32.const 0))',
        f'(func (export "iki_notes_len") (result i32) (i32.const {len(REFUSAL.encode())}))',
    ).replace(
        '(memory (export "memory") 1)',
        f'(memory (export "memory") 1) (data (i32.const 0) "{_escape(REFUSAL)}")',
    ),
)
#: Says it wrote more than its memory holds.
OVERREACHES = recording(
    "(i32.const 0)",
    rest=REST.replace(
        '(func (export "iki_output_len") (result i32) (i32.const 0))',
        '(func (export "iki_output_len") (result i32) (i32.const 1000000))',
    ),
)
#: One memory.fill over 48 MiB: one instruction that touches every byte.
FILLS = recording(
    f"(memory.fill (i32.const 0) (i32.const 7) (i32.const {48 * MiB})) (i32.const 0)",
    rest=REST.replace('(memory (export "memory") 1)', '(memory (export "memory") 769)'),
)
#: Declares more memory than the cap: 300 MiB.
TOO_BIG = recording(
    "(i32.const 0)",
    rest=REST.replace('(memory (export "memory") 1)', '(memory (export "memory") 4800)'),
)
#: What a writer that wanted the network or the filesystem would have to import.
IMPORTS_WASI = recording(
    "(i32.const 0)",
    rest='(import "wasi_snapshot_preview1" "fd_write" (func (param i32 i32 i32 i32)'
    " (result i32)))" + REST,
)


def reading(abi: int, source: str = "") -> str:
    """``source`` (an echo, by default), saying it reads ``abi``."""
    return (source or ECHO).replace(
        '(func (export "iki_abi") (result i32) (i32.const 1))',
        f'(func (export "iki_abi") (result i32) (i32.const {abi}))',
    )


#: Reads an ABI older than any this Iki runs.
ABI_0 = reading(0, recording("(i32.const 0)"))
#: Reads an ABI newer than this Iki runs.
ABI_2 = reading(2, recording("(i32.const 0)"))
#: Lacks ``iki_notes``.
INCOMPLETE = recording(
    "(i32.const 0)",
    rest=REST.replace('(func (export "iki_notes") (result i32) (i32.const 0))', ""),
)
