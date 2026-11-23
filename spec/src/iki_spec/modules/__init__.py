"""The compiled WebAssembly modules first-party plugins' actions call.

A plugin's action may compute with a module built from the Rust in ``worker/crates`` --
the plasmid plugin's molecular module, which places primers, measures duplexes and cuts
DNA (ADR 0200). No such module is tracked in git: git holds the Rust and the module's
SHA-256 digest, and the bytes are built. They are built into this package, by
``scripts/build-plasmid-tools-wasm.sh`` from a checkout and by the server image's wasm
stage, so iki-spec's wheel carries them wherever it is installed.

That matters because ``iki deploy build`` exports each pinned plugin with ``git archive``,
which has the digest but not the module. The deployment CLI takes the module from here,
checks it against the exported digest, and writes it into the plugin's directory before
bundling it -- so a worker gets a module someone vouched for, and needs no Rust toolchain
of its own.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["BUILT", "module"]

#: Where the built modules are: this package's own directory.
BUILT = Path(__file__).parent


def module(name: str) -> Path | None:
    """The built module called `name` (``plasmid-tools.wasm``), or None where this
    installation hasn't got it. A caller checks it against its digest before using it."""
    if "/" in name or "\\" in name or name.startswith("."):
        return None
    found = BUILT / name
    return found if found.is_file() and found.stat().st_size > 0 else None
