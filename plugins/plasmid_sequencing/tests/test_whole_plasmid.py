"""Whole-plasmid verification on public pUC19 nanopore reads, and on hand-edited consensuses.

minimap2's answers are recorded (see replay.py); everything this plugin computes from them
runs here: the circle's folding, each read's indels moved 3', the consensus and its
polishing, the comparison, HGVS names, features and support.
"""

from __future__ import annotations

import csv
import gzip
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent


def load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


replay = load("plasmid_sequencing_replay", HERE / "replay.py")


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    found = replay.module()
    answers = replay.replaying()
    monkeypatch.setattr(found, "minimap2", answers)
    monkeypatch.setattr(found, "minimap2_version", lambda: answers.version)
    return found


@pytest.fixture
def results(script: ModuleType, tmp_path: Path) -> dict[str, tuple[dict[str, Any], Path]]:
    out = {}
    for name, (given, out_dir) in replay.scenarios(tmp_path).items():
        out[name] = (replay.run(script, given, out_dir), out_dir)
    return out


def values(results: dict[str, tuple[dict[str, Any], Path]], name: str) -> dict[str, Any]:
    output, _ = results[name]
    (created,) = output["creates"]
    assert created["type"] == "iki/plasmid_sequencing.WholePlasmidAlignment"
    found: dict[str, Any] = created["values"]
    return found


# --- Public reads: SRA run SRR17915342, a MinION run of pUC19 -----------------------------


def test_public_puc19_reads_show_their_multiple_cloning_site_insert(
    results: dict[str, tuple[dict[str, Any], Path]],
) -> None:
    found = values(results, "reads")
    assert found["agreement"] == "differs"
    assert found["source"] == "reads"
    assert found["reads_total"] == 41
    assert found["reads_aligned"] == 37  # four short reads align nowhere
    assert (found["reference_length"], found["topology"]) == (2686, "circular")
    # Depth counts reads, each once: 36 of the 41 are longer than pUC19 and pass some of it
    # twice, but no base can have more reads than the 37 that aligned.
    assert found["mean_depth"] == 35.9
    assert found["least_depth"] == 33
    assert found["low_coverage"] == []
    assert (found["consensus_length"], found["unplaced"]) == (2686 + 84 - 6 - 2, [])
    # Three bases in runs of one base have no majority of reads: they're left uncalled, N.
    assert found["called_percent"] == 99.89
    assert found["polishing_rounds"] == 2
    named = [
        (d["hgvs"][:22], d["kind"], d["features"], d["homopolymer"]) for d in found["differences"]
    ]
    assert named == [
        # The run's plasmid has 84 bases in its multiple cloning site (KpnI to BamHI) that
        # L09137.2 doesn't, and has six fewer, with every read agreeing.
        ("o.413_418del", "deletion", [], 4),
        ("o.419_420insACTGGCGGAT", "insertion", [], 1),
        ("o.423T>A", "substitution", [], 1),
        # Single-base deletions in runs of six and seven, where nanopore reads' errors fall
        # most (Delahaye & Nicolas 2021). These reads alone can't say whether the prep has
        # them: they're candidates for a reviewer, who weighs their support and the run.
        ("o.887del", "deletion", ["ori (rep_origin)"], 6),
        ("o.1446del", "deletion", ["ori (rep_origin)"], 7),
    ]
    deletion, insertion, substitution = found["differences"][:3]
    assert insertion["length_change"] == 84
    assert len(insertion["observed"]) == 84
    assert (deletion["depth"], deletion["support"], deletion["fraction"]) == (35, 35, 1.0)
    # The 84 inserted bases: every read across them has the whole insertion.
    assert (insertion["depth"], insertion["support"]) == (34, 34)
    assert (substitution["forward"], substitution["reverse"]) == (20, 14)
    homopolymer_deletions = found["differences"][3:]
    assert [d["fraction"] for d in homopolymer_deletions] == [0.778, 0.703]
    for each in found["differences"]:
        assert each["support"] <= each["depth"] <= found["reads_aligned"]
    assert found["read_set"] == replay.READ_SET_ID
    assert found["read_set_revision"] == 2
    assert found["sample"] == replay.SAMPLE_ID
    # The plasmid and its sequence by their registry entries; the record read, by its own.
    assert found["plasmid"] == replay.PLASMID_ENTRY
    assert (found["plasmid_record_id"], found["plasmid_revision"]) == (replay.PLASMID_ID, 3)
    assert found["reference"] == found["members"][0] == replay.SEQUENCE_ENTRY
    assert found["method"].startswith("minimap2 2.31-r1302 (-x map-ont")
    # The features each difference was placed in, kept as they were when it was compared.
    assert [(f["name"], f["strand"]) for f in found["features"]] == [
        ("lac promoter", "reverse"),
        ("ori", "reverse"),
        ("AmpR", "reverse"),
    ]


