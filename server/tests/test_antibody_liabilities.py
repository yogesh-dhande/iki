"""The antibody plugin's sequence liabilities: the motifs a developability screen reports
in a numbered chain.

The chains here are numbered by `number_chains`' script first, with the same stand-in
ANARCI as `test_antibody_numbering.py` (what the real one said in the image), so the
liabilities are placed on the numbering ANARCI really gave them. The plugin's own
fixtures (`plugins/antibody/tests/find_liabilities.yaml`) cover each rule on a made-up
domain, where every motif is in one place.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator
from test_antibody_numbering import SEQUENCES
from test_antibody_numbering import protein as numbered_protein
from test_antibody_numbering import run as number

from iki_spec import schemas

SCRIPT = Path(__file__).resolve().parents[2] / "plugins" / "antibody" / "scripts" / "liabilities.py"
EXTENSION = SCRIPT.parents[1] / "types" / "protein.yaml"
FIELDS = yaml.safe_load(EXTENSION.read_text())["fields"]
#: What the extension says each field holds, so the script's output is checked by it.
LIABILITIES = Draft202012Validator(FIELDS["liabilities"]["schema"])
COUNTS = Draft202012Validator(FIELDS["liability_counts"]["schema"])


def screen(
    tmp_path: Path, proteins: list[dict[str, Any]]
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    """Run the liabilities script on chains as they'd be stored, and check its output."""
    output_path = tmp_path / "liabilities.json"
    output_path.unlink(missing_ok=True)
    (tmp_path / "screen.json").write_text(
        json.dumps(
            {
                "run": {"hrid": "RUN-000010"},
                "params": {},
                "inputs": {"protein": proteins},
            }
        )
    )
    result = subprocess.run(  # noqa: S603 - our own script
        [sys.executable, str(SCRIPT)],
        env={
            "PATH": os.environ["PATH"],
            "IKI_INPUT": str(tmp_path / "screen.json"),
            "IKI_OUTPUT": str(output_path),
        },
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    output: dict[str, Any] = json.loads(output_path.read_text())
    assert not list(schemas.check("run-output", output))
    for update in output["updates"]["protein"]:
        LIABILITIES.validate(update["values"]["liabilities"])
        COUNTS.validate(update["values"]["liability_counts"])
        assert update["values"]["liability_count"] == len(update["values"]["liabilities"])
    return result, output


def chains(
    tmp_path: Path, *names: str, scheme: str = "imgt", cdr_definition: str | None = None
) -> list[dict[str, Any]]:
    """Chains as `number_chains` leaves them: their sequences, and what ANARCI found."""
    proteins = [numbered_protein(i, SEQUENCES[name], name) for i, name in enumerate(names, start=1)]
    _, output = number(tmp_path, proteins, scheme, cdr_definition)
    numbered = {u["entity"]: u["values"] for u in output["updates"]["protein"]}
    return [
        {**p, "values": {**p["values"], **numbered[p["id"]]}}
        for p in proteins
        if p["id"] in numbered
    ]


def test_trastuzumabs_heavy_chain_has_its_known_ng_and_dg_motifs(tmp_path: Path) -> None:
    result, output = screen(tmp_path, chains(tmp_path, "vh"))

    ((update,),) = output["updates"].values()
    values = update["values"]
    # Trastuzumab's NG in CDR-H2 (Kabat H54, IMGT 62) and DG in CDR-H3 are the hot spots
    # Lu et al. (2019) measured; the W and M in CDR-H3 oxidise, and E1 cyclises.
    assert values["liabilities"] == [
        {
            "kind": "pyroglutamate",
            "motif": "E",
            "position": 1,
            "number": "1",
            "region": "framework",
            "severity": "low",
        },
        {
            "kind": "isomerisation",
            "motif": "DT",
            "position": 31,
            "number": "36",
            "region": "cdr_h1",
            "severity": "medium",
        },
        {
            "kind": "deamidation",
            "motif": "NG",
            "position": 55,
            "number": "62",
            "region": "cdr_h2",
            "severity": "high",
        },
        {
            "kind": "oxidation",
            "motif": "W",
            "position": 99,
            "number": "107",
            "region": "cdr_h3",
            "severity": "medium",
        },
        {
            "kind": "isomerisation",
            "motif": "DG",
            "position": 102,
            "number": "110",
            "region": "cdr_h3",
            "severity": "high",
        },
        {
            "kind": "oxidation",
            "motif": "M",
            "position": 107,
            "number": "115",
            "region": "cdr_h3",
            "severity": "medium",
        },
    ]
    assert values["liability_counts"] == {
        "deamidation": 1,
        "isomerisation": 2,
        "oxidation": 2,
        "pyroglutamate": 1,
    }
    assert values["liability_count"] == 6
    # Each motif is where the chain's sequence says it is.
    sequence = SEQUENCES["vh"]
    for found in values["liabilities"]:
        start = found["position"] - 1
        assert sequence[start : start + len(found["motif"])] == found["motif"]
    assert "::progress 1.00 1 of 1" in result.stdout
    assert "PRT-000001: 6 liabilities (deamidation 1, isomerisation 2" in result.stdout
    assert output["messages"] == []


def test_trastuzumabs_light_chain_has_one_deamidation_site_in_cdr_l1(tmp_path: Path) -> None:
    _, output = screen(tmp_path, chains(tmp_path, "vl"))

    values = output["updates"]["protein"][0]["values"]
    assert values["liabilities"] == [
        {
            "kind": "deamidation",
            "motif": "NT",
            "position": 30,
            "number": "36",
            "region": "cdr_l1",
            "severity": "medium",
        }
    ]
    assert values["liability_counts"] == {"deamidation": 1}
    # Its two cysteines are the variable domain's disulphide (IMGT 23 and 104), so neither
    # is noncanonical, and its first residue (D) doesn't cyclise.
    assert "noncanonical_cysteine" not in values["liability_counts"]
    assert "pyroglutamate" not in values["liability_counts"]


def test_an_scfvs_two_domains_are_screened_along_the_one_chain(tmp_path: Path) -> None:
    _, output = screen(tmp_path, chains(tmp_path, "scfv"))

    values = output["updates"]["protein"][0]["values"]
    # The heavy domain's six, then the light domain's one, in order along the chain.
    assert [(f["kind"], f["position"], f["region"]) for f in values["liabilities"]][-1] == (
        "deamidation",
        165,
        "cdr_l1",
    )
    assert values["liability_count"] == 7
    assert [f["position"] for f in values["liabilities"]] == sorted(
        f["position"] for f in values["liabilities"]
    )


def test_a_cdr_motif_in_framework_is_left_alone(tmp_path: Path) -> None:
    """Deamidation, isomerisation and oxidation count in the CDRs, where published screens
    report them; a sequon counts anywhere in a variable domain."""
    _, output = screen(tmp_path, chains(tmp_path, "vh"))

    values = output["updates"]["protein"][0]["values"]
    found = {(f["kind"], f["position"]) for f in values["liabilities"]}
    sequence = SEQUENCES["vh"]
    # The chain has NS, NT, DT and tryptophans in framework, which aren't reported.
    assert (sequence[83:85], sequence[76:78], sequence[89:91]) == ("NS", "NT", "DT")
    assert found.isdisjoint(
        {("deamidation", 84), ("deamidation", 77), ("isomerisation", 90), ("oxidation", 47)}
    )
    assert all(f["region"] != "framework" for f in values["liabilities"] if f["position"] > 1)


def test_the_scheme_the_chain_was_numbered_in_says_where_its_cdrs_are(tmp_path: Path) -> None:
    _, output = screen(tmp_path, chains(tmp_path, "vh", scheme="kabat"))

    values = output["updates"]["protein"][0]["values"]
    # The numbers are Kabat's, so the hot spots are where Lu et al. (2019) name them:
    # NG at H54 and DG at H98. Kabat's CDR-H2 is 50 to 65, which reaches a DS that IMGT's
    # 56 to 65 leaves in framework.
    assert [(f["kind"], f["motif"], f["number"], f["region"]) for f in values["liabilities"]] == [
        ("pyroglutamate", "E", "1", "framework"),
        ("isomerisation", "DT", "31", "cdr_h1"),
        ("deamidation", "NG", "54", "cdr_h2"),
        ("isomerisation", "DS", "61", "cdr_h2"),
        ("oxidation", "W", "95", "cdr_h3"),
        ("isomerisation", "DG", "98", "cdr_h3"),
        ("oxidation", "M", "100C", "cdr_h3"),
    ]


@pytest.mark.parametrize("scheme", ["martin", "aho"])
def test_martin_and_aho_numbering_have_their_own_disulphide_positions(
    tmp_path: Path, scheme: str
) -> None:
    """Martin's numbering puts the disulphide at H22 and H92, as Chothia's does, and AHo's
    at 23 and 106 (ANARCI's numbering of trastuzumab): neither cysteine is reported. The
    CDRs are those of the scheme's CDR definition (Chothia's for Martin's numbering, IMGT's
    for AHo's), so the same CDR motifs are found, numbered in the scheme."""
    _, output = screen(tmp_path, chains(tmp_path, "vh", "vl", scheme=scheme))

    vh, vl = (u["values"] for u in output["updates"]["protein"])
    assert "noncanonical_cysteine" not in vh["liability_counts"]
    assert "noncanonical_cysteine" not in vl["liability_counts"]
    found = {(f["kind"], f["motif"], f["region"]) for f in vh["liabilities"]}
    # Trastuzumab's NG in CDR-H2 and DG in CDR-H3 are in both definitions' CDRs.
    assert {("deamidation", "NG", "cdr_h2"), ("isomerisation", "DG", "cdr_h3")} <= found


def test_kabat_s_cdr_h1_keeps_h35c(tmp_path: Path) -> None:
    """A methionine at H35C, where Kabat's numbering puts a long CDR-H1's third insertion,
    before the conserved W36, is in CDR-H1, as one at H35B is."""
    numbered = chains(tmp_path, "long_h1", scheme="kabat")
    values = numbered[0]["values"]
    domain = values["variable_domains"][0]
    sequence = list(values["sequence"])
    for index, (n, insertion, _) in enumerate(domain["numbering"]):
        if (n, insertion) in ((35, "B"), (35, "C")):
            domain["numbering"][index][2] = sequence[index] = "M"
    values["sequence"] = "".join(sequence)

    _, output = screen(tmp_path, numbered)

    found = [
        (f["kind"], f["number"], f["region"])
        for f in output["updates"]["protein"][0]["values"]["liabilities"]
    ]
    assert ("oxidation", "35B", "cdr_h1") in found
    assert ("oxidation", "35C", "cdr_h1") in found


def test_the_cdr_definition_says_where_the_cdrs_are(tmp_path: Path) -> None:
    """Kabat's numbering with Contact's CDRs: CDR-H2 is H47-H58, so the DS at H61 that
    Kabat's CDR-H2 (H50-H65) reaches is framework's, and the numbers are still Kabat's."""
    _, output = screen(tmp_path, chains(tmp_path, "vh", scheme="kabat", cdr_definition="contact"))

    found = [
        (f["kind"], f["motif"], f["number"], f["region"])
        for f in output["updates"]["protein"][0]["values"]["liabilities"]
    ]
    assert ("deamidation", "NG", "54", "cdr_h2") in found
    assert not [f for f in found if f[2] == "61"]


def test_a_domain_whose_cdrs_werent_found_is_screened_for_what_counts_anywhere(
    tmp_path: Path,
) -> None:
    numbered = chains(tmp_path, "vh")
    del numbered[0]["values"]["variable_domains"][0]["cdrs"]

    _, output = screen(tmp_path, numbered)

    values = output["updates"]["protein"][0]["values"]
    assert [f["kind"] for f in values["liabilities"]] == ["pyroglutamate"]
    assert output["messages"][0]["text"] == (
        "vh has a variable domain whose CDRs numbering didn't find, so the motifs that count "
        "only in a CDR weren't looked for in it."
    )


def test_a_sequon_and_an_extra_cysteine_are_found_anywhere_in_a_domain(tmp_path: Path) -> None:
    """A chain whose CDR-H3 carries a sequon and a cysteine of its own: both are reported,
    while the two cysteines at IMGT 23 and 104 are its disulphide."""
    odd = SEQUENCES["vh"].replace("SRWGGDGFYAMDY", "SRWGNCSFYAMDY")
    numbered = chains(tmp_path, "vh")
    numbered[0]["values"]["sequence"] = odd
    domain = numbered[0]["values"]["variable_domains"][0]
    domain["numbering"] = [
        [number, insertion, odd[index]]
        for index, (number, insertion, _) in enumerate(domain["numbering"])
    ]

    _, output = screen(tmp_path, numbered)

    values = output["updates"]["protein"][0]["values"]
    found = [(f["kind"], f["motif"], f["number"], f["severity"]) for f in values["liabilities"]]
    assert ("n_glycosylation", "NCS", "109", "high") in found
    assert ("noncanonical_cysteine", "C", "110", "high") in found
    assert values["liability_counts"]["noncanonical_cysteine"] == 1


def test_a_vhhs_second_disulphide_is_reported_as_noncanonical_cysteines(tmp_path: Path) -> None:
    """cAb-Lys3 (PDB 1MEL): its CDR1 and CDR3 cysteines (IMGT 38 and 111F) bond each other,
    as its SSBOND records say (Cys33-Cys109), beside the conserved Cys22-Cys96. Sequence
    alone can't tell that, so they're reported as cysteines beyond the canonical pair, not
    as unpaired ones."""
    _, output = screen(tmp_path, chains(tmp_path, "cab_lys3"))

    values = output["updates"]["protein"][0]["values"]
    cysteines = [
        (f["kind"], f["position"], f["number"], f["region"])
        for f in values["liabilities"]
        if f["motif"] == "C"
    ]
    assert cysteines == [
        ("noncanonical_cysteine", 33, "38", "cdr_h1"),
        ("noncanonical_cysteine", 109, "111F", "cdr_h3"),
    ]


@pytest.mark.parametrize(
    ("middle", "found"),
    [
        # N-A-S is a sequon; N-P-S isn't; N-X-S is one unless X is proline, so it's uncertain.
        ("A", [{"motif": "NAS"}]),
        ("P", []),
        ("X", [{"motif": "NXS", "uncertain": True}]),
    ],
)
def test_a_sequon_with_an_unknown_middle_residue_is_uncertain(
    tmp_path: Path, middle: str, found: list[dict[str, Any]]
) -> None:
    odd = SEQUENCES["vh"].replace("SRWGGDGFYAMDY", f"SRWGN{middle}SFYAMDY")
    numbered = chains(tmp_path, "vh")
    numbered[0]["values"]["sequence"] = odd
    domain = numbered[0]["values"]["variable_domains"][0]
    domain["numbering"] = [
        [number, insertion, odd[index]]
        for index, (number, insertion, _) in enumerate(domain["numbering"])
    ]

    _, output = screen(tmp_path, numbered)

    sequons = [
        {k: v for k, v in f.items() if k in ("motif", "uncertain")}
        for f in output["updates"]["protein"][0]["values"]["liabilities"]
        if f["kind"] == "n_glycosylation"
    ]
    assert sequons == found


def test_a_chain_without_a_disulphide_has_its_lone_cysteine_reported(tmp_path: Path) -> None:
    """IMGT 104's cysteine is what the one at 23 pairs with: without it, both would be
    unpaired, so the one that's left is reported."""
    numbered = chains(tmp_path, "vh")
    domain = numbered[0]["values"]["variable_domains"][0]
    at = next(i for i, (n, _, _) in enumerate(domain["numbering"]) if n == 104)
    domain["numbering"][at] = [104, "", "A"]
    numbered[0]["values"]["sequence"] = SEQUENCES["vh"][:at] + "A" + SEQUENCES["vh"][at + 1 :]

    _, output = screen(tmp_path, numbered)

    values = output["updates"]["protein"][0]["values"]
    unpaired = [f for f in values["liabilities"] if f["kind"] == "noncanonical_cysteine"]
    assert [(f["number"], f["region"], f["severity"]) for f in unpaired] == [
        ("23", "framework", "high")
    ]


def test_a_chain_that_hasnt_been_numbered_is_left_as_it_was(tmp_path: Path) -> None:
    numbered = chains(tmp_path, "vh")
    unnumbered = {
        "id": "00000000-0000-0000-0000-000000000009",
        "hrid": "PRT-000009",
        "values": {"sequence": SEQUENCES["lysozyme"], "name": "Lysozyme"},
    }

    _, output = screen(tmp_path, [unnumbered, *numbered])

    assert [u["entity"][-1] for u in output["updates"]["protein"]] == ["1"]
    assert output["messages"] == [
        {
            "level": "warning",
            "text": "Lysozyme hasn't been numbered, so its liabilities can't be placed: "
            "number its chains first. It was left as it was.",
            "entity": "00000000-0000-0000-0000-000000000009",
        }
    ]


def test_a_run_keeps_at_most_100_messages(tmp_path: Path) -> None:
    unnumbered = [
        {
            "id": f"00000000-0000-0000-0000-{i:012d}",
            "hrid": f"PRT-{i:06d}",
            "values": {"sequence": SEQUENCES["lysozyme"]},
        }
        for i in range(1, 151)
    ]

    _, output = screen(tmp_path, unnumbered)

    assert output["updates"] == {"protein": []}
    assert len(output["messages"]) == 100
    assert output["messages"][-1] == {"level": "warning", "text": "And 51 more warnings."}


def shifted(values: dict[str, Any], by: int) -> None:
    """Moves a chain's only variable domain `by` residues along it, as residues added
    before it (`by` > 0) or the domain's first ones cut off (`by` < 0) would."""
    (domain,) = values["variable_domains"]
    if by < 0:
        domain["numbering"] = domain["numbering"][-by:]
    else:
        domain["start"] += by
    domain["end"] += by
    domain["cdrs"] = [None if c is None else [c[0] + by, c[1] + by] for c in domain["cdrs"]]


def test_a_fragment_that_starts_past_imgt_23_keeps_its_cysteine_at_104(tmp_path: Path) -> None:
    """A VH fragment that starts at IMGT 26 hasn't the cysteine at 23: that says nothing of
    whether the one at 104 has its partner, so it isn't reported. Nor is the one at 23 in a
    fragment that ends before 104. One the domain does reach but has another residue at,
    or none, is a loss (as test_a_chain_without_a_disulphide_has_its_lone_cysteine_reported
    shows)."""
    numbered = chains(tmp_path, "vh")
    values = numbered[0]["values"]
    (domain,) = values["variable_domains"]
    cut = next(i for i, (n, _, _) in enumerate(domain["numbering"]) if n >= 26)
    assert [n for n, _, _ in domain["numbering"][:cut]][-1] == 25
    values["sequence"] = values["sequence"][cut:]
    shifted(values, -cut)

    _, output = screen(tmp_path, numbered)

    found = output["updates"]["protein"][0]["values"]["liabilities"]
    assert not [f for f in found if f["kind"] == "noncanonical_cysteine"]

    numbered = chains(tmp_path, "vh")
    values = numbered[0]["values"]
    (domain,) = values["variable_domains"]
    end = next(i for i, (n, _, _) in enumerate(domain["numbering"]) if n >= 100)
    domain["numbering"] = domain["numbering"][:end]
    domain["end"] = end
    domain["cdrs"] = domain["cdrs"][:2]
    values["sequence"] = values["sequence"][:end]

    _, output = screen(tmp_path, numbered)

    found = output["updates"]["protein"][0]["values"]["liabilities"]
    assert not [f for f in found if f["kind"] == "noncanonical_cysteine"]

    # A domain that reaches past 23, but without a residue numbered 23, has lost it.
    numbered = chains(tmp_path, "vh")
    values = numbered[0]["values"]
    (domain,) = values["variable_domains"]
    at = next(i for i, (n, _, _) in enumerate(domain["numbering"]) if n == 23)
    del domain["numbering"][at]
    values["sequence"] = values["sequence"][:at] + values["sequence"][at + 1 :]
    domain["end"] -= 1
    domain["cdrs"] = [[a - 1, b - 1] for a, b in domain["cdrs"]]

    _, output = screen(tmp_path, numbered)

    found = output["updates"]["protein"][0]["values"]["liabilities"]
    assert [(f["kind"], f["number"]) for f in found if f["motif"] == "C"] == [
        ("noncanonical_cysteine", "104")
    ]


def test_a_chains_first_residue_cyclises_wherever_its_variable_domain_starts(
    tmp_path: Path,
) -> None:
    """QA before trastuzumab's VH, as a tag leaves it: the Q at the chain's N-terminus
    cyclises, though it's no domain's, so it has no number and its region is `outside`.
    The VH's own E, now the third residue, doesn't."""
    numbered = chains(tmp_path, "vh")
    values = numbered[0]["values"]
    values["sequence"] = "QA" + values["sequence"]
    shifted(values, 2)

    _, output = screen(tmp_path, numbered)

    found = output["updates"]["protein"][0]["values"]["liabilities"]
    pyroglutamate = [f for f in found if f["kind"] == "pyroglutamate"]
    assert pyroglutamate == [
        {
            "kind": "pyroglutamate",
            "motif": "Q",
            "position": 1,
            "region": "outside",
            "severity": "low",
        }
    ]
    # The rest are where they were, two residues along.
    assert [(f["kind"], f["position"], f["number"]) for f in found[1:]] == [
        ("isomerisation", 33, "36"),
        ("deamidation", 57, "62"),
        ("oxidation", 101, "107"),
        ("isomerisation", 104, "110"),
        ("oxidation", 109, "115"),
    ]
    # One that starts with a methionine, as an unprocessed chain does, has none.
    values["sequence"] = "MA" + values["sequence"][2:]
    _, output = screen(tmp_path, numbered)
    found = output["updates"]["protein"][0]["values"]["liabilities"]
    assert not [f for f in found if f["kind"] == "pyroglutamate"]


def test_renumbering_a_screened_chain_finds_its_liabilities_again(tmp_path: Path) -> None:
    """Trastuzumab's VH screened on its IMGT numbering has 6 liabilities, its methionine
    numbered 115. Numbered again in Kabat's scheme, its liabilities are Kabat's: 7, the
    methionine at H100C and CDR-H2's DS at H61 (Kabat's CDR-H2 is H50-H65), as screening
    the Kabat-numbered chain gives them. One that wasn't screened is left unscreened."""
    (imgt,) = chains(tmp_path, "vh")
    _, output = screen(tmp_path, [imgt])
    screened = {**imgt, "values": {**imgt["values"], **output["updates"]["protein"][0]["values"]}}
    assert screened["values"]["liability_count"] == 6

    _, renumbered = number(
        tmp_path, [screened, numbered_protein(2, SEQUENCES["vl"], "vl")], "kabat"
    )

    again, unscreened = (u["values"] for u in renumbered["updates"]["protein"])
    _, fresh = screen(tmp_path, chains(tmp_path, "vh", scheme="kabat"))
    expected = fresh["updates"]["protein"][0]["values"]
    assert {k: again[k] for k in ("liabilities", "liability_counts", "liability_count")} == expected
    assert again["liability_count"] == 7
    assert ("oxidation", "100C") in [(f["kind"], f["number"]) for f in again["liabilities"]]
    assert "liability_count" not in unscreened
    assert {
        "level": "info",
        "text": "vh's liabilities were found again on the new numbering: 7.",
        "entity": screened["id"],
    } in renumbered["messages"]


def test_a_loops_residues_arent_framework_when_its_cdrs_werent_found(tmp_path: Path) -> None:
    """Bovine BLV1H12 (PDB 4K3D), numbered in IMGT's scheme with Kabat's CDRs, which
    ANARCI can't number its 61-residue CDR-H3 in: its CDRs aren't found, so the six
    cysteines inside its CDR-H3, at residues 108, 118, 124, 129, 130 and 138, are in an
    unknown region, not framework."""
    (chain,) = chains(tmp_path, "blv1h12", cdr_definition="kabat")
    assert chain["values"]["variable_domains"][0].get("cdrs") is None
    _, output = screen(tmp_path, [chain])
    found = output["updates"]["protein"][0]["values"]["liabilities"]
    cysteines = [
        (f["position"], f["region"]) for f in found if f["kind"] == "noncanonical_cysteine"
    ]
    assert {108, 118, 124, 129, 130, 138} <= {p for p, _ in cysteines}
    assert {r for _, r in cysteines} == {"unknown"}
    assert not [f for f in found if f["region"] == "framework"]
