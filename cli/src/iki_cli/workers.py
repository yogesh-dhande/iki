"""What a deployment's workers need, from the same lockfile as its server (ADR 0047).

``iki deploy build --workers DIRECTORY`` writes:

- ``plugins/<label>/``: each plugin's code, exported at the commit the lockfile
  pins (the server's image gets only its manifests), in a directory named by
  its label (``iki__protein`` for ``iki/protein``, ADR 0113);
- ``plugins/plugins.json``: what the worker tells the server it has. Each
  plugin's name, version and commit, and each environment it builds, with
  the build's digest and where it's built;
- ``build-environments.sh``: builds those environments, for the worker
  image's Dockerfile to run.

An environment's digest covers only what decides its contents: its Python
version and requirements file, or its conda lockfile. So an environment
that didn't change keeps its digest, and its layer stays cached, across
plugin commits.

Every ``iki deploy build`` also bundles each pinned plugin's code for the server
to give self-hosted workers (ADR 0059): a gzipped tar, the same bytes
for the same files, with what's needed to build each environment where it lands.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Any

from iki_spec import API_VERSION, naming
from iki_spec.model import Environment, Plugin

__all__ = [
    "ENVIRONMENTS",
    "SCRIPT",
    "WORKER_PLUGINS",
    "WorkerPlugin",
    "bundle",
    "environments",
    "prepared_source",
    "write",
]

#: Where the worker image builds environments, unless told otherwise.
ENVIRONMENTS = "/opt/iki/envs"
#: What the worker reads, in ``IKI_PLUGINS_DIR``.
WORKER_PLUGINS = "plugins.json"
SCRIPT = "build-environments.sh"

#: Copied from a local plugin directory, for development, without these.
_IGNORED = shutil.ignore_patterns(".git", "__pycache__", ".venv", "node_modules", ".DS_Store")


@dataclass(frozen=True)
class WorkerPlugin:
    plugin: Plugin
    directory: Path
    #: None for a local directory.
    sha: str | None


def _sha256(file: Path) -> str:
    return hashlib.sha256(file.read_bytes()).hexdigest()


def prepared_source(name: str, directory: Path, output: Path, *, pinned: bool = False) -> Path:
    """Stage first-party worker resources pinned by the plugin's source tree.

    The annotation digest applies on every host. The measurement digest comes from
    the Linux x86-64 release build; other development hosts use the installed module
    and its notice. Both worker distributions read these resources from iki-spec.
    """
    if name == "iki/plasmid":
        return _prepared_annotation(directory, output)
    if name != "iki/dna":
        return directory
    relative = Path("data", "oligo-measurement.wasm")
    generated = directory / relative
    marker = generated.with_suffix(".sha256")
    if not marker.exists() and not marker.is_symlink():
        # Historical Git trees can carry their own module. Only discard an explicitly
        # ignored build product in a local checkout that no longer requests it.
        git = shutil.which("git")
        if pinned or not generated.exists() or git is None:
            return directory
        ignored = subprocess.run(  # noqa: S603 - git, with a fixed relative path
            [git, "-C", str(directory), "check-ignore", "--quiet", "--", str(relative)],
            capture_output=True,
            check=False,
        )
        if ignored.returncode != 0:
            return directory
    notice = generated.with_name("rust-standard-library.html")
    if any(path.is_symlink() for path in (generated.parent, marker, generated, notice)):
        raise ValueError("The DNA measurement, digest, and notice must be regular plugin files.")
    if any(path.exists() and not path.is_file() for path in (marker, generated, notice)):
        raise ValueError("The DNA measurement, digest, and notice must be regular files.")

    resources: dict[str, bytes] = {}
    if marker.is_file():
        for name in (generated.name, notice.name):
            installed = files("iki_spec").joinpath(name)
            if not installed.is_file():
                raise ValueError(f"iki-spec lacks {name}. Run scripts/build-wasm.sh.")
            resources[name] = installed.read_bytes()
            if not resources[name]:
                raise ValueError(f"iki-spec's {name} is empty. Run scripts/build-wasm.sh.")
        expected = marker.read_text("ascii").strip()
        if re.fullmatch(r"[0-9a-f]{64}", expected) is None:
            raise ValueError("The DNA measurement marker must hold a SHA-256 digest.")
        module = resources[generated.name]
        if platform.system() == "Linux" and platform.machine().lower() in {"x86_64", "amd64"}:
            if hashlib.sha256(module).hexdigest() != expected:
                raise ValueError("The DNA measurement in iki-spec differs from the pinned digest.")
        else:
            sys.stderr.write(
                f"iki/dna: using the installed measurement on {platform.system()} "
                f"{platform.machine()}. Its SHA-256 pin is compared on Linux x86-64, "
                "the release build platform.\n"
            )

    return _stage_resources(directory, output, relative, resources)


def _prepared_annotation(directory: Path, output: Path) -> Path:
    """Stage annotation resources only when the clone pins the installed module. The digest
    is verified on every host, including for a local checkout."""
    relative = Path("data/dna-annotation.wasm")
    generated = directory / relative
    marker = generated.with_name(generated.name + ".sha256")
    if not marker.exists() and not marker.is_symlink():
        # Older pinned plugin trees used the marker without the module suffix.
        marker = generated.with_suffix(".sha256")
    if not marker.exists() and not marker.is_symlink():
        return directory
    notice = generated.with_name("rust-standard-library.html")
    paths = (marker, generated, notice)
    if generated.parent.is_symlink() or any(
        path.is_symlink() or (path.exists() and not path.is_file()) for path in paths
    ):
        raise ValueError(
            "The DNA annotation module, digest and notice must be regular plugin files."
        )

    resources: dict[str, bytes] = {}
    package = files("iki_spec")
    for path in (generated, notice):
        installed = package.joinpath(path.name)
        if not installed.is_file():
            raise ValueError(
                f"iki-spec lacks {path.name}. Run scripts/build-dna-annotation-wasm.sh."
            )
        resources[path.name] = installed.read_bytes()
    digest = hashlib.sha256(resources[generated.name]).hexdigest()
    expected = marker.read_bytes().strip()
    if digest.encode("ascii") != expected:
        raise ValueError(
            f"{generated.name} in iki-spec differs from the pinned digest. "
            f"Expected {expected.decode('ascii', errors='backslashreplace')}, actual {digest}."
        )

    return _stage_resources(directory, output, relative, resources)


def _stage_resources(
    directory: Path, output: Path, relative: Path, resources: dict[str, bytes]
) -> Path:
    """Replace or remove the build product in a private copy for both worker distributions."""
    shutil.copytree(directory, output, ignore=_IGNORED, symlinks=True)
    target = output / relative
    if not resources:
        target.unlink()
    for name, data in resources.items():
        asset = target.with_name(name)
        # A local build product copied into staging may be read-only.
        asset.unlink(missing_ok=True)
        asset.write_bytes(data)
        asset.chmod(0o644)
    return output


def _built(env: Environment) -> bool:
    """Whether workers build it: a Python or conda environment for the backend or
    self-hosted tier. The container and vm tiers run images instead."""
    native = env.requirements is not None or env.conda_lockfile is not None
    return native and bool(set(env.tiers) & {"backend", "self_hosted"})


def _definition(env: Environment, directory: Path) -> dict[str, str]:
    if env.conda_lockfile is not None:
        return {"kind": "conda", "lockfile_sha256": _sha256(directory / env.conda_lockfile)}
    assert env.python_version is not None and env.requirements is not None  # noqa: S101 - the schema
    return {
        "kind": "python",
        "python": env.python_version,
        "requirements_sha256": _sha256(directory / env.requirements),
    }


def _digest(definition: dict[str, str]) -> str:
    canonical = json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def environments(plugin: Plugin, directory: Path) -> dict[str, dict[str, Any]]:
    """How a worker builds each of `plugin`'s environments from its bundle: the digest it
    reports, and the files (by path in the bundle) and Python version the build takes."""
    found: dict[str, dict[str, Any]] = {}
    for name, env in sorted(plugin.environments.items()):
        if not _built(env):
            continue
        definition = _definition(env, directory)
        entry: dict[str, Any] = {"digest": _digest(definition), "kind": definition["kind"]}
        if env.conda_lockfile is not None:
            entry["lockfile"] = str(PurePosixPath(env.conda_lockfile))
        else:
            assert env.python_version is not None and env.requirements is not None  # noqa: S101 - the schema
            entry["python"] = env.python_version
            entry["requirements"] = str(PurePosixPath(env.requirements))
            entry["packages"] = _has_requirements(directory / env.requirements)
        found[name] = entry
    return found


def _files(directory: Path) -> list[Path]:
    ignored = {".git", "__pycache__", ".venv", "node_modules", ".DS_Store"}
    found: list[Path] = []
    for root, dirs, names in os.walk(directory):
        dirs[:] = sorted(d for d in dirs if d not in ignored)
        here = Path(root)
        found.extend(here / name for name in names if name not in ignored)
        found.extend(here / d for d in dirs if (here / d).is_symlink())
    return sorted(found)


def bundle(directory: Path, output: Path) -> dict[str, Any]:
    """Writes `directory`'s code to `output`, a gzipped tar workers fetch, and returns its
    ``sha256`` and ``size``. Only regular files: a symbolic link could point anywhere on
    the computer that unpacks it, so a plugin with one is refused. Times, owners and
    modes other than executable are left out, so the same files give the same bytes."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for file in _files(directory):
            relative = file.relative_to(directory).as_posix()
            if file.is_symlink() or not file.is_file():
                raise ValueError(
                    f"{relative} is a symbolic link or special file; plugins hold only "
                    "regular files"
                )
            info = tarfile.TarInfo(relative)
            info.size = file.stat().st_size
            info.mode = 0o755 if os.access(file, os.X_OK) else 0o644
            info.mtime = 0
            with file.open("rb") as data:
                tar.addfile(info, data)
    packed = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=packed, mtime=0) as zipped:
        zipped.write(raw.getvalue())
    output.write_bytes(packed.getvalue())
    return {"sha256": hashlib.sha256(packed.getvalue()).hexdigest(), "size": output.stat().st_size}


