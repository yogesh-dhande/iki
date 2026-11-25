"""Compose the dev-ci deployment from the product config and the fixture overlay."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import yaml

from iki_spec import API_VERSION, compute, lockfile

FIXTURES = Path(__file__).resolve().parent
DEV = FIXTURES.parent


def compose(output: Path) -> None:
    """Write the combined configuration where this image build can read it."""
    pins = []
    for source in (DEV, FIXTURES):
        result = lockfile.load_lock(source / lockfile.FILENAME, test_only=source == FIXTURES)
        if result.lock is None:
            raise ValueError("\n".join(f"{p.file}: {p.message}" for p in result.problems))
        for plugin in result.lock.plugins:
            pin = plugin.to_dict()
            for key in ("local", "settings"):
                if key in pin:
                    pin[key] = os.path.relpath(source / pin[key], output)
            pins.append(pin)

    output.mkdir(parents=True, exist_ok=True)
    (output / lockfile.FILENAME).write_text(
        yaml.safe_dump(
            {"apiVersion": API_VERSION, "kind": lockfile.KIND, "plugins": pins},
            sort_keys=False,
        )
    )
    (output / compute.FILENAME).write_text(
        "---\n".join((source / compute.FILENAME).read_text() for source in (DEV, FIXTURES))
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    compose(parser.parse_args().output)
