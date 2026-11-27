"""`iki plugin writers`: a plugin's export writers, and the digests that pin them (ADR 0230)."""

from __future__ import annotations

import argparse
import platform
import sys
import tempfile
from pathlib import Path
from typing import TextIO

from iki_spec.writers import build as writer_build
from iki_spec.writers.check import check_module
from iki_spec.writers.manifest import crates, read_writers
from iki_spec.yamlsource import read_documents


def add_command(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    writers = commands.add_parser(
        "writers",
        help="List a plugin's writers, or build them and write their digests.",
        description=(
            "List each writer the plugin declares, with the digest it tracks and the crates "
            "its Cargo.lock pins. With --update, build each in the linux/amd64 builder a "
            "deployment's image uses (needs Docker) and write writers/<name>.wasm.sha256: "
            "commit it with the change to the Rust, so a reviewer sees both."
        ),
    )
    writers.add_argument(
        "directory",
        nargs="?",
        default=".",
        type=Path,
        help="The plugin's directory (default: the current directory).",
    )
    writers.add_argument(
        "--update", action="store_true", help="Build each writer and write its digest."
    )
    writers.add_argument(
        "--mirror",
        default="",
        metavar="URL",
        help="A Cargo registry mirror to fetch crates from (a sparse registry URL).",
    )
    writers.set_defaults(handler=run_writers)


def _canonical_host() -> bool:
    return platform.system() == "Linux" and platform.machine().lower() in {"x86_64", "amd64"}


def run_writers(args: argparse.Namespace, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    root: Path = args.directory
    documents, problems = read_documents(root / "plugin.yaml", "plugin.yaml")
    data = documents[0].data if documents else None
    if problems or not isinstance(data, dict) or not isinstance(data.get("name"), str):
        sys.stderr.write(f"error: {root / 'plugin.yaml'} isn't a plugin manifest.\n")
        return 1
    name = data["name"]
    try:
        writers = read_writers(name, data, root)
    except (KeyError, TypeError) as error:
        sys.stderr.write(
            f"error: a writer in plugin.yaml lacks {error}: run `iki plugin validate`.\n"
        )
        return 1
    if not writers:
        out.write(f"{name} declares no writers.\n")
        return 0
    if args.update:
        with tempfile.TemporaryDirectory(prefix="iki-writers-") as tmp:
            try:
                built = writer_build.build_in_docker(
                    name, root, list(writers.values()), Path(tmp), mirror=args.mirror
                )
            except (ValueError, OSError) as error:
                sys.stderr.write(f"error: {error}\n")
                return 1
            except Exception as error:
                sys.stderr.write(f"error: the builder failed: {error}\n")
                return 1
        for qualified, writer in writers.items():
            module = built[qualified]
            refused = check_module(module)
            if refused:
                sys.stderr.write(
                    f"error: {qualified} built, but can't be a writer: "
                    + " ".join(p.message for p in refused)
                    + "\n"
                )
                return 1
            line = writer_build.digest_line(writer, module)
            (root / f"{writer.module}.wasm.sha256").write_text(line)
            if _canonical_host():
                (root / f"{writer.module}.wasm").write_bytes(module)
            out.write(f"Wrote {writer.module}.wasm.sha256: {line.split()[0]}\n")
        return 0
    for qualified, writer in writers.items():
        out.write(f"{qualified}: {writer.label} ({writer.format}, many: {writer.many})\n")
        out.write(f"  module {writer.module}, memory {writer.memory // (1024 * 1024)} MiB\n")
        out.write(f"  sha256 {writer.sha256 or '(missing: run with --update)'}\n")
        pinned = crates(root, writer)
        if pinned:
            out.write("  crates " + ", ".join(f"{n} {v}" for n, v in pinned) + "\n")
    return 0
