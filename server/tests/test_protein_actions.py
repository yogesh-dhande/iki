"""The protein plugin's actions: what its calculations, translations, imports and UniProt
fetches give, run as a worker runs them (an input.json in, an output.json out)."""

import hashlib
import io
import json
import subprocess
import sys
import urllib.error
import urllib.parse
from collections.abc import Iterator
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from iki_spec import schemas

SCRIPTS = Path(__file__).resolve().parents[2] / "plugins" / "protein" / "scripts"
ORGANISM = Path(__file__).resolve().parents[2] / "spec/src/iki_spec/core/types/organism.yaml"
#: Human haemoglobin's alpha chain (UniProt P69905).
#: Human ubiquitin (UniProt P0CG48's first 76 residues).
UBIQUITIN = "MQIFVKTLTGKTITLEVEPSDTIENVKAKIQDKEGIPPDQQRLIFAGKQLEDGRTLSDYNIQKESTLHLVLRLRGG"
HBA = (
    "MVLSPADKTNVKAAWGKVGAHAGEYGAEALERMFLSFPTTKTYFPHFDLSHGSAQVKGHGKKVADALTNAVAHVDDMPNALSALSDLHAH"
    "KLRVDPVNFKLLSHCLLVTLAAHLPAEFTPAVHASLDKFLASVSTVLTSKYR"
)


def entity(i: int, **values: Any) -> dict[str, Any]:
    return {"id": f"00000000-0000-0000-0000-{i:012d}", "hrid": f"X-{i}", "values": values}


