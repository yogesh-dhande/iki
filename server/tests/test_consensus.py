"""An alignment's consensus, registered as a sequence: the dna plugin's for alignments of DNA,
the protein plugin's for proteins, one script that both ship.

The rule is Geneious's (its manual, "Consensus sequences"): a column's residue is the
fewest commonest residues that reach the threshold's share of its rows, as their
ambiguity code; a row's ambiguity code is partial support for each residue it stands for;
residues tied at the edge are taken together or not at all. Its worked example: a column
of 6 A, 3 G and 1 T is A at a threshold up to 60%, R above it to 90%, and D above that.

The end-to-end test aligns reads cut from pUC19 (GenBank L09137.2), some read on the
other strand, one across its origin and one with a sequencing error, with the dna
plugin's align_reads, and checks that their consensus is pUC19."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from iki_spec import schemas

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "plugins" / "dna" / "scripts" / "consensus.py"
PROTEIN_SCRIPT = ROOT / "plugins" / "protein" / "scripts" / "consensus.py"
READS = ROOT / "plugins" / "dna" / "scripts" / "reads.py"
ALIGN_PROTEINS = ROOT / "plugins" / "protein" / "scripts" / "align.py"
#: pUC19 as GenBank L09137.2 has it (NCBI nuccore, FASTA): 2,686 bases, circular.
PUC19 = "".join((Path(__file__).parent / "data" / "L09137.2.fasta").read_text().splitlines()[1:])
COMPLEMENT = str.maketrans("ACGT", "TGCA")
ALIGNMENT = {
    "id": "00000000-0000-0000-0000-000000000099",
    "hrid": "ALN-000001",
    "type": "iki/core.Alignment",
}


def reverse_complement(bases: str) -> str:
    return bases.translate(COMPLEMENT)[::-1]


def run_script(
    tmp_path: Path, script: Path, run_input: dict[str, Any], *args: str
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
    (tmp_path / "input.json").write_text(json.dumps(run_input))
    output = tmp_path / "output.json"
    output.unlink(missing_ok=True)
    out_dir = tmp_path / "out"
    out_dir.mkdir(exist_ok=True)
    result = subprocess.run(  # noqa: S603 - our own script
        [sys.executable, "-I", str(script), *args],
        env={
            "IKI_INPUT": str(tmp_path / "input.json"),
            "IKI_OUTPUT": str(output),
            "IKI_OUT_DIR": str(out_dir),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if not output.exists():
        return result, None
    found: dict[str, Any] = json.loads(output.read_text())
    assert not list(schemas.check("run-output", found))
    return result, found


def consensus(
    tmp_path: Path,
    fasta: str,
    *,
    alphabet: str = "nucleotide",
    reference: str | None = None,
    name: str = "Clone 4 reads",
    **params: Any,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
    path = tmp_path / "aligned.fasta"
    path.write_text(fasta)
    alignment: dict[str, Any] = {
        **ALIGNMENT,
        "schema_version": 1,
        "revision": 1,
        "values": {"name": name, "aligned": "f1"},
    }
    if reference:
        alignment["included"] = {
            "reference": [
                {
                    "id": "00000000-0000-0000-0000-000000000001",
                    "hrid": reference,
                    "type": "iki/dna.DNASequence",
                    "schema_version": 3,
                    "revision": 1,
                    "values": {},
                }
            ]
        }
    return run_script(
        tmp_path,
        SCRIPT if alphabet == "nucleotide" else PROTEIN_SCRIPT,
        {
            "run": {"hrid": "RUN-000003"},
            "inputs": {"alignment": [alignment]},
            "params": params,
            "files": {"f1": {"name": "alignment.fasta", "path": str(path)}},
        },
        alphabet,
    )


def column(*rows: str) -> str:
    """An alignment of one-letter rows, a column of them."""
    return "".join(f">r{i}\n{r}\n" for i, r in enumerate(rows, start=1))


def made(output: dict[str, Any] | None) -> dict[str, Any]:
    assert output is not None
    (created,) = output["creates"]
    values: dict[str, Any] = created["values"]
    return values


def test_both_plugins_ship_the_same_script() -> None:
    assert SCRIPT.read_bytes() == PROTEIN_SCRIPT.read_bytes()


@pytest.mark.parametrize(
    ("threshold", "called"),
    [(50, "A"), (60, "A"), (61, "R"), (90, "R"), (91, "D"), (100, "D")],
)
def test_the_threshold_calls_geneiouss_worked_example(
    tmp_path: Path, threshold: float, called: str
) -> None:
    """6 A, 3 G and 1 T: A up to 60%, R (A or G) to 90%, D (A, G or T) above."""
    _, output = consensus(tmp_path, column(*"AAAAAAGGGT"), threshold=threshold)

    assert made(output)["bases"] == called


def test_residues_tied_at_the_edge_are_taken_together(tmp_path: Path) -> None:
    # Two As and two Gs: A alone would reach 50%, but G has as many.
    _, output = consensus(tmp_path, column(*"AAGG"))
    assert made(output)["bases"] == "R"
    # T has half; A, C and G, tied, come together to reach 90%: N.
    _, output = consensus(tmp_path, column(*"ACGTTT"), threshold=90)
    assert made(output)["bases"] == "N"
    # Three tied: A, C and G together, V.
    _, output = consensus(tmp_path, column(*"AACCGG"), threshold=60)
    assert made(output)["bases"] == "V"


def test_a_rows_ambiguity_code_is_partial_support_for_each_base(tmp_path: Path) -> None:
    # A, A and R: A has 2.5 of 3 rows, 83%; R's G has half a row.
    _, output = consensus(tmp_path, column("A", "A", "R"), threshold=80)
    assert made(output)["bases"] == "A"
    _, output = consensus(tmp_path, column("A", "A", "R"), threshold=90)
    assert made(output)["bases"] == "R"
    # N says nothing of which base: a column of N is N, and N beside a base defers to it.
    _, output = consensus(tmp_path, column("N", "N"))
    assert made(output)["bases"] == "N"
    _, output = consensus(tmp_path, column("N", "C"))
    assert made(output)["bases"] == "C"


def test_without_ambiguity_codes_a_column_no_base_reaches_is_n(tmp_path: Path) -> None:
    fasta = ">r1\nACGT\n>r2\nACGA\n>r3\nACGA\n>r4\nACCT\n"

    _, output = consensus(tmp_path, fasta, ambiguity=False)

    # Column 3 is 3 G to 1 C (75%); column 4, 2 T to 2 A.
    assert made(output)["bases"] == "ACGN"
    assert output is not None and output["metrics"]["ambiguous"] == 1


def test_gaps_and_unobserved_places(tmp_path: Path) -> None:
    """A column more rows have a gap in than a base is left out; `.`, Iki's no
    observation, counts as neither; places no row observes are N inside the consensus, and
    left out at a linear one's ends."""
    fasta = ">r1\n..ACG-T...AC..\n>r2\n..ACGAT...ACG.\n>r3\n..AC--T...A-G.\n"

    result, output = consensus(tmp_path, fasta)

    # Column 6: two gaps to one A, left out. Column 7 is T. Columns 8-10: nobody, N.
    # Column 12: two Cs to one gap. Column 13: two Gs, r1 unobserved.
    values = made(output)
    assert values["bases"] == "ACGTNNNACG"
    assert values["topology"] == "linear" and values["strandedness"] == "double"
    assert output is not None
    assert output["metrics"] == {
        "rows": 3,
        "length": 10,
        "ambiguous": 0,
        "unobserved": 3,
        "columns_left_out": 1,
    }
    assert output["messages"] == [
        {"level": "warning", "text": "No row observes 3 of the consensus's places, so they're N."}
    ]
    assert (
        "1 column mostly gaps left out. 3 places no row observes written N."
        in (values["description"])
    )
    assert result.returncode == 0
    # As many gaps as bases keep the base: a gap seen by half the rows isn't confirmed.
    _, output = consensus(tmp_path, column("A", "-"))
    assert made(output)["bases"] == "A"

    _, output = consensus(tmp_path, fasta, topology="circular")
    assert made(output)["bases"] == "NNACGTNNNACGN"


