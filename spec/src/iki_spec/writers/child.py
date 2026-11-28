"""One export's process (ADR 0230): load one writer, run its calls, exit.

    python -I -m iki_spec.writers.child --modules /opt/iki/writers   # in the sandbox
    python -I -m iki_spec.writers.child --local                      # a development server
    python -I -m iki_spec.writers.child --modules DIR --verify       # readiness: load each

It's started with an empty environment and only its pipes, and is used once. It imports
Wasmtime and makes the engine before the export arrives, so a waiting child answers at
once. Once it has its module it gives up what it doesn't need: it may create no file, open
no more descriptors and start no process. Then it serves the export's calls
(:mod:`iki_spec.writers.protocol`) and exits when its stream ends or a call fails.

In the sandbox (``--modules``) it loads only what the image installed there, checked as
:mod:`iki_spec.writers.install` left it: root-owned, read-only, its digests matching. The
server names the writer and the digest it expects; a different module is refused. With
``--local`` the server names the module's file too, which only a development server does.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import platform
import stat
import sys
from importlib.metadata import version
from pathlib import Path
from typing import IO, Any, BinaryIO

import wasmtime

from iki_spec.writers import MANIFEST
from iki_spec.writers.check import check_module
from iki_spec.writers.protocol import Closed, read_frame, read_json, write_frame, write_json
from iki_spec.writers.runtime import ENTRIES, MAX_MEMORY, Instance, Limits, WriterFailed, engine


class Refused(Exception):  # noqa: N818 - answered to the server as an error
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def host_target() -> str | None:
    if platform.system() != "Linux":
        return None
    return {"x86_64": "x86_64-unknown-linux-gnu", "aarch64": "aarch64-unknown-linux-gnu"}.get(
        platform.machine()
    )


def _image_owned(path: Path, *, directory: bool = False) -> None:
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise Refused("unavailable", f"{path} isn't a root-owned, read-only image file.")


def manifest(directory: Path) -> dict[str, Any]:
    """The image's writers, checked as the image's own files: root-owned and read-only."""
    for part in (*reversed(directory.parents), directory):
        _image_owned(part, directory=True)
    file = directory / MANIFEST
    _image_owned(file)
    found = json.loads(file.read_bytes())
    if not isinstance(found, dict) or found.get("format") != 1:
        raise Refused("unavailable", "The writers' manifest isn't one this Iki reads.")
    return found


def _digest(file: BinaryIO) -> str:
    file.seek(0)
    digest = hashlib.sha256()
    while chunk := file.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def load_image(
    compiler: wasmtime.Engine, directory: Path, writer: str, sha256: str
) -> wasmtime.Module:
    """The installed module of ``writer``, if its digest is ``sha256``: its precompiled code
    when that checks out against the manifest, the host and this Wasmtime, else compiled
    from its verified ``.wasm``."""
    found = manifest(directory)
    entry = (found.get("writers") or {}).get(writer)
    if not isinstance(entry, dict):
        raise Refused("no_writer", f"This sandbox has no writer {writer}.")
    if entry.get("wasm_sha256") != sha256:
        raise Refused(
            "digest_mismatch",
            f"The sandbox's {writer} is {entry.get('wasm_sha256')}, not {sha256}: the server "
            "and the sandbox run different builds.",
        )
    wasm = directory / str(entry["wasm"])
    _image_owned(wasm)
    with wasm.open("rb") as source:
        if _digest(source) != sha256:
            raise Refused("digest_mismatch", f"{writer}'s module doesn't match its digest.")
        source.seek(0)
        binary = source.read()
    if problems := check_module(binary):
        raise Refused("rejected", " ".join(p.message for p in problems))
    native = directory / str(entry.get("cwasm") or "")
    usable = (
        entry.get("cwasm")
        and found.get("wasmtime_version") == version("wasmtime")
        and found.get("target") == host_target()
    )
    if usable:
        try:
            _image_owned(native)
            with native.open("rb") as code:
                if _digest(code) == entry.get("cwasm_sha256"):
                    # The checked inode stays open while Wasmtime maps it: no second
                    # lookup can put other code in its place.
                    return wasmtime.Module.deserialize_file(compiler, f"/dev/fd/{code.fileno()}")
        except (OSError, Refused, wasmtime.WasmtimeError):
            pass
    return wasmtime.Module(compiler, binary)


def load_local(compiler: wasmtime.Engine, path: str, sha256: str) -> wasmtime.Module:
    """A development server's module, from the file it names, if its digest is ``sha256``."""
    with Path(path).open("rb") as source:
        binary = source.read()
    if hashlib.sha256(binary).hexdigest() != sha256:
        raise Refused("digest_mismatch", "The writer's module doesn't match its digest.")
    if problems := check_module(binary):
        raise Refused("rejected", " ".join(p.message for p in problems))
    return wasmtime.Module(compiler, binary)