def _has_requirements(file: Path) -> bool:
    """Whether a requirements file names any package: an environment may need none."""
    for line in file.read_text("utf-8").splitlines():
        text = line.split("#", 1)[0].strip()
        if text and not text.startswith("--hash"):
            return True
    return False


def _build_lines(item: WorkerPlugin, env: Environment, prefix: str) -> list[str]:
    q = shlex.quote
    # Files in the bundle, beside this script, wherever it's copied.
    code = f"plugins/{naming.app_label(item.plugin.name)}"
    if env.conda_lockfile is not None:
        lockfile = f"{code}/{PurePosixPath(env.conda_lockfile)}"
        return [
            f'micromamba create --yes --quiet --prefix {q(prefix)} --file "$here"/{q(lockfile)}'
        ]
    assert env.python_version is not None and env.requirements is not None  # noqa: S101 - the schema
    # uv downloads the Python version when the image has none.
    lines = [f"uv venv --quiet --python {q(env.python_version)} {q(prefix)}"]
    if _has_requirements(item.directory / env.requirements):
        requirements = f"{code}/{PurePosixPath(env.requirements)}"
        # Every package pinned and hashed, so nothing else is installed.
        lines.append(
            f"uv pip install --quiet --python {q(prefix)} --require-hashes --no-deps "
            f'--requirement "$here"/{q(requirements)}'
        )
    return lines


