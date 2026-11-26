"""How a writer's module is built (ADR 0230): the builder stages the plugin image runs, and
``iki plugin writers --update`` runs too, so the digest an author commits is the one a
deployment's build reproduces.

The builder is Iki's pinned Rust image, always as ``linux/amd64``: the platform the digest
is of, since rustc links its own precompiled wasm32 standard library, which differs by
host. Fetching the crates is the only step with a network, from crates.io or the site's
mirror (``IKI_CARGO_MIRROR``), or from crates the plugin vendors. Cargo checks each against
``Cargo.lock``. The build itself runs with no network (``RUN --network=none``) and no
credentials, its paths remapped by the SDK's ``build-writer.sh``, so a build script or a
procedural macro runs where it can reach nothing.

``deploy/plugins.Dockerfile`` holds :data:`BUILDER_ARG` and :data:`BUILDER_STAGES` word for
word; a test keeps them so.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from iki_spec import naming
from iki_spec.model import Writer
from iki_spec.writers import DIRECTORY, LIST, SDK, sdk_source

__all__ = ["BUILDER", "BUILDER_ARG", "BUILDER_STAGES", "SYNTAX", "build_in_docker", "dockerfile"]

#: Dockerfile frontend, as Iki's other Dockerfiles pin it.
SYNTAX = (
    "# syntax=docker/dockerfile:1.28.0@sha256:"
    "bb22d9815c728170f72750f4e5b0d672e06176142e1d602c7e66c050100b7e5b"
)
#: The builder: the Rust image Iki's own modules are built with (server/Dockerfile).
BUILDER = (
    "rust:1.93.0-slim-trixie@sha256:"
    "760ad1d638d70ebbd0c61e06210e1289cbe45ff6425e3ea6e01241de3e14d08e"
)
BUILDER_ARG = f"ARG IKI_WRITER_BUILDER={BUILDER}\n"
BUILDER_STAGES = """\
# --- Plugins' writers (ADR 0230) ----------------------------------------------
# Each writer's Rust crate becomes its WebAssembly module here: on linux/amd64, the
# platform its tracked digest is of, offline, with no credentials and its paths
# remapped. Fetching the crates is the only step with a network. A site without
# crates.io sets IKI_CARGO_MIRROR to its registry mirror, or its plugins vendor
# their crates. `iki plugin writers --update` runs these same stages.
FROM --platform=linux/amd64 ${IKI_WRITER_BUILDER} AS writer-build
ARG IKI_CARGO_MIRROR=
COPY plugins/iki_installed_plugins/__init__.py plugins/iki_installed_plugins/writer[s] /src/writers/
RUN set -eu; mkdir -p /out; \\
    if [ -s /src/writers/writers.txt ]; then \\
      rustup target add wasm32-unknown-unknown; \\
      while read -r crate; do \\
        sh /src/writers/sdk/iki-writer/build-writer.sh fetch "/src/writers/$crate"; \\
      done < /src/writers/writers.txt; \\
    fi
RUN --network=none set -eu; \\
    if [ -s /src/writers/writers.txt ]; then \\
      while read -r crate; do \\
        mkdir -p "/out/$(dirname "$crate")"; \\
        sh /src/writers/sdk/iki-writer/build-writer.sh build \
          "/src/writers/$crate" "/out/$crate.wasm"; \\
      done < /src/writers/writers.txt; \\
    fi
FROM scratch AS writer-wasm
COPY --from=writer-build /out /
"""


def dockerfile() -> str:
    """The builder alone, as ``iki plugin writers --update`` runs it."""
    return f"{SYNTAX}\n{BUILDER_ARG}\n{BUILDER_STAGES}"


def stage(plugin: str, root: Path, writers: Sequence[Writer], context: Path) -> dict[str, str]:
    """A build context for the builder of ``writers``, laid out as a deployment's build
    lays it out; returns each writer's crate path in it, by qualified ID."""
    sdk = sdk_source()
    if sdk is None:
        raise ValueError("This Iki has no iki-writer SDK to build writers with.")
    registry = context / "plugins" / "iki_installed_plugins"
    out = registry / DIRECTORY
    out.mkdir(parents=True)
    (registry / "__init__.py").write_text("")
    ignore = shutil.ignore_patterns("target", ".git", "*.wasm")
    shutil.copytree(sdk, out / SDK, ignore=shutil.ignore_patterns("target", ".git", "Cargo.lock"))
    crates: dict[str, str] = {}
    for writer in writers:
        crate = f"{naming.app_label(plugin)}/{writer.name}"
        shutil.copytree(root / writer.module, out / crate, ignore=ignore)
        crates[writer.qualified_name] = crate
    (out / LIST).write_text("".join(f"{c}\n" for c in crates.values()))
    (context / "Dockerfile").write_text(dockerfile())
    return crates


def build_in_docker(
    plugin: str, root: Path, writers: Sequence[Writer], work: Path, *, mirror: str = ""
) -> dict[str, bytes]:
    """Each writer's module, as the plugin image's builder makes it. Needs Docker."""
    docker = shutil.which("docker")
    if docker is None:
        raise ValueError(
            "Building a writer's module needs Docker: it's built in the linux/amd64 "
            "builder a deployment's image uses, so its digest is the one that build makes."
        )
    context, out = work / "context", work / "out"
    crates = stage(plugin, root, writers, context)
    command = [
        docker,
        "build",
        "--platform",
        "linux/amd64",
        "--target",
        "writer-wasm",
        "--output",
        f"type=local,dest={out}",
        *(["--build-arg", f"IKI_CARGO_MIRROR={mirror}"] if mirror else []),
        str(context),
    ]
    subprocess.run(command, check=True)  # noqa: S603 - docker, with a context made here
    return {writer: (out / f"{crate}.wasm").read_bytes() for writer, crate in crates.items()}


def digest_line(writer: Writer, module: bytes) -> str:
    """What ``<module>.wasm.sha256`` holds: the digest, and the module's file name."""
    return f"{hashlib.sha256(module).hexdigest()}  {Path(writer.module).name}.wasm\n"
