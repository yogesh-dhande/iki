"""Reads a bundle, trusting nothing in it until its bytes and signature check out (ADR 0096).

An archive could come from anywhere, so nothing is extracted to disk. Every member must
be a regular file with a name a bundle has, listed in ``bundle.json`` with its SHA-256
and size; a blob's name must be its own SHA-256. ``bundle.json.sig`` must be a valid
signature of ``bundle.json`` by the key it names, and that key one this deployment trusts
for the deployment it names.
"""

from __future__ import annotations

import hashlib
import json
import re
import tarfile
import zlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from iki_server.bundles import keys
from iki_spec import schemas

#: The most ``bundle.json`` may be.
MANIFEST_LIMIT = 16 * 1024 * 1024
#: The most one NDJSON line may be.
LINE_LIMIT = 64 * 1024 * 1024
CHUNK = 1024 * 1024
_MEMBER = re.compile(r"bundle\.json|bundle\.json\.sig|[a-z]+\.ndjson|blobs/sha256/[0-9a-f]{64}")
_LINES = {
    "people.ndjson": "person",
    "agents.ndjson": "agent",
    "files.ndjson": "file",
    "entries.ndjson": "entry",
    "entities.ndjson": "entity",
    "outside.ndjson": "outside",
    "runs.ndjson": "run",
}


class BundleError(Exception):
    """Why a bundle can't be read, or isn't to be trusted."""


@dataclass(frozen=True)
class Bundle:
    path: Path
    #: The archive's own SHA-256, as the audit log names it.
    sha256: str
    size: int
    manifest: dict[str, Any]
    #: The deployment this one trusts it as, or None if it doesn't trust the key.
    trusted_as: str | None

    @property
    def source(self) -> str:
        return str(self.manifest["source"]["deployment"])

    def lines(self, member: str) -> Iterator[dict[str, Any]]:
        """Each line of an NDJSON member, checked against its schema."""
        part = _LINES[member]
        if not any(m["path"] == member for m in self.manifest["members"]):
            return
        with tarfile.open(self.path, "r:gz") as tar:
            stream = _open(tar, member)
            for number, line in enumerate(_split(stream, member), start=1):
                try:
                    row = json.loads(line)
                except ValueError:
                    raise BundleError(f"Line {number} of {member} isn't JSON.") from None
                issues = schemas.check("bundle", row, part=part)
                if issues:
                    raise BundleError(f"Line {number} of {member}: {issues[0].message}")
                yield row

    def blob(self, sha256: str) -> Iterator[bytes]:
        """A file's bytes, in chunks."""
        with tarfile.open(self.path, "r:gz") as tar:
            stream = _open(tar, f"blobs/sha256/{sha256}")
            yield from iter(lambda: stream.read(CHUNK), b"")


def read(path: Path, trusted: Mapping[str, tuple[str, ...]], *, limit: int) -> Bundle:
    """The bundle at ``path``, once its members, their hashes and its signature check out.

    ``limit`` is the most its members may come to, uncompressed.
    """
    found: dict[str, tuple[str, int]] = {}
    manifest_bytes = signature = b""
    total = 0
    try:
        with tarfile.open(path, "r:gz") as tar:
            for info in tar:
                name = info.name
                if not info.isreg() or not _MEMBER.fullmatch(name):
                    raise BundleError(f"{name!r} isn't something a bundle holds.")
                if name in found:
                    raise BundleError(f"{name} is in the archive twice.")
                total += info.size
                if total > limit:
                    raise BundleError(
                        f"It comes to more than {limit:,} bytes uncompressed, more than this "
                        "deployment takes (IKI_BUNDLE_MAX_BYTES)."
                    )
                stream = tar.extractfile(info)
                if stream is None:
                    raise BundleError(f"{name} can't be read.")
                if name == "bundle.json":
                    if info.size > MANIFEST_LIMIT:
                        raise BundleError("Its bundle.json is too large.")
                    manifest_bytes = stream.read()
                    found[name] = (hashlib.sha256(manifest_bytes).hexdigest(), info.size)
                elif name == "bundle.json.sig":
                    signature = stream.read(65)
                    found[name] = ("", info.size)
                else:
                    found[name] = _hash(stream)
    except (tarfile.TarError, EOFError, OSError, zlib.error) as error:
        raise BundleError(f"It isn't a bundle Iki can read: {error}.") from None
    if not manifest_bytes:
        raise BundleError("It has no bundle.json, so it isn't a bundle.")
    try:
        manifest = json.loads(manifest_bytes)
    except ValueError:
        raise BundleError("Its bundle.json isn't JSON.") from None
    if not isinstance(manifest, dict) or manifest.get("apiVersion") != "iki/v1":
        raise BundleError(
            "Its bundle.json isn't a bundle of a format this Iki reads (iki/v1). A newer Iki "
            "may have made it."
        )
    issues = schemas.check("bundle", manifest)
    if issues:
        raise BundleError(f"Its bundle.json isn't valid: {issues[0].message}")
    if not signature:
        raise BundleError("It isn't signed (it has no bundle.json.sig).")
    key = manifest["source"]["key"]
    if not keys.verify(key, signature, manifest_bytes):
        raise BundleError(
            "Its signature doesn't match its bundle.json: it was changed after it was signed."
        )
    listed = {m["path"]: (m["sha256"], m["size"]) for m in manifest["members"]}
    present = {k: v for k, v in found.items() if k not in ("bundle.json", "bundle.json.sig")}
    if extra := sorted(present.keys() - listed.keys()):
        raise BundleError(f"It holds {extra[0]}, which its bundle.json doesn't list.")
    if missing := sorted(listed.keys() - present.keys()):
        raise BundleError(f"It's missing {missing[0]}, which its bundle.json lists.")
    for name, expected in listed.items():
        if present[name] != expected:
            raise BundleError(f"{name} isn't what its bundle.json says: it was changed.")
        if name.startswith("blobs/sha256/") and name.rsplit("/", 1)[1] != expected[0]:
            raise BundleError(f"{name} holds bytes of another SHA-256.")
    source = manifest["source"]["deployment"]
    sha, size = _file_hash(path)
    return Bundle(
        path=path,
        sha256=sha,
        size=size,
        manifest=manifest,
        trusted_as=source if key in trusted.get(source, ()) else None,
    )


def _hash(stream: IO[bytes]) -> tuple[str, int]:
    sha = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: stream.read(CHUNK), b""):
        sha.update(chunk)
        size += len(chunk)
    return sha.hexdigest(), size


def _file_hash(path: Path) -> tuple[str, int]:
    with path.open("rb") as stream:
        return _hash(stream)


def _open(tar: tarfile.TarFile, name: str) -> IO[bytes]:
    try:
        stream = tar.extractfile(name)
    except KeyError:
        raise BundleError(f"It has no {name}.") from None
    if stream is None:
        raise BundleError(f"{name} can't be read.")
    return stream


def _split(stream: IO[bytes], member: str) -> Iterator[bytes]:
    while line := stream.readline(LINE_LIMIT + 1):
        if len(line) > LINE_LIMIT:
            raise BundleError(f"A line of {member} is longer than {LINE_LIMIT:,} bytes.")
        if line.strip():
            yield line