def write(
    plugins: Sequence[WorkerPlugin], output: Path, env_root: str = ENVIRONMENTS
) -> dict[str, Any]:
    """Writes the workers' bundle to ``output`` (which mustn't exist), for environments
    built in ``env_root``, and returns what ``plugins.json`` holds."""
    code = output / "plugins"
    code.mkdir(parents=True)
    listed = []
    script = [
        "#!/bin/sh",
        "# Generated by `iki deploy build`: builds the environments workers run actions in.",
        "# The worker image's Dockerfile runs it, with uv and micromamba on PATH.",
        "set -eu",
        'here=$(cd "$(dirname "$0")" && pwd)',
    ]
    for item in sorted(plugins, key=lambda p: p.plugin.name):
        name = item.plugin.name
        label = naming.app_label(name)
        shutil.copytree(item.directory, code / label, ignore=_IGNORED, symlinks=True)
        environments = {}
        for env_name, env in sorted(item.plugin.environments.items()):
            if not _built(env):
                continue
            prefix = f"{env_root}/{label}/{env_name}"
            definition = _definition(env, item.directory)
            environments[env_name] = {
                "digest": _digest(definition),
                "kind": definition["kind"],
                "prefix": prefix,
            }
            script.append(f"echo {shlex.quote(f'Building {name}.{env_name}')}")
            script.append(f"mkdir -p {shlex.quote(f'{env_root}/{label}')}")
            script.extend(_build_lines(item, env, prefix))
        listed.append(
            {
                "name": name,
                "version": item.plugin.version,
                "sha": item.sha,
                "environments": environments,
            }
        )
    manifest = {"apiVersion": API_VERSION, "kind": "WorkerPlugins", "plugins": listed}
    (code / WORKER_PLUGINS).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    (output / SCRIPT).write_text("\n".join(script) + "\n")
    (output / SCRIPT).chmod(0o755)
    return manifest
