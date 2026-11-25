#!/usr/bin/env python3
"""Fails if a locked dependency was released less than 3 days ago (ADR 0019).

A hijacked release is usually found and pulled within days, so nothing that new gets
in. pnpm refuses such versions itself (``minimumReleaseAge``) and uv resolves without
them (``exclude-newer``); this checks what's actually locked, for those and for cargo,
which has no such setting.

- ``uv.lock``: when each package's files were uploaded, as the lockfile records.
- ``worker/Cargo.lock``: each crate's publish time, from the crates.io index.
- Product and example plugins' ``requirements.txt``: each pin must be in uv.lock, so the
  plugin's tests run what its environment installs, and its age is checked there.
- Their ``images/*/uv.lock``: what a plugin's image installs, checked as uv.lock is.
  A package from an index that doesn't say when it was uploaded (PyTorch's CPU builds)
  is as old as the same release on PyPI, which it's built from.

Usage: ``scripts/supply-chain/release-age.py`` from anywhere. Needs network for cargo.
"""

from __future__ import annotations

import json
import re
import sys
import time
import tomllib
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MINIMUM_AGE = timedelta(days=3)
CRATES_REGISTRY = "registry+https://github.com/rust-lang/crates.io-index"
INDEX = "https://index.crates.io"
PYPI = "https://pypi.org/simple"

#: Releases allowed in though they're new, each with why. Keep uv's matching
#: ``exclude-newer-package`` entry (pyproject.toml) in step. Once a release is 3 days old
#: the check says its exception can go; once it isn't locked, the check fails until it
#: goes, so a stale exception can't hold a package back.
EXCEPTIONS: dict[tuple[str, str], str] = {}


#: A registry answering 503 or dropping a connection says nothing about how old a release is,
#: so a lookup that fails that way is worth asking again rather than failing the check. Only
#: transient answers are retried: a 404 is an answer, and the caller still raises on it.
_TRANSIENT = frozenset({408, 425, 429, 500, 502, 503, 504})


def _fetch(url: str, *, attempts: int = 4) -> bytes:
    """The bytes at url, asking again after a transient failure, backing off between tries."""
    request = urllib.request.Request(  # noqa: S310 - callers pass a fixed https URL
        url, headers={"User-Agent": "iki-release-age-check"}
    )
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                data: bytes = response.read()
                return data
        except urllib.error.HTTPError as error:
            if error.code not in _TRANSIENT or attempt == attempts:
                raise
            why = f"HTTP {error.code}"
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt == attempts:
                raise
            why = str(getattr(error, "reason", error))
        sys.stderr.write(f"  {url} gave {why}; asking again ({attempt} of {attempts - 1})\n")
        time.sleep(2**attempt)
    raise AssertionError("unreachable")  # pragma: no cover


def _when(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def python_releases(lock: Path) -> list[tuple[str, str, datetime]]:
    """(name, version, released) for each package uv.lock takes from a registry: when its
    first file was uploaded."""
    found = []
    for package in tomllib.loads(lock.read_text(encoding="utf-8"))["package"]:
        if "registry" not in package.get("source", {}):
            continue
        files = [package.get("sdist", {}), *package.get("wheels", [])]
        times = [_when(f["upload-time"]) for f in files if "upload-time" in f]
        if not times and package["source"]["registry"] != PYPI:
            times = [pypi_released(package["name"], package["version"])]
        if not times:
            raise SystemExit(
                f"uv.lock doesn't record when {package['name']} {package['version']} was "
                "uploaded. Relock with a current uv."
            )
        found.append((package["name"], package["version"], min(times)))
    return found


def pypi_released(name: str, version: str) -> datetime:
    """When a release's first file was uploaded to PyPI: for a local build of it, such as
    torch 2.15.0+cpu, the release it was built from."""
    public = version.split("+", 1)[0]
    files = json.loads(_fetch(f"https://pypi.org/pypi/{name}/{public}/json"))["urls"]
    if not files:
        raise SystemExit(f"PyPI has no files for {name} {public}, to tell how old it is.")
    return min(_when(f["upload_time_iso_8601"]) for f in files)


def _index_path(name: str) -> str:
    name = name.lower()
    if len(name) <= 2:
        return f"{len(name)}/{name}"
    if len(name) == 3:
        return f"3/{name[0]}/{name}"
    return f"{name[:2]}/{name[2:4]}/{name}"


def crate_releases(lock: Path) -> list[tuple[str, str, datetime]]:
    """(name, version, published) for each crate Cargo.lock takes from crates.io."""
    found = []
    packages = tomllib.loads(lock.read_text(encoding="utf-8")).get("package", [])
    for package in packages:
        if package.get("source") != CRATES_REGISTRY:
            continue
        name, version = package["name"], package["version"]
        lines = _fetch(f"{INDEX}/{_index_path(name)}").decode().splitlines()
        entry = next((e for e in map(json.loads, lines) if e["vers"] == version), None)
        if entry is None or "pubtime" not in entry:
            raise SystemExit(f"The crates.io index has no publish time for {name} {version}.")
        found.append((name, version, _when(entry["pubtime"])))
    return found


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def plugin_files(root: Path, pattern: str) -> list[Path]:
    return sorted(
        file for base in ("plugins", "examples/plugins") for file in (root / base).glob(pattern)
    )


def plugin_pins(root: Path) -> list[tuple[Path, str, str]]:
    """(file, name, version) for each ``name==version`` in a product or example plugin's
    requirements."""
    found = []
    for file in plugin_files(root, "*/requirements.txt"):
        for line in file.read_text(encoding="utf-8").splitlines():
            pinned = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)", line)
            if pinned:
                found.append((file, _normalized(pinned[1]), pinned[2]))
    return found