def test_the_reference_can_be_left_out(tmp_path: Path) -> None:
    fasta = ">DNA-000001/11-14 pUC19\nACGT\n>read1 forward\nACCT\n>read2 forward\nACCT\n"

    _, output = consensus(tmp_path, fasta, reference="DNA-000001", include_reference=False)

    values = made(output)
    assert values["bases"] == "ACCT"
    assert values["name"] == "Consensus of Clone 4 reads"
    assert values["description"].startswith(
        "The consensus of ALN-000001's 2 rows (its reference, DNA-000001, left out), each "
        "column's residue that at least 50% of its rows have, or the ambiguity code of those "
        "that together do."
    )
    assert output is not None
    assert output["creates"][0]["derived_from"] == [ALIGNMENT["id"]]
    assert output["messages"][0]["text"] == (
        "DNA-000001, the reference, is left out of the consensus."
    )

    _, output = consensus(tmp_path, fasta, reference="DNA-000001", name="x", include_reference=True)
    # C has 2 of 3 rows, 67%.
    assert made(output)["bases"] == "ACCT"
    _, output = consensus(tmp_path, fasta, reference="DNA-000001", threshold=70)
    assert made(output)["bases"] == "ACST"


def test_rna_gives_dna_and_lower_case_is_read(tmp_path: Path) -> None:
    _, output = consensus(tmp_path, ">a\nacgu\n>b\nACGU\n", name="tRNA", strandedness="single")

    values = made(output)
    assert values["bases"] == "ACGT"
    assert values["strandedness"] == "single"


