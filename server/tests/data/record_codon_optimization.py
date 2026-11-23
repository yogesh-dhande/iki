"""Records what the DNA plugin's `optimize_codons` writes for a real gene, for the
component test that draws it (server/tests/test_dna_components.py), which has to be given
a whole design rather than a plausible-looking one. Run it from the repository root:

    uv run python server/tests/data/record_codon_optimization.py \
        > server/tests/data/codon_optimization.json

The gene is human beta-globin (HBB), NCBI RefSeq NM_000518.5's coding sequence, 147 amino
acids and a stop, rewritten in the codons of _Escherichia coli_ K-12 MG1655 without the
EcoRI or BamHI site: a gene expressed in E. coli for most of the time recombinant protein
has been made there, and long enough that the strip the component draws has something to
show. The design's own numbers -- the codon adaptation index and GC content before and
after, and which codons changed -- are checked in server/tests/test_dna_codon_optimization.py
and in the action's own tests (plugins/dna/tests/optimize_codons.yaml); this file only
keeps them where a browser test can be handed them.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "plugins" / "dna" / "scripts" / "optimize.py"

#: Human beta-globin's coding sequence (HBB, NM_000518.5).
HBB = (
    "ATGGTGCATCTGACTCCTGAGGAGAAGTCTGCCGTTACTGCCCTGTGGGGCAAGGTGAACGTGGATGAAGTTGGTGGTGAG"
    "GCCCTGGGCAGGCTGCTGGTGGTCTACCCTTGGACCCAGAGGTTCTTTGAGTCCTTTGGGGATCTGTCCACTCCTGATGCT"
    "GTTATGGGCAACCCTAAGGTGAAGGCTCATGGCAAGAAAGTGCTCGGTGCCTTTAGTGATGGCCTGGCTCACCTGGACAAC"
    "CTCAAGGGCACCTTTGCCACACTGAGTGAGCTGCACTGTGACAAGCTGCACGTGGATCCTGAGAACTTCAGGCTCCTGGGC"
    "AACGTGCTGGTCTGTGTGCTGGCCCATCACTTTGGCAAAGAATTCACCCCACCAGTGCAGGCTGCCTATCAGAAAGTGGTG"
    "GCTGGTGTGGCTAATGCCCTGGCCCACAAGTATCACTAA"
)

GIVEN = {
    "params": {
        "rare_codon_threshold": 0.1,
        "gc_min": 30.0,
        "gc_max": 70.0,
        "gc_window": 50,
        "gc_window_min": 25.0,
        "gc_window_max": 75.0,
        "longest_homopolymer": 6,
        "longest_repeat": 15,
        "longest_hairpin_stem": 10,
        "hairpin_loop": 30,
        "avoid_host_motifs": True,
    },
    "inputs": {
        "optimization": [
            {
                "id": "00000000-0000-0000-0000-000000000001",
                "hrid": "OPT-000001",
                "values": {
                    "name": "HBB for E. coli",
                    "host": "e_coli_k12",
                    "strategy": "highest_usage",
                    "genetic_code": "standard",
                    "avoid_enzymes": ["EcoRI", "BamHI"],
                },
                "included": {
                    "template": [
                        {
                            "id": "00000000-0000-0000-0000-000000000002",
                            "hrid": "DNA-000002",
                            "values": {
                                "name": "HBB",
                                "bases": HBB,
                                "topology": "linear",
                                "strandedness": "double",
                                "accession": "NM_000518.5",
                            },
                        }
                    ]
                },
            }
        ]
    },
}


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        (work / "input.json").write_text(json.dumps(GIVEN), encoding="utf-8")
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
    values = written["updates"]["optimization"][0]["values"]
    recorded = {
        "gene": "HBB (NM_000518.5)",
        "host": "Escherichia coli K-12 substr. MG1655 (GCF_000005845.2)",
        "recorded_by": "server/tests/data/record_codon_optimization.py",
        "values": {"name": "HBB for E. coli", "host": "e_coli_k12", **values},
    }
    json.dump(recorded, sys.stdout, indent=2, sort_keys=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
