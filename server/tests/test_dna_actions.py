"""The DNA plugin's actions: what its sequence calculations give, run as a worker runs
them (an input.json in, an output.json out)."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from first_party_dependencies import FIRST_PARTY, PLUGINS

from iki_spec import schemas

DNA = PLUGINS / "dna"
SCRIPT = DNA / "scripts" / "sequences.py"


def run(tmp_path: Path, kind: str, name: str, bases: list[str]) -> dict[str, Any]:
    entities = [
        {"id": f"00000000-0000-0000-0000-{i:012d}", "hrid": f"X-{i}", "values": {"bases": b}}
        for i, b in enumerate(bases)
    ]
    (tmp_path / "input.json").write_text(json.dumps({"inputs": {name: entities}}))
    result = subprocess.run(  # noqa: S603 - our own script
        # Not isolated: the script imports `oligos`, its sibling, as a worker's own
        # `python ./scripts/sequences.py` does.
        [sys.executable, str(SCRIPT), kind],
        env={"IKI_INPUT": str(tmp_path / "input.json"), "IKI_OUTPUT": str(tmp_path / "out.json")},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1].startswith("::progress 1.00 ")
    output: dict[str, Any] = json.loads((tmp_path / "out.json").read_text())
    assert not list(schemas.check("run-output", output))
    return {u["entity"][-1]: u["values"] for u in output["updates"][name]}


def test_composition_counts_gc_among_bases_whose_pairing_is_known(tmp_path: Path) -> None:
    found = run(tmp_path, "composition", "sequence", ["ATGCGC", "atgcNNNNS", "NNNN"])
    assert found["0"] == {"length": 6, "gc_content": 66.67}
    assert found["1"] == {"length": 9, "gc_content": 60.0}
    assert found["2"] == {"length": 4}  # nothing to count


@pytest.mark.parametrize(
    ("bases", "sodium", "buffer"),
    [
        # The first number is SantaLucia's Tm in sodium alone, which Biopython 1.88 gives
        # as Tm_NN with DNA_NN3 (SantaLucia 1998), Na=50, saltcorr=5 and 50 nM of strands
        # in all, his CT: dnac1=25, dnac2=25 (nM of each). By hand, from his table 2 for
        # M13 reverse: ΔH -184.4 kcal/mol, ΔS -500.7 cal/K/mol, -526.06 at 50 mM Na+
        # (+0.368 x 23 x ln 0.05), so Tm = -184400 / (-526.06 + 1.987 ln(50e-9/4)) -
        # 273.15 = 54.84 °C.
        #
        # The second is what the action now reports: the same primer in the buffer a PCR
        # runs in -- 1.5 mM Mg2+ and 0.6 mM dNTPs besides the sodium -- corrected by
        # Owczarzy et al. 2008, equation 16. The magnesium is worth several degrees, which
        # is the point of quoting it: a primer annealed at its sodium Tm runs cold.
        ("AGCGGATAACAATTTCACACAGGA", 54.8, 60.0),  # M13 reverse
        ("GTAAAACGACGGCCAGT", 49.2, 53.8),  # M13 forward (-20)
        ("TAATACGACTCACTATAGGG", 44.6, 49.5),  # T7 promoter
        # Its own complement, so all 50 nM of strands are this one and the duplex carries
        # SantaLucia's symmetry term, as the pure-Python action this replaced did and as
        # Biopython's Tm_NN does with selfcomp=True and dnac1=50: 17.31 in sodium alone.
        ("GCGCGC", 17.3, 19.1),
    ],
)
def test_a_primers_melting_temperature_is_the_one_its_buffer_gives(
    tmp_path: Path, bases: str, sodium: float, buffer: float
) -> None:
    (found,) = run(tmp_path, "primers", "primer", [bases]).values()
    assert found["melting_temperature"] == pytest.approx(buffer, abs=0.05)
    assert found["melting_temperature"] > sodium
    assert found["length"] == len(bases)


def test_primers_with_ambiguous_bases_get_no_melting_temperature(tmp_path: Path) -> None:
    (found,) = run(tmp_path, "primers", "primer", ["ACGTNACGTACGTACGT"]).values()
    assert "melting_temperature" not in found
    assert "hairpin_free_energy" not in found
    assert found["gc_content"] == 50.0
    assert found["longest_run"] == 1


def test_a_primer_that_folds_back_on_itself_reports_the_hairpin(tmp_path: Path) -> None:
    """A stem of six G-C pairs round a four-base loop: the kind of thing a primer design
    is rejected for, since the 3' end is held in the stem rather than priming. A primer
    that folds into nothing gets a positive free energy rather than no value, so the
    field can be filtered on: a hairpin below about -9 kcal/mol is worth redesigning."""
    (folded,) = run(tmp_path, "primers", "primer", ["GGGCCCTTTTGGGCCC"]).values()
    assert folded["hairpin_free_energy"] < -5
    (flat,) = run(tmp_path, "primers", "primer", ["AGCGGATAACAATTTCACACAGGA"]).values()
    assert flat["hairpin_free_energy"] > 0


def test_a_self_complementary_primer_dimerises_with_itself(tmp_path: Path) -> None:
    """Twelve alternating G and C pair with another copy over their whole length: eleven
    G-C stacks at about -2 kcal/mol each, so about -22 and nothing worth ordering."""
    (found,) = run(tmp_path, "primers", "primer", ["GCGCGCGCGCGC"]).values()
    assert found["self_dimer_free_energy"] == pytest.approx(-22.77, abs=0.01)
    assert found["longest_run"] == 1


def test_a_run_of_one_base_is_counted(tmp_path: Path) -> None:
    (found,) = run(tmp_path, "primers", "primer", ["ACGTGGGGGACGTACGT"]).values()
    assert found["longest_run"] == 5
    # The last five bases against their complement, which is what a polymerase extends
    # from: ACGT is 4 of the 5, so the end is held by two A-T and one G-C stack.
    assert -9 < found["end_stability"] < 0


def test_every_field_the_action_declares_is_one_a_primer_can_get(tmp_path: Path) -> None:
    """A field in `effects` the script never writes would promise an empty column."""
    declared = yaml.safe_load((DNA / "actions" / "primer_properties.yaml").read_text())
    (fields,) = [update["fields"] for update in declared["effects"]["updates"]]
    found = run(tmp_path, "primers", "primer", ["GGGCCCTTTTGGGCCC"])
    assert set(fields) == set(next(iter(found.values())))


# --- Importing files -----------------------------------------------------------------------------

IMPORTS = SCRIPT.with_name("imports.py")
FILE_ID = "0190e9a4-0000-7000-8000-000000000001"


def imported(
    tmp_path: Path, text: str, *, name: str = "seqs.txt", topology: str = "linear"
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    run_input = {
        "params": {"file": FILE_ID, "topology": topology},
        "files": {FILE_ID: {"name": name, "path": str(path), "size": path.stat().st_size}},
    }
    (tmp_path / "input.json").write_text(json.dumps(run_input))
    out = tmp_path / "out.json"
    out.unlink(missing_ok=True)
    out_dir = tmp_path / "out"
    out_dir.mkdir(exist_ok=True)
    result = subprocess.run(  # noqa: S603 - our own script
        [sys.executable, "-I", str(IMPORTS)],
        env={
            "IKI_INPUT": str(tmp_path / "input.json"),
            "IKI_OUTPUT": str(out),
            "IKI_OUT_DIR": str(out_dir),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if not out.exists():
        return result, None
    output: dict[str, Any] = json.loads(out.read_text())
    assert not list(schemas.check("run-output", output))
    return result, output


def made(output: dict[str, Any]) -> list[dict[str, Any]]:
    """What a run creates, but the organisms its records are from (ADR 0140)."""
    return [c for c in output["creates"] if c["type"] != "iki/core.Organism"]


BASES = ("gattacagat" * 6).upper()
GENBANK = f"""\
LOCUS       pDemo                     60 bp    DNA     circular SYN 11-NOV-2026
DEFINITION  A demonstration plasmid, cut down to a
            fragment.