def unlocked_pins(
    pins: list[tuple[Path, str, str]], locked: list[tuple[str, str, datetime]]
) -> list[tuple[Path, str, str]]:
    """The plugin pins uv.lock doesn't have at the same version."""
    have = {(_normalized(name), version) for name, version, _ in locked}
    return [pin for pin in pins if pin[1:] not in have]


def too_new(
    releases: list[tuple[str, str, datetime]], now: datetime
) -> list[tuple[str, str, datetime]]:
    return [r for r in releases if now - r[2] < MINIMUM_AGE]


def main() -> int:
    now = datetime.now(UTC)
    failed = False
    locked: set[tuple[str, str]] = set()
    images = [
        (f"Python ({lock.relative_to(ROOT)})", lock, python_releases)
        for lock in plugin_files(ROOT, "*/images/*/uv.lock")
    ]
    for label, lock, read in (
        ("Python (uv.lock)", ROOT / "uv.lock", python_releases),
        ("Rust (worker/Cargo.lock)", ROOT / "worker" / "Cargo.lock", crate_releases),
        *images,
    ):
        releases = read(lock)
        locked.update((name, version) for name, version, _ in releases)
        new = too_new(releases, now)
        for name, version, _ in [r for r in new if r[:2] in EXCEPTIONS]:
            why = EXCEPTIONS[name, version]
            sys.stdout.write(f"  {name} {version} is new, and allowed: {why}.\n")
        new = [r for r in new if r[:2] not in EXCEPTIONS]
        for name, version, released in new:
            ready = (released + MINIMUM_AGE).strftime("%Y-%m-%d %H:%M UTC")
            sys.stdout.write(f"  {name} {version} is too new: it can be locked from {ready}.\n")
        failed = failed or bool(new)
        sys.stdout.write(
            f"{label}: {len(releases) - len(new)} of {len(releases)} at least 3 days old.\n"
        )
    pins = plugin_pins(ROOT)
    for file, name, version in unlocked_pins(pins, python_releases(ROOT / "uv.lock")):
        failed = True
        sys.stdout.write(
            f"  {file.relative_to(ROOT)} pins {name} {version}, which uv.lock doesn't: pin "
            "the same release in pyproject.toml's dev dependencies, or recompile it.\n"
        )
    sys.stdout.write(f"Product and example plugins: {len(pins)} pins, checked against uv.lock.\n")
    for name, version in sorted(EXCEPTIONS):
        if (name, version) not in locked:
            failed = True
            sys.stdout.write(
                f"The exception for {name} {version} is stale: it isn't locked any more. "
                "Remove it here and from pyproject.toml's exclude-newer-package.\n"
            )
    for name, version, released in python_releases(ROOT / "uv.lock"):
        if (name, version) in EXCEPTIONS and now - released >= MINIMUM_AGE:
            sys.stdout.write(
                f"The exception for {name} {version} isn't needed any more (it's 3 days "
                "old): remove it here and from pyproject.toml's exclude-newer-package.\n"
            )
    if failed:
        sys.stdout.write(
            "Lock an older version (uv: `uv lock`; cargo: `cargo update -p NAME --precise V`).\n"
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