def test_reads_files_say_what_the_record_says(
    results: dict[str, tuple[dict[str, Any], Path]],
) -> None:
    output, out_dir = results["reads"]
    found = values(results, "reads")
    rows = (out_dir / "alignment.fasta").read_text(encoding="utf-8").split(">")[1:]
    reference_row, consensus_row = ("".join(r.splitlines()[1:]) for r in rows)
    assert rows[0].startswith("DNA-000099\n") and rows[1].startswith("PLS-000002_consensus\n")
    # One column per reference base, and one more for each inserted base.
    assert len(reference_row) == len(consensus_row) == 2686 + 84
    assert reference_row.replace("-", "") == replay.puc19()
    consensus = "".join((out_dir / "consensus.fasta").read_text(encoding="utf-8").splitlines()[1:])
    assert len(consensus) == 2686 + 84 - 6 - 2 == found["consensus_length"]
    assert consensus == consensus_row.replace("-", "")
    with (out_dir / "coverage.tsv").open(encoding="utf-8") as handle:
        coverage = list(csv.DictReader(handle, delimiter="\t"))
    assert len(coverage) == 2686
    assert min(int(row["depth"]) for row in coverage) == found["least_depth"]
    assert [row["position"] for row in coverage if row["consensus"] == "N"] == [
        "1134",
        "2043",
        "2602",
    ]
    assert max(int(row["depth"]) for row in coverage) <= found["reads_aligned"]
    with (out_dir / "differences.tsv").open(encoding="utf-8") as handle:
        listed = list(csv.DictReader(handle, delimiter="\t"))
    assert [row["hgvs"] for row in listed] == [d["hgvs"] for d in found["differences"]]
    with gzip.open(out_dir / "alignments.paf.gz", "rt", encoding="utf-8") as handle:
        assert sum(1 for _ in handle) >= found["reads_aligned"]
    coverage_bins = found["coverage"]
    assert coverage_bins["bin_size"] == 8  # 2,686 bases in at most 360 bins
    assert len(coverage_bins["mean"]) == len(coverage_bins["min"]) == 336
    assert output["metrics"] == {
        "differences": 5,
        "called_percent": 99.89,
        "reads": 41,
        "aligned": 37,
        "mean_depth": 35.9,
    }


# --- A service's consensus (synthetic edits of L09137.2) -----------------------------------


def test_a_rotated_consensus_of_the_same_bases_matches(
    results: dict[str, tuple[dict[str, Any], Path]],
) -> None:
    found = values(results, "same-rotated")
    assert (found["agreement"], found["differences"], found["called_percent"]) == (
        "matches",
        [],
        100.0,
    )
    assert found["source"] == "consensus"
    assert found["consensus_given"] == "given"
    assert "read_set" not in found and "mean_depth" not in found
    _, out_dir = results["same-rotated"]
    consensus = "".join((out_dir / "consensus.fasta").read_text(encoding="utf-8").splitlines()[1:])
    # In the reference's frame, from its origin, whatever base the service began from.
    assert consensus == replay.puc19()


def test_a_deletion_across_the_origin_is_one_named_round_it(
    results: dict[str, tuple[dict[str, Any], Path]],
) -> None:
    (only,) = values(results, "origin-deletion")["differences"]
    # Bases 2684-2686 and 1-2 (GTCTC) are gone; base 3 is G, as 2684 was, so HGVS's 3'
    # rule names the same loss one place on, and the record places it there too.
    assert (only["hgvs"], only["start"], only["end"], only["length_change"]) == (
        "o.2685_3del",
        2685,
        3,
        -5,
    )
    assert "depth" not in only  # a consensus has no reads of its own


