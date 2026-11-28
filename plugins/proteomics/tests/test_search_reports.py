"""Check the report adapters on the producers' own tables (tests/data, with their sources
in its README) and on small, explicitly synthetic ones."""

import json
import re
import runpy
from pathlib import Path

import pytest

SCRIPT = runpy.run_path(str(Path(__file__).parents[1] / "scripts/import_search.py"))


def test_maxquant_abundance_zero_means_missing_but_ratio_zero_stays_numeric():
    rows = [
        "Sequence\tProteins\tIntensity sample A\tLFQ intensity A\tiBAQ\t"
        "Intensity B\tRatio H/L\tReverse",
        "PEPTIDE\tsp|P00330|ADH1_YEAST;P12345\t0\t0.0\t0\t2.5\t0\t+",
    ]
    (row,) = SCRIPT["parsed"]("maxquant_txt", rows)
    assert row["quantities"] == {
        "Intensity sample A": None,
        "LFQ intensity A": None,
        "iBAQ": None,
        "Intensity B": 2.5,
        "Ratio H/L": 0.0,
    }
    assert row["accessions"] == ["P00330", "P12345"]
    assert row["level"] == "peptide"
    assert row["decoy"]


def test_diann_keeps_two_precursors_and_one_shared_protein_group_quantity():
    rows = [
        "Run\tProtein.Group\tStripped.Sequence\tPrecursor.Id\tPrecursor.Quantity\tPG.MaxLFQ",
        "A\tP00330\tPEPTIDE\tPEPTIDE2\t20\t123",
        "A\tP00330\tPEPTIDE\tPEPTIDE3\t30\t123",
    ]
    first, group, second = SCRIPT["parsed"]("diann_tsv", rows)
    assert first["quantities"] == {"Precursor.Quantity": 20.0}
    assert second["quantities"] == {"Precursor.Quantity": 30.0}
    assert group["quantities"] == {"PG.MaxLFQ": 123.0}
    assert group["level"] == "protein_group"


def test_fragpipe_preserves_peptide_and_protein_intensity_labels():
    peptide = ["Peptide Sequence\tProtein ID\tA Intensity", "PEPTIDE\tP00330\t2.5"]
    protein = ["Protein ID\tA MaxLFQ Intensity\tA Spectral Count", "P00330\t9\t1"]
    (peptide_hit,) = SCRIPT["parsed"]("fragpipe_tsv", peptide)
    (protein_hit,) = SCRIPT["parsed"]("fragpipe_tsv", protein)
    assert peptide_hit["quantities"] == {"A Intensity": 2.5}
    assert protein_hit["quantities"] == {"A MaxLFQ Intensity": 9.0, "A Spectral Count": 1.0}


def test_result_keeps_raw_references_and_the_downloadable_artifact(tmp_path, monkeypatch):
    source = tmp_path / "synthetic.tsv"
    source.write_text("Protein IDs\tIntensity\nP00330;P12345\t42\n")
    run = {
        "params": {"format": "maxquant_txt", "reports": ["report"], "version": "synthetic"},
        "inputs": {
            "dataset": [{"id": "dataset", "revision": 3, "values": {"raw_files": ["raw"]}}],
            "proteins": [
                {
                    "id": "project-record",
                    "entry_id": "registry-entry",
                    "values": {"uniprot_accession": "P00330"},
                },
            ],
        },
        "files": {"report": {"name": source.name, "path": str(source), "sha256": "synthetic"}},
    }
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(run))
    output = tmp_path / "output.json"
    artifacts = tmp_path / "out"
    artifacts.mkdir()
    monkeypatch.setenv("IKI_INPUT", str(input_path))
    monkeypatch.setenv("IKI_OUTPUT", str(output))
    monkeypatch.setenv("IKI_OUT_DIR", str(artifacts))

    SCRIPT["main"]()

    artifact = artifacts / "identifications.jsonl"
    result = json.loads(output.read_text())["creates"][0]
    assert result["derived_from"] == ["dataset", "project-record"]
    assert result["values"]["dataset_revision"] == 3
    assert result["values"]["raw_files"] == ["raw"]
    assert result["values"]["proteins"] == ["registry-entry"]
    assert json.loads(artifact.read_text())["registered_proteins"] == ["registry-entry"]


def test_invalid_columns_point_to_the_report_problem():
    with pytest.raises(ValueError, match="Choose MaxQuant"):
        list(SCRIPT["parsed"]("maxquant_txt", ["Unexpected\tHeader", "row\t1"]))


