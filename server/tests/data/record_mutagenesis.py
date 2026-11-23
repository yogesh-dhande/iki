"""Records what the DNA plugin's `apply_mutagenesis` writes for real designs, for the
component test that draws them (server/tests/test_dna_components.py), which has to be
given whole designs rather than plausible-looking ones. Run it from the repository root:

    uv run python server/tests/data/record_mutagenesis.py \
        > server/tests/data/mutagenesis.json

Two designs are recorded:

- **pUC19** (GenBank L09137.2, 2,686 bp, circular) with three changes at once: the C of
  its EcoRI site changed to a T, six bases taken out of its ampicillin resistance gene,
  and a BamHI site put into its origin of replication. Three changes in one plasmid is
  what the strip the component draws exists to show, and each gets its own QuikChange
  pair, so the primer table has six oligos in it.
- **Human beta-globin** (HBB, NCBI RefSeq NM_000518.5) with the sickle cell change,
  g.20A>T: the design says where the coding region is, so the run reports what the change
  does to the protein (Ingram, Nature 180:326, 1957, read HGVS's way from the initiator
  methionine: p.Glu7Val).

The designs' own numbers -- which changes were made, where HGVS puts each, what they do to
the protein and the primers designed -- are checked in server/tests/test_dna_mutations.py,
server/tests/test_dna_primers.py and the action's own tests
(plugins/dna/tests/apply_mutagenesis.yaml); this file only keeps them where a browser test
can be handed them.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "plugins" / "dna" / "scripts" / "mutate.py"

#: pUC19, GenBank L09137.2, as the repository already keeps it for the sequencing tests.
PUC19 = "".join(
    line.strip()
    for line in (Path(__file__).resolve().parent / "L09137.2.fasta").read_text().splitlines()
    if not line.startswith(">")
)

#: Human beta-globin's coding sequence (HBB, NM_000518.5).
HBB = (
    "ATGGTGCATCTGACTCCTGAGGAGAAGTCTGCCGTTACTGCCCTGTGGGGCAAGGTGAACGTGGATGAAGTTGGTGGTGAG"
    "GCCCTGGGCAGGCTGCTGGTGGTCTACCCTTGGACCCAGAGGTTCTTTGAGTCCTTTGGGGATCTGTCCACTCCTGATGCT"
    "GTTATGGGCAACCCTAAGGTGAAGGCTCATGGCAAGAAAGTGCTCGGTGCCTTTAGTGATGGCCTGGCTCACCTGGACAAC"
    "CTCAAGGGCACCTTTGCCACACTGAGTGAGCTGCACTGTGACAAGCTGCACGTGGATCCTGAGAACTTCAGGCTCCTGGGC"
    "AACGTGCTGGTCTGTGTGCTGGCCCATCACTTTGGCAAAGAATTCACCCCACCAGTGCAGGCTGCCTATCAGAAAGTGGTG"
    "GCTGGTGTGGCTAATGCCCTGGCCCACAAGTATCACTAA"
)


def given(values: dict[str, Any], template: dict[str, Any]) -> dict[str, Any]:
    return {
        "params": {},
        "inputs": {
            "mutagenesis": [
                {
                    "id": "00000000-0000-0000-0000-000000000001",
                    "hrid": "MUT-000001",
                    "values": values,
                    "included": {
                        "template": [
                            {
                                "id": "00000000-0000-0000-0000-000000000002",
                                "hrid": "DNA-000002",
                                "values": template,
                            }
                        ]
                    },
                }
            ]
        },
    }


def run(payload: dict[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        (work / "input.json").write_text(json.dumps(payload), encoding="utf-8")
        subprocess.run(  # noqa: S603 - the plugin's own script
            [sys.executable, str(SCRIPT)],
            env={
                **os.environ,
                "IKI_INPUT": str(work / "input.json"),
                "IKI_OUTPUT": str(work / "output.json"),
            },
            check=True,
            stdout=sys.stderr,
        )
        written = json.loads((work / "output.json").read_text(encoding="utf-8"))
    found: dict[str, Any] = written["updates"]["mutagenesis"][0]["values"]
    return found


PLASMID = given(
    {
        "name": "pUC19 recut",
        "changes": ["o.401C>T", "o.1000_1005del", "o.1500_1501insGGATCC"],
        "primer_design": "overlapping",
    },
    {
        "name": "pUC19",
        "bases": PUC19,
        "topology": "circular",
        "strandedness": "double",
        "accession": "L09137.2",
    },
)
GENE = given(
    {
        "name": "HBB sickle",
        "changes": ["g.20A>T"],
        "coding_start": 1,
        "coding_end": 444,
        "primer_design": "overlapping",
    },
    {
        "name": "HBB",
        "bases": HBB,
        "topology": "linear",
        "strandedness": "double",
        "accession": "NM_000518.5",
    },
)


def main() -> None:
    recorded = {
        "recorded_by": "server/tests/data/record_mutagenesis.py",
        "plasmid": {
            "template": "pUC19 (L09137.2), 2,686 bp, circular",
            "values": {"name": "pUC19 recut", **run(PLASMID)},
        },
        "gene": {
            "template": "HBB (NM_000518.5), 444 bp, linear",
            "values": {"name": "HBB sickle", **run(GENE)},
        },
    }
    json.dump(recorded, sys.stdout, indent=2, sort_keys=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
