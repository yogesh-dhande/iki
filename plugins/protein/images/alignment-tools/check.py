"""Checks an alignment-tools image: MAFFT aligns three short proteins, one missing three
residues the others have, and puts the gap where those residues are.

    docker run --rm --network none -v "$PWD/plugins/protein/images/alignment-tools:/check:ro" \
        IMAGE python -I /check/check.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

GIVEN = {
    "a": "MKTAYIAKQRQISFVKSHFSRQ",
    "b": "MKTAYIAKQRFVKSHFSRQ",  # without QIS
    "c": "MKTAYLAKQRQISFVKSHFSRQ",
}
EXPECTED = {
    "a": "MKTAYIAKQRQISFVKSHFSRQ",
    "b": "MKTAYIAKQR---FVKSHFSRQ",
    "c": "MKTAYLAKQRQISFVKSHFSRQ",
}


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        given = Path(tmp, "given.fasta")
        given.write_text("".join(f">{k}\n{v}\n" for k, v in GIVEN.items()), encoding="ascii")
        done = subprocess.run(  # noqa: S603 - MAFFT from the image, on a file made here
            ["mafft", "--auto", "--quiet", "--amino", str(given)],  # noqa: S607 - the image's
            capture_output=True,
            text=True,
            check=False,
        )
    if done.returncode != 0:
        sys.exit(f"mafft failed ({done.returncode}):\n{done.stderr[-2000:]}")
    aligned: dict[str, str] = {}
    name = ""
    for line in done.stdout.splitlines():
        if line.startswith(">"):
            name = line[1:].strip()
            aligned[name] = ""
        elif name:
            aligned[name] += line.strip().upper()
    if aligned != EXPECTED:
        sys.exit(f"expected {EXPECTED}, MAFFT aligned {aligned}")
    version = subprocess.run(
        ["mafft", "--version"],  # noqa: S607 - the image's
        capture_output=True,
        text=True,
        check=True,
    ).stderr.strip()
    print(f"MAFFT {version}: the gap is where the missing residues are")


if __name__ == "__main__":
    main()