def script(
    tmp_path: Path, name: str, run_input: dict[str, Any], *args: str
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
    (tmp_path / "input.json").write_text(json.dumps(run_input))
    out = tmp_path / "out.json"
    out.unlink(missing_ok=True)
    out_dir = tmp_path / "out"
    out_dir.mkdir(exist_ok=True)
    result = subprocess.run(  # noqa: S603 - our own script
        [sys.executable, "-I", str(SCRIPTS / name), *args],
        env={
            "IKI_INPUT": str(tmp_path / "input.json"),
            "IKI_OUTPUT": str(out),
            "IKI_OUT_DIR": str(out_dir),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if not out.exists():
        return result, None
    output: dict[str, Any] = json.loads(out.read_text())
    assert not list(schemas.check("run-output", output))
    return result, output


# --- Alignment -----------------------------------------------------------------------------------

#: Human haemoglobin's beta chain (UniProt P68871).
HBB = (
    "MVHLTPEEKSAVTALWGKVNVDEVGGEALGRLLVVYPWTQRFFESFGDLSTPDAVMGNPKVKAHGKKVLGAFSDGLAHLDNLKGTF"
    "ATLSELHCDKLHVDPENFRLLGNVLVCVLAHHFGKEFTPPVQAAYQKVVAGVANALAHKYH"
)
NEEDLE = {"mode": "global", "matrix": "BLOSUM62", "gap_open": 10, "gap_extend": 0.5}


def aligned(
    tmp_path: Path, query: str, target: str, **params: Any
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
    return script(
        tmp_path,
        "align.py",
        {
            "run": {"hrid": "RUN-000007"},
            "params": {**NEEDLE, **params},
            "inputs": {
                "query": [entity(1, sequence=query, name="HBA_HUMAN")],
                "target": [entity(2, sequence=target, name="HBB_HUMAN")],
            },
        },
    )


def test_aligns_two_proteins_as_needle_and_keeps_the_alignment(tmp_path: Path) -> None:
    result, output = aligned(tmp_path, HBA, HBB)

    assert result.returncode == 0, result.stderr
    assert output is not None
    # Unrounded shares of the 149 columns: 65 identical, 90 similar, 9 gapped.
    assert output["metrics"] == {
        "score": 292.5,
        "length": 149,
        "identity_percent": pytest.approx(100 * 65 / 149),
        "similarity_percent": pytest.approx(100 * 90 / 149),
        "gaps_percent": pytest.approx(100 * 9 / 149),
    }
    text = (tmp_path / "out" / "alignment.txt").read_text()
    assert "# Matrix: BLOSUM62" in text and "# Identity:   65/149 (43.6%)" in text
    assert "X-1      1 MV-LSPADKTNVKAAWGKVGAHAGEYGAEALERMFLSFPTTKTYFPHF-D     48" in text
    assert text.rstrip().endswith(
        "X-2     99 VDPENFRLLGNVLVCVLAHHFGKEFTPPVQAAYQKVVAGVANALAHKYH    147"
    )
    fasta = (tmp_path / "out" / "alignment.fasta").read_text().split(">")[1:]
    rows = ["".join(record.splitlines()[1:]) for record in fasta]
    assert [r.replace("-", "") for r in rows] == [HBA, HBB]
    assert len(rows[0]) == len(rows[1]) == 149
    # The FASTA is an alignment of the two in Iki, each row headed by its HRID.
    assert [record.split("\n", 1)[0] for record in fasta] == ["X-1", "X-2"]
    (alignment,) = made(output)
    assert alignment == {
        "type": "iki/core.Alignment",
        "values": {
            "name": "HBA_HUMAN and HBB_HUMAN",
            "description": "43.6% identical and 60.4% similar over 149 columns, scoring 292.5.",
            "reference": entity(2)["id"],
            "members": [entity(1)["id"], entity(2)["id"]],
            "aligned": {"$file": "alignment.fasta", "media_type": "chemical/seq-aa-fasta"},
            "method": "Biopython PairwiseAligner, global as EMBOSS needle: BLOSUM62, gap opening "
            "10, extension 0.5",
        },
        "derived_from": [entity(1)["id"], entity(2)["id"]],
    }


def test_end_gaps_are_free_globally_and_local_finds_the_best_part(tmp_path: Path) -> None:
    part = HBA[30:80]
    _, whole = aligned(tmp_path, HBA, part)
    _, local = aligned(tmp_path, HBA, part, mode="local")

    assert whole is not None and local is not None
    assert whole["metrics"]["score"] == local["metrics"]["score"]
    assert whole["metrics"]["length"] == len(HBA)
    assert local["metrics"]["length"] == 50 and local["metrics"]["identity_percent"] == 100
    assert "X-1     31 " in (tmp_path / "out" / "alignment.txt").read_text()
    # The local rows are parts of their proteins, numbered as their own (Jalview's
    # HRID/start-end); the whole of one is headed by its HRID alone.
    fasta = (tmp_path / "out" / "alignment.fasta").read_text()
    assert [line for line in fasta.splitlines() if line.startswith(">")] == [">X-1/31-80", ">X-2"]


def test_penalties_and_matrices_change_the_alignment(tmp_path: Path) -> None:
    _, default = aligned(tmp_path, HBA, HBB)
    _, costly = aligned(tmp_path, HBA, HBB, gap_open=50)
    _, pam = aligned(tmp_path, HBA, HBB, matrix="PAM250")

    assert default is not None and costly is not None and pam is not None
    assert costly["metrics"]["score"] < default["metrics"]["score"]
    assert pam["metrics"]["score"] != default["metrics"]["score"]


def test_identity_is_kept_unrounded_so_a_threshold_compares_the_true_share(
    tmp_path: Path,
) -> None:
    # 899 of 999 columns identical is 89.98999%: under a 90% threshold, though it rounds
    # to 90.0. One W for an A in each of 100 places changes nothing else.
    query = "A" * 999
    target = "".join("W" if i % 10 == 5 else "A" for i in range(999))
    _, output = aligned(tmp_path, query, target, gap_open=100)
    assert output is not None
    assert target.count("W") == 100
    assert output["metrics"]["identity_percent"] == pytest.approx(100 * 899 / 999)
    assert output["metrics"]["identity_percent"] < 90
    assert "# Identity:   899/999 (90.0%)" in (tmp_path / "out" / "alignment.txt").read_text()


def test_similar_columns_are_those_the_matrix_scores_above_zero(tmp_path: Path) -> None:
    # BLOSUM62 scores X with X -1: identical, but not similar, as EMBOSS counts it.
    _, output = aligned(tmp_path, "WWXWW", "WWXWW")
    assert output is not None
    assert output["metrics"]["identity_percent"] == 100
    assert output["metrics"]["similarity_percent"] == 80


def test_selenocysteine_pyrrolysine_and_j_align_as_any_residue(tmp_path: Path) -> None:
    query = HBA[:40] + "U" + HBA[41:80] + "O" + HBA[81:120] + "J" + HBA[121:]
    _, output = aligned(tmp_path, query, query)
    assert output is not None
    assert output["metrics"]["identity_percent"] == 100
    assert output["metrics"]["similarity_percent"] == 100
    fasta = (tmp_path / "out" / "alignment.fasta").read_text().split(">")[1:]
    assert ["".join(record.splitlines()[1:]) for record in fasta] == [query, query]


def test_every_stop_at_the_end_is_left_out_as_the_identity_leaves_them(
    tmp_path: Path,
) -> None:
    """HBA and HBA** are one protein (ADR 0138): aligned, they're identical, with no
    gap for the second stop."""
    for stops in ("*", "**"):
        result, output = aligned(tmp_path, HBA, HBA + stops)
        assert result.returncode == 0, result.stderr
        assert output is not None
        metrics = output["metrics"]
        assert (metrics["length"], metrics["identity_percent"], metrics["gaps_percent"]) == (
            len(HBA),
            100.0,
            0.0,
        ), stops


def test_a_local_alignment_with_nothing_above_zero_is_none(tmp_path: Path) -> None:
    result, output = aligned(tmp_path, "AAAA", "WWWW", mode="local")
    assert result.returncode == 0, result.stderr
    assert output is not None
    assert made(output) == []
    assert output["metrics"]["identity_percent"] == 0
    assert [m["text"] for m in output["messages"]] == [
        "X-1 and X-2 have no local alignment: no part of one scores above zero against the other."
    ]


@pytest.mark.parametrize(
    ("query", "target", "why"),
    [
        ("*", HBB, "X-1 has no residues to align."),
        ("A" * 20_000, "A" * 20_000, "are too long to align here: 20,000 by 20,000 residues"),
    ],
)
def test_what_cant_be_aligned_fails_its_run_saying_why(
    tmp_path: Path, query: str, target: str, why: str
) -> None:
    result, output = aligned(tmp_path, query, target)

    assert result.returncode == 1 and output is None
    assert why in result.stdout


# --- Properties ----------------------------------------------------------------------------------


def properties(tmp_path: Path, *sequences: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    proteins = [entity(i, sequence=s) for i, s in enumerate(sequences)]
    result, output = script(
        tmp_path, "proteins.py", {"inputs": {"protein": proteins}}, "properties"
    )
    assert result.returncode == 0, result.stderr
    assert output is not None
    assert result.stdout.splitlines()[-1].startswith("::progress 1.00 ")
    found = {u["entity"][-1]: u["values"] for u in output["updates"]["protein"]}
    return found, output["messages"]


def test_properties_are_protparams(tmp_path: Path) -> None:
    found = properties(tmp_path, HBA)[0]["0"]
    profile = found.pop("hydropathy")
    # ExPASy's ProtParam gives 15257.55 Da: Iki sums Biopython's average residue masses
    # (IUPACData.protein_weights), 12 ppm lower than ExPASy's table. ExPASy also gives
    # pI 8.72, 9970 M⁻¹cm⁻¹ (one cysteine: no cystine), GRAVY 0.048, instability 6.97.
    assert found == {
        "length": 142,
        "molecular_weight": 15257.36,
        "isoelectric_point": 8.72,
        "extinction_coefficient_oxidized": 9970,
        "extinction_coefficient_reduced": 9970,
        "gravy": 0.048,
        "instability_index": 6.97,
    }
    # ProtScale's Kyte-Doolittle profile, window 9: none for the first and last four residues;
    # residue 5's window is MVLSPADKT, whose values sum to 1.2.
    assert len(profile) == 142
    assert profile[:4] == profile[-4:] == [None] * 4
    assert profile[4] == 0.13
    assert all(-4.5 <= v <= 4.5 for v in profile[4:-4])


def test_a_protein_shorter_than_the_window_has_no_hydropathy(tmp_path: Path) -> None:
    assert properties(tmp_path, "MKWV")[0]["0"]["hydropathy"] == [None] * 4


def test_cystines_add_to_the_oxidized_extinction_coefficient(tmp_path: Path) -> None:
    found, _ = properties(tmp_path, "MCWCYC")
    # Pace et al.: W 5500, Y 1490, and 125 for each cystine (two of the three cysteines).
    assert found["0"]["extinction_coefficient_reduced"] == 5500 + 1490
    assert found["0"]["extinction_coefficient_oxidized"] == 5500 + 1490 + 125


def test_a_final_stop_is_left_out_and_other_residues_leave_only_the_length(
    tmp_path: Path,
) -> None:
    found, messages = properties(tmp_path, "mkwv*", "MKXV", "MK*V", "*")
    assert found["0"] == properties(tmp_path, "MKWV")[0]["0"]
    # Its length is its amino acids: a stop is none (NCBI's tables; Biopython's to_stop).
    assert found["0"]["length"] == 4
    assert found["1"] == {"length": 4}
    assert found["2"] == {"length": 3}
    assert found["3"] == {"length": 0}
    everything = (
        "molecular weight, isoelectric point, extinction coefficients, GRAVY, hydropathy and "
        "instability index have"
    )
    assert [(m["level"], m["entity"][-1], m["text"]) for m in messages] == [
        (
            "warning",
            "1",
            f"Its {everything} no value for X at residue 3, so they weren't computed.",
        ),
        (
            "warning",
            "2",
            f"Its {everything} no value for a stop (*) at residue 3, so they weren't computed.",
        ),
        ("warning", "3", "It has no residues, so only its length was set."),
    ]


def test_a_final_stop_leaves_the_hydropathy_one_value_a_residue(tmp_path: Path) -> None:
    found, _ = properties(tmp_path, "AAAAAGGGG*")
    assert found["0"]["length"] == 9
    assert len(found["0"]["hydropathy"]) == 9


def test_the_isoelectric_point_is_where_bjellqvists_charge_is_zero_however_acidic(
    tmp_path: Path,
) -> None:
    found, _ = properties(tmp_path, "D" * 10, "R" * 10, UBIQUITIN)
    # Bisection of Biopython's IsoelectricPoint.charge_at_pH (Bjellqvist's pK values)
    # between pH 0 and 14 gives 3.0797 and 12.9542; its own pi() stops at 4.05 and 12.
    # Ubiquitin is 6.56 either way, as ExPASy's Compute pI/Mw gives.
    assert [found[k]["isoelectric_point"] for k in "012"] == [3.08, 12.95, 6.56]


def test_an_isoelectric_point_beyond_ph_14_isnt_saved_but_the_rest_are(tmp_path: Path) -> None:
    found, messages = properties(tmp_path, "R" * 120, "R" * 10)
    # Bisection of Biopython's IsoelectricPoint.charge_at_pH (Bjellqvist's pK values) puts
    # R120's charge at zero at pH 14.0756, past the scale its pK values were measured on.
    # The rest are ProtParam's: 120 x 156.1875 + 18.0153 = 18760.30 Da (Biopython's
    # average residue masses), GRAVY -4.5 (Kyte-Doolittle R), instability index
    # 10 / 120 x 119 x 58.28 = 577.94 (Guruprasad's R-R weight).
    assert "isoelectric_point" not in found["0"]
    assert {
        k: found["0"][k] for k in ("length", "molecular_weight", "gravy", "instability_index")
    } == {
        "length": 120,
        "molecular_weight": 18760.3,
        "gravy": -4.5,
        "instability_index": 577.94,
    }
    assert found["0"]["extinction_coefficient_reduced"] == 0
    assert [(m["level"], m["entity"][-1], m["text"]) for m in messages] == [
        (
            "warning",
            "0",
            "Its isoelectric point by Bjellqvist's model would be pH 14.08, above 14, where the "
            "model's pK values don't hold, so it wasn't saved.",
        )
    ]
    # R10's, 12.95, is within it.
    assert found["1"]["isoelectric_point"] == 12.95


def test_stops_at_the_end_are_all_left_out(tmp_path: Path) -> None:
    found, messages = properties(tmp_path, "M**", "M*", "M")
    # Methionine alone: 131.1926 + 18.0153 = 149.21 Da (Biopython's average masses), pI 5.28.
    assert found["0"] == found["1"] == found["2"]
    assert found["0"]["length"] == 1
    assert found["0"]["molecular_weight"] == 149.21
    assert found["0"]["isoelectric_point"] == 5.28
    assert messages == []


def test_selenocysteine_pyrrolysine_and_j_keep_the_properties_they_have_values_for(
    tmp_path: Path,
) -> None:
    found, messages = properties(tmp_path, "MU", "MO", "MJ")
    # Biopython's average residue masses (IUPACData.protein_weights) give MU 299.2492 and
    # MO 386.5094 Da; MI and ML both weigh 262.3689 Da, so MJ does.
    assert found["0"] == {"length": 2, "molecular_weight": 299.25}
    assert found["1"] == {"length": 2, "molecular_weight": 386.51}
    # Neither I nor L is charged or absorbs at 280 nm: MI and ML give pI 5.28 and none.
    assert found["2"] == {
        "length": 2,
        "molecular_weight": 262.37,
        "isoelectric_point": 5.28,
        "extinction_coefficient_oxidized": 0,
        "extinction_coefficient_reduced": 0,
    }
    assert [m["text"] for m in messages] == [
        "Its isoelectric point, extinction coefficients, GRAVY, hydropathy and instability "
        "index have no value for U at residue 2, so they weren't computed.",
        "Its isoelectric point, extinction coefficients, GRAVY, hydropathy and instability "
        "index have no value for O at residue 2, so they weren't computed.",
        "Its GRAVY, hydropathy and instability index have no value for J at residue 2, so "
        "they weren't computed.",
    ]


# --- Translation ---------------------------------------------------------------------------------

PARAMS = {"strand": "forward", "frame": 1, "table": 1, "to_stop": True}


def translated(
    tmp_path: Path, sequences: list[dict[str, Any]], **params: Any
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
    run_input = {"params": PARAMS | params, "inputs": {"sequence": sequences}}
    return script(tmp_path, "proteins.py", run_input, "translate")


def test_translates_into_a_protein_linked_to_its_dna(tmp_path: Path) -> None:
    dna = entity(7, bases="atggccaagtaaggg", name="pDemo", organism="Escherichia coli")
    result, output = translated(tmp_path, [dna])
    assert result.returncode == 0, result.stderr
    assert output is not None
    assert made(output) == [
        {
            "type": "iki/protein.Protein",
            "values": {
                "name": "pDemo (+1)",
                "description": (
                    "Translated from X-7, frame +1, with NCBI's genetic code 1 (Standard)."
                ),
                "sequence": "MAK",
                "encoded_by": dna["id"],
                "organism": "Escherichia coli",
            },
            "derived_from": [dna["id"]],
        }
    ]


@pytest.mark.parametrize(
    ("params", "sequence"),
    [
        ({"to_stop": False}, "MAK*G"),
        ({"frame": 2}, "WPSK"),  # TGG CCA AGT AAG; the last two bases are left out
        ({"strand": "reverse"}, "PLLGH"),  # CCC TTA CTT GGC CAT
        ({"strand": "reverse", "frame": 3}, "LTWP"),  # CTT ACT TGG CCA, and T left out
        ({"table": 2, "to_stop": False}, "MAK*G"),
    ],
)
def test_frames_strands_and_codes(tmp_path: Path, params: dict[str, Any], sequence: str) -> None:
    _, output = translated(tmp_path, [entity(1, bases="ATGGCCAAGTAAGGG")], **params)
    assert output is not None
    assert made(output)[0]["values"]["sequence"] == sequence


def test_genetic_codes_differ_where_ncbi_says(tmp_path: Path) -> None:
    # TGA is a stop in the standard code and tryptophan in vertebrate mitochondria; AGA
    # is arginine and a stop.
    bases = "ATGTGAAGA"
    found = {}
    for table in (1, 2):
        _, output = translated(tmp_path, [entity(1, bases=bases)], table=table, to_stop=False)
        assert output is not None
        found[table] = made(output)[0]["values"]["sequence"]
    assert found == {1: "M*R", 2: "MW*"}


def test_ambiguous_codons_give_what_every_reading_agrees_on(tmp_path: Path) -> None:
    # GAY is D either way; NNN could be anything.
    _, output = translated(tmp_path, [entity(1, bases="ATGGAYNNN")])
    assert output is not None
    assert made(output)[0]["values"]["sequence"] == "MDX"


def test_a_sequence_that_gives_no_residues_fails_its_own_run(tmp_path: Path) -> None:
    good, stop = entity(1, bases="ATGAAA"), entity(2, bases="TAAATG", name="pStop")
    short = entity(3, bases="AC", name="pShort")
    result, output = translated(tmp_path, [good, stop, short])
    assert result.returncode == 0, result.stderr
    assert output is not None
    assert [c["derived_from"] for c in made(output)] == [[good["id"]]]
    assert output["messages"] == [
        {
            "level": "error",
            "text": "pStop gives no residues in frame +1: it starts with a stop codon.",
            "entity": stop["id"],
        },
        {
            "level": "error",
            "text": "pShort gives no residues in frame +1: it has no whole codon.",
            "entity": short["id"],
        },
    ]


def test_stop_codons_alone_are_no_protein(tmp_path: Path) -> None:
    stops = entity(1, bases="TAATAA", name="pStops")
    result, output = translated(tmp_path, [stops], to_stop=False)
    assert result.returncode == 0, result.stderr
    assert output is not None
    assert made(output) == []
    assert [m["text"] for m in output["messages"]] == [
        "pStops gives no residues in frame +1: it has only stop codons."
    ]


def test_a_complete_coding_sequence_starts_with_methionine(tmp_path: Path) -> None:
    # NCBI's table 11 lists GTG as an initiator, which a complete CDS starts with M
    # (Biopython's translate(table=11, cds=True) gives MK); a frame reads it as V.
    dna = [entity(1, bases="GTGAAATAA")]
    _, frame = translated(tmp_path, dna, table=11)
    _, cds = translated(tmp_path, dna, table=11, complete_cds=True)
    assert frame is not None
    assert cds is not None
    assert frame["creates"][0]["values"]["sequence"] == "VK"
    assert cds["creates"][0]["values"]["sequence"] == "MK"
    assert cds["creates"][0]["values"]["description"] == (
        "Translated from X-1, frame +1, as a complete coding sequence, with NCBI's genetic "
        "code 11 (Bacterial)."
    )


def test_what_isnt_a_complete_coding_sequence_fails_its_own_run(tmp_path: Path) -> None:
    good = entity(1, bases="ATGAAATAA")
    no_start, no_stop, inner = (
        entity(2, bases="AAAAAATAA", name="pNoStart"),
        entity(3, bases="ATGAAAAAA", name="pNoStop"),
        entity(4, bases="ATGTAAAAATAA", name="pInner"),
    )
    result, output = translated(tmp_path, [good, no_start, no_stop, inner], complete_cds=True)
    assert result.returncode == 0, result.stderr
    assert output is not None
    assert [c["values"]["sequence"] for c in made(output)] == ["MK"]
    assert [m["text"] for m in output["messages"]] == [
        "pNoStart isn't a complete coding sequence in frame +1: First codon 'AAA' is not a "
        "start codon.",
        "pNoStop isn't a complete coding sequence in frame +1: Final codon 'AAA' is not a "
        "stop codon.",
        "pInner isn't a complete coding sequence in frame +1: Extra in frame stop codon 'TAA' "
        "found.",
    ]


@pytest.mark.parametrize("table", [27, 28, 31])
def test_codes_whose_stops_are_also_amino_acids_cant_stop_at_the_first(
    tmp_path: Path, table: int
) -> None:
    result, output = translated(tmp_path, [entity(1, bases="ATGTGATGA")], table=table)
    assert result.returncode == 1
    assert output is None
    assert "can't stop at the first stop codon" in result.stdout.splitlines()[-1]


def test_a_frame_says_its_last_codon_could_have_been_a_stop(tmp_path: Path) -> None:
    # Code 27: TGA is W, or a stop at a coding sequence's end. A frame keeps it as W
    # (Biopython gives MWW); a complete CDS ends there (MW).
    dna = [entity(1, bases="ATGTGATGA", name="pKaryo")]
    _, frame = translated(tmp_path, dna, table=27, to_stop=False)
    _, cds = translated(tmp_path, dna, table=27, complete_cds=True)
    assert frame is not None
    assert cds is not None
    assert frame["creates"][0]["values"]["sequence"] == "MWW"
    assert [m["text"] for m in frame["messages"]] == [
        "pKaryo's last codon in frame +1, TGA, is W or a stop in genetic code 27, so it was "
        "kept as W: translate it as a complete coding sequence to end it there."
    ]
    assert cds["creates"][0]["values"]["sequence"] == "MW"
    assert cds["messages"] == []


@pytest.mark.parametrize(
    ("table", "bases", "kept", "could"),
    [
        # NCBI code 28 (Condylostoma): TAA and TAG are Q, TGA is W, each also a stop.
        # TAR is TAA or TAG: Q or a stop.
        (28, "ATGTAR", "MQ", "Q"),
        # TRA is TAA (Q) or TGA (W): not one amino acid, but a stop either way.
        (28, "ATGTRA", "MX", "Q, W"),
        # NCBI code 31 (Blastocrithidia): TAA and TAG are E, or a stop.
        (31, "ATGTAR", "ME", "E"),
    ],
)
def test_an_ambiguous_last_codon_says_it_could_have_been_a_stop(
    tmp_path: Path, table: int, bases: str, kept: str, could: str
) -> None:
    _, frame = translated(
        tmp_path, [entity(1, bases=bases, name="pCiliate")], table=table, to_stop=False
    )
    assert frame is not None
    assert frame["creates"][0]["values"]["sequence"] == kept
    assert [m["text"] for m in frame["messages"]] == [
        f"pCiliate's last codon in frame +1, {bases[3:]}, is {could} or a stop in genetic code "
        f"{table}, so it was kept as {kept[-1]}: translate it as a complete coding sequence to "
        "end it there."
    ]
    # A last codon that's only ever an amino acid says nothing: TGG is W in each.
    _, plain = translated(tmp_path, [entity(1, bases="ATGTGG")], table=table, to_stop=False)
    assert plain is not None
    assert plain["messages"] == []


def test_a_genetic_code_ncbi_doesnt_use_fails_the_run(tmp_path: Path) -> None:
    result, output = translated(tmp_path, [entity(1, bases="ATG")], table=7)
    assert result.returncode == 1
    assert output is None
    assert result.stdout.splitlines()[-1] == (
        "::error NCBI has no genetic code 7: choose one of 1, 2, 3, 4, 5, 6, 9, 10, 11, 12, "
        "13, 14, 15, 16, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33."
    )


# --- Importing files -----------------------------------------------------------------------------

FILE_ID = "0190e9a4-0000-7000-8000-000000000001"
UNIPROT_FASTA = f"""\
>sp|P69905|HBA_HUMAN Hemoglobin subunit alpha OS=Homo sapiens OX=9606 GN=HBA1 PE=1 SV=2
{HBA[:60]}
{HBA[60:120]}
{HBA[120:]}
"""


def imported(
    tmp_path: Path, text: str, name: str = "proteins.fasta"
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    run_input = {
        "params": {"file": FILE_ID},
        "files": {FILE_ID: {"name": name, "path": str(path), "size": path.stat().st_size}},
    }
    return script(tmp_path, "imports.py", run_input)


def made(output: dict[str, Any]) -> list[dict[str, Any]]:
    """What a run creates, but the organisms its records are from (ADR 0140)."""
    return [c for c in output["creates"] if c["type"] != "iki/core.Organism"]


def test_uniprot_headers_give_the_entry_accession_organism_and_gene(tmp_path: Path) -> None:
    result, output = imported(tmp_path, UNIPROT_FASTA)
    assert result.returncode == 0, result.stderr
    assert output is not None
    # Its organism, by UniProt's OX=, made the first time it's met (ADR 0140).
    organism, create = output["creates"]
    assert organism == {
        "type": "iki/core.Organism",
        "key": "taxon:9606",
        "values": {"taxon_id": 9606, "scientific_name": "Homo sapiens"},
        "fills": ["scientific_name"],
    }
    assert create["values"] == {
        "name": "HBA_HUMAN",
        "description": "Hemoglobin subunit alpha",
        "uniprot_accession": "P69905",
        "organism": {"$create": "taxon:9606"},
        "gene": "HBA1",
        "sequence": HBA,
    }
    # Its origin is the UniProtKB entry and sequence version the file says it's from, in
    # the file the run read (ADR 0154).
    assert create["origin"] == {
        "source": "FASTA file",
        "id": "P69905",
        "version": "sequence version 2",
        "url": "https://www.uniprot.org/uniprotkb/P69905/entry",
        "file": FILE_ID,
        "sha256": hashlib.sha256(UNIPROT_FASTA.encode()).hexdigest(),
    }
    assert output["metrics"] == {"records": 1}


#: Human calmodulin-1 and -2 (UniProt P0DP23 and P0DP24, as UniProt's REST API gives them):
#: two entries, from two genes, of one 149-residue protein.
CALMODULIN = (
    "MADQLTEEQIAEFKEAFSLFDKDGDGTITTKELGTVMRSLGQNPTEAELQDMINEVDADGNGTIDFPEFLTMMARKMKDTDSEEE"
    "IREAFRVFDKDGNGYISAAELRHVMTNLGEKLTDEEVDEMIREADIDGDGQVNYEEFVQMMTAK"
)


def test_each_uniprot_record_of_one_protein_keeps_its_own_accession_as_its_origin(
    tmp_path: Path,
) -> None:
    text = "".join(
        f">sp|{accession}|{entry} Calmodulin-{n} OS=Homo sapiens OX=9606 GN={gene} PE=1 SV=1\n"
        f"{CALMODULIN}\n"
        for n, (accession, entry, gene) in enumerate(
            [("P0DP23", "CALM1_HUMAN", "CALM1"), ("P0DP24", "CALM2_HUMAN", "CALM2")], start=1
        )
    )
    result, output = imported(tmp_path, text)
    assert result.returncode == 0, result.stderr
    assert output is not None
    assert [(c["origin"]["id"], c["origin"]["version"]) for c in made(output)] == [
        ("P0DP23", "sequence version 1"),
        ("P0DP24", "sequence version 1"),
    ]
    assert len(CALMODULIN) == 149


def test_other_headers_give_a_name_a_description_and_an_organism_in_brackets(
    tmp_path: Path,
) -> None:
    text = (
        ">NP_000549.1 hemoglobin subunit alpha [Homo sapiens]\nMVLSPADKTN\n"
        ">tr|A0A0|X_Y A protein OS=Mus musculus\nmkwv*\n"
        ">\nMKV\n"
    )
    result, output = imported(tmp_path, text)
    assert result.returncode == 0, result.stderr
    assert output is not None
    # An organism named without its taxon ID is left out, saying so: a name can't say
    # which taxon it is (ADR 0140).
    assert [c["values"] for c in output["creates"]] == [
        {
            "name": "NP_000549.1",
            "description": "hemoglobin subunit alpha",
            "sequence": "MVLSPADKTN",
        },
        # Not an accession UniProt would give, so only its entry name is kept.
        {"name": "X_Y", "description": "A protein", "sequence": "MKWV"},
        {"sequence": "MKV"},
    ]
    assert output["creates"][2]["origin"]["id"] == "Record 3"
    assert [m["text"] for m in output["messages"]] == [
        "Record 1 (NP_000549.1): its organism, Homo sapiens, has no NCBI Taxonomy ID, so "
        "it's left out.",
        "Record 2 (X_Y): its organism, Mus musculus, has no NCBI Taxonomy ID, so it's left out.",
    ]


def test_an_organisms_name_is_the_one_its_file_gives_not_ncbis(tmp_path: Path) -> None:
    """UniProt P0A7G6 (RecA) names taxon 83333 Escherichia coli (strain K12), where NCBI's
    Taxonomy calls it Escherichia coli K-12: the name kept is the file's, filling only an
    entry that has none, and the Organism type says so rather than calling it NCBI's."""
    text = (
        ">sp|P0A7G6|RECA_ECOLI Protein RecA OS=Escherichia coli (strain K12) OX=83333 "
        "GN=recA PE=1 SV=2\nMAIDENKQKA\n"
    )
    organism = imported(tmp_path, text)[1]["creates"][0]  # type: ignore[index]
    assert organism == {
        "type": "iki/core.Organism",
        "key": "taxon:83333",
        "values": {"taxon_id": 83333, "scientific_name": "Escherichia coli (strain K12)"},
        "fills": ["scientific_name"],
    }
    described = yaml.safe_load(ORGANISM.read_text())["fields"]["scientific_name"]["description"]
    assert "NCBI Taxonomy." not in described
    assert "first imported" in described


def test_organisms_keep_ncbis_brackets_and_quotes(tmp_path: Path) -> None:
    text = (
        # UniProt M1RNJ8 (ATH1_CANGB), its organism by the name NCBI gave taxon 5478 before
        # moving it to Nakaseomyces glabratus.
        ">sp|M1RNJ8|ATH1_CANGB Periplasmic/secreted acid trehalase ATH1 "
        "OS=[Candida] glabrata OX=5478 GN=ATH1 PE=1 SV=1\nMGFKDKILFW\n"
        # UniProt P0DP26, mouse calmodulin.
        ">sp|P0DP26|CALM1_MOUSE Calmodulin-1 OS=Mus musculus OX=10090 GN=Calm1 PE=1 SV=1\n"
        "MADQLTEEQI\n"
        ">tr|A0A0|X_Y A protein OS='Nostoc azollae' 0708 OX=551115\nMKV\n"
        # NCBI's WP_507980491.1 (fetched 2026-11-19): its organism in brackets, holding
        # NCBI's own for a genus it hasn't placed, and no taxon ID.
        ">WP_507980491.1 phage tail family protein [[Clostridium] scindens]\nMIEIKYVCSN\n"
    )
    result, output = imported(tmp_path, text)
    assert result.returncode == 0, result.stderr
    assert output is not None
    assert [c["values"] for c in output["creates"] if c["type"] == "iki/core.Organism"] == [
        {"taxon_id": 5478, "scientific_name": "[Candida] glabrata"},
        {"taxon_id": 10090, "scientific_name": "Mus musculus"},
        {"taxon_id": 551115, "scientific_name": "'Nostoc azollae' 0708"},
    ]
    last = made(output)[-1]["values"]
    assert last["description"] == "phage tail family protein"
    assert "organism" not in last
    assert [m["text"] for m in output["messages"]] == [
        "Record 4 (WP_507980491.1): its organism, [Clostridium] scindens, has no NCBI "
        "Taxonomy ID, so it's left out."
    ]


def test_a_large_file_goes_in_a_creates_file(tmp_path: Path) -> None:
    text = "".join(f">p{i}\nMK{'W' * (i % 7)}V\n" for i in range(1500))
    result, output = imported(tmp_path, text)
    assert result.returncode == 0, result.stderr
    assert output is not None
    assert output["creates_file"] == {"$file": "creates.ndjson"}
    lines = (tmp_path / "out" / "creates.ndjson").read_text().splitlines()
    assert len(lines) == 1500
    assert json.loads(lines[-1])["values"]["name"] == "p1499"


def test_every_stop_at_a_records_end_is_left_out(tmp_path: Path) -> None:
    result, output = imported(tmp_path, ">a\nMKV**\n")
    assert result.returncode == 0, result.stderr
    assert output is not None
    assert [c["values"]["sequence"] for c in made(output)] == ["MKV"]


@pytest.mark.parametrize(
    ("text", "error"),
    [
        (">a\nMK*V\n", "Record 1 (a) has a stop (*) at residue 3: import the protein up to it."),
        (">a\nMK-V\n", "Record 1 (a) has a gap (-) at residue 3: import unaligned sequences."),
        (
            ">a\nMK7V\n>b\nMK!\n",
            "Record 2 (b) has '!' at residue 3, which isn't an amino acid's code.",
        ),
        (">a\n\n", "Record 1 (a) has no residues."),
        ("MKV\n", "It isn't FASTA: records start with '>'."),
        ("\n\n", "It isn't FASTA: records start with '>'."),
    ],
)
def test_a_record_that_cant_be_read_fails_the_import_saying_why(
    tmp_path: Path, text: str, error: str
) -> None:
    result, output = imported(tmp_path, text)
    assert result.returncode == 1
    assert output is None
    assert result.stdout.splitlines()[-1] == f"::error proteins.fasta: {error}"


# --- Fetching from UniProt -----------------------------------------------------------------------

HBB = (
    "MVHLTPEEKSAVTALWGKVNVDEVGGEALGRLLVVYPWTQRFFESFGDLSTPDAVMGNPKVKAHGKKVLGAFSDGLAHLDNLKGTFATLSE"
    "LHCDKLHVDPENFRLLGNVLVCVLAHHFGKEFTPPVQAAYQKVVAGVANALAHKYH"
)


class FakeUniProt:
    """UniProt's accessions endpoint, answering from a few entries (in its own order)."""

    def __init__(self) -> None:
        self.records = {
            "P69905": UNIPROT_FASTA,
            "P68871": ">sp|P68871|HBB_HUMAN Hemoglobin subunit beta OS=Homo sapiens "
            f"OX=9606 GN=HBB PE=1 SV=2\n{HBB}\n",
        }
        self.asked: list[dict[str, list[str]]] = []
        self.failures: list[int] = []

    def __call__(self, request: Any, timeout: float) -> Any:
        assert request.full_url.startswith("https://rest.uniprot.org/uniprotkb/accessions?")
        assert request.get_header("User-agent") == "iki-protein"
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        self.asked.append(query)
        if self.failures:
            code = self.failures.pop(0)
            raise urllib.error.HTTPError(request.full_url, code, "Slow down", Message(), None)
        wanted = query["accessions"][0].split(",")
        body = "".join(text for acc, text in sorted(self.records.items()) if acc in wanted)
        response = io.BytesIO(body.encode())
        headers = Message()
        headers["X-UniProt-Release"] = "2026_04"
        response.headers = headers  # type: ignore[attr-defined]
        return response


@pytest.fixture
def uniprot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Any]:
    """The fetch script, loaded here with UniProt faked."""
    import importlib
    import time
    import urllib.request

    monkeypatch.syspath_prepend(str(SCRIPTS))
    for name in ("uniprot", "imports"):
        sys.modules.pop(name, None)  # the dna plugin's tests load their own `imports`
    module = importlib.import_module("uniprot")
    fake = FakeUniProt()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    monkeypatch.setenv("IKI_INPUT", str(tmp_path / "input.json"))
    monkeypatch.setenv("IKI_OUTPUT", str(tmp_path / "out.json"))
    yield SimpleNamespace(script=module, fake=fake, slept=slept)
    for name in ("uniprot", "imports"):
        sys.modules.pop(name, None)


def fetched(tmp_path: Path, uniprot: Any, accessions: str) -> dict[str, Any]:
    (tmp_path / "input.json").write_text(json.dumps({"params": {"accessions": accessions}}))
    uniprot.script.main()
    output: dict[str, Any] = json.loads((tmp_path / "out.json").read_text())
    assert not list(schemas.check("run-output", output))
    return output


def test_fetches_entries_from_uniprot_in_the_order_asked(tmp_path: Path, uniprot: Any) -> None:
    output = fetched(tmp_path, uniprot, "p69905, P68871\nP69905")
    assert [a["accessions"] for a in uniprot.fake.asked] == [["P69905,P68871"]]
    assert [a["format"] for a in uniprot.fake.asked] == [["fasta"]]
    hba, hbb = made(output)
    assert (hba["values"]["name"], hbb["values"]["name"]) == ("HBA_HUMAN", "HBB_HUMAN")
    assert hbb["values"]["sequence"] == HBB
    origin = hba["origin"]
    assert {k: origin[k] for k in ("source", "id", "version", "url")} == {
        "source": "UniProtKB",
        "id": "P69905",
        "version": "release 2026_04, sequence version 2",
        "url": "https://www.uniprot.org/uniprotkb/P69905/entry",
    }
    assert origin["retrieved_at"].endswith("+00:00")
    assert origin["sha256"] == hashlib.sha256(UNIPROT_FASTA.encode()).hexdigest()
    assert output["metrics"] == {"records": 2}


def test_asks_in_batches_with_a_pause_between(
    tmp_path: Path, uniprot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(uniprot.script, "BATCH", 1)
    fetched(tmp_path, uniprot, "P68871 P69905")
    assert [a["accessions"] for a in uniprot.fake.asked] == [["P68871"], ["P69905"]]
    assert uniprot.slept == [0.2]


def test_waits_and_asks_again_when_uniprot_says_to_slow_down(tmp_path: Path, uniprot: Any) -> None:
    uniprot.fake.failures = [429, 503]
    output = fetched(tmp_path, uniprot, "P69905")
    assert len(uniprot.fake.asked) == 3
    assert uniprot.slept == [2, 4]
    assert made(output)[0]["values"]["uniprot_accession"] == "P69905"


@pytest.mark.parametrize(
    ("accessions", "error"),
    [
        (
            "P69905 Q99999",
            "::error UniProt has no entry Q99999: it may have been merged into another or "
            "made obsolete. Search for it at https://www.uniprot.org.",
        ),
        (
            "P69905 HBA_HUMAN",
            "::error HBA_HUMAN isn't a UniProtKB accession, such as P69905 or P69905-2.",
        ),
        (" , ", "::error Give at least one accession, such as P69905."),
    ],
)
def test_a_fetch_fails_whole_and_says_why(
    tmp_path: Path,
    uniprot: Any,
    capsys: pytest.CaptureFixture[str],
    accessions: str,
    error: str,
) -> None:
    with pytest.raises(SystemExit) as stopped:
        fetched(tmp_path, uniprot, accessions)
    assert stopped.value.code == 1
    assert capsys.readouterr().out.splitlines()[-1] == error
    assert not (tmp_path / "out.json").exists()