ACCESSION   L09137
VERSION     L09137.2
KEYWORDS    .
SOURCE      synthetic construct
  ORGANISM  synthetic construct
            other sequences; artificial sequences.
FEATURES             Location/Qualifiers
     source          1..60
                     /organism="synthetic construct"
                     /mol_type="other DNA"
                     /db_xref="taxon:32630"
     promoter        complement(5..20)
                     /label="lac promoter"
                     /note="a long note that wraps onto
                     the next line"
     CDS             join(50..60,
                     1..10)
                     /gene="bla"
                     /translation="MSIQHFRVAL
                     IPFFAAF"
     misc_feature    J00194.1:100..202
                     /note="elsewhere"
ORIGIN
        1 {" ".join(BASES[i : i + 10].lower() for i in range(0, 60, 10))}
//
"""


def test_a_genbank_record_keeps_its_names_features_and_where_it_came_from(
    tmp_path: Path,
) -> None:
    result, output = imported(tmp_path, GENBANK + "\n" + GENBANK.replace("pDemo", "pDemo2"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    # Its organism, by the NCBI Taxonomy ID of its source feature, made once and first
    # (ADR 0140), and both sequences from it.
    organism, first, second = output["creates"]
    assert organism == {
        "type": "iki/core.Organism",
        "key": "taxon:32630",
        "values": {"taxon_id": 32630, "scientific_name": "synthetic construct"},
        "fills": ["scientific_name"],
    }
    assert first["type"] == "iki/dna.DNASequence"
    values = first["values"]
    assert values == {
        "name": "pDemo",
        "description": "A demonstration plasmid, cut down to a fragment",
        "organism": {"$create": "taxon:32630"},
        "accession": "L09137.2",
        "bases": BASES,
        "topology": "circular",
        "features": [
            {
                "type": "promoter",
                "start": 5,
                "end": 20,
                "strand": "reverse",
                "name": "lac promoter",
                "qualifiers": {
                    "label": "lac promoter",
                    "note": "a long note that wraps onto the next line",
                },
            },
            {
                # Across the origin of the circular plasmid.
                "type": "CDS",
                "start": 50,
                "end": 10,
                "strand": "forward",
                "parts": [
                    {"start": 50, "end": 60, "strand": "forward"},
                    {"start": 1, "end": 10, "strand": "forward"},
                ],
                "name": "bla",
                "qualifiers": {
                    "gene": "bla",
                    "translation": "MSIQHFRVALIPFFAAF",
                    "location": "join(50..60,1..10)",
                },
            },
        ],
    }
    record = GENBANK.encode()
    assert first["origin"] == {
        "source": "GenBank file",
        "id": "pDemo",
        "version": "L09137.2",
        "file": FILE_ID,
        "sha256": hashlib.sha256(record).hexdigest(),
    }
    assert second["values"]["name"] == "pDemo2"
    assert output["metrics"] == {"records": 2}
    # The feature on another record is left out, and said so.
    assert output["messages"][0] == {
        "level": "warning",
        "text": "Record 1 (pDemo): a misc_feature on another record (J00194.1:100..202) "
        "was left out.",
    }
    assert result.stdout.splitlines()[-1] == "::progress 1.00 2 records"


def with_feature(location: str, *, length: int = 100, topology: str = "circular") -> str:
    """A GenBank record of `length` bases with one feature at `location`."""
    bases = ("acgt" * length)[:length]
    origin = "".join(
        f"{i + 1:>9} {' '.join(bases[j : j + 10] for j in range(i, min(i + 60, length), 10))}\n"
        for i in range(0, length, 60)
    )
    return (
        f"LOCUS       pLoc  {length} bp    DNA     {topology} SYN 11-NOV-2026\n"
        "FEATURES             Location/Qualifiers\n"
        f"     misc_feature    {location}\n"
        f"ORIGIN\n{origin}//\n"
    )


@pytest.mark.parametrize(
    ("location", "topology", "expected"),
    [
        # INSDC's feature table: a join covers its parts, not what lies between them.
        (
            "join(2..4,10..12)",
            "linear",
            {
                "start": 2,
                "end": 12,
                "strand": "forward",
                "parts": [
                    {"start": 2, "end": 4, "strand": "forward"},
                    {"start": 10, "end": 12, "strand": "forward"},
                ],
            },
        ),
        # The reverse strand's join across the origin: 16 bases, read 5 to 1 then 100 to 90.
        (
            "complement(join(90..100,1..5))",
            "circular",
            {
                "start": 90,
                "end": 5,
                "strand": "reverse",
                "parts": [
                    {"start": 1, "end": 5, "strand": "reverse"},
                    {"start": 90, "end": 100, "strand": "reverse"},
                ],
            },
        ),
        # A site between two bases covers neither.
        (
            "5^6",
            "linear",
            {
                "start": 5,
                "end": 6,
                "strand": "forward",
                "parts": [{"start": 5, "end": 6, "strand": "forward", "between": True}],
            },
        ),
        # Parts out of order on a circle don't make it cross the origin.
        (
            "join(10..12,2..4)",
            "circular",
            {
                "start": 2,
                "end": 12,
                "strand": "forward",
                "parts": [
                    {"start": 10, "end": 12, "strand": "forward"},
                    {"start": 2, "end": 4, "strand": "forward"},
                ],
            },
        ),
        # Parts on both strands, as trans-splicing has.
        (
            "join(1..5,complement(10..20))",
            "linear",
            {
                "start": 1,
                "end": 20,
                "strand": "mixed",
                "parts": [
                    {"start": 1, "end": 5, "strand": "forward"},
                    {"start": 10, "end": 20, "strand": "reverse"},
                ],
            },
        ),
        # A plain range is its start and end alone.
        ("1..9", "linear", {"start": 1, "end": 9, "strand": "forward"}),
        ("complement(7)", "linear", {"start": 7, "end": 7, "strand": "reverse"}),
        # INSDC's < and >: an end beyond the base it names, so 2..9 is only what's known.
        (
            "<2..>9",
            "linear",
            {
                "start": 2,
                "end": 9,
                "strand": "forward",
                "parts": [
                    {
                        "start": 2,
                        "end": 9,
                        "strand": "forward",
                        "partial_start": True,
                        "partial_end": True,
                    }
                ],
            },
        ),
        # SV40's VP1 along its other strand, partial at its 5' end (its start, read 3' to 5').
        (
            "complement(2651..>3745)",
            "circular",
            {
                "start": 2651,
                "end": 3745,
                "strand": "reverse",
                "parts": [{"start": 2651, "end": 3745, "strand": "reverse", "partial_end": True}],
            },
        ),
        # INSDC's order(): found in that order, nothing said of joining them.
        (
            "order(2..4,10..12)",
            "linear",
            {
                "start": 2,
                "end": 12,
                "strand": "forward",
                "operator": "order",
                "parts": [
                    {"start": 2, "end": 4, "strand": "forward"},
                    {"start": 10, "end": 12, "strand": "forward"},
                ],
            },
        ),
    ],
)
def test_a_features_parts_say_where_it_lies(
    tmp_path: Path, location: str, topology: str, expected: dict[str, Any]
) -> None:
    length = 5243 if "3745" in location else 100
    result, output = imported(tmp_path, with_feature(location, topology=topology, length=length))
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    (feature,) = made(output)[0]["values"]["features"]
    feature.pop("qualifiers", None)
    assert feature == {"type": "misc_feature", **expected}


@pytest.mark.parametrize(
    "location", ["5^7", "1.5", "join(1..5,200..210)", "join(1..2,order(3..4,5..6))"]
)
def test_a_location_that_isnt_one_on_the_sequence_is_left_out_saying_so(
    tmp_path: Path, location: str
) -> None:
    # 5^7 isn't between neighbours; 1.5 (one base somewhere in 1 to 5) is too vague to
    # draw; INSDC doesn't let join and order nest.
    result, output = imported(tmp_path, with_feature(location, topology="linear"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    assert "features" not in made(output)[0]["values"]
    assert output["messages"][0]["text"] == (
        f"Record 1 (pLoc): a misc_feature at {location!r} was left out."
    )


@pytest.mark.parametrize(
    "location", ["join(90..100,1..5)", "complement(join(90..100,1..5))", "100^1"]
)
def test_a_location_across_the_origin_is_only_on_a_circular_molecule(
    tmp_path: Path, location: str
) -> None:
    """A join that runs off the end of a record and back to base 1, or a site between its
    last base and its first, says the molecule is a circle. On a record whose LOCUS line
    says `linear` the two can't both be right, so the feature is refused, with what is
    wrong with it: a linear molecule has two ends, and nothing of it reads across them.
    The same location on a circular record is kept, and crosses its origin."""
    result, output = imported(tmp_path, with_feature(location, topology="linear"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    assert "features" not in made(output)[0]["values"]
    assert output["messages"][0]["text"] == (
        f"Record 1 (pLoc): a misc_feature at {location!r} reads across the origin, which only "
        "a circular molecule has, and its LOCUS line says linear. It was left out: say the "
        "molecule is circular, or give the feature's place on the linear one."
    )
    # On a circle it is a feature across the origin, which is kept as it is written.
    result, output = imported(tmp_path, with_feature(location, topology="circular"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    (feature,) = made(output)[0]["values"]["features"]
    assert (feature["start"], feature["end"]) == (
        90 if "90" in location else 100,
        1 if location == "100^1" else 5,
    )
    assert output["messages"] == []


def test_a_records_topology_is_the_one_its_locus_line_gives(tmp_path: Path) -> None:
    """INSDC X01095.1 (NCBI nuccore): pGKL1, the linear killer plasmid of Kluyveromyces
    lactis, whose LOCUS line reads `8874 bp DNA linear PLN` and whose feature table gives
    the terminal inverted repeats at its two ends. Its topology comes from that line, not
    from the `topology` param, which only says what a FASTA record can't: a record that
    says it is linear is imported as linear even when the param asks for circles, since
    the record knows and the param is a default for files that don't say.
    """
    text = (Path(__file__).parent / "data" / "plasmids" / "X01095.1.gb").read_text(encoding="utf-8")
    result, output = imported(tmp_path, text, topology="circular")
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    (record,) = made(output)
    values = record["values"]
    assert values["topology"] == "linear"
    # Its LOCUS says `DNA`, not `ds-DNA`, so the record says nothing of its strands and
    # the field's own default, double, stands.
    assert "strandedness" not in values
    assert len(values["bases"]) == 8874
    assert values["accession"] == "X01095.1"
    repeats = [f for f in values["features"] if f["type"] == "repeat_region"]
    assert [(f["start"], f["end"]) for f in repeats] == [(1, 202), (8673, 8874)]
    # Nothing of it reads across its ends: every feature's end is at or after its start.
    assert all(f["end"] >= f["start"] for f in values["features"])
    assert output["messages"] == []


def test_an_organism_keeps_ncbis_brackets(tmp_path: Path) -> None:
    """GenBank AB020730.1 (NCBI nuccore, fetched 2026-11-19): a 16S rRNA gene of
    [Clostridium] scindens (NCBI Taxonomy 29347), bracketed as NCBI writes a genus it hasn't
    placed."""
    text = (Path(__file__).parent / "data" / "AB020730.gb").read_text(encoding="utf-8")
    result, output = imported(tmp_path, text)
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    organism, record = output["creates"]
    assert organism["values"] == {"taxon_id": 29347, "scientific_name": "[Clostridium] scindens"}
    assert record["values"]["organism"] == {"$create": "taxon:29347"}
    assert output["messages"] == []


def test_a_record_with_fewer_bases_than_its_locus_says_fails_the_import(tmp_path: Path) -> None:
    """AB020730.1 again, its LOCUS line saying 1459 bp (NCBI's record has 1,459 bases: 24
    full lines of 60, then 19). With one line of its ORIGIN gone but its `//` kept, it
    would be registered under AB020730.1 as a sequence 60 bases short of it."""
    text = (Path(__file__).parent / "data" / "AB020730.gb").read_text(encoding="utf-8")
    line = next(x for x in text.splitlines(keepends=True) if x.startswith("      661 "))
    result, output = imported(tmp_path, text.replace(line, ""))
    assert result.returncode == 1
    assert output is None  # nothing is half imported
    assert result.stdout.splitlines()[-1] == (
        "::error seqs.txt: Record 1 (AB020730) has 1399 bases, but its LOCUS line says it "
        "has 1459: is its ORIGIN cut short?"
    )


@pytest.mark.parametrize(
    ("taxon", "name"),
    [
        (1263066, "[Clostridium] clostridioforme CAG:511"),
        (5478, "[Candida] glabrata"),
        (551115, "'Nostoc azollae' 0708"),
        (10090, "Mus musculus"),
    ],
)
def test_an_organisms_name_is_kept_as_ncbi_writes_it(tmp_path: Path, taxon: int, name: str) -> None:
    text = GENBANK.replace("synthetic construct", name).replace("taxon:32630", f"taxon:{taxon}")
    organism = imported(tmp_path, text)[1]["creates"][0]  # type: ignore[index]
    assert organism["values"] == {"taxon_id": taxon, "scientific_name": name}


def test_an_organism_whose_name_cant_be_kept_is_its_taxon_saying_so(tmp_path: Path) -> None:
    # GenBank writes "." for a field it has nothing for.
    text = GENBANK.replace("synthetic construct", ".")
    result, output = imported(tmp_path, text)
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    assert output["creates"][0]["values"] == {"taxon_id": 32630}
    assert (
        "Record 1 (pDemo): its organism's name, ., isn't one an organism's scientific name "
        "can be, so its organism has only its NCBI Taxonomy ID, 32630."
    ) in [m["text"] for m in output["messages"]]


@pytest.mark.parametrize(
    ("molecule", "strandedness"),
    [
        ("ss-DNA", "single"),
        ("ds-DNA", "double"),
        # GenBank writes an mRNA in the T alphabet: kept as its cDNA's bases, one strand.
        ("mRNA", "single"),
        ("RNA", "single"),
        ("ds-RNA", "double"),
        # Plain DNA doesn't say: the field's default stands.
        ("DNA", None),
    ],
)
def test_locus_says_how_many_strands_the_molecule_has(
    tmp_path: Path, molecule: str, strandedness: str | None
) -> None:
    text = GENBANK.replace("60 bp    DNA     circular", f"60 bp    {molecule:<7} circular")
    result, output = imported(tmp_path, text)
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    assert made(output)[0]["values"].get("strandedness") == strandedness


SOURCE = """\
     source          1..60
                     /organism="synthetic construct"
                     /mol_type="other DNA"
                     /db_xref="taxon:32630"