def imported(tmp_path, format_, tables, accessions=()):
    files = {}
    for i, rows in enumerate(tables):
        source = tmp_path / f"synthetic-{i}.tsv"
        source.write_text("\n".join(rows) + "\n")
        files[str(i)] = {"name": source.name, "path": str(source), "sha256": str(i)}
    artifact = tmp_path / "identifications.jsonl"
    run = {
        "params": {"format": format_, "reports": list(files), "version": "synthetic"},
        "inputs": {
            "dataset": [{"id": "dataset", "revision": 1, "values": {}}],
            "proteins": [
                {"id": accession, "values": {"uniprot_accession": accession}}
                for accession in accessions
            ],
        },
        "files": files,
    }
    [record] = SCRIPT["result"](run, artifact)["creates"]
    return record["values"], [json.loads(line) for line in artifact.read_text().splitlines()]


def test_mztab_keeps_decoy_flags_and_only_links_target_evidence(tmp_path):
    rows = [
        "MTD\tmzTab-version\t1.0.0",
        "PRH\taccession\topt_global_cv_PRIDE:0000303_decoy_hit",
        "PRT\tP00330\t1",
        "PEH\tsequence\taccession\topt_global_cv_MS:1002217_decoy_peptide",
        "PEP\tPEPTIDR\tP00330\ttrue",
        "PSH\tsequence\tPSM_ID\taccession\topt_global_cv_MS:1002217_decoy_peptide",
        "PSM\tPEPTIDR\t1\tP00330\t1",
        "PSM\tPEPTIDE\t2\tP12345\t0",
    ]
    result, reported = imported(tmp_path, "mztab_1_0", [rows], ["P00330", "P12345"])
    assert [row["decoy"] for row in reported] == [True, True, True, False]
    assert [row["registered_proteins"] for row in reported] == [[], [], [], ["P12345"]]
    assert result["proteins"] == ["P12345"]
    assert result["summary"]["counts"] == {"spectrum_match": 1}
    assert result["summary"]["excluded_counts"] == {
        "decoy": {"protein_group": 1, "peptide": 1, "spectrum_match": 1}
    }


def test_mztab_missing_calculation_error_and_numeric_zero_are_distinct(tmp_path):
    rows = [
        "MTD\tmzTab-version\t1.0.0",
        "PRH\taccession\tprotein_abundance_assay[1]\tprotein_abundance_assay[2]\t"
        "protein_abundance_assay[3]\tprotein_abundance_assay[4]",
        "PRT\tP00330\tnull\tNaN\tINF\t0",
    ]
    _, [row] = imported(tmp_path, "mztab_1_0", [rows])
    assert list(row["quantities"].values()) == [None, "NaN", "INF", 0.0]


@pytest.mark.parametrize("decoy_first", [True, False])
def test_diann_group_quantities_come_from_target_precursors(decoy_first):
    target = "A\tP00330\tPEPTIDE\tPEPTIDE2\t20\t123\t0"
    decoy = "A\tP00330\tPEPTIDR\tPEPTIDR2\t99\t999\t1"
    rows = [
        "Run\tProtein.Group\tStripped.Sequence\tPrecursor.Id\tPrecursor.Quantity\tPG.MaxLFQ\tDecoy",
        *((decoy, target) if decoy_first else (target, decoy)),
    ]
    found = list(SCRIPT["parsed"]("diann_tsv", rows))
    [group] = [row for row in found if row["level"] == "protein_group"]
    assert not group.get("decoy")
    assert group["quantities"] == {"PG.MaxLFQ": 123.0}
    precursors = [row for row in found if row["level"] == "precursor"]
    assert [row["decoy"] for row in precursors] == [decoy_first, not decoy_first]


@pytest.mark.parametrize(
    "rows",
    [
        ["Spectrum\tPeptide\tCharge\tProtein ID", "run.1.1.2\tPEPTIDE\t2\tP00330"],
        ["Peptide Sequence\tCharge\tM/Z\tProtein ID", "PEPTIDE\t2\t400.5\tP00330"],
    ],
)
def test_fragpipe_psm_and_ion_tables_are_refused_as_peptides(rows):
    with pytest.raises(ValueError, match="PSM and ion"):
        list(SCRIPT["parsed"]("fragpipe_tsv", rows))


def test_mztab_counts_identifications_once_with_all_accessions_in_the_preview(tmp_path):
    rows = [
        "MTD\tmzTab-version\t1.0.0",
        "PEH\tsequence\taccession\tmodifications",
        "PEP\tPEPTIDE\tP00330\t0",
        "PEP\tPEPTIDE\tP12345\t0",
        "PEP\tPEPTIDE\tP00330\t1-UNIMOD:35",
        "PSH\tsequence\tPSM_ID\taccession\tspectra_ref",
        "PSM\tPEPTIDE\t4\tP00330\tms_run[1]:scan=4",
        "PSM\tPEPTIDE\t5\tP00330\tms_run[1]:scan=5",
        "PSM\tPEPTIDE\t4\tP12345\tms_run[1]:scan=4",
    ]
    other = [row.replace("ms_run[1]:scan=", "ms_run[2]:scan=") for row in rows]
    result, observations = imported(tmp_path, "mztab_1_0", [rows, other], ["P00330", "P12345"])
    summary = result["summary"]
    assert summary["counts"] == {"peptide": 4, "spectrum_match": 4}
    assert [source["counts"] for source in summary["sources"]] == [
        {"peptide": 2, "spectrum_match": 2}
    ] * 2
    [psm] = [
        row for row in summary["preview"] if row["report"] == "0" and row["reported_id"] == "4"
    ]
    assert psm["accessions"] == ["P00330", "P12345"]
    assert psm["registered_proteins"] == ["P00330", "P12345"]
    assert len(observations) == 12
    assert len(summary["preview"]) == 8
    assert result["proteins"] == ["P00330", "P12345"]