@pytest.mark.parametrize(
    ("fasta", "params", "said"),
    [
        (
            ">a\nACGT\n>b\nACG\n",
            {},
            "ALN-000001's rows aren't all the same length, so it isn't an alignment.",
        ),
        (
            ">a\nMKV\n>b\nMKE\n",
            {},
            "ALN-000001 isn't an alignment of nucleotides: b has 'E' at column 3, not an IUPAC "
            "base.",
        ),
        (">a\n---\n>b\n---\n", {}, "ALN-000001 has no column a consensus has a residue in."),
        ("", {}, "ALN-000001's file has no rows."),
        (
            column("A" * 1_000_001, "A" * 1_000_001),
            {},
            "ALN-000001 is too long to read here: 1,000,001 columns is more than 1,000,000.",
        ),
        (
            "".join(f">r{i}\n{'A' * 1_000_000}\n" for i in range(101)),
            {},
            "ALN-000001 is too big to read here: 101 rows of 1,000,000 columns is more than "
            "100,000,000 places.",
        ),
    ],
)
def test_what_has_no_consensus_fails_saying_why(
    tmp_path: Path, fasta: str, params: dict[str, Any], said: str
) -> None:
    result, output = consensus(tmp_path, fasta, **params)

    assert output is None
    assert result.returncode == 1
    assert result.stdout.strip() == f"::error {said}"


def test_only_the_reference_is_no_consensus(tmp_path: Path) -> None:
    result, output = consensus(
        tmp_path, ">DNA-000001\nACGT\n", reference="DNA-000001", include_reference=False
    )

    assert output is None
    assert result.stdout.strip() == "::error ALN-000001 has no rows but its reference, DNA-000001."


# --- Reads of pUC19 give pUC19 --------------------------------------------------------------------


def test_the_consensus_of_reads_of_puc19_is_puc19(tmp_path: Path) -> None:
    """Eleven reads of 600 bases, every 250 bases round pUC19, so each place is read two or
    three times: every other one read from the other strand, the last across the origin,
    and one with a wrong base, which the others outvote."""
    assert len(PUC19) == 2686
    reads = []
    for i, start in enumerate(range(0, len(PUC19), 250)):
        cut = (PUC19 + PUC19)[start : start + 600]
        if i == 3:
            # A wrong base at pUC19's 1,051, which two other reads have right.
            cut = cut[:300] + ("A" if cut[300] != "A" else "C") + cut[301:]
        reads.append((f"read{i + 1:02d}", reverse_complement(cut) if i % 2 else cut))
    path = tmp_path / "reads.fasta"
    path.write_text("".join(f">{n}\n{b}\n" for n, b in reads))
    aligned_dir = tmp_path / "aligned"
    aligned_dir.mkdir()
    _, aligned = run_script(
        aligned_dir,
        READS,
        {
            "run": {"hrid": "RUN-000001"},
            "inputs": {
                "reference": [
                    {
                        "id": "00000000-0000-0000-0000-000000000001",
                        "hrid": "DNA-000001",
                        "values": {"name": "pUC19", "bases": PUC19, "topology": "circular"},
                    }
                ]
            },
            "params": {"reads": ["f1"], "trim": True},
            "files": {"f1": {"name": "reads.fasta", "path": str(path)}},
        },
    )
    assert aligned is not None
    assert aligned["metrics"]["aligned"] == 11
    fasta = (aligned_dir / "out" / "alignment.fasta").read_text()

    _, output = consensus(
        tmp_path,
        fasta,
        reference="DNA-000001",
        include_reference=False,
        topology="circular",
    )

    values = made(output)
    assert values["bases"] == PUC19
    assert values["topology"] == "circular"
    assert output is not None
    assert output["metrics"]["ambiguous"] == 0
    assert output["metrics"]["rows"] == 11