def test_a_consensus_on_the_other_strand_is_turned_round(
    results: dict[str, tuple[dict[str, Any], Path]],
) -> None:
    (only,) = values(results, "substitution-other-strand")["differences"]
    assert (only["hgvs"], only["kind"], only["features"]) == (
        "o.1000A>C",
        "substitution",
        ["ori (rep_origin)"],
    )


def test_an_inserted_site_is_named_between_its_flanking_bases(
    results: dict[str, tuple[dict[str, Any], Path]],
) -> None:
    (only,) = values(results, "insertion")["differences"]
    assert (only["hgvs"], only["start"], only["end"], only["features"]) == (
        "o.1700_1701insGAATTC",
        1700,
        1701,
        ["AmpR (CDS)"],
    )


def test_a_linear_consensus_that_stops_short_is_incomplete(
    results: dict[str, tuple[dict[str, Any], Path]],
) -> None:
    found = values(results, "linear-short")
    assert found["agreement"] == "incomplete"
    assert found["differences"] == []
    assert found["low_coverage"] == [{"start": 2637, "end": 2686}]
    assert found["called_percent"] == 98.14


def test_a_consensus_with_the_plasmid_twice_over_doesnt_match(
    results: dict[str, tuple[dict[str, Any], Path]],
) -> None:
    found = values(results, "two-laps")
    # Every base of the plasmid is in it, unchanged; but its second lap is placed where the
    # first already was, so it isn't one copy of the plasmid.
    assert (found["agreement"], found["differences"], found["called_percent"]) == (
        "differs",
        [],
        100.0,
    )
    assert found["consensus_length"] == 2 * 2686
    assert found["unplaced"] == [{"start": 2687, "end": 5372, "bases": 2686, "why": "repeated"}]
    assert "2,686 (2,687–5,372) repeat bases already placed on DNA-000099" in found["description"]
    _, out_dir = results["two-laps"]
    consensus = "".join((out_dir / "consensus.fasta").read_text(encoding="utf-8").splitlines()[1:])
    assert consensus == replay.puc19() * 2  # kept as it was, not cut to one lap


def test_a_consensus_longer_than_the_plasmid_doesnt_match(
    results: dict[str, tuple[dict[str, Any], Path]],
) -> None:
    found = values(results, "linear-longer")
    assert (found["agreement"], found["differences"]) == ("differs", [])
    assert found["unplaced"] == [{"start": 2687, "end": 3136, "bases": 450, "why": "unaligned"}]
    _, out_dir = results["linear-longer"]
    consensus = "".join((out_dir / "consensus.fasta").read_text(encoding="utf-8").splitlines()[1:])
    assert consensus == replay.puc19() + replay.unrelated(450)