def test_maxquant_lists_protein_group_exclusions_separately(tmp_path):
    rows = [
        "Protein IDs\tIntensity\tReverse\tPotential contaminant\tOnly identified by site",
        "P00330\t42\t\t\t",
        "REV__P12345\t10\t+\t\t",
        "CON__P12345\t20\t\t+\t",
        "P99999\t30\t\t\t+",
    ]
    result, _ = imported(tmp_path, "maxquant_txt", [rows], ["P00330", "P99999"])
    summary = result["summary"]
    assert summary["counts"] == {"protein_group": 1}
    assert summary["excluded_counts"] == {
        "decoy": {"protein_group": 1},
        "contaminant": {"protein_group": 1},
        "site_only": {"protein_group": 1},
    }
    assert [row["flags"] for row in summary["preview"]] == [
        [],
        ["Reverse"],
        ["Potential contaminant"],
        ["Only identified by site"],
    ]
    assert result["proteins"] == ["P00330"]


def test_each_report_records_the_table_it_was_read_as(tmp_path):
    peptides = ["Sequence\tProteins\tIntensity", "PEPTIDE\tP00330\t2"]
    evidence = ["Sequence\tProteins\tRaw file\tIntensity", "PEPTIDE\tP00330\tA\t2"]
    groups = ["Protein IDs\tIntensity", "P00330\t2"]
    result, _ = imported(tmp_path, "maxquant_txt", [peptides, evidence, groups])
    tables = [source["table"] for source in result["summary"]["sources"]]
    assert tables == ["peptides.txt", "evidence.txt", "proteinGroups.txt"]
    mztab = ["MTD\tmzTab-version\t1.0.0", "PRH\taccession", "PRT\tP00330"]
    result, _ = imported(tmp_path, "mztab_1_0", [mztab])
    assert result["summary"]["sources"][0]["table"] == "mzTab 1.0.0"
    assert result["format"] == "mztab_1_0"


def test_a_parquet_report_is_refused_with_how_to_convert_it(tmp_path):
    # Apache Parquet files start and end with the magic bytes PAR1:
    # https://parquet.apache.org/docs/file-format/
    source = tmp_path / "report.parquet"
    source.write_bytes(b"PAR1" + bytes(16) + b"PAR1")
    run = {
        "params": {"format": "diann_tsv", "reports": ["report"], "version": "2.0"},
        "inputs": {"dataset": [{"id": "dataset", "revision": 1, "values": {}}]},
        "files": {"report": {"name": source.name, "path": str(source), "sha256": "p"}},
    }
    with pytest.raises(ValueError, match=r"report\.parquet: This is a Parquet file"):
        SCRIPT["result"](run, tmp_path / "identifications.jsonl")


def test_an_unsupported_format_is_named():
    with pytest.raises(ValueError, match="supported formats"):
        list(SCRIPT["parsed"]("diann", ["Run"]))


DATA = Path(__file__).parent / "data"


def read(format_, path):
    about = {}
    with SCRIPT["lines_at"](path) as source:
        rows = list(SCRIPT["parsed"](format_, source, about))
    return about.get("table"), rows


@pytest.mark.parametrize(
    ("name", "table", "level"),
    [
        ("proteinGroups.txt", "proteinGroups.txt", "protein_group"),
        ("peptides.txt", "peptides.txt", "peptide"),
        ("evidence.txt", "evidence.txt", "precursor"),
    ],
)
def test_maxquants_own_tables_are_read_at_their_level(name, table, level):
    """MaxQuant 1.6.17's own output: every row of each table, once, at its level."""
    found, rows = read("maxquant_txt", DATA / "maxquant-1.6.17" / name)
    assert found == table
    assert [row["level"] for row in rows] == [level] * 6
    assert all(row["accessions"] for row in rows)