# --- Proteins ------------------------------------------------------------------------------------


def test_a_proteins_consensus_writes_b_z_and_j_or_x(tmp_path: Path) -> None:
    fasta = ">PRT-000001\nMDEIKW\n>PRT-000002\nMNQLRW\n"

    _, output = consensus(tmp_path, fasta, alphabet="protein", name="Globins")

    values = made(output)
    # D/N is B, E/Q is Z, I/L is J; K/R has no code of its own: X.
    assert values == {
        "name": "Consensus of Globins",
        "description": values["description"],
        "sequence": "MBZJXW",
    }
    assert output is not None
    assert output["creates"][0]["type"] == "iki/protein.Protein"
    assert output["metrics"]["ambiguous"] == 4

    _, output = consensus(tmp_path, fasta, alphabet="protein", ambiguity=False)
    assert made(output)["sequence"] == "MXXXXW"


def test_haemoglobins_aligned_by_the_protein_plugin_give_their_consensus(tmp_path: Path) -> None:
    """Human haemoglobin's alpha and beta chains (UniProt P69905, P68871), aligned as
    needle aligns them by the protein plugin: where they agree, their consensus is their
    residue; where one has a gap the other's residue is kept, gaps not outnumbering it."""
    hba = (
        "MVLSPADKTNVKAAWGKVGAHAGEYGAEALERMFLSFPTTKTYFPHFDLSHGSAQVKGHGKKVADALTNAVAHVDDMPNAL"
        "SALSDLHAHKLRVDPVNFKLLSHCLLVTLAAHLPAEFTPAVHASLDKFLASVSTVLTSKYR"
    )
    hbb = (
        "MVHLTPEEKSAVTALWGKVNVDEVGGEALGRLLVVYPWTQRFFESFGDLSTPDAVMGNPKVKAHGKKVLGAFSDGLAHLD"
        "NLKGTFATLSELHCDKLHVDPENFRLLGNVLVCVLAHHFGKEFTPPVQAAYQKVVAGVANALAHKYH"
    )
    pair_dir = tmp_path / "pair"
    pair_dir.mkdir()
    _, aligned = run_script(
        pair_dir,
        ALIGN_PROTEINS,
        {
            "run": {"hrid": "RUN-000002"},
            "params": {"mode": "global", "matrix": "BLOSUM62", "gap_open": 10, "gap_extend": 0.5},
            "inputs": {
                "query": [{"id": "a", "hrid": "PRT-000001", "values": {"sequence": hba}}],
                "target": [{"id": "b", "hrid": "PRT-000002", "values": {"sequence": hbb}}],
            },
        },
    )
    assert aligned is not None and aligned["metrics"]["length"] == 149
    fasta = (pair_dir / "out" / "alignment.fasta").read_text()
    rows = ["".join(r.splitlines()[1:]) for r in fasta.split(">")[1:]]

    _, output = consensus(tmp_path, fasta, alphabet="protein", name="HBA and HBB")

    sequence = made(output)["sequence"]
    assert len(sequence) == 149  # a gap is one row of two: never more than the residue
    pairs = {"DN": "B", "EQ": "Z", "IL": "J"}
    for got, a, b in zip(sequence, *rows, strict=True):
        if "-" in (a, b):
            assert got == (b if a == "-" else a)
        elif a == b:
            assert got == a
        else:
            assert got == pairs.get("".join(sorted(a + b)), "X")
    # 65 of the 149 columns are identical, as needle counts them.
    assert sum(1 for a, b in zip(*rows, strict=True) if a == b) == 65
