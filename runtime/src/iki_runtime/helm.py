"""HELM, read by iki-wasm: the reader Iki itself reads HELM with (ADR 0144), so a plugin's
action sees the same polymers, monomers, positions and connections as Iki's forms, its
identities and its other plugins.

A HELM string is read into its canonical form and what it holds: its polymers in
canonical order, each one's monomers in order with its repeats of a fixed count written
out, its connections, and its polymer groups. Everything here is canonical: polymers are
numbered again from 1 in their canonical order, a ring is turned to the unit it reads
least from, and a missing monomer (``_``) on its own or as a base is left out. So a
position names the same monomer however the string was written, and a message that names
one should say it's the canonical form's.

It needs two things beside Python's standard library, which an environment gets by
declaring ``iki_runtime: { helm: true }`` (ADR 0288):

- ``wasmtime``, the WebAssembly runtime, from the environment's own requirements (Iki's
  first-party plugins pin ``wasmtime==51.0.0``); it's imported only when HELM is read.
- iki-wasm's module, ``iki_wasm.wasm``, of the Iki that built the environment. The build
  puts it at ``share/iki-runtime/iki_wasm.wasm`` in the environment's prefix, checked
  against the digest the environment's build is keyed by. ``$IKI_RUNTIME_WASM`` names
  another (a lab's own image); and a Python that has iki-spec installed, as Iki's own
  checkout and ``iki plugin test`` do, reads with the module iki-spec carries.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import stat
import sys
import threading
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Sequence

__all__ = [
    "ENVIRONMENT_VARIABLE",
    "FUEL",
    "Connection",
    "End",
    "Group",
    "GroupMember",
    "HelmError",
    "Length",
    "Monomer",
    "Polymer",
    "Reading",
    "UnavailableError",
    "canonical",
    "module_path",
    "precompile",
    "precompiled",
    "read",
    "read_all",
]

#: Names the module to read with, where the environment's own isn't (a lab's own image).
ENVIRONMENT_VARIABLE = "IKI_RUNTIME_WASM"
#: Where an environment Iki builds has the module, under its prefix (``sys.prefix``).
IN_ENVIRONMENT = Path("share", "iki-runtime", "iki_wasm.wasm")
#: Instructions reading 1,000 characters of HELM may take, with room for the identical
#: polymers whose numberings canonicalising tries: iki-spec's budget (iki_spec.helm).
FUEL = 20_000_000
#: The most a request may be: iki-wasm's own limit on its input.
_MAX_REQUEST = 64 * 1024 * 1024


class HelmError(ValueError):
    """Why a string isn't HELM, or can't be canonical, and at which character (from 0)."""

    def __init__(self, message: str, at: int) -> None:
        super().__init__(message)
        self.message = message
        self.at = at


class UnavailableError(RuntimeError):
    """HELM can't be read here: wasmtime isn't installed, or iki-wasm's module isn't
    there, doesn't load, or stopped."""


@dataclass(frozen=True)
class Monomer:
    """A monomer of a polymer, in order, as the canonical form has it."""

    #: Its position, from 1, as connections count: each monomer, a branch's too.
    position: int
    #: Its ID, without brackets; its SMILES, if it's in-line; or a list's members as
    #: written, such as ``(A,G)``.
    id: str
    #: It's a branch monomer, hanging from the backbone monomer before it: a base.
    branch: bool
    #: It's in-line SMILES rather than a monomer library's ID.
    smiles: bool
    #: It's one of several monomers (``,``), or a mixture of them (``+``).
    list: bool
    #: The unit it's in (a nucleotide, for RNA), from 1.
    unit: int


@dataclass(frozen=True)
class Length:
    """How many amino acids, or nucleotides with a base, a polymer has."""

    fewest: int
    #: None when there's no most: a ``*``, any number of monomers.
    most: int | None