@pytest.mark.parametrize(
    ("name", "named", "unit"),
    [
        ("msms.txt", "msms.txt or msmsScans.txt", "MS/MS scan"),
        ("msmsScans.txt", "msms.txt or msmsScans.txt", "MS/MS scan"),
        ("allPeptides.txt", "allPeptides.txt", "MS1 feature"),
        (
            "modificationSpecificPeptides.txt",
            "modificationSpecificPeptides.txt",
            "modified form of a peptide",
        ),
    ],
)
def test_maxquants_scan_feature_and_modified_form_tables_are_refused(name, named, unit):
    """They have Sequence, Proteins and Raw file too. Read as evidence.txt, each MS/MS
    scan of msms.txt counted as a precursor."""
    with pytest.raises(ValueError, match=f"MaxQuant's {re.escape(named)}, one row per {unit}"):
        read("maxquant_txt", DATA / "maxquant-1.6.17" / name)


def test_a_diann_1_8_main_report_gives_precursors_and_their_protein_groups():
    table, rows = read("diann_tsv", DATA / "diann" / "1.8-report.tsv")
    assert table == "main report"
    precursors = [row for row in rows if row["level"] == "precursor"]
    groups = [row for row in rows if row["level"] == "protein_group"]
    assert len(precursors) == 4
    assert {(g["run"], *g["accessions"]) for g in groups} == {
        (p["run"], *p["accessions"]) for p in precursors
    }
    assert set(groups[0]["quantities"]) == {"PG.MaxLFQ", "PG.Normalised", "PG.Quantity"}


def test_a_diann_2_main_report_keeps_its_top_n_quantity_and_leaves_decoys_out_of_groups():
    _, rows = read("diann_tsv", DATA / "diann" / "2-report.tsv")
    precursors = [row for row in rows if row["level"] == "precursor"]
    groups = [row for row in rows if row["level"] == "protein_group"]
    assert [row["decoy"] for row in precursors] == [False, False, False, True]
    assert [(g["run"], g["accessions"]) for g in groups] == [
        ("HeLa_01", ["P37108"]),
        ("HeLa_01", ["P02769"]),
        ("HeLa_02", ["P02769"]),
    ]
    assert groups[1]["quantities"] == {"PG.MaxLFQ": 4.1e7, "PG.TopN": 4.4e7}


@pytest.mark.parametrize(
    ("version", "name", "table", "level"),
    [
        ("fragpipe-22.0", "protein.tsv", "protein", "protein_group"),
        ("fragpipe-22.0", "peptide.tsv", "peptide", "peptide"),
        ("fragpipe-23.1", "combined_protein.tsv", "protein", "protein_group"),
        ("fragpipe-23.1", "combined_peptide.tsv", "peptide", "peptide"),
    ],
)
def test_fragpipes_protein_and_peptide_tables_are_read(version, name, table, level):
    found, rows = read("fragpipe_tsv", DATA / version / name)
    assert found == table
    assert [row["level"] for row in rows] == [level] * 3
    assert all(row["accessions"] for row in rows)


def test_a_fragpipe_peptide_tables_probability_is_its_score():
    _, rows = read("fragpipe_tsv", DATA / "fragpipe-22.0" / "peptide.tsv")
    assert [set(row["scores"]) for row in rows] == [{"Probability"}] * 3


@pytest.mark.parametrize(
    ("version", "name", "message"),
    [
        ("fragpipe-22.0", "psm.tsv", "PSM and ion tables are unsupported"),
        ("fragpipe-22.0", "ion.tsv", "PSM and ion tables are unsupported"),
        ("fragpipe-23.1", "combined_ion.tsv", "PSM and ion tables are unsupported"),
        ("fragpipe-23.1", "combined_modified_peptide.tsv", "one row per modified form"),
    ],
)
def test_fragpipes_psm_ion_and_modified_peptide_tables_are_refused(version, name, message):
    with pytest.raises(ValueError, match=message):
        read("fragpipe_tsv", DATA / version / name)


def test_identifications_are_counted_once_however_far_apart_they_repeat():
    seen = SCRIPT["Seen"]()
    keys = [("spectrum_match", "", str(n)) for n in range(200_000)]
    assert all(seen.add(key) for key in keys)
    assert not any(seen.add(key) for key in reversed(keys))
    assert seen.count == len(keys)
    # Twelve bytes a slot, at most 60% full: a fixed cost per identification.
    assert len(seen.slots) == 12 * 2**19
    assert 0.3 < seen.count / 2**seen.bits <= 0.6


def test_a_report_with_too_many_identifications_is_refused(monkeypatch):
    seen = SCRIPT["Seen"]()
    monkeypatch.setitem(seen.add.__globals__, "MAX_IDENTIFICATIONS", 3)
    for n in range(3):
        seen.add(("peptide", "", f"PEPTIDE{n}", ""))
    with pytest.raises(ValueError, match="more than 3 spectrum matches and peptides"):
        seen.add(("peptide", "", "PEPTIDE3", ""))
