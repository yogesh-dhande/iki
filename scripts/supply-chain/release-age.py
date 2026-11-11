#!/usr/bin/env python3
"""Fails if a locked dependency was released less than 3 days ago (ADR 0019).

A hijacked release is usually found and pulled within days, so nothing that new gets
in. pnpm refuses such versions itself (``minimumReleaseAge``) and uv resolves without
them (``exclude-newer``); this checks what's actually locked, for those and for cargo,
which has no such setting.

- ``uv.lock``: when each package's files were uploaded, as the lockfile records.
- ``worker/Cargo.lock``: each crate's publish time, from the crates.io index.

Usage: ``scripts/supply-chain/release-age.py`` from anywhere. Needs network for cargo.
"""

from __future__ import annotations

import json
import sys
import tomllib
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MINIMUM_AGE = timedelta(days=3)
CRATES_REGISTRY = "registry+https://github.com/rust-lang/crates.io-index"
INDEX = "https://index.crates.io"

#: Releases allowed in though they're new, each with why. Keep uv's matching
#: ``exclude-newer-package`` entry (pyproject.toml) in step. Once a release is 3 days old
#: the check says its exception can go; once it isn't locked, the check fails until it
#: goes, so a stale exception can't hold a package back.
EXCEPTIONS: dict[tuple[str, str], str] = {
    ("playwright", "1.65.0"): "test tool, never shipped: 1.64's WebKit reports spurious "
    "style-src-attr CSP violations against the document",
}


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
        if not times:
            raise SystemExit(
                f"uv.lock doesn't record when {package['name']} {package['version']} was "
                "uploaded. Relock with a current uv."
            )
        found.append((package["name"], package["version"], min(times)))
    return found


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
        request = urllib.request.Request(  # noqa: S310 - a fixed https URL
            f"{INDEX}/{_index_path(name)}", headers={"User-Agent": "iki-release-age-check"}
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            lines = response.read().decode().splitlines()
        entry = next((e for e in map(json.loads, lines) if e["vers"] == version), None)
        if entry is None or "pubtime" not in entry:
            raise SystemExit(f"The crates.io index has no publish time for {name} {version}.")
        found.append((name, version, _when(entry["pubtime"])))
    return found


def too_new(
    releases: list[tuple[str, str, datetime]], now: datetime
) -> list[tuple[str, str, datetime]]:
    return [r for r in releases if now - r[2] < MINIMUM_AGE]


def main() -> int:
    now = datetime.now(UTC)
    failed = False
    locked: set[tuple[str, str]] = set()
    for label, lock, read in (
        ("Python (uv.lock)", ROOT / "uv.lock", python_releases),
        ("Rust (worker/Cargo.lock)", ROOT / "worker" / "Cargo.lock", crate_releases),
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