"""


def sources(*blocks: str) -> str:
    """GENBANK with these source features in place of its one."""
    return GENBANK.replace(SOURCE, "".join(blocks))


def source(location: str, organism: str, taxon: int, *marks: str) -> str:
    lines = [
        f"     source          {location}",
        f'                     /organism="{organism}"',
        '                     /mol_type="genomic DNA"',
        f'                     /db_xref="taxon:{taxon}"',
        *(f"                     /{mark}" for mark in marks),
    ]
    return "\n".join(lines) + "\n"


HUMAN_SEGMENT = source("1..20", "Homo sapiens", 9606)


@pytest.mark.parametrize(
    ("given", "organism"),
    [
        # INSDC's /focus marks the main organism of a record with several, whatever the
        # order: a human segment listed first doesn't make the mouse record human.
        (
            sources(HUMAN_SEGMENT, source("1..60", "Mus musculus", 10090, "focus")),
            (10090, "Mus musculus"),
        ),
        # /transgenic marks the organism that took the DNA in: the main one.
        (
            sources(HUMAN_SEGMENT, source("1..60", "Mus musculus", 10090, "transgenic")),
            (10090, "Mus musculus"),
        ),
        # With neither, the one source that spans the whole record: a vector with an E.
        # coli segment, listed first.
        (
            sources(
                source("21..40", "Escherichia coli", 562),
                source("1..60", "synthetic construct", 32630),
            ),
            (32630, "synthetic construct"),
        ),
    ],
)
def test_a_records_organism_is_its_main_source_name_and_taxon_together(
    tmp_path: Path, given: str, organism: tuple[int, str]
) -> None:
    result, output = imported(tmp_path, given)
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    (made_organism,) = [c for c in output["creates"] if c["type"] == "iki/core.Organism"]
    taxon, name = organism
    assert made_organism["values"] == {"taxon_id": taxon, "scientific_name": name}
    (sequence,) = made(output)
    assert sequence["values"]["organism"] == {"$create": f"taxon:{taxon}"}
    # The other source stays, as a feature named by its organism, with its span and taxon.
    (other,) = [f for f in sequence["values"]["features"] if f["type"] == "source"]
    assert other["name"] in ("Homo sapiens", "Escherichia coli")
    assert (other["start"], other["end"]) in ((1, 20), (21, 40))
    assert other["qualifiers"]["db_xref"] in ("taxon:9606", "taxon:562")


def test_a_record_with_no_main_source_leaves_its_organism_out_saying_why(
    tmp_path: Path,
) -> None:
    """A human-mouse chimera with neither marked, nor one the whole of it: which organism
    it's of isn't said, so none is taken, and both sources are kept as features."""
    chimera = sources(
        source("1..30", "Homo sapiens", 9606), source("31..60", "Mus musculus", 10090)
    )
    result, output = imported(tmp_path, chimera)
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    assert [c["type"] for c in output["creates"]] == ["iki/dna.DNASequence"]
    (sequence,) = made(output)
    assert "organism" not in sequence["values"]
    kept = [(f["name"], f["start"], f["end"]) for f in sequence["values"]["features"]]
    assert kept[:2] == [("Homo sapiens", 1, 30), ("Mus musculus", 31, 60)]
    assert output["messages"][0] == {
        "level": "warning",
        "text": "Record 1 (pDemo): its organism is left out: of its 2 source features, not "
        "one alone is marked as its main organism (/focus or /transgenic) or spans the whole "
        "record. Each is kept as a feature.",
    }


#: A RefSeq model's COMMENT, as NCBI writes it (XM_090324667.1, cut down).
MODEL_COMMENT = """\
COMMENT     MODEL REFSEQ:  This record is predicted by automated computational
            analysis. This record is derived from a genomic sequence
            (NC_143544) annotated using gene prediction method: Gnomon.
            Also see:
                Documentation of NCBI's Annotation Process

            ##Genome-Annotation-Data-START##
            Annotation Provider         :: NCBI RefSeq
            Annotation Status           :: Full annotation
            Annotation Pipeline         :: NCBI Eukaryotic Genome Annotation
                                           Pipeline (EGAP)
            Annotation Software Version :: 10.6
            Annotation Method           :: Gnomon; cmsearch; tRNAscan-SE
            ##Genome-Annotation-Data-END##
