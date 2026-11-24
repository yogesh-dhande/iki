"""iki-wasm, run by wasmtime: the module browsers evaluate CEL with (ADR 0090), so what
Iki checks and evaluates here is exactly what they do.

Apps' expressions are checked with it when an app is saved, and plugins' rules and
guards (ADR 0127, ADR 0128) when a plugin is validated, and evaluated at each save and
state change. The module is built by ``scripts/build-wasm.sh``, which copies it next to
this file, as the server's image does. Each thread has an instance of its own
(wasmtime's stores aren't shared between threads), and each call has a budget of fuel,
so no expression can hold anything up however it's written.
"""

from __future__ import annotations

import hashlib
import json
import logging
import platform
import stat
import threading
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import wasmtime

__all__ = ["FUEL", "WASM", "UnavailableError", "call", "melting_temperature"]

#: Where the module is: built and copied here by scripts/build-wasm.sh, and by the image.
WASM = Path(__file__).with_name("iki_wasm.wasm")
# Only the server image installs native code here. Wheels and editable installs ship Wasm.
_IMAGE_WASM = Path("/opt/iki/wasm")
_logger = logging.getLogger(__name__)
#: Instructions checking 1,000 characters may take: the costliest expression of that
#: length (a list of 450 numbers) takes about 70 million.
FUEL = 200_000_000
#: The exports a call can name.
EXPORTS = (
    "iki_check",
    "iki_check_rules",
    "iki_evaluate_rules",
    "iki_state_origins",
    "iki_helm",
    "iki_annotate_dna_sequence",
    "iki_melting_temperature",
)


class UnavailableError(RuntimeError):
    """iki-wasm isn't here, or doesn't load, or stopped."""


_lock = threading.Lock()
_compiled: tuple[wasmtime.Engine, wasmtime.Module] | None = None
_local = threading.local()


def _host_target() -> str | None:
    if platform.system() != "Linux":
        return None
    return {
        "x86_64": "x86_64-unknown-linux-gnu",
        "aarch64": "aarch64-unknown-linux-gnu",
    }.get(platform.machine())


def _require_image_file(path: Path, *, directory: bool = False) -> None:
    info = path.lstat()
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError(f"{path} is not an immutable image {'directory' if directory else 'file'}")


def _image_module(engine: wasmtime.Engine) -> wasmtime.Module | None:
    if not _IMAGE_WASM.exists(follow_symlinks=False):
        return None
    try:
        # Deserializing native code requires a trusted source. Every component of the
        # image path must be owned by root, non-symlinked and unwritable by the service.
        for directory in (*reversed(_IMAGE_WASM.parents), _IMAGE_WASM):
            _require_image_file(directory, directory=True)
        metadata = _IMAGE_WASM / "iki_wasm.json"
        native = _IMAGE_WASM / "iki_wasm.cwasm"
        _require_image_file(metadata)
        _require_image_file(native)
        manifest = json.loads(metadata.read_bytes())
        if not isinstance(manifest, dict):
            raise ValueError("invalid iki_wasm.json")
        if type(manifest.get("format")) is not int or manifest["format"] != 1:
            raise ValueError("unsupported native artifact format")
        if manifest.get("consume_fuel") is not True:
            raise ValueError("native artifact requires fuel metering")
        target = _host_target()
        if target is None or manifest.get("target") != target:
            raise ValueError("native artifact target does not match the host")
        if manifest.get("wasmtime_version") != version("wasmtime"):
            raise ValueError(
                "native artifact Wasmtime version does not match the installed version"
            )
        with WASM.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        if manifest.get("wasm_sha256") != digest:
            raise ValueError("native artifact was built from a different Wasm module")
        with native.open("rb") as code:
            if manifest.get("cwasm_sha256") != hashlib.file_digest(code, "sha256").hexdigest():
                raise ValueError("native artifact checksum mismatch")
            # Keep the checked inode open while Wasmtime maps it. No second path lookup
            # can substitute different code between the digest check and deserialization.
            code.seek(0)
            module = wasmtime.Module.deserialize_file(engine, f"/dev/fd/{code.fileno()}")
    except (OSError, ValueError, PackageNotFoundError, wasmtime.WasmtimeError) as error:
        _logger.warning("Could not load precompiled iki-wasm (%s). Compiling packaged Wasm.", error)
        return None
    _logger.info(
        "Loaded precompiled iki-wasm", extra={"wasm_target": target, "wasm_sha256": digest}
    )
    return module


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
            # A cold portable fallback must fit a maintenance process's memory budget.
            config.parallel_compilation = False
            engine = wasmtime.Engine(config)
            try:
                module = _image_module(engine)
                if module is None:
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
        for name in ("iki_input", "iki_output", "iki_output_len", *EXPORTS):
            try:
                function = exports[name]
            except KeyError:
                raise UnavailableError(
                    f"iki-wasm has no function {name}: it's an older build. Run "
                    "scripts/build-wasm.sh."
                ) from None
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


def call(export: str, request: dict[str, object], fuel: int) -> dict[str, Any]:
    """One of the module's exports, with a JSON request: its JSON answer. Raises
    :class:`UnavailableError` when the module isn't there, runs out of fuel, or refuses
    the request as malformed."""
    if export not in EXPORTS:
        raise ValueError(f"iki-wasm has no export {export} to call.")
    payload = json.dumps(request).encode()
    # iki_input bounds its allocation to MAX_BYTES (iki-wasm/src/lib.rs).
    if len(payload) > 64 * 1024 * 1024:
        raise UnavailableError("The JSON request exceeds the module’s 64 MiB input limit.")
    instance: _Instance | None = getattr(_local, "instance", None)
    if instance is None:
        instance = _local.instance = _Instance()
    try:
        answer = instance.call(export, payload, fuel)
    except wasmtime.Trap as trap:
        # A trapped instance's memory is in no known state: the next call gets a new one.
        _local.instance = None
        raise UnavailableError(f"iki-wasm stopped: {trap.message}") from trap
    found: dict[str, Any] = json.loads(answer)
    if "error" in found:
        raise UnavailableError(f"iki-wasm refused the request: {found['error']}")
    return found


def melting_temperature(bases: str) -> float | None:
    """A supplied unmodified DNA oligo's fixed-condition melting temperature in °C.

    Uses the shared SantaLucia (1998) measurement with 50 mM sodium and 50 nM
    total strand concentration. Ambiguous bases or fewer than two bases give None.
    """
    if not bases.isascii() or len(bases) < 2:
        return None
    answer = call("iki_melting_temperature", {"bases": bases}, FUEL * (1 + len(bases) // 1000))
    celsius = answer["celsius"]
    return float(celsius) if celsius is not None else None