@dataclass(frozen=True)
class Polymer:
    """A simple polymer, as the canonical form has it."""

    #: Its canonical ID, such as ``RNA1``.
    id: str
    #: ``PEPTIDE``, ``RNA``, ``CHEM`` or ``BLOB``.
    type: str
    #: Its units as the canonical form writes them.
    units: int
    length: Length
    #: Its monomers in order, repeats of a fixed count written out.
    monomers: tuple[Monomer, ...]
    #: It has a repeat of a range of counts, a list of monomers, a monomer HELM keeps for
    #: an unknown one (``*``, ``_``, a PEPTIDE's ``X``, an RNA's ``N``), or it's a BLOB.
    ambiguous: bool


@dataclass(frozen=True)
class End:
    """One end of a connection."""

    #: The polymer, or polymer group (``G1``), it's in, by canonical ID.
    polymer: str
    #: The monomer's position, from 1; None when the end names monomer IDs, or ``?``,
    #: rather than a position.
    position: int | None
    #: Its attachment point (``R1``), ``pair`` for a base pair's hydrogen bonds, or ``?``.
    attachment: str
    #: Where it is as the canonical form writes it: the position, or what's there.
    at: str


@dataclass(frozen=True)
class Connection:
    """A bond between two monomers, or a base pair, as the canonical form writes it."""

    #: The connection, canonical: ``RNA1,CHEM1,1:R1-1:R1``.
    text: str
    one: End
    other: End

    @property
    def pair(self) -> bool:
        """It's a base pair: hydrogen bonds, which join no atoms."""
        return self.one.attachment == "pair" and self.other.attachment == "pair"


@dataclass(frozen=True)
class GroupMember:
    id: str
    #: Its ratio, as written without trailing zeros, if it's given.
    ratio: str | None


@dataclass(frozen=True)
class Group:
    """A polymer group: polymers (or groups) that are one of them, or a mixture."""

    #: Its canonical ID, such as ``G1``.
    id: str
    #: Its members are a mixture (``+``), all there; otherwise it's one of them (``,``).
    mixture: bool
    members: tuple[GroupMember, ...]


@dataclass(frozen=True)
class Reading:
    """A HELM string read: its canonical form, and what it holds, in canonical order."""

    canonical: str
    polymers: tuple[Polymer, ...]
    connections: tuple[Connection, ...]
    groups: tuple[Group, ...]
    #: It was written as HELM 1: no ``V2.0``, base pairs in its third section.
    version1: bool

    def polymer(self, id: str) -> Polymer:
        """The polymer with this canonical ID. Raises KeyError."""
        for p in self.polymers:
            if p.id == id:
                return p
        raise KeyError(id)


def read(text: str) -> Reading:
    """``text``, HELM 2 or HELM 1, read. Raises :class:`HelmError` if it isn't HELM, and
    :class:`UnavailableError` if it can't be read here."""
    (found,) = read_all([text])
    if isinstance(found, HelmError):
        raise found
    return found


