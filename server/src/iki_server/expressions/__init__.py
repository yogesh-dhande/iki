"""Checking apps' expressions (ADR 0090) with iki-wasm, the module browsers evaluate them
with, run here by wasmtime: what the server accepts is exactly what browsers run.

The module is built by ``scripts/build-wasm.sh``, which copies it next to this file, as
the server's image does. Each thread has an instance of its own (wasmtime's stores aren't
shared between threads), and each call has a budget of fuel, so no expression can hold a
request up however it's written.
"""

import json
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import wasmtime

#: Where the module is: built and copied here by scripts/build-wasm.sh, and by the image.
WASM = Path(__file__).with_name("iki_wasm.wasm")
#: Instructions reading one expression may take: the costliest of 1,000 characters (a
#: list of 450 numbers) takes about 70 million.
FUEL = 200_000_000
#: The longest expression, in characters, as iki-wasm also checks.
MAX_LENGTH = 1000


class UnavailableError(RuntimeError):
    """iki-wasm isn't here, or doesn't load."""


@dataclass(frozen=True)
class Checked:
    """What one expression reads, or why it doesn't."""

    names: tuple[str, ...] = ()
    error: str | None = None

    @property
    def valid(self) -> bool:
        return self.error is None


_lock = threading.Lock()
_compiled: tuple[wasmtime.Engine, wasmtime.Module] | None = None
_local = threading.local()


def _module() -> tuple[wasmtime.Engine, wasmtime.Module]:
    global _compiled
    with _lock:
        if _compiled is None:
            if not WASM.is_file():
                raise UnavailableError(
                    f"iki-wasm isn't built: {WASM} is missing. Run scripts/build-wasm.sh."
                )
            config = wasmtime.Config()
            config.consume_fuel = True
            engine = wasmtime.Engine(config)
            try:
                module = wasmtime.Module.from_file(engine, str(WASM))
            except wasmtime.WasmtimeError as error:
                raise UnavailableError(f"iki-wasm doesn't load: {error}") from error
            _compiled = (engine, module)
        return _compiled


class _Instance:
    def __init__(self) -> None:
        engine, module = _module()
        self.store = wasmtime.Store(engine)
        # Setting the module up runs some of it too, which a new store has no fuel for.
        self.store.set_fuel(FUEL)
        exports = wasmtime.Instance(self.store, module, []).exports(self.store)
        memory = exports["memory"]
        if not isinstance(memory, wasmtime.Memory):
            raise UnavailableError("iki-wasm has no memory to share.")
        self.memory = memory
        self.functions: dict[str, wasmtime.Func] = {}
        for name in ("iki_input", "iki_check", "iki_evaluate", "iki_output", "iki_output_len"):
            function = exports[name]
            if not isinstance(function, wasmtime.Func):
                raise UnavailableError(f"iki-wasm has no function {name}.")
            self.functions[name] = function

    def call(self, export: str, payload: bytes, fuel: int) -> bytes:
        store, functions = self.store, self.functions
        store.set_fuel(fuel)
        at = functions["iki_input"](store, len(payload))
        self.memory.write(store, payload, at)
        functions[export](store)
        start = functions["iki_output"](store)
        length = functions["iki_output_len"](store)
        return bytes(self.memory.read(store, start, start + length))


def _call(export: str, request: dict[str, object], fuel: int) -> dict[str, object]:
    payload = json.dumps(request).encode()
    instance: _Instance | None = getattr(_local, "instance", None)
    if instance is None:
        instance = _local.instance = _Instance()
    try:
        answer = instance.call(export, payload, fuel)
    except wasmtime.Trap as trap:
        # A trapped instance's memory is in no known state: the next call gets a new one.
        _local.instance = None
        raise UnavailableError(f"iki-wasm stopped: {trap.message}") from trap
    found: dict[str, object] = json.loads(answer)
    if "error" in found:
        raise UnavailableError(f"iki-wasm refused the request: {found['error']}")
    return found


def check(expressions: Sequence[str]) -> list[Checked]:
    """Whether each expression reads, and the names it uses (those that `map`, `filter`,
    `all` and `exists` bind aside), as browsers will read it."""
    if not expressions:
        return []
    found = _call("iki_check", {"expressions": list(expressions)}, FUEL * len(expressions))
    out = []
    for each in found["checked"]:  # type: ignore[attr-defined]
        if "error" in each:
            out.append(Checked(error=str(each["error"])))
        else:
            out.append(Checked(names=tuple(each["names"])))
    return out
