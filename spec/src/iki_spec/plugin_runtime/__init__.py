"""The plugin runtime's wheel this Iki ships (ADR 0288).

``iki-plugin-runtime`` (``runtime/`` in Iki's repository, imported as ``iki_runtime``)
holds the readers plugins' actions share. An environment that declares
``iki_runtime: true`` gets it installed: the copy built with this Iki, which iki-spec
carries here, as it carries the modules plugins' actions call (:mod:`iki_spec.modules`).
``scripts/build-plugin-runtime.sh`` builds it from a checkout, and the server image's
plugin-runtime stage does in the image; a wheel of iki-spec is built only with it.

``iki deploy build`` puts the wheel beside the environments that declare it, and its
SHA-256 into their build digests, so a worker installs only the bytes this release
built, and an upgrade that changes them rebuilds those environments.

An environment that declares ``iki_runtime: { helm: true }`` gets iki-wasm's module too,
for ``iki_runtime.helm``: the copy iki-spec carries (``iki_spec/iki_wasm.wasm``), whose
SHA-256 joins the environment's build digest. The server image's iki-spec carries the
module its wasm stage verified against ``server/iki_wasm.wasm.sha256``, with that pin
beside it, and is checked against it again. A checkout's is cargo's build on its own
host, whose bytes differ from the pin's by the paths cargo builds them in (even on Linux
x86-64), so it has no pin to be checked against: it goes by its digest alone.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

__all__ = ["DIRECTORY", "NAME", "WASM", "Module", "UnavailableError", "Wheel", "wasm", "wheel"]

#: The distribution's name, on PyPI too.
NAME = "iki-plugin-runtime"
#: Where the built wheel is: this package's own directory.
DIRECTORY = Path(__file__).parent
_FILE = re.compile(r"iki_plugin_runtime-(?P<version>[^-]+)-py3-none-any\.whl")
#: iki-wasm's module, which iki-spec carries, and ``iki_runtime.helm`` reads HELM with.
WASM = DIRECTORY.parent / "iki_wasm.wasm"


class UnavailableError(RuntimeError):
    """This installation of iki-spec has no plugin runtime wheel, or more than one."""


@dataclass(frozen=True)
class Wheel:
    """The wheel: where it is, its version and its bytes' SHA-256."""

    path: Path
    version: str
    sha256: str

    @property
    def name(self) -> str:
        """Its file name, which installers read its name and version from."""
        return self.path.name


@cache
def _read(path: Path, size: int, mtime: int) -> Wheel:
    found = _FILE.fullmatch(path.name)
    assert found is not None  # noqa: S101 - wheel() matched it
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return Wheel(path=path, version=found["version"], sha256=digest)


def wheel() -> Wheel:
    """The wheel this iki-spec carries. Raises :class:`UnavailableError`, saying how to
    build it, if there's none or more than one."""
    found = sorted(p for p in DIRECTORY.iterdir() if _FILE.fullmatch(p.name))
    if len(found) != 1:
        raise UnavailableError(
            f"iki-spec has {'no' if not found else len(found)} {NAME} wheel"
            f"{'s' if len(found) > 1 else ''} in {DIRECTORY}: run "
            "scripts/build-plugin-runtime.sh in Iki's repository (make test-wasm runs it), "
            "or install an iki-spec built there."
        )
    info = found[0].stat()
    return _read(found[0], info.st_size, info.st_mtime_ns)


@dataclass(frozen=True)
class Module:
    """iki-wasm's module: where it is, its bytes' SHA-256, and the one Iki pins, if it's
    the server image's verified copy, which carries its pin beside it."""

    path: Path
    sha256: str
    pinned: str | None

    #: Its name in an environment, and in a bundle.
    name = "iki_wasm.wasm"


@cache
def _read_module(path: Path, size: int, mtime: int) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def wasm() -> Module:
    """iki-wasm's module this iki-spec carries. Raises :class:`UnavailableError`, saying
    how to build it, if there's none."""
    if not WASM.is_file() or WASM.stat().st_size == 0:
        raise UnavailableError(
            f"iki-spec has no iki-wasm module at {WASM}: run scripts/build-wasm.sh in Iki's "
            "repository (make test-wasm runs it), or install an iki-spec built there."
        )
    info = WASM.stat()
    pin = WASM.with_name(WASM.name + ".sha256")
    words = pin.read_text("ascii").split() if pin.is_file() else []
    return Module(
        WASM, _read_module(WASM, info.st_size, info.st_mtime_ns), words[0] if words else None
    )