def test_a_consensus_file_must_hold_one_sequence(script: ModuleType, tmp_path: Path) -> None:
    two = tmp_path / "two.fasta"
    two.write_text(">a\nACGT\n>b\nACGT\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        script.verify(replay.consensus_run(two, replay.puc19()), tmp_path)


# --- The parts, on synthetic sequences ------------------------------------------------------


@pytest.mark.parametrize(
    ("reference", "start", "stop", "bases", "circular", "name"),
    [
        ("ACGTACGT", 2, 3, "T", False, "g.3G>T"),
        ("ACGTACGT", 2, 3, "", False, "g.3del"),
        # The 3' rule: a deletion in AAA is named at its last A.
        ("CAAAG", 1, 2, "", False, "g.4del"),
        ("CAAAG", 1, 3, "", False, "g.3_4del"),
        # An insertion copying the bases before it is a duplication.
        ("CAAAG", 4, 4, "A", False, "g.4dup"),
        ("CATGG", 3, 3, "AT", False, "g.2_3dup"),
        ("CATGG", 3, 3, "CC", False, "g.3_4insCC"),
        ("ACGTTT", 1, 3, "CG"[::-1].translate(str.maketrans("CG", "GC")), False, "g.2_3inv"),
        ("ACGTTT", 1, 3, "TT", False, "g.2_3delinsTT"),
        # Round a circle's origin: its last base and first are side by side.
        ("TCGCGA", 5, 7, "", True, "o.6_1del"),
        ("TCGCGA", 6, 6, "G", True, "o.6_1insG"),
        ("AACGTA", 5, 6, "", True, "o.2del"),
    ],
)
def test_hgvs_names(
    script: ModuleType, reference: str, start: int, stop: int, bases: str, circular: bool, name: str
) -> None:
    change = script.Change(start, stop, bases, 0, len(bases))
    assert script.named(reference, change, circular, "X") == name


@pytest.mark.parametrize(
    ("reference", "start", "stop", "bases", "name", "kind"),
    [
        ("ACGTTT", 1, 3, "CG", "g.2_3inv", "inversion"),
        ("CATGG", 3, 3, "AT", "g.2_3dup", "duplication"),
        ("ACGTTT", 1, 3, "TT", "g.2_3delinsTT", "deletion_insertion"),
        ("CAAAG", 1, 2, "", "g.4del", "deletion"),
        ("CATGG", 3, 3, "CC", "g.3_4insCC", "insertion"),
        ("ACGTACGT", 2, 3, "T", "g.3G>T", "substitution"),
    ],
)
def test_a_changes_kind_is_what_its_name_says(
    script: ModuleType, reference: str, start: int, stop: int, bases: str, name: str, kind: str
) -> None:
    named = script.named(reference, script.Change(start, stop, bases, 0, len(bases)), False, "X")
    assert (named, script.kind_of(named)) == (name, kind)


def test_a_long_insertion_is_named_by_where_it_is_in_the_consensus(script: ModuleType) -> None:
    change = script.Change(3, 3, "G" * 150, 3, 153)
    assert (
        script.named("ACGTACGT", change, False, "PLS-1_consensus")
        == "g.3_4ins[PLS-1_consensus:g.4_153]"
    )


def test_a_change_in_a_joins_gap_isnt_in_the_feature(script: ModuleType) -> None:
    # A CDS join(1..3,10..12): its bases are 1-3 and 10-12, not the 4-9 between.
    sequence = {
        "features": [
            {
                "type": "CDS",
                "name": "split",
                "start": 1,
                "end": 12,
                "strand": "mixed",
                "parts": [
                    {"start": 1, "end": 3, "strand": "forward"},
                    {"start": 10, "end": 12, "strand": "reverse"},
                ],
            },
            {"type": "misc_feature", "name": "unstranded", "start": 20, "end": 30},
        ]
    }
    split, unstranded = script.features_of(sequence, {})
    assert split["parts"] == [{"start": 1, "end": 3}, {"start": 10, "end": 12}]
    # Its strand as the sequence has it: mixed, and none where there's none.
    assert (split["strand"], unstranded["strand"]) == ("mixed", "none")
    assert script.falls_in([split], 6, 6, 40) == []
    assert script.falls_in([split], 1, 2, 40) == ["split (CDS)"]
    assert script.falls_in([split], 10, 11, 40) == ["split (CDS)"]
    assert script.falls_in([split], 2, 9, 40) == []  # from a part into the gap


@pytest.mark.parametrize(
    ("feature", "first", "last", "inside"),
    [
        ({"start": 10, "end": 20}, 9, 19, True),
        ({"start": 10, "end": 20}, 8, 9, False),
        ({"start": 10, "end": 20}, 19, 20, False),
        # A feature across the origin of a 100-base circle: 95 to 5.
        ({"start": 95, "end": 5}, 98, 101, True),
        ({"start": 95, "end": 5}, 3, 4, True),
        ({"start": 95, "end": 5}, 5, 6, False),
        ({"start": 95, "end": 5}, 50, 50, False),
    ],
)
def test_features_hold_changes_round_the_origin(
    script: ModuleType, feature: dict[str, int], first: int, last: int, inside: bool
) -> None:
    assert script.holds(feature, first, last, 100) is inside


def test_a_reads_indel_in_a_run_is_moved_3_prime_before_it_is_counted(script: ModuleType) -> None:
    target = "GCAAAATG"
    # Two reads lose an A from the run, the aligner putting each loss at another A.
    early = script.Hit("r1", 0, 8, 0, 7, True, 7, [script.Event(2, 3, "", 2, 2)])
    late = script.Hit("r2", 0, 8, 0, 7, False, 7, [script.Event(4, 5, "", 4, 4)])
    pileup = script.Pileup.of(len(target))
    for hit in (early, late):
        pileup.add(hit, target)
    assert pileup.calls[5]["-", True] == pileup.calls[5]["-", False] == 1
    assert all(pileup.calls[k].get(("-", True), 0) == 0 for k in range(5))
    call = script.base_call(pileup, 5, 2)
    assert (call.allele, call.depth, call.forward, call.reverse) == ("-", 2, 1, 1)


def test_an_insertion_is_called_only_by_most_reads_across_it(script: ModuleType) -> None:
    target = "ACGTACGT"
    with_one = [
        script.Hit(f"r{i}", 0, 8, 0, 9, True, 8, [script.Event(4, 4, "G", 4, 5)]) for i in range(2)
    ]
    without = [script.Hit(f"s{i}", 0, 8, 0, 8, True, 8, []) for i in range(2)]
    pileup = script.Pileup.of(len(target))
    for hit in with_one + without:
        pileup.add(hit, target)
    # Two of four reads isn't more than half.
    assert script.insert_call(pileup, 4, 2) is None
    pileup.add(script.Hit("r3", 0, 8, 0, 9, True, 8, [script.Event(4, 4, "G", 4, 5)]), target)
    inserted = script.insert_call(pileup, 4, 2)
    assert inserted is not None and (inserted.allele, inserted.support, inserted.depth) == (
        "G",
        3,
        5,
    )
    assert script.insert_call(pileup, 4, 6) is None  # under the minimum depth


def test_a_tied_insertion_is_n_whatever_the_reads_order(script: ModuleType) -> None:
    # Before a G, so neither inserted base moves 3'.
    target = "ACGTGCGT"

    def read(name: str, inserted: str) -> Any:
        events = [script.Event(4, 4, inserted, 4, 4 + len(inserted))] if inserted else []
        return script.Hit(name, 0, 8, 0, 8 + len(inserted), True, 8, events)

    reads = [read(f"a{i}", "A") for i in range(3)] + [read(f"t{i}", "T") for i in range(3)]
    reads += [read(f"n{i}", "") for i in range(4)]
    for order in (reads, reads[::-1]):
        pileup = script.Pileup.of(len(target), target)
        for hit in order:
            pileup.add(hit, target)
        call = script.insert_call(pileup, 4, 2)
        # Six of ten insert one base: there is an insertion, but which base is a tie.
        assert call is not None
        assert (call.allele, call.depth, call.support) == ("N", 10, 6)


def test_a_read_that_passes_a_base_twice_is_one_read(script: ModuleType) -> None:
    # One read, eight bases long, round a four-base circle twice (on the doubled target).
    target = "ACGT" * 2
    pileup = script.Pileup.of(4, target)
    pileup.add(script.Hit("r", 0, 8, 0, 8, True, 8, []), target)
    assert [pileup.depth(k) for k in range(4)] == [1, 1, 1, 1]
    assert script.base_call(pileup, 0, 2).allele == "N"  # one read isn't two
    assert script.base_call(pileup, 0, 1).allele == "A"


def test_a_reads_passes_that_disagree_vote_for_neither(script: ModuleType) -> None:
    target = "ACGT" * 2
    pileup = script.Pileup.of(4, target)
    # Two passes of one read over base 2: one reads G, one has T there.
    pileup.add_read(
        [
            script.Hit("r", 0, 4, 0, 4, True, 4, []),
            script.Hit("r", 4, 8, 4, 8, True, 4, [script.Event(6, 7, "T", 6, 7)]),
        ],
        target,
    )
    assert script.alleles(pileup.calls[2]) == {script.UNSURE: 1}
    assert script.base_call(pileup, 2, 1).allele == "N"


def test_a_change_of_two_bases_is_supported_by_reads_with_both(script: ModuleType) -> None:
    # The consensus has TG where the reference has GA. Four reads have TA, four GG, two TG:
    # six reads have each new base, but only two have both.
    consensus = "AAAACCTGCCAAAA"

    def read(name: str, events: list[Any]) -> Any:
        return script.Hit(name, 0, 14, 0, 14, True, 14, events)

    pileup = script.Pileup.of(len(consensus), consensus)
    for i in range(4):
        pileup.add(read(f"ta{i}", [script.Event(7, 8, "A", 7, 8)]), consensus)
        pileup.add(read(f"gg{i}", [script.Event(6, 7, "G", 6, 7)]), consensus)
    for i in range(2):
        pileup.add(read(f"tg{i}", []), consensus)
    call = script.support_of(pileup, 6, 8, "GA", "TG")
    assert (call.depth, call.support) == (10, 2)
    # Reads with the reference's GA are across it, and against it.
    for i in range(3):
        pileup.add(
            read(f"ga{i}", [script.Event(6, 7, "G", 6, 7), script.Event(7, 8, "A", 7, 8)]),
            consensus,
        )
    call = script.support_of(pileup, 6, 8, "GA", "TG")
    assert (call.depth, call.support) == (13, 2)


def test_wrapped_fastq_records_are_counted_once_each(script: ModuleType, tmp_path: Path) -> None:
    # Two records, their bases and qualities each on two lines, in 12 lines; a quality
    # line may start with @, as Phred 31 does.
    wrapped = tmp_path / "wrapped.fastq"
    wrapped.write_text(
        "@r1\nACGT\nACGT\n+\n@III\nIIII\n@r2\nGGCC\nAA\n+\nIIII\nII\n", encoding="utf-8"
    )
    assert script.count_reads([wrapped]) == 2
    plain = tmp_path / "plain.fastq.gz"
    with gzip.open(plain, "wt", encoding="utf-8") as handle:
        handle.write("@a\nACGT\n+\nIIII\n@b\nAC\n+\nII\n")
    assert script.count_reads([wrapped, plain]) == 4


def test_a_base_without_a_majority_or_enough_reads_is_n(script: ModuleType) -> None:
    pileup = script.Pileup.of(1)
    pileup.calls[0].update({("A", True): 2, ("C", True): 2})
    assert script.base_call(pileup, 0, 1).allele == "N"
    pileup.calls[0][("A", False)] += 1
    assert script.base_call(pileup, 0, 1).allele == "A"
    assert script.base_call(pileup, 0, 6).allele == "N"


def test_a_circular_read_is_folded_onto_the_circle(script: ModuleType) -> None:
    target = "ACGTAC" * 2
    # A read from base 5 of a six-base circle round to base 2, on the doubled reference.
    hit = script.Hit("r", 4, 9, 0, 5, True, 5, [])
    pileup = script.Pileup.of(6)
    pileup.add(hit, target)
    assert [pileup.depth(k) for k in range(6)] == [1, 1, 1, 0, 1, 1]


def test_minimap2_paf_with_cs_is_read(script: ModuleType) -> None:
    line = "\t".join(
        [
            *["read", "12", "1", "11", "-", "ref", "20", "5", "14", "8", "10", "60"],
            *["tp:A:P", "cs:Z::3*ag+tt:2-c:2"],
        ]
    )
    secondary = line.replace("tp:A:P", "tp:A:S")
    (hit,) = script.hits([line, secondary])
    assert (hit.start, hit.stop, hit.forward, hit.qstart, hit.qstop) == (5, 14, False, 1, 11)
    assert [(e.start, e.stop, e.bases, e.qstart, e.qstop) for e in hit.events] == [
        (8, 9, "G", 4, 5),
        (9, 9, "TT", 5, 7),
        (11, 12, "", 9, 9),
    ]


def test_runs_join_round_the_origin(script: ModuleType) -> None:
    assert script.regions([0, 1, 5, 8, 9], 10, True) == [(8, 1), (5, 5)]
    assert script.regions([0, 1, 5, 8, 9], 10, False) == [(0, 1), (5, 5), (8, 9)]


def test_a_homopolymer_is_the_run_a_change_touches(script: ModuleType) -> None:
    assert script.homopolymer("GCAAAAAAG", script.Change(8, 9, "", 0, 0), False) == 1
    assert script.homopolymer("GCAAAAAAG", script.Change(7, 8, "", 0, 0), False) == 6
    # Round the origin: AA at the end and AAA at the start are one run of five.
    assert script.homopolymer("AAACGTAA", script.Change(0, 1, "", 0, 0), True) == 5
