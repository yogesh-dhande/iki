"""What a WebAssembly module must be to be a plugin's writer (ADR 0230), read from its bytes.

The checker runs on every module the plugin image builds, again in ``iki plugin validate``,
and again in the sandbox when it loads one. It refuses:

- any import, so a writer can call nothing outside its own memory;
- the ABI's exports missing, or of the wrong type;
- more than one memory, a shared memory or a 64-bit memory;
- a module over 16 MiB, a function body over 1 MiB, or a ``br_table`` with more than
  65,536 targets: the inputs behind Wasmtime's two Critical advisories of October 2026,
  sandbox escapes through Cranelift miscompiling huge function bodies and huge
  ``br_table`` jumps;
- instructions of the proposals the sandbox's engine has off (SIMD, threads, exceptions,
  GC and typed function references, tail calls, wide arithmetic), so a module is refused
  here with a reason rather than failing to compile there.

It decodes every instruction of every function body, rather than scanning for opcodes,
because an opcode's byte also turns up inside other instructions' immediates. It reads
structure only; Wasmtime validates the module when it compiles it.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

__all__ = [
    "ABI",
    "ABI_VERSION",
    "MAX_BR_TABLE_TARGETS",
    "MAX_FUNCTION_BODY",
    "MAX_MODULE_BYTES",
    "Problem",
    "check_module",
]

#: The ABI a writer of this Iki speaks: ``iki_abi`` returns it.
ABI_VERSION = 1
#: The functions every writer exports, as (params, results), and its ``memory``.
ABI: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "iki_abi": ((), ("i32",)),
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
MAX_MODULE_BYTES = 16 * MiB
MAX_FUNCTION_BODY = 1 * MiB
MAX_BR_TABLE_TARGETS = 65_536

_MAGIC = b"\x00asm\x01\x00\x00\x00"
_VALTYPES = {0x7F: "i32", 0x7E: "i64", 0x7D: "f32", 0x7C: "f64", 0x70: "funcref", 0x6F: "externref"}
_KINDS = {0: "func", 1: "table", 2: "memory", 3: "global", 4: "tag"}


@dataclass(frozen=True)
class Problem:
    """Why a module can't be a writer: a code a caller can match, and the words to show."""

    code: str
    message: str


class _Malformed(Exception):  # noqa: N818 - internal, turned into a Problem
    pass


