"""Records the melting temperatures every Iki module computes for 21,360 oligos, for
server/tests/test_melting_temperatures.py and web/packages/science/src/oligo-temperature.test.ts,
which hold each module to them. Run it from the repository root, with the modules built
(`scripts/build-wasm.sh`, `scripts/build-oligo-checks-wasm.sh` and
`scripts/build-plasmid-tools-wasm.sh`):

    uv run python server/tests/data/record_melting_temperatures.py \
        > server/tests/data/melting_temperatures.json

The oligos are 20,000 drawn from SHA-256 -- oligo `i` is the digest of `iki-oligo-<i>`,
its first byte setting a length of 6 to 60 and the rest its bases, two bits each from the
lowest -- and then every palindrome of 4 to 10 bases, which is where conventions part.
Both tests make them the same way and check `bases_sha256` before reading a value, so a
change to how they are made can't pass for a change in what is measured.

Four columns, each from the module that computes it:

- **sodium**: the strand against its exact complement at 50 mM Na+, 25 nM of each strand
  (a self-complementary strand one species at 50 nM, with the symmetry term). The DNA
  sequence record's measurement (`iki_wasm`'s `iki_melting_temperature`, the DNA plugin's
  `oligo-measurement`, the browser's translation of it), the primer checks under
  `Conditions::PRIMER` (`oligo-checks`), and a primer alone in the plasmid plugin
  (`plasmid-tools`) all give it, from one implementation (ADR 0191).
- **buffer**: the primer checks' buffer melting temperature, in the checks' default PCR
  buffer with Owczarzy's magnesium correction (ADR 0201).
- **on_template**: a binding site, the primer annealed to a template strand it matches
  exactly. Two molecules, so the same as **sodium** except for a palindrome, which is
  only self-complementary against its own complement; those are listed by index.
- **mismatched**: the same site where the template's middle base doesn't pair with the
  primer's.

The values were recorded from main at b9e31fdb, where the sodium temperature had two
implementations, `oligo.rs` and `thermodynamics.rs`. They agreed on every oligo here, and
the one implementation since gives every column unchanged. What did change is the plasmid
plugin's reading of a palindromic primer alone: it took the two-strand reading, 2.4
degrees low for GGATCC, and now gives **sodium** like everything else. The values are
checked against SantaLucia 1998 and Primer3 by the modules' own tests; this file only
says they still give what they gave.
"""

import hashlib
import importlib.util
import itertools
import json
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
DNA = ROOT / "plugins" / "dna" / "scripts"
PLASMID = ROOT / "plugins" / "plasmid" / "scripts"

#: How many oligos are drawn at random, before the palindromes.
DRAWN = 20_000
#: The halves the palindromes are built from: 2 to 5 bases, so 4 to 10 in all.
HALVES = range(2, 6)
#: A batch the checks module takes in one call: well under its request limit.
BATCH = 500
#: The primer checks' own name for the 50 mM Na+ conditions, `Conditions::PRIMER`.
PRIMER = {
    "sodium": 50.0,
    "potassium": 0.0,
    "tris": 0.0,
    "magnesium": 0.0,
    "dntp": 0.0,
    "strand": 25.0,
    "other_strand": 25.0,
    "salt": "santalucia",
}
COMPLEMENT = str.maketrans("ACGT", "TGCA")
#: A base that pairs with none of A's, C's, G's or T's partners at once: the mismatch.
MISPAIRED = {"A": "C", "C": "A", "G": "T", "T": "G"}


def oligos() -> list[str]:
    """The oligos, in the order the columns hold them."""
    made = []
    for i in range(DRAWN):
        digest = hashlib.sha256(f"iki-oligo-{i}".encode()).digest()
        length = 6 + digest[0] % 55
        bits = int.from_bytes(digest[1:], "big")
        made.append("".join("ACGT"[(bits >> (2 * k)) & 3] for k in range(length)))
    for size in HALVES:
        for half in itertools.product("ACGT", repeat=size):
            left = "".join(half)
            made.append(left + reverse_complement(left))
    return made


def reverse_complement(bases: str) -> str:
    return bases.translate(COMPLEMENT)[::-1]


def mismatched(bases: str) -> str:
    """`bases` with its middle base swapped for one that doesn't pair where it did."""
    middle = len(bases) // 2
    return bases[:middle] + MISPAIRED[bases[middle]] + bases[middle + 1 :]


def digest(made: list[str]) -> str:
    return hashlib.sha256("\n".join(made).encode()).hexdigest()


def script(name: str, where: Path) -> ModuleType:
    """A plugin's script, imported by bare name as a run imports it."""
    if str(where) not in sys.path:
        sys.path.insert(0, str(where))
    spec = importlib.util.spec_from_file_location(name, where / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def checked(made: list[str], conditions: dict[str, Any] | None) -> list[float | None]:
    """The primer checks' melting temperature of each oligo, in `conditions` or the
    checks' default buffer."""
    checks = script("oligos", DNA)
    got: dict[int, float | None] = {}
    for start in range(0, len(made), BATCH):
        request: dict[str, Any] = {
            "oligos": [
                {"hrid": str(start + at), "bases": bases}
                for at, bases in enumerate(made[start : start + BATCH])
            ]
        }
        if conditions is not None:
            request["conditions"] = conditions
        for oligo in checks.call("iki_check_oligos", request)["oligos"]:
            got[int(oligo["oligo"])] = oligo.get("melting_temperature")
    return [got[at] for at in range(len(made))]


def sodium_measurements(made: list[str]) -> dict[str, list[float | None]]:
    """Every module's 50 mM Na+ temperature of each oligo against its exact complement."""
    from iki_spec import wasm

    measurement = script("sequences", DNA)
    priming = script("priming", PLASMID)
    each: dict[str, Callable[[str], float | None]] = {
        "iki_wasm": wasm.melting_temperature,
        "oligo-measurement": measurement.melting_temperature,
        "plasmid-tools": priming.melting_temperature,
    }
    found = {name: [measure(bases) for bases in made] for name, measure in each.items()}
    found["oligo-checks"] = checked(made, PRIMER)
    return found


def record() -> dict[str, Any]:
    made = oligos()
    sodium = sodium_measurements(made)
    first = next(iter(sodium.values()))
    disagreeing = [name for name, values in sodium.items() if values != first]
    assert not disagreeing, f"{disagreeing} disagree with {next(iter(sodium))}"
    priming = script("priming", PLASMID)
    on_template = [priming.melting_temperature(bases, bases) for bases in made]
    return {
        "recorded_by": "server/tests/data/record_melting_temperatures.py",
        "oligos": len(made),
        "bases_sha256": digest(made),
        "sodium": first,
        "buffer": checked(made, None),
        "on_template": {
            str(at): tm
            for at, (bases, tm) in enumerate(zip(made, on_template, strict=True))
            if bases == reverse_complement(bases)
        },
        "mismatched": [priming.melting_temperature(bases, mismatched(bases)) for bases in made],
    }


if __name__ == "__main__":
    json.dump(record(), sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")
