"""Write the PSI-MS terms that Iki's mzML reader needs (ADR 0224).

The mzML 1.1 mapping rules say an ``instrumentConfiguration`` gives one child term of
MS:1000031 (instrument model) and a ``software`` element one or more child terms of
MS:1000531 (software). Knowing which accessions those are takes the controlled
vocabulary, so this keeps, for each term under either root, its parents under that root:
nothing else of the vocabulary, and none of its text.

Run it with a release of ``psi-ms.obo``, then commit the JSON it writes::

    curl -fsSLo /tmp/psi-ms.obo \\
      https://raw.githubusercontent.com/HUPO-PSI/psi-ms-CV/v4.2.18/psi-ms.obo
    uv run scripts/build_psi_ms_terms.py /tmp/psi-ms.obo

The vocabulary is CC BY 4.0, by the HUPO Proteomics Standards Initiative (see NOTICE).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "server/src/iki_server/files/psi_ms_terms.json"
ROOTS = {"instrument_models": "MS:1000031", "software": "MS:1000531"}
_ACCESSION = re.compile(r"MS:\d{7}", re.ASCII)


def _terms(obo: str) -> tuple[str, dict[str, list[str]]]:
    """The release's data-version, and each current term's is_a parents."""
    version = ""
    parents: dict[str, list[str]] = {}
    term: str | None = None
    obsolete: set[str] = set()
    for line in obo.splitlines():
        if line.startswith("data-version: ") and not version:
            version = line.removeprefix("data-version: ").strip()
        if line.startswith("["):
            term = None
            if line == "[Term]":
                term = ""
            continue
        if term is None:
            continue
        if line.startswith("id: "):
            term = line.removeprefix("id: ").strip()
            parents.setdefault(term, [])
        elif line.startswith("is_a: ") and term:
            parents[term].append(line.removeprefix("is_a: ").split(" ", 1)[0])
        elif line.strip() == "is_obsolete: true" and term:
            obsolete.add(term)
    if not version:
        raise SystemExit("That isn't a psi-ms.obo release: it has no data-version.")
    return version, {t: p for t, p in parents.items() if t not in obsolete}


def _under(root: str, parents: dict[str, list[str]]) -> dict[str, list[str]]:
    children: dict[str, list[str]] = {}
    for term, its in parents.items():
        for parent in its:
            children.setdefault(parent, []).append(term)
    found: set[str] = set()
    stack = [root]
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in found:
                found.add(child)
                stack.append(child)
    return {
        term: sorted(p for p in parents[term] if p in found)
        for term in sorted(found)
        if _ACCESSION.fullmatch(term)
    }


def _dumps(data: dict[str, object]) -> str:
    """JSON with a term to a line, so a new release's diff shows the terms it changed."""
    lines = []
    for key, value in data.items():
        if isinstance(value, dict):
            terms = [f"    {json.dumps(t)}: {json.dumps(p)}" for t, p in value.items()]
            lines.append(f"  {json.dumps(key)}: {{\n" + ",\n".join(terms) + "\n  }")
        else:
            lines.append(f"  {json.dumps(key)}: {json.dumps(value)}")
    return "{\n" + ",\n".join(lines) + "\n}\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("obo", type=Path, help="A release of psi-ms.obo")
    args = parser.parse_args()
    version, parents = _terms(args.obo.read_text(encoding="utf-8"))
    data: dict[str, object] = {
        "vocabulary": "PSI-MS",
        "version": version,
        "source": f"https://github.com/HUPO-PSI/psi-ms-CV/blob/v{version}/psi-ms.obo",
        "licence": "CC BY 4.0, the HUPO Proteomics Standards Initiative",
        **{key: _under(root, parents) for key, root in ROOTS.items()},
    }
    OUT.write_text(_dumps(data), encoding="utf-8")
    sys.stdout.write(f"Wrote {OUT.relative_to(ROOT)} from PSI-MS {version}.\n")


if __name__ == "__main__":
    main()