class _Refused(Exception):  # noqa: N818 - internal, turned into a Problem
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class _Reader:
    data: bytes
    at: int = 0
    end: int = field(default=-1)

    def __post_init__(self) -> None:
        if self.end < 0:
            self.end = len(self.data)

    def done(self) -> bool:
        return self.at >= self.end

    def byte(self) -> int:
        if self.at >= self.end:
            raise _Malformed("unexpected end")
        value = self.data[self.at]
        self.at += 1
        return value

    def take(self, n: int) -> bytes:
        if n < 0 or self.at + n > self.end:
            raise _Malformed("unexpected end")
        chunk = self.data[self.at : self.at + n]
        self.at += n
        return chunk

    def u32(self) -> int:
        result = shift = 0
        for _ in range(5):
            b = self.byte()
            result |= (b & 0x7F) << shift
            if not b & 0x80:
                if result > 0xFFFFFFFF:
                    raise _Malformed("u32 out of range")
                return result
            shift += 7
        raise _Malformed("u32 too long")

    def signed(self, bits: int) -> int:
        result = shift = 0
        for _ in range((bits + 6) // 7):
            b = self.byte()
            result |= (b & 0x7F) << shift
            shift += 7
            if not b & 0x80:
                if b & 0x40:
                    result -= 1 << shift
                return result
        raise _Malformed("signed integer too long")

    def name(self) -> str:
        return self.take(self.u32()).decode("utf-8", "replace")

    def sub(self, size: int) -> _Reader:
        if self.at + size > self.end:
            raise _Malformed("section past the end")
        inner = _Reader(self.data, self.at, self.at + size)
        self.at += size
        return inner


def check_module(binary: bytes) -> list[Problem]:
    """Everything that stops ``binary`` being a writer, in the order found; empty if it is
    one as far as its structure goes. The ABI's export types are checked by
    :func:`iki_spec.writers.runtime.check_exports` once Wasmtime has compiled it."""
    problems: list[Problem] = []
    if len(binary) > MAX_MODULE_BYTES:
        problems.append(
            Problem(
                "too_large",
                f"The module is {len(binary):,} bytes; a writer may be at most "
                f"{MAX_MODULE_BYTES:,} (16 MiB).",
            )
        )
        return problems
    if not binary.startswith(_MAGIC):
        return [Problem("not_wasm", "This isn't a WebAssembly 1.0 core module.")]
    reader = _Reader(binary, len(_MAGIC))
    memories = 0
    exports: dict[str, str] = {}
    try:
        while not reader.done():
            section = reader.byte()
            body = reader.sub(reader.u32())
            if section == 0:
                continue  # custom: names, producers; nothing runs from it
            if section == 1:
                _types(body)
            elif section == 2:
                for module, name, kind in _imports(body):
                    memories += kind == "memory"
                    problems.append(
                        Problem(
                            "imports",
                            f"It imports {module}.{name} ({kind}): a writer may import nothing.",
                        )
                    )
            elif section == 5:
                count, found = _memories(body)
                memories += count
                problems.extend(found)
            elif section == 7:
                exports.update(_exports(body))
            elif section == 10:
                problems.extend(_code(body))
            elif section == 13:
                raise _Refused(
                    "proposal", "It declares exception tags: writers can't use exceptions."
                )
            elif section in (3, 4, 6, 8, 9, 11, 12):
                continue
            else:
                raise _Malformed(f"unknown section {section}")
    except _Malformed as why:
        return [*problems, Problem("malformed", f"The module is malformed: {why}.")]
    except _Refused as why:
        return [*problems, Problem(why.code, str(why))]
    if memories > 1:
        problems.append(Problem("memory", "It has more than one memory; a writer has one."))
    if exports.get("memory") != "memory":
        problems.append(Problem("abi", "It doesn't export its memory as `memory`."))
    problems.extend(
        Problem("abi", f"It doesn't export the function `{name}`.")
        for name in ABI
        if exports.get(name) != "func"
    )
    return problems


def _types(body: _Reader) -> None:
    for _ in range(body.u32()):
        form = body.byte()
        if form != 0x60:
            raise _Refused(
                "proposal", "It declares GC or typed-function types: writers can't use them."
            )
        for _ in range(2):
            for _ in range(body.u32()):
                _valtype(body)


def _valtype(reader: _Reader) -> str:
    code = reader.byte()
    if code == 0x7B:
        raise _Refused("proposal", "It uses SIMD (v128): writers can't.")
    if code not in _VALTYPES:
        raise _Refused(
            "proposal", "It uses reference types beyond funcref and externref: writers can't."
        )
    return _VALTYPES[code]


def _limits(reader: _Reader, *, memory: bool) -> list[Problem]:
    flags = reader.byte()
    problems = []
    if memory and flags & 0x02:
        problems.append(Problem("memory", "Its memory is shared: a writer's can't be."))
    if memory and flags & 0x04:
        problems.append(Problem("memory", "Its memory is 64-bit: a writer's is 32-bit."))
    if memory and flags & 0x08:
        problems.append(Problem("memory", "Its memory has a custom page size: writers can't."))
    if flags & ~0x0F:
        raise _Malformed("bad limits")
    reader.u32() if not flags & 0x04 else reader.signed(64)
    if flags & 0x01:
        reader.u32() if not flags & 0x04 else reader.signed(64)
    if memory and flags & 0x08:
        reader.u32()
    return problems


def _imports(body: _Reader) -> Iterator[tuple[str, str, str]]:
    for _ in range(body.u32()):
        module, name = body.name(), body.name()
        kind = body.byte()
        if kind == 0:
            body.u32()
        elif kind == 1:
            body.byte()
            _limits(body, memory=False)
        elif kind == 2:
            _limits(body, memory=True)
        elif kind == 3:
            _valtype(body)
            body.byte()
        elif kind == 4:
            body.byte()
            body.u32()
        else:
            raise _Malformed("bad import kind")
        yield module, name, _KINDS.get(kind, "?")


def _memories(body: _Reader) -> tuple[int, list[Problem]]:
    problems: list[Problem] = []
    count = body.u32()
    for _ in range(count):
        problems.extend(_limits(body, memory=True))
    return count, problems


def _exports(body: _Reader) -> dict[str, str]:
    found = {}
    for _ in range(body.u32()):
        name = body.name()
        kind = body.byte()
        body.u32()
        found[name] = _KINDS.get(kind, "?")
    return found


def _code(body: _Reader) -> list[Problem]:
    problems = []
    for index in range(body.u32()):
        size = body.u32()
        if size > MAX_FUNCTION_BODY:
            problems.append(
                Problem(
                    "function_too_large",
                    f"Function {index}'s body is {size:,} bytes; a writer's may be at most "
                    f"{MAX_FUNCTION_BODY:,} (1 MiB).",
                )
            )
            body.take(size)
            continue
        problems.extend(_function(body.sub(size), index))
    return problems


def _blocktype(reader: _Reader) -> None:
    code = reader.data[reader.at] if reader.at < reader.end else None
    if code == 0x40 or (code is not None and code in _VALTYPES) or code == 0x7B:
        if code == 0x40:
            reader.byte()
        else:
            _valtype(reader)
        return
    if reader.signed(33) < 0:
        raise _Malformed("bad block type")


def _memarg(reader: _Reader) -> None:
    align = reader.u32()
    if align & 0x40:
        raise _Refused("proposal", "It addresses a second memory: writers have one.")
    reader.u32()


#: Opcodes with no immediates: control, parametric and every numeric instruction
#: (0x45-0xC4, sign extension included).
_PLAIN = {0x00, 0x01, 0x05, 0x0B, 0x0F, 0x1A, 0x1B, 0xD1, *range(0x45, 0xC5)}
#: Opcodes taking one u32: branches, calls, locals, globals, tables, memory size and
#: grow (a memory index), ref.func.
_ONE_U32 = {0x0C, 0x0D, 0x10, 0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x3F, 0x40, 0xD2}
_PROPOSALS = {
    **dict.fromkeys((0x06, 0x07, 0x08, 0x09, 0x0A, 0x18, 0x19, 0x1F), "exceptions"),
    **dict.fromkeys((0x12, 0x13), "tail calls"),
    **dict.fromkeys((0x14, 0x15, 0xD3, 0xD4, 0xD5, 0xD6), "typed function references"),
    0xFB: "garbage collection",
    0xFD: "SIMD",
    0xFE: "threads",
}
#: 0xFC's instructions: saturating truncation (0-7) has none; bulk memory and tables
#: take these many u32s.
_FC = {
    **dict.fromkeys(range(8), 0),
    8: 2,  # memory.init data memory
    9: 1,  # data.drop
    10: 2,  # memory.copy dst src
    11: 1,  # memory.fill
    12: 2,  # table.init
    13: 1,  # elem.drop
    14: 2,  # table.copy
    15: 1,  # table.grow
    16: 1,  # table.size
    17: 1,  # table.fill
}


def _function(reader: _Reader, index: int) -> list[Problem]:
    problems: list[Problem] = []
    for _ in range(reader.u32()):
        reader.u32()
        _valtype(reader)
    while not reader.done():
        op = reader.byte()
        if op in _PLAIN:
            continue
        if op in _ONE_U32:
            reader.u32()
        elif op in (0x02, 0x03, 0x04):
            _blocktype(reader)
        elif op == 0x0E:
            targets = reader.u32()
            if targets > MAX_BR_TABLE_TARGETS:
                problems.append(
                    Problem(
                        "br_table_too_large",
                        f"Function {index} has a br_table of {targets:,} targets; a writer's "
                        f"may have at most {MAX_BR_TABLE_TARGETS:,}.",
                    )
                )
            for _ in range(targets + 1):
                reader.u32()
        elif op == 0x11:
            reader.u32()
            reader.u32()
        elif op == 0x1C:
            for _ in range(reader.u32()):
                _valtype(reader)
        elif 0x28 <= op <= 0x3E:
            _memarg(reader)
        elif op == 0x41:
            reader.signed(32)
        elif op == 0x42:
            reader.signed(64)
        elif op == 0x43:
            reader.take(4)
        elif op == 0x44:
            reader.take(8)
        elif op == 0xD0:
            _valtype(reader)
        elif op == 0xFC:
            sub = reader.u32()
            if sub not in _FC:
                raise _Refused(
                    "proposal", f"Function {index} uses instruction 0xFC {sub}: writers can't."
                )
            for _ in range(_FC[sub]):
                reader.u32()
        elif op in _PROPOSALS:
            raise _Refused("proposal", f"Function {index} uses {_PROPOSALS[op]}: writers can't.")
        else:
            raise _Malformed(f"unknown instruction 0x{op:02x} in function {index}")
    return problems