def read_all(texts: Sequence[str]) -> list[Reading | HelmError]:
    """Each HELM string read, or why it isn't HELM, in one call to the module. Raises
    :class:`UnavailableError` if HELM can't be read here."""
    if not texts:
        return []
    size = sum(len(t) for t in texts)
    answer = _call("iki_helm", {"helm": list(texts)}, FUEL * (1 + size // 1000))
    out: list[Reading | HelmError] = []
    for each in answer["readings"]:
        if "error" in each:
            out.append(HelmError(str(each["error"]), int(each.get("at", 0))))
        else:
            out.append(_reading(each))
    return out


def canonical(text: str) -> str:
    """``text``'s canonical form. Raises :class:`HelmError` if it isn't HELM."""
    return read(text).canonical


# --- What iki-wasm says, as values --------------------------------------------------------

#: A connection's two ends after its polymers: each where it is, then its attachment
#: point. What an end names when it isn't a position (``(A,G)``, ``?``) comes first.
_ENDS = re.compile(r"(?P<a>.+?):(?P<ra>R\d+|pair|\?)-(?P<b>.+):(?P<rb>R\d+|pair|\?)", re.S)


def _end(polymer: str, at: str, attachment: str) -> End:
    position = int(at) if at.isdigit() else None
    return End(polymer=polymer, position=position, attachment=attachment, at=at)


def _connection(text: str) -> Connection:
    one, other, rest = [*text.split(",", 2), "", ""][:3]
    ends = _ENDS.fullmatch(rest)
    if ends is None:
        # Not one this reads; each end says what's there, and names no position.
        return Connection(text, End(one, None, "?", rest), End(other, None, "?", rest))
    return Connection(
        text,
        _end(one, ends["a"], ends["ra"]),
        _end(other, ends["b"], ends["rb"]),
    )


def _reading(found: Any) -> Reading:  # noqa: ANN401 - iki-wasm's JSON
    return Reading(
        canonical=str(found["canonical"]),
        polymers=tuple(
            Polymer(
                id=p["id"],
                type=p["type"],
                units=int(p["units"]),
                length=Length(int(p["length"]["fewest"]), p["length"]["most"]),
                monomers=tuple(
                    Monomer(
                        position=int(m["position"]),
                        id=m["id"],
                        branch=bool(m["branch"]),
                        smiles=bool(m["smiles"]),
                        list=bool(m["list"]),
                        unit=int(m["unit"]),
                    )
                    for m in p["monomers"]
                ),
                ambiguous=bool(p["ambiguous"]),
            )
            for p in found["polymers"]
        ),
        connections=tuple(_connection(c) for c in found["connections"]),
        groups=tuple(
            Group(
                id=g["id"],
                mixture=bool(g["mixture"]),
                members=tuple(GroupMember(m["id"], m.get("ratio")) for m in g["members"]),
            )
            for g in found["groups"]
        ),
        version1=bool(found["version1"]),
    )


# --- The module ----------------------------------------------------------------------------


def module_path() -> Path:
    """The iki-wasm module HELM is read with: ``$IKI_RUNTIME_WASM``'s, the environment's
    own, or the one iki-spec carries, the first that's there. Raises
    :class:`UnavailableError` saying where it looked."""
    named = os.environ.get(ENVIRONMENT_VARIABLE, "").strip()
    if named:
        given = Path(named)
        if not given.is_file():
            raise UnavailableError(
                f"${ENVIRONMENT_VARIABLE} names {given}, which isn't a file: it names "
                "iki-wasm's module, iki_wasm.wasm, of the Iki this runs with."
            )
        return given
    own = Path(sys.prefix) / IN_ENVIRONMENT
    if own.is_file():
        return own
    spec = find_spec("iki_spec")
    for location in (spec.submodule_search_locations or []) if spec is not None else []:
        carried = Path(location) / "iki_wasm.wasm"
        if carried.is_file():
            return carried
    raise UnavailableError(
        f"iki-wasm's module isn't here: not at {own}, nor named by ${ENVIRONMENT_VARIABLE}. "
        "An environment Iki builds has it when it declares `iki_runtime: { helm: true }`; "
        "elsewhere, set the variable to iki_wasm.wasm of your Iki's release."
    )


_lock = threading.Lock()
_compiled: tuple[Any, Any] | None = None
_local = threading.local()


@atexit.register
def _release() -> None:
    """Frees the module and this thread's instance while wasmtime can still free them: at
    exit, after Python has torn its modules down, it can't, and says so on stderr."""
    global _compiled, _local
    _compiled, _local = None, threading.local()


def _wasmtime() -> Any:  # noqa: ANN401 - wasmtime, which this package doesn't depend on
    try:
        # Imported here, not above: only reading HELM needs it.
        import wasmtime
    except ImportError:
        raise UnavailableError(
            "iki_runtime.helm reads HELM with wasmtime, which isn't installed here: add it "
            "to the environment's requirements, pinned and hashed (Iki's plugins pin "
            "wasmtime==51.0.0)."
        ) from None
    return wasmtime


def _engine(wasmtime: Any) -> Any:  # noqa: ANN401 - wasmtime's
    config = wasmtime.Config()
    config.consume_fuel = True
    # One thread compiles it, so an action's memory isn't spent all at once.
    config.parallel_compilation = False
    return wasmtime.Engine(config)


def precompiled(module: Path) -> Path:
    """Where the module's code, compiled for this machine, is kept beside it: named by its
    bytes' SHA-256, so it's this module's and no other's."""
    digest = hashlib.sha256(module.read_bytes()).hexdigest()
    return module.with_name(f"{module.stem}.{digest[:32]}.cwasm")


def _trusted(path: Path) -> bool:
    """Whether compiled code at ``path`` is as trusted as the environment's own: a regular
    file, this user's or root's, that no one else can write."""
    try:
        info = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode):
        return False
    if os.name != "posix":
        return True
    return info.st_uid in (0, os.getuid()) and not info.st_mode & 0o022


def precompile(module: Path) -> Path:
    """Compiles ``module`` for this machine, as the environment's build does, so a run
    loads it in milliseconds rather than compiling it for seconds. Returns where it is."""
    wasmtime = _wasmtime()
    target = precompiled(module)
    code = wasmtime.Module.from_file(_engine(wasmtime), str(module)).serialize()
    partial = target.with_name(f".{target.name}.{os.getpid()}")
    partial.write_bytes(code)
    partial.chmod(0o644)
    partial.replace(target)
    return target


def _module() -> tuple[Any, Any]:
    global _compiled  # compiled once, for every thread
    with _lock:
        if _compiled is None:
            wasmtime = _wasmtime()
            path = module_path()
            engine = _engine(wasmtime)
            module = None
            ready = precompiled(path)
            if _trusted(ready):
                try:
                    module = wasmtime.Module.deserialize_file(engine, str(ready))
                except wasmtime.WasmtimeError:
                    module = None  # another wasmtime's, or another machine's: compile it
            if module is None:
                try:
                    module = wasmtime.Module.from_file(engine, str(path))
                except wasmtime.WasmtimeError as error:
                    raise UnavailableError(f"iki-wasm doesn't load from {path}: {error}") from None
            _compiled = (engine, module)
        return _compiled


class _Instance:
    def __init__(self) -> None:
        wasmtime = _wasmtime()
        engine, module = _module()
        self.store = wasmtime.Store(engine)
        # Setting the module up runs some of it too, which a new store has no fuel for.
        self.store.set_fuel(FUEL)
        exports = wasmtime.Instance(self.store, module, []).exports(self.store)
        try:
            self.memory = exports["memory"]
            self.functions = {
                name: exports[name]
                for name in ("iki_input", "iki_output", "iki_output_len", "iki_helm")
            }
        except KeyError as missing:
            raise UnavailableError(
                f"iki-wasm's module has no {missing.args[0]}: it isn't the module of an Iki "
                "this runtime reads HELM with."
            ) from None

    def call(self, export: str, payload: bytes, fuel: int) -> bytes:
        store, functions = self.store, self.functions
        store.set_fuel(fuel)
        at = functions["iki_input"](store, len(payload))
        self.memory.write(store, payload, at)
        functions[export](store)
        start = functions["iki_output"](store)
        length = functions["iki_output_len"](store)
        return bytes(self.memory.read(store, start, start + length))


def _call(export: str, request: Any, fuel: int) -> Any:  # noqa: ANN401 - JSON
    payload = json.dumps(request).encode()
    if len(payload) > _MAX_REQUEST:
        raise UnavailableError("The request exceeds iki-wasm's 64 MiB input limit.")
    wasmtime = _wasmtime()
    instance: _Instance | None = getattr(_local, "instance", None)
    if instance is None:
        instance = _local.instance = _Instance()
    try:
        answer = instance.call(export, payload, fuel)
    except wasmtime.Trap as trap:
        # A trapped instance's memory is in no known state: the next call gets a new one.
        _local.instance = None
        raise UnavailableError(f"iki-wasm stopped: {trap.message}") from None
    found = json.loads(answer)
    if "error" in found:
        raise UnavailableError(f"iki-wasm refused the request: {found['error']}")
    return found


if __name__ == "__main__":
    # `python -m iki_runtime.helm precompile MODULE`: what an environment's build runs.
    if len(sys.argv) != 3 or sys.argv[1] != "precompile":
        sys.exit("usage: python -m iki_runtime.helm precompile MODULE")
    try:
        sys.stdout.write(f"{precompile(Path(sys.argv[2]))}\n")
    except UnavailableError as error:
        sys.exit(str(error))
