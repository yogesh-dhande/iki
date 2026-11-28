"""How a writer's module is built (ADR 0230): the builder stages the plugin image runs, and
``iki plugin writers --update`` runs too, so the digest an author commits is the one a
deployment's build reproduces.

A module's bytes depend on its crate (the Rust, and the ``Cargo.lock`` that pins every
crate it's built from, iki-writer among them) and on the builder: :data:`BUILDER`, a Rust
image pinned by digest, always run as ``linux/amd64``, the platform the digest is of, since
rustc links its own precompiled wasm32 standard library, which differs by host; and the
SDK's ``build-writer.sh``, which runs the build in it. The builder is pinned apart from the
toolchain Iki builds its own code with, and moves only in a minor release, which says so:
it changes every writer's digest. The digest file records all of them
(:func:`digest_text`), so when a deployment's build makes another module, it can say which
input differs (:func:`differences`).

Fetching the crates is the only step with a network, from crates.io or the site's mirror
(``IKI_CARGO_MIRROR``), or from crates the plugin vendors. Cargo checks each against
``Cargo.lock``. The build itself runs with no network (``RUN --network=none``) and no
credentials, its paths remapped by ``build-writer.sh``, so a build script or a procedural
macro runs where it can reach nothing.

``deploy/plugins.Dockerfile`` holds :data:`BUILDER_ARG` and :data:`BUILDER_STAGES` word for
word; a test keeps them so.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from iki_spec import naming
from iki_spec.model import Writer
from iki_spec.writers import DIRECTORY, LIST, SDK, sdk_source

__all__ = [
    "BUILDER",
    "BUILDER_ARG",
    "BUILDER_STAGES",
    "BUILT_BY",
    "SDK_FILES",
    "SYNTAX",
    "BuiltWith",
    "build_in_docker",
    "built_with",
    "differences",
    "digest_text",
    "dockerfile",
    "read_built_with",
    "source_digest",
]

#: Dockerfile frontend, as Iki's other Dockerfiles pin it.
SYNTAX = (
    "# syntax=docker/dockerfile:1.28.0@sha256:"
    "bb22d9815c728170f72750f4e5b0d672e06176142e1d602c7e66c050100b7e5b"
)
#: The writer builder: a Rust image, pinned by digest on its own. It isn't the image Iki
#: builds its own code with (server/Dockerfile's RUST_IMAGE), which moves when Iki's
#: toolchain does: moving this one changes every plugin writer's digest, so it moves only
#: in a minor release, and its notes say so. scripts/writer-toolchain.sh reads its Rust
#: version, which local builds and iki-writer's packaging use.
BUILDER = (
    "rust:1.93.0-slim-trixie@sha256:"
    "760ad1d638d70ebbd0c61e06210e1289cbe45ff6425e3ea6e01241de3e14d08e"
)
BUILDER_ARG = f"ARG IKI_WRITER_BUILDER={BUILDER}\n"
#: What the builder stage writes beside the modules: the builder it ran in.
BUILT_BY = "builder"
#: What a build stages of the SDK: what build-writer.sh packages iki-writer from, and
#: the checksum that crate must have.
SDK_FILES = ("Cargo.toml", "Cargo.lock", "README.md", "build-writer.sh", "iki-writer.crate.sha256")
BUILDER_STAGES = """\
# --- Plugins' writers (ADR 0230) ----------------------------------------------
# Each writer's Rust crate becomes its WebAssembly module here: on linux/amd64, the
# platform its tracked digest is of, offline, with no credentials and its paths
# remapped. Fetching the crates is the only step with a network. A site without
# crates.io sets IKI_CARGO_MIRROR to its registry mirror, or its plugins vendor
# their crates. `iki plugin writers --update` runs these same stages.
FROM --platform=linux/amd64 ${IKI_WRITER_BUILDER} AS writer-build
ARG IKI_WRITER_BUILDER
ARG IKI_CARGO_MIRROR=
COPY plugins/iki_installed_plugins/__init__.py plugins/iki_installed_plugins/writer[s] /src/writers/
RUN set -eu; mkdir -p /out; printf '%s\\n' "$IKI_WRITER_BUILDER" > /out/builder; \\
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
        sh /src/writers/sdk/iki-writer/build-writer.sh build \\
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
    ignore = shutil.ignore_patterns(*NOT_SOURCE)
    stage_sdk(sdk, out / SDK)
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


def stage_sdk(sdk: Path, to: Path) -> None:
    """The SDK as a build stages it: what build-writer.sh packages iki-writer from."""
    to.mkdir(parents=True)
    for name in SDK_FILES:
        shutil.copyfile(sdk / name, to / name)
    shutil.copytree(sdk / "src", to / "src")
    (to / "build-writer.sh").chmod(0o755)


# --- What a module was built with ----------------------------------------------------------------

#: What of a writer's crate isn't its source, and a build leaves behind: Cargo's output,
#: git's and the Finder's.
NOT_SOURCE = frozenset({"target", ".git", ".DS_Store"})


