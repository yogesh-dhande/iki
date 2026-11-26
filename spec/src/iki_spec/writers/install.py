"""The plugin image's last word on its writers (ADR 0230): check what the builder built,
and install it.

    python -m iki_spec.writers.install BUILD BUILT OUT

``BUILD`` is the build's writers directory (``iki_installed_plugins/writers``, with
``writers.json``), ``BUILT`` what the offline builder stage made of each crate
(``<crate>.wasm``), and ``OUT`` where the image keeps them (``/opt/iki/writers``). For each
writer it:

1. compares the module's SHA-256 with the digest the plugin tracks, and fails naming the
   writer and both digests if they differ;
2. refuses a module the checker refuses (imports, the wrong ABI, a second or shared
   memory, an oversized module, function body or ``br_table``);
3. compiles it with the sandbox's engine, checks its exports' types, and starts it once
   within its memory, so one that can't start fails here rather than on a download;
4. writes the module, its precompiled code and ``manifest.json`` with both digests, this
   Wasmtime's version and the target, for the sandbox to check before it maps the code.

Any failure exits non-zero, so a bad module fails the image build.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Any

import wasmtime

from iki_spec.writers import BUILD, MANIFEST
from iki_spec.writers.check import check_module
from iki_spec.writers.child import host_target
from iki_spec.writers.runtime import Instance, WriterFailed, engine


class InstallError(Exception):
    """A writer that can't be installed, and why."""


def install(build: Path, built: Path, out: Path) -> dict[str, Any]:
    """Checks and installs every writer the build lists; returns the manifest."""
    described = build / BUILD
    writers: dict[str, Any] = (
        json.loads(described.read_text("utf-8")).get("writers", {}) if described.is_file() else {}
    )
    compiler = engine()
    out.mkdir(parents=True, exist_ok=True)
    installed: dict[str, Any] = {}
    for writer, entry in sorted(writers.items()):
        crate = str(entry["crate"])
        module = built / f"{crate}.wasm"
        if not module.is_file():
            raise InstallError(f"{writer}: the builder made no module of {crate}.")
        binary = module.read_bytes()
        made = hashlib.sha256(binary).hexdigest()
        if made != entry["sha256"]:
            raise InstallError(
                f"{writer}: the module built from its source is {made}, but the plugin's "
                f"{entry['name']}.wasm.sha256 says {entry['sha256']}. If the Rust or its "
                "Cargo.lock changed on purpose, run `iki plugin writers --update` and commit "
                "the digest; otherwise the source isn't what was reviewed."
            )
        if problems := check_module(binary):
            raise InstallError(f"{writer}: " + " ".join(p.message for p in problems))
        try:
            compiled = wasmtime.Module(compiler, binary)
            Instance(compiled, compiler, int(entry["memory"]))
        except (WriterFailed, wasmtime.WasmtimeError) as error:
            raise InstallError(f"{writer}: {error}") from None
        native = compiled.serialize()
        place = out / f"{crate}.wasm"
        place.parent.mkdir(parents=True, exist_ok=True)
        place.write_bytes(binary)
        place.with_suffix(".cwasm").write_bytes(native)
        installed[writer] = {
            "wasm": f"{crate}.wasm",
            "wasm_sha256": made,
            "cwasm": f"{crate}.cwasm",
            "cwasm_sha256": hashlib.sha256(native).hexdigest(),
            "memory": int(entry["memory"]),
        }
    found = {
        "format": 1,
        "wasmtime_version": version("wasmtime"),
        "target": host_target(),
        "writers": installed,
    }
    (out / MANIFEST).write_text(json.dumps(found, indent=2, sort_keys=True) + "\n", "utf-8")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("build", type=Path)
    parser.add_argument("built", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args(argv)
    try:
        found = install(args.build, args.built, args.out)
    except InstallError as error:
        sys.stderr.write(f"error: {error}\n")
        return 1
    for writer, entry in found["writers"].items():
        sys.stdout.write(f"Installed {writer} ({entry['wasm_sha256']})\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
