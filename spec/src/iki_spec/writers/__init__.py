"""Plugins' export writers (ADR 0230): Rust built to WebAssembly, run in a sandbox.

- :mod:`iki_spec.writers.check` reads a module's bytes and refuses what can't be a writer.
- :mod:`iki_spec.writers.runtime` runs one under Wasmtime with its limits.
- :mod:`iki_spec.writers.abi` is the ABIs this Iki runs, and each one's requests.
- :mod:`iki_spec.writers.protocol` is how the server talks to a writer's process.
- :mod:`iki_spec.writers.child` is that process: one export, then it exits.
- :mod:`iki_spec.writers.install` checks and precompiles the modules a plugin image built.
- :mod:`iki_spec.writer_sandbox` is the sandbox service that starts the children.

Where things are:

- In a plugin, a writer is a Rust crate at ``writers/<name>/`` with its ``Cargo.lock``
  (which pins iki-writer from crates.io with its checksum), and
  ``writers/<name>.wasm.sha256`` beside it: the SHA-256 of the module the Linux x86-64
  writer builder makes, and what it was built with. No ``.wasm`` is ever tracked in git.
- In a build (``iki deploy build``, ``iki plugin build``), ``iki_installed_plugins/writers/``
  holds each crate at ``<plugin label>/<name>/``; at ``sdk/iki-writer/``, the build script
  and this Iki's iki-writer, which a crate pinned to it is built against;
  ``writers.txt`` (the crates, one per line, for the image's shell stage) and
  ``writers.json`` (each writer's digest, memory and, in a development build, the module
  built here).
- In an image, ``/opt/iki/writers`` holds each module, its precompiled code and
  ``manifest.json``, root-owned and read-only.
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "BUILD",
    "DIRECTORY",
    "IMAGE",
    "LIST",
    "MANIFEST",
    "SDK",
    "SDK_NAME",
    "sdk_source",
]

#: A plugin's writers' directory, and the build's.
DIRECTORY = "writers"
#: Where an image installs the checked modules.
IMAGE = Path("/opt/iki/writers")
#: The image's list of what it installed.
MANIFEST = "manifest.json"
#: The build's description of its writers, and its plain list of crates.
BUILD = "writers.json"
LIST = "writers.txt"
#: Where a build puts the SDK, under its writers' directory.
SDK = "sdk/iki-writer"
SDK_NAME = "iki-writer"


def sdk_source() -> Path | None:
    """The iki-writer crate's source: Iki's checkout's, or the copy iki-spec's wheel
    carries. None where neither is."""
    here = Path(__file__).resolve()
    for candidate in (
        here.parents[4] / "worker" / "crates" / SDK_NAME if len(here.parents) > 4 else None,
        here.parent / "sdk" / SDK_NAME,
    ):
        if candidate is not None and (candidate / "Cargo.toml").is_file():
            return candidate
    return None