def source_digest(crate: Path) -> str:
    """The SHA-256 of a writer crate's source: every file a build stages of it, by path
    and content, its ``Cargo.lock`` and any ``vendor/`` included."""
    digest = hashlib.sha256()
    files = sorted(
        p
        for p in crate.rglob("*")
        if not NOT_SOURCE.intersection(p.relative_to(crate).parts)
        and (p.is_symlink() or p.is_file())
    )
    for file in files:
        if file.is_symlink():
            raise ValueError(f"{file} is a symbolic link: a writer's crate holds only its files")
        content = hashlib.sha256(file.read_bytes()).hexdigest()
        digest.update(f"{file.relative_to(crate).as_posix()}\0{content}\n".encode())
    return digest.hexdigest()


def sdk_pin(crate: Path) -> str:
    """The iki-writer a crate's ``Cargo.lock`` pins, as ``<version> <checksum>``; empty
    when it pins none, or one without crates.io's checksum."""
    try:
        lock = tomllib.loads((crate / "Cargo.lock").read_text("utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    for package in lock.get("package", ()):
        if package.get("name") == "iki-writer" and package.get("checksum"):
            return f"{package.get('version')} {package['checksum']}"
    return ""


def script_digest(sdk: Path | None = None) -> str:
    """The SHA-256 of the build-writer.sh this Iki builds writers with."""
    found = sdk or sdk_source()
    script = found / "build-writer.sh" if found is not None else None
    if script is None or not script.is_file():
        return ""
    return hashlib.sha256(script.read_bytes()).hexdigest()


@dataclass(frozen=True)
class BuiltWith:
    """The inputs a module is built from, besides the toolchain's platform: the crate's
    source, the iki-writer its Cargo.lock pins (part of that source, said apart because
    it's the SDK), the builder image and the build script."""

    source: str
    sdk: str
    builder: str
    script: str


def built_with(crate: Path, *, builder: str = BUILDER, sdk: Path | None = None) -> BuiltWith:
    return BuiltWith(source_digest(crate), sdk_pin(crate), builder, script_digest(sdk))


_LINES = (
    ("source", "source"),
    ("iki-writer", "sdk"),
    ("builder", "builder"),
    ("build-writer.sh", "script"),
)


def digest_text(writer: Writer, module: bytes, built: BuiltWith) -> str:
    """What ``<module>.wasm.sha256`` holds: the digest and the module's file name, as
    ``sha256sum`` writes them, then what it was built with, as comments."""
    lines = [f"{hashlib.sha256(module).hexdigest()}  {Path(writer.module).name}.wasm"]
    for word, attribute in _LINES:
        value = getattr(built, attribute)
        if value:
            lines.append(f"# {word} {value}")
    return "\n".join(lines) + "\n"


def read_built_with(text: str) -> BuiltWith | None:
    """What a digest file says its module was built with; None if it doesn't say."""
    found: dict[str, str] = {}
    words = dict(_LINES)
    for line in text.splitlines():
        if line.startswith("# "):
            word, _, value = line[2:].partition(" ")
            if word in words:
                found[words[word]] = value.strip()
    if "source" not in found or "builder" not in found:
        return None
    return BuiltWith(
        found["source"], found.get("sdk", ""), found["builder"], found.get("script", "")
    )


def differences(recorded: BuiltWith | None, current: BuiltWith) -> list[str]:
    """Which of a module's inputs differ from those its tracked digest was written with,
    each said with what to do; empty when none do."""
    if recorded is None:
        return [
            "its digest file doesn't say what the module was built with, so which input "
            "differs can't be told: run `iki plugin writers --update` and commit the digest"
        ]
    out = []
    if recorded.sdk != current.sdk:
        out.append(
            f"its Cargo.lock pins iki-writer {_sdk(current.sdk)}, and the digest was written "
            f"with iki-writer {_sdk(recorded.sdk)}: if that's meant, run "
            "`iki plugin writers --update` and commit the digest"
        )
    elif recorded.source != current.source:
        out.append(
            "its source (the Rust, or its Cargo.lock) isn't what the digest was written "
            "from: if the change is meant, run `iki plugin writers --update` and commit the "
            "digest; if it isn't, the source isn't what was reviewed"
        )
    if recorded.builder != current.builder:
        out.append(
            f"the digest was written with the writer builder {recorded.builder}, and this "
            f"build ran in {current.builder}: run `iki plugin writers --update` with this "
            "Iki and commit the digest, or build the image with "
            f"--build-arg IKI_WRITER_BUILDER={recorded.builder}"
        )
    if recorded.script and current.script and recorded.script != current.script:
        out.append(
            "the digest was written by another Iki's build-writer.sh: run "
            "`iki plugin writers --update` with this Iki and commit the digest"
        )
    return out


def _sdk(pin: str) -> str:
    version, _, checksum = pin.partition(" ")
    return f"{version} ({checksum[:12]}…)" if checksum else "(none from crates.io)"
