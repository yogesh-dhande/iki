#!/usr/bin/env python3
"""Fails if an exception in .grype.yaml has expired, or doesn't say when it does.

Grype has no expiry of its own, so each `ignore` rule is preceded by a comment that
starts `# Until YYYY-MM-DD:` and says why it's safe. Like osv-scanner.toml's
`ignoreUntil`, an expired exception fails CI again until it's renewed or removed.

Usage: ``scripts/supply-chain/image-exceptions.py`` from anywhere.
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UNTIL = re.compile(r"^\s*# Until (\d{4}-\d{2}-\d{2}):")


def expiries(lines: list[str]) -> list[tuple[str, str | None]]:
    """(vulnerability, the date its exception expires, if its comment says) for each rule."""
    found = []
    for number, line in enumerate(lines):
        rule = re.match(r"^\s*- vulnerability:\s*(\S+)", line)
        if rule is None:
            continue
        until = None
        # The comment block just above the rule.
        for above in reversed(lines[:number]):
            if not above.strip().startswith("#"):
                break
            if match := UNTIL.match(above):
                until = match[1]
        found.append((rule[1], until))
    return found


def main() -> int:
    lines = (ROOT / ".grype.yaml").read_text(encoding="utf-8").splitlines()
    today = datetime.now(UTC).date().isoformat()
    failed = False
    for vulnerability, until in expiries(lines):
        if until is None:
            failed = True
            sys.stdout.write(
                f"  .grype.yaml's exception for {vulnerability} doesn't say when it expires "
                "(a `# Until YYYY-MM-DD:` comment above it).\n"
            )
        elif until < today:
            failed = True
            sys.stdout.write(
                f"  .grype.yaml's exception for {vulnerability} expired on {until}: update the "
                "image, or renew it with why.\n"
            )
        else:
            sys.stdout.write(f"  {vulnerability} is excepted until {until}.\n")
    sys.stdout.write(
        "Image scan exceptions: " + ("some have expired.\n" if failed else "current.\n")
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