def _confine() -> None:
    """Gives up what a writer never needs, so code that got out of Wasm finds less here: no
    files written, no new descriptors, no new processes, no core dumps."""
    try:
        import resource
    except ImportError:  # pragma: no cover - not Unix
        return
    limits = [(resource.RLIMIT_FSIZE, 0), (resource.RLIMIT_CORE, 0)]
    if sys.platform.startswith("linux"):
        limits.append((resource.RLIMIT_NPROC, 0))
    highest = (
        max((int(n.name) for n in Path("/proc/self/fd").iterdir()), default=2)
        if Path("/proc/self/fd").is_dir()
        else 2
    )
    limits.append((resource.RLIMIT_NOFILE, highest + 1))
    for which, value in limits:
        with contextlib.suppress(ValueError, OSError):
            resource.setrlimit(which, (value, value))


def serve(
    stdin: IO[bytes], stdout: IO[bytes], compiler: wasmtime.Engine, modules: Path | None
) -> int:
    """One export, from its header to its last call."""
    try:
        header = read_json(stdin)
    except Closed:
        return 0  # a waiting child the launcher no longer needs
    try:
        writer, sha256 = str(header["writer"]), str(header["sha256"])
        memory = int(header["memory"])
        if not 0 < memory <= MAX_MEMORY:
            raise Refused("limits", "The writer's memory is outside its limits.")
        # The deployment's limits, as the server gives them: Iki's where it doesn't.
        limits = Limits.of(header.get("limits"))
        if modules is not None:
            if "path" in header:
                raise Refused("refused", "The sandbox loads only the writers its image installed.")
            module = load_image(compiler, modules, writer, sha256)
        else:
            module = load_local(compiler, str(header["path"]), sha256)
        instance = Instance(module, compiler, memory, limits)
    except (KeyError, TypeError, ValueError) as error:
        write_json(stdout, {"error": {"code": "malformed", "message": f"Bad header: {error}"}})
        return 2
    except Refused as why:
        write_json(stdout, {"error": {"code": why.code, "message": str(why)}})
        return 1
    except (WriterFailed, OSError, wasmtime.WasmtimeError) as error:
        code = error.code if isinstance(error, WriterFailed) else "unavailable"
        write_json(stdout, {"error": {"code": code, "message": str(error)}})
        return 1
    _confine()
    # The ABI the module reads, which the server builds each request for.
    write_json(stdout, {"ready": True, "abi": instance.abi})
    begun = False
    while True:
        try:
            ask = read_json(stdin)
            request = read_frame(stdin)
        except Closed:
            return 0
        entry = ask.get("call")
        if entry not in ENTRIES:
            write_json(stdout, {"failed": {"code": "malformed", "message": "No such call."}})
            return 2
        try:
            if entry == "begin":
                # Each file starts from a fresh instance: a ZIP's files (`many: zip`) share
                # nothing but the export they're in.
                if begun:
                    instance = Instance(module, compiler, memory, limits)
                begun = True
            done = instance.call(str(entry), request)
        except WriterFailed as failed:
            write_json(stdout, {"failed": {"code": failed.code, "message": str(failed)}})
            return 1
        write_json(stdout, {"status": done.status, "notes": done.notes, "fuel": done.fuel})
        write_frame(stdout, done.output)


def verify(compiler: wasmtime.Engine, modules: Path) -> dict[str, str | None]:
    """Each installed writer, and what stops it loading: None when it loads."""
    try:
        found = manifest(modules)
    except (OSError, ValueError, Refused) as error:
        return {"*": str(error)}
    out: dict[str, str | None] = {}
    for writer, entry in sorted((found.get("writers") or {}).items()):
        try:
            module = load_image(compiler, modules, writer, str(entry.get("wasm_sha256")))
            Instance(module, compiler, int(entry.get("memory") or MAX_MEMORY))
            out[writer] = None
        except (Refused, WriterFailed, OSError, ValueError, wasmtime.WasmtimeError) as error:
            out[writer] = str(error)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One plugin writer's export.")
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--modules", type=Path, help="the image's installed writers")
    where.add_argument("--local", action="store_true", help="a development server's child")
    parser.add_argument("--verify", action="store_true", help="load each installed writer")
    args = parser.parse_args(argv)
    compiler = engine()
    if args.verify:
        if args.modules is None:
            parser.error("--verify needs --modules")
        sys.stdout.write(json.dumps(verify(compiler, args.modules)) + "\n")
        return 0
    return serve(sys.stdin.buffer, sys.stdout.buffer, compiler, args.modules)


if __name__ == "__main__":
    sys.exit(main())