"""
MODEL = GENBANK.replace("FEATURES ", MODEL_COMMENT + "FEATURES ")
#: What it says made it: NCBI's prediction.
PREDICTED = {
    "kind": "computational",
    "method": "gene prediction",
    "software": [
        {"name": "Gnomon"},
        {"name": "NCBI Eukaryotic Genome Annotation Pipeline (EGAP)", "version": "10.6"},
    ],
}


def test_a_genbank_record_says_if_its_single_stranded(tmp_path: Path) -> None:
    # Its strands are part of its identity (ADR 0138): M13's genome is a single strand.
    single = GENBANK.replace("60 bp    DNA     circular", "60 bp ss-DNA     circular")
    result, output = imported(tmp_path, single + "\n" + GENBANK.replace("pDemo", "pDemo2"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    first, second = made(output)
    assert first["values"]["strandedness"] == "single"
    assert "strandedness" not in second["values"]


def test_a_refseq_models_record_says_ncbi_predicted_it(tmp_path: Path) -> None:
    result, output = imported(tmp_path, MODEL + "\n" + GENBANK)
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    model, other = made(output)
    assert model["origin"]["made"] == PREDICTED
    # A record that doesn't say how its sequence was made gets nothing.
    assert "made" not in other["origin"]
    # A COMMENT that isn't a model's says nothing of how either.
    result, output = imported(
        tmp_path, GENBANK.replace("FEATURES ", "COMMENT     Sequenced by the lab.\nFEATURES ")
    )
    assert output is not None
    assert "made" not in made(output)[0]["origin"]


def test_fasta_records_take_their_names_and_the_topology_given(tmp_path: Path) -> None:
    text = ">gb|L09137.2| pUC19 cloning vector\nACGTACGT\nacgtn\n\n>seq2\nGGCC\n"
    result, output = imported(tmp_path, text, name="vectors.fa", topology="circular")
    assert result.returncode == 0, result.stderr
    assert output is not None
    first, second = (c["values"] for c in made(output))
    assert first == {
        "name": "L09137.2",
        "description": "pUC19 cloning vector",
        "accession": "L09137.2",
        "bases": "ACGTACGTACGTN",
        "topology": "circular",
    }
    assert second == {"name": "seq2", "bases": "GGCC", "topology": "circular"}
    origin = made(output)[1]["origin"]
    assert (origin["source"], origin["id"]) == ("FASTA file", "seq2")
    assert origin["sha256"] == hashlib.sha256(b">seq2\nGGCC\n").hexdigest()
    # RefSeq's accessions, NZ_ ones too (NCBI's RefSeq accession formats).
    for header, accession in [
        ("ref|NZ_CP009273.1|", "NZ_CP009273.1"),
        ("ref|NM_000546.6|", "NM_000546.6"),
        ("NZ_AAAA01000001.1", "NZ_AAAA01000001.1"),
    ]:
        _, output = imported(tmp_path, f">{header} a record\nACGT\n", name="vectors.fa")
        assert output is not None
        assert made(output)[0]["values"]["accession"] == accession


NEITHER = (
    "seqs.txt: It isn't FASTA (records start with '>'), GenBank (records start with LOCUS) "
    "or a SnapGene file."
)


def test_a_large_file_is_read_a_line_at_a_time_into_a_creates_file(tmp_path: Path) -> None:
    count = 2500
    text = "".join(f">seq{n} number {n}\n{BASES[: 10 + n % 40]}\n" for n in range(count))
    result, output = imported(tmp_path, text, name="many.fa")
    assert result.returncode == 0, result.stdout
    assert output is not None
    assert "creates" not in output
    assert output["creates_file"] == {"$file": "creates.ndjson"}
    assert output["metrics"] == {"records": count}
    lines = (tmp_path / "out" / "creates.ndjson").read_text().splitlines()
    assert len(lines) == count
    issues = [schemas.check("run-output", json.loads(line), part="create") for line in lines]
    assert not any(issues)
    last = json.loads(lines[-1])
    assert last["values"]["name"] == f"seq{count - 1}"
    assert last["values"]["description"] == f"number {count - 1}"
    assert last["origin"]["id"] == f"seq{count - 1}"
    # It says how far it's got as it goes, and the log stays short.
    assert "::progress 0.39 1,000 records" in result.stdout
    assert "… and 1,500 more records." in result.stdout


def test_a_few_records_go_in_output_json(tmp_path: Path) -> None:
    _, output = imported(tmp_path, ">a\nACGT\n>b\nGGCC\n")
    assert output is not None
    assert [c["values"]["name"] for c in made(output)] == ["a", "b"]
    assert not (tmp_path / "out" / "creates.ndjson").exists()


@pytest.mark.parametrize(
    ("text", "why"),
    [
        (">one\nACGU\n", "seqs.txt: Record 1 (one) has U at base 4: it's RNA, not DNA."),
        (
            ">one\nAC--GT\n",
            "seqs.txt: Record 1 (one) has a gap (-) at base 3: import unaligned sequences.",
        ),
        (
            ">a\nACGT\n>b\nACJT\n",
            "seqs.txt: Record 2 (b) has 'J' at base 3, which isn't an IUPAC base.",
        ),
        (">empty\n\n", "seqs.txt: Record 1 (empty) has no bases."),
        (
            "ACGT\n",
            NEITHER,
        ),
        (
            "",
            NEITHER,
        ),
        (
            GENBANK.replace("//\n", ""),
            "seqs.txt: Record 1 doesn't end with '//': is the file cut short?",
        ),
        (
            GENBANK.replace("60 bp", "59 bp"),
            "seqs.txt: Record 1 (pDemo) has 60 bases, but its LOCUS line says it has 59: "
            "is its LOCUS line wrong?",
        ),
        # GenBank's ms-: mixed strands, which neither single nor double would say. As
        # either, it would be registered as another molecule.
        (
            GENBANK.replace("60 bp    DNA    ", "60 bp    ms-DNA "),
            "seqs.txt: Record 1 (pDemo) is mixed-stranded (LOCUS says ms-), partly single "
            "and partly double, which a DNA sequence's strandedness can't say yet: as either, "
            "it would be registered as another molecule. Leave it out of what you import.",
        ),
    ],
)
def test_a_record_that_cant_be_read_fails_the_import_saying_why(
    tmp_path: Path, text: str, why: str
) -> None:
    result, output = imported(tmp_path, text)
    assert result.returncode == 1
    assert output is None  # nothing is half imported
    assert result.stdout.splitlines()[-1] == f"::error {why}"


# --- Fetching from NCBI --------------------------------------------------------------------------

NCBI = SCRIPT.with_name("ncbi.py")


class FakeNcbi:
    """Answers efetch requests with GenBank records it holds, by accession."""

    def __init__(self, *failures: int) -> None:
        self.records = {
            "L09137.2": GENBANK,
            "NM_000546.6": GENBANK.replace("pDemo", "TP53")
            .replace("L09137", "NM_000546")
            .replace(".2\n", ".6\n"),
        }
        self.failures = list(failures)
        self.asked: list[dict[str, list[str]]] = []

    def __call__(self, request: Any, timeout: float) -> Any:
        import io
        import urllib.error
        import urllib.parse

        # Nothing in the URL: a key would be in access logs.
        assert request.full_url == "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
        assert timeout <= 120
        self.asked.append(urllib.parse.parse_qs(request.data.decode()))
        if self.failures:
            code = self.failures.pop(0)
            raise urllib.error.HTTPError(request.full_url, code, "Slow down", {}, io.BytesIO())  # type: ignore[arg-type]
        wanted = self.asked[-1]["id"][0].split(",")
        found = [
            text
            for accession, text in self.records.items()
            if accession in wanted or accession.split(".")[0] in wanted
        ]
        return io.BytesIO("\n".join(found).encode())


@pytest.fixture
def ncbi(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """The fetch script, loaded here with NCBI faked."""
    import importlib
    import time
    import urllib.request

    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    module = importlib.import_module("ncbi")
    fake = FakeNcbi()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    monkeypatch.setenv("IKI_INPUT", str(tmp_path / "input.json"))
    monkeypatch.setenv("IKI_OUTPUT", str(tmp_path / "out.json"))
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    return SimpleNamespace(script=module, fake=fake, slept=slept)


def fetched(
    tmp_path: Path, ncbi: Any, accessions: str, email: str | None = "lab@example.com"
) -> dict[str, Any]:
    settings = {"ncbi_email": email} if email else {}
    run_input = {"params": {"accessions": accessions}, "settings": settings}
    (tmp_path / "input.json").write_text(json.dumps(run_input))
    ncbi.script.main()
    output: dict[str, Any] = json.loads((tmp_path / "out.json").read_text())
    assert not list(schemas.check("run-output", output))
    return output


def test_fetches_records_from_ncbi_and_says_which_version_and_when(
    tmp_path: Path, ncbi: Any
) -> None:
    output = fetched(tmp_path, ncbi, "nm_000546, L09137.2\nNM_000546")
    (asked,) = ncbi.fake.asked
    assert asked == {
        "db": ["nuccore"],
        "id": ["NM_000546,L09137.2"],
        "rettype": ["gbwithparts"],
        "retmode": ["text"],
        "tool": ["iki-dna"],
        "email": ["lab@example.com"],
    }
    tp53, demo = made(output)
    assert (tp53["values"]["name"], demo["values"]["name"]) == ("TP53", "pDemo")
    assert tp53["values"]["accession"] == "NM_000546.6"
    origin = tp53["origin"]
    assert {k: origin[k] for k in ("source", "id", "version", "url")} == {
        "source": "NCBI Nucleotide",
        "id": "NM_000546",
        "version": "NM_000546.6",
        "url": "https://www.ncbi.nlm.nih.gov/nuccore/NM_000546.6",
    }
    assert origin["retrieved_at"].endswith("+00:00")
    assert origin["sha256"] == hashlib.sha256(ncbi.fake.records["NM_000546.6"].encode()).hexdigest()
    assert output["metrics"] == {"records": 2}
    assert "made" not in origin


def test_fetches_refseq_accessions_of_every_form(tmp_path: Path, ncbi: Any) -> None:
    # NCBI's RefSeq accession formats: NZ_ before an INSDC accession, versioned or not.
    ncbi.fake.records["NZ_CP009273.1"] = (
        GENBANK.replace("pDemo", "BW25113").replace("L09137", "NZ_CP009273").replace(".2\n", ".1\n")
    )
    output = fetched(tmp_path, ncbi, "NZ_CP009273.1 nz_cp009273")
    assert ncbi.fake.asked[0]["id"] == ["NZ_CP009273.1,NZ_CP009273"]
    assert made(output)[0]["values"]["accession"] == "NZ_CP009273.1"


def test_a_fetched_refseq_model_says_ncbi_predicted_it(tmp_path: Path, ncbi: Any) -> None:
    ncbi.fake.records["L09137.2"] = MODEL
    output = fetched(tmp_path, ncbi, "L09137.2")
    assert made(output)[0]["origin"]["made"] == PREDICTED


def test_asks_ncbi_in_batches_no_faster_than_it_allows(
    tmp_path: Path, ncbi: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ncbi.script, "BATCH", 1)
    fetched(tmp_path, ncbi, "L09137.2 NM_000546.6")
    assert [a["id"] for a in ncbi.fake.asked] == [["L09137.2"], ["NM_000546.6"]]
    assert ncbi.slept == [0.34]


def test_an_api_key_goes_in_the_body_and_lets_it_ask_faster(
    tmp_path: Path, ncbi: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ncbi.script, "BATCH", 1)
    monkeypatch.setenv("NCBI_API_KEY", "k3y")
    fetched(tmp_path, ncbi, "L09137.2 NM_000546.6")
    assert [a["api_key"] for a in ncbi.fake.asked] == [["k3y"], ["k3y"]]
    assert ncbi.slept == [0.1]


def test_waits_and_asks_again_when_ncbi_says_to_slow_down(tmp_path: Path, ncbi: Any) -> None:
    ncbi.fake.failures = [429, 503]
    output = fetched(tmp_path, ncbi, "L09137")
    assert len(ncbi.fake.asked) == 3
    assert ncbi.slept == [2, 4]
    assert made(output)[0]["origin"]["version"] == "L09137.2"


@pytest.mark.parametrize(
    ("accessions", "email", "error"),
    [
        ("L09137 X99999", "lab@example.com", "::error NCBI has no nucleotide record X99999."),
        (
            "L09137 not-one",
            "lab@example.com",
            "::error NOT-ONE isn't a nucleotide accession, such as NM_000546.6 or L09137.",
        ),
        (" , ", "lab@example.com", "::error Give at least one accession, such as NM_000546.6."),
        (
            "L09137",
            None,
            "::error The dna plugin's ncbi_email setting isn't set: NCBI asks automated "
            "clients for an address. Ask your Iki administrator to set it.",
        ),
    ],
)
def test_a_fetch_fails_whole_and_says_why(
    tmp_path: Path,
    ncbi: Any,
    capsys: pytest.CaptureFixture[str],
    accessions: str,
    email: str | None,
    error: str,
) -> None:
    with pytest.raises(SystemExit) as stopped:
        fetched(tmp_path, ncbi, accessions, email)
    assert stopped.value.code == 1
    assert capsys.readouterr().out.splitlines()[-1] == error
    assert not (tmp_path / "out.json").exists()


def test_a_fetch_needs_the_contact_address_before_it_runs() -> None:
    """Fetching names `ncbi_email` as a setting it can't run without (ADR 0169), so on a
    deployment without it the fetch isn't offered and a launch is refused before it's
    queued, the script's own check left as a backstop; nothing else the plugin does needs
    it. The dev cluster sets it, so it can fetch."""
    from iki_spec.validate import check_settings_text, load_plugin

    plugin = load_plugin(DNA, dependencies=FIRST_PARTY[DNA]).plugin
    assert plugin is not None
    needing = {a.name for a in plugin.actions.values() if a.requires_settings}
    assert needing == {"fetch_sequences"}
    assert plugin.actions["fetch_sequences"].requires_settings == ("ncbi_email",)
    (unset,) = check_settings_text(plugin, "", "dna.yaml")
    assert unset.code == "setting-needed"
    assert unset.message == (
        "`ncbi_email` isn't set, so Fetch DNA sequences from NCBI can't run on this deployment."
    )
    dev = SCRIPT.parents[3] / "deploy" / "dev" / "settings" / "dna.yaml"
    assert check_settings_text(plugin, dev.read_text(), dev.name) == []


def test_a_placeholder_accession_is_no_accession(tmp_path: Path) -> None:
    """Benchling and Biopython write `ACCESSION   .` and `VERSION     .` for a record with
    none: the dot says there isn't one, so the record has no accession."""
    text = GENBANK.replace("ACCESSION   L09137", "ACCESSION   .").replace(
        "VERSION     L09137.2", "VERSION     ."
    )
    result, output = imported(tmp_path, text)
    assert result.returncode == 0, result.stdout
    assert output is not None
    (record,) = made(output)
    assert "accession" not in record["values"]
    assert "version" not in record["origin"]
