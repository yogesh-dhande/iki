"""The antibody plugin's numbering: proteins through ANARCI, into their variable domains,
CDRs and closest human germlines.

ANARCI needs HMMER's hmmscan, which is in the plugin's image, not here: these tests put a
stand-in ANARCI on the path that answers with what the real one said about the same
chains, in the image (data/anarci.json, from data/record_anarci.py), and records how it
was called. The image's own check runs the real one."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator

from iki_spec import schemas

SCRIPT = Path(__file__).resolve().parents[2] / "plugins" / "antibody" / "scripts" / "numbering.py"
RECORDED = Path(__file__).parent / "data" / "anarci.json"
SEQUENCES: dict[str, str] = json.loads(RECORDED.read_text())["sequences"]
EXTENSION = SCRIPT.parents[1] / "types" / "protein.yaml"
#: What the extension says `variable_domains` holds, so the script's output is checked by it.
FIELDS: dict[str, Any] = yaml.safe_load(EXTENSION.read_text())["fields"]
DOMAINS = Draft202012Validator(FIELDS["variable_domains"]["schema"])

FAKE = """\
import json, os
from pathlib import Path

def anarci(sequences, **called):
    recorded = json.loads(Path(os.environ["RECORDED"]).read_text())
    calls = Path(os.environ["CALLED"])
    before = json.loads(calls.read_text()) if calls.exists() else []
    asked = {"sequences": [s for _, s in sequences], **called}
    calls.write_text(json.dumps([*before, asked], default=sorted))
    species = "all" if called["allowed_species"] is None else "+".join(called["allowed_species"])
    names = {s: n for n, s in recorded["sequences"].items()}
    answers = [recorded["schemes"][called["scheme"]][species][names[s]] for _, s in sequences]
    return [a[0] for a in answers], [a[1] for a in answers], [None for _ in answers]
"""


def protein(i: int, sequence: str, name: str | None = None) -> dict[str, Any]:
    values: dict[str, Any] = {"sequence": sequence}
    if name:
        values["name"] = name
    return {"id": f"00000000-0000-0000-0000-{i:012d}", "hrid": f"PRT-{i:06d}", "values": values}


def run(
    tmp_path: Path,
    proteins: list[dict[str, Any]],
    scheme: str | None = "imgt",
    cdr_definition: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    fake = tmp_path / "fake" / "anarci"
    fake.mkdir(parents=True, exist_ok=True)
    (fake / "__init__.py").write_text(FAKE)
    output_path = tmp_path / "output.json"
    output_path.unlink(missing_ok=True)
    (tmp_path / "input.json").write_text(
        json.dumps(
            {
                "run": {"hrid": "RUN-000009"},
                "params": {
                    key: value
                    for key, value in (("scheme", scheme), ("cdr_definition", cdr_definition))
                    if value is not None
                },
                "inputs": {"protein": proteins},
            }
        )
    )
    result = subprocess.run(  # noqa: S603 - our own script
        [sys.executable, str(SCRIPT)],
        env={
            "PATH": os.environ["PATH"],
            "PYTHONPATH": str(tmp_path / "fake"),
            "IKI_INPUT": str(tmp_path / "input.json"),
            "IKI_OUTPUT": str(output_path),
            "RECORDED": str(RECORDED),
            "CALLED": str(tmp_path / "called"),
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
        DOMAINS.validate(update["values"]["variable_domains"])
        # Each value fits its field, or the server refuses the run's every update.
        for name, value in update["values"].items():
            if isinstance(value, str) and "max_length" in FIELDS[name]:
                assert len(value) <= FIELDS[name]["max_length"], (name, len(value))
    return result, output


def called(tmp_path: Path) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = json.loads((tmp_path / "called").read_text())
    return found


def test_a_heavy_chain_gets_its_imgt_cdrs_and_closest_human_germline(tmp_path: Path) -> None:
    result, output = run(tmp_path, [protein(1, SEQUENCES["vh"], "Trastuzumab VH")])

    ((update,),) = output["updates"].values()
    values = update["values"]
    assert update["entity"] == "00000000-0000-0000-0000-000000000001"
    # Trastuzumab's, as IMGT/3Dstructure-DB numbers 1N8Z.
    assert {k: v for k, v in values.items() if k != "variable_domains"} == {
        "chain_type": "heavy",
        "species": "human",
        "numbering_scheme": "imgt",
        "cdr_definition": "imgt",
        "cdr_h1": "GFNIKDTY",
        "cdr_h2": "IYPTNGYT",
        "cdr_h3": "SRWGGDGFYAMDY",
        "cdr_l1": None,
        "cdr_l2": None,
        "cdr_l3": None,
        "heavy_v_gene": "IGHV3-66*01",
        "heavy_germline_identity": 82.5,
        "heavy_j_gene": "IGHJ4*01",
        # Numbering a chain again leaves nothing of the last time.
        "light_v_gene": None,
        "light_germline_identity": None,
        "light_j_gene": None,
    }
    (domain,) = values["variable_domains"]
    assert (domain["chain"], domain["start"], domain["end"]) == ("H", 1, 120)
    assert domain["numbering"][:2] == [[1, "", "E"], [2, "", "V"]]
    assert "".join(aa for _, _, aa in domain["numbering"]) == SEQUENCES["vh"]
    assert domain["species"] == "human"
    # IMGT's CDRs, 27-38, 56-65 and 105-117, are residues 26-33, 51-58 and 97-109.
    assert domain["cdrs"] == [[26, 33], [51, 58], [97, 109]]
    assert (
        domain["human"]
        == domain["closest"]
        == {
            "v_gene": "IGHV3-66*01",
            "v_identity": 82.5,
            "j_gene": "IGHJ4*01",
            "j_identity": 92.9,
        }
    )
    assert "::progress 1.00 1 of 1" in result.stdout
    assert "PRT-000001: chain_type heavy, species human, cdr_h3 SRWGGDGFYAMDY" in result.stdout
    assert "messages" not in output or output["messages"] == []


def test_anarci_is_asked_with_every_species_then_with_human_alone(tmp_path: Path) -> None:
    run(tmp_path, [protein(1, SEQUENCES["vh"] + "*"), protein(2, SEQUENCES["vl"].lower())])

    # IMGT's CDRs are in IMGT's numbering, so it's asked nothing more.
    every, human = called(tmp_path)
    # A final stop is left out, and lower case read as upper.
    assert every["sequences"] == human["sequences"] == [SEQUENCES["vh"], SEQUENCES["vl"]]
    # Antibody domains alone: not a T-cell receptor's.
    assert every["allow"] == human["allow"] == ["H", "K", "L"]
    assert every["assign_germline"] is human["assign_germline"] is True
    assert (every["allowed_species"], human["allowed_species"]) == (None, ["human"])


def test_a_mouse_chain_is_numbered_as_a_mouse_s_and_far_from_human(tmp_path: Path) -> None:
    _, output = run(tmp_path, [protein(1, SEQUENCES["mouse"], "D1.3 VH")])

    values = output["updates"]["protein"][0]["values"]
    (domain,) = values["variable_domains"]
    assert values["species"] == domain["species"] == "mouse"
    assert domain["closest"]["v_gene"] == "IGHV2-6-7*01"
    assert domain["closest"]["v_identity"] == 97.9
    # Its humanness is against the closest human gene, not its own species' gene.
    assert (values["heavy_v_gene"], values["heavy_germline_identity"]) == (
        domain["human"]["v_gene"],
        domain["human"]["v_identity"],
    )
    assert values["heavy_v_gene"].startswith("IGHV")
    assert values["heavy_germline_identity"] < 70
    assert values["cdr_h3"] == "ARERDYRLDY"


#: Trastuzumab's CDRs (PDB 1N8Z; Carter et al. 1992), by each definition, read by hand off
#: its sequence with the boundaries A. C. R. Martin tabulates (bioinf.org.uk/abs): Kabat's
#: H31-H35B, H50-H65, H95-H102 and L24-L34, L50-L56, L89-L97 in Kabat's numbering; in
#: Chothia's, Chothia's H26-H32, H52-H56, H95-H102, AbM's H26-H35, H50-H58, H95-H102 and
#: Contact's H30-H35, H47-H58, H93-H101 and L30-L36, L46-L55, L89-L96 (Chothia's and AbM's
#: light CDRs are Kabat's); IMGT's 27-38, 56-65 and 105-117 (IMGT/3Dstructure-DB, 1N8Z).
TRASTUZUMAB_CDRS = {
    "kabat": (
        ("DTYIH", "RIYPTNGYTRYADSVKG", "WGGDGFYAMDY"),
        ("RASQDVNTAVA", "SASFLYS", "QQHYTTPPT"),
    ),
    "chothia": (("GFNIKDT", "YPTNGY", "WGGDGFYAMDY"), ("RASQDVNTAVA", "SASFLYS", "QQHYTTPPT")),
    "abm": (("GFNIKDTYIH", "RIYPTNGYTR", "WGGDGFYAMDY"), ("RASQDVNTAVA", "SASFLYS", "QQHYTTPPT")),
    "contact": (("KDTYIH", "WVARIYPTNGYTR", "SRWGGDGFYAMD"), ("NTAVAWY", "LLIYSASFLY", "QQHYTTPP")),
    "imgt": (("GFNIKDTY", "IYPTNGYT", "SRWGGDGFYAMDY"), ("QDVNTA", "SAS", "QQHYTTPPT")),
}


@pytest.mark.parametrize(
    ("scheme", "definition", "numberings"),
    [
        ("imgt", "imgt", {"imgt"}),
        ("kabat", "kabat", {"kabat"}),
        ("chothia", "chothia", {"chothia"}),
        # Martin's numbering is Chothia's in the CDRs, and AHo's defines none of its own.
        ("martin", "chothia", {"martin", "chothia"}),
        ("aho", "imgt", {"aho", "imgt"}),
    ],
)
def test_each_scheme_has_its_own_cdrs(
    tmp_path: Path, scheme: str, definition: str, numberings: set[str]
) -> None:
    _, output = run(tmp_path, [protein(1, SEQUENCES["vh"]), protein(2, SEQUENCES["vl"])], scheme)

    vh, vl = (u["values"] for u in output["updates"]["protein"])
    heavy, light = TRASTUZUMAB_CDRS[definition]
    assert {c["scheme"] for c in called(tmp_path)} == numberings
    assert (vh["cdr_h1"], vh["cdr_h2"], vh["cdr_h3"]) == heavy
    assert (vl["cdr_l1"], vl["cdr_l2"], vl["cdr_l3"]) == light
    assert vh["numbering_scheme"] == vl["numbering_scheme"] == scheme
    assert vh["cdr_definition"] == vl["cdr_definition"] == definition
    assert (vl["chain_type"], vl["light_v_gene"]) == ("kappa", "IGKV1-39*01")


@pytest.mark.parametrize("scheme", ["imgt", "kabat", "chothia", "martin", "aho"])
@pytest.mark.parametrize("definition", ["imgt", "kabat", "chothia", "abm", "contact"])
def test_a_cdr_definition_finds_the_same_residues_in_any_numbering(
    tmp_path: Path, scheme: str, definition: str
) -> None:
    _, output = run(
        tmp_path, [protein(1, SEQUENCES["vh"]), protein(2, SEQUENCES["vl"])], scheme, definition
    )

    vh, vl = (u["values"] for u in output["updates"]["protein"])
    assert (
        (vh["cdr_h1"], vh["cdr_h2"], vh["cdr_h3"]),
        (vl["cdr_l1"], vl["cdr_l2"], vl["cdr_l3"]),
    ) == (TRASTUZUMAB_CDRS[definition])
    assert (vh["numbering_scheme"], vh["cdr_definition"]) == (scheme, definition)
    # The residues are numbered in the scheme chosen, whichever numbering found the CDRs.
    (domain,) = vh["variable_domains"]
    canonical = {"imgt": 104, "kabat": 92, "chothia": 92, "martin": 92, "aho": 106}[scheme]
    assert [n for n, _, aa in domain["numbering"] if aa == "C"][-1] == canonical


def test_kabat_s_cdr_h1_keeps_every_insertion_at_h35(tmp_path: Path) -> None:
    # Three residues more in CDR-H1 than trastuzumab's: Kabat's numbering puts them at H35A to
    # H35C, all before the conserved W36, so all are CDR-H1's (as AbNumber's Kabat borders
    # have it); Chothia's numbering puts them at H31A-H31C, inside AbM's CDR-H1.
    _, output = run(tmp_path, [protein(1, SEQUENCES["long_h1"], "Long H1")], "kabat")

    values = output["updates"]["protein"][0]["values"]
    assert values["cdr_h1"] == "DTYSGSIH"
    _, output = run(tmp_path, [protein(1, SEQUENCES["long_h1"])], "kabat", "abm")
    assert output["updates"]["protein"][0]["values"]["cdr_h1"] == "GFNIKDTYSGSIH"


def test_the_scheme_is_imgt_unless_chosen(tmp_path: Path) -> None:
    _, output = run(tmp_path, [protein(1, SEQUENCES["vh"])], scheme=None)

    assert {c["scheme"] for c in called(tmp_path)} == {"imgt"}
    values = output["updates"]["protein"][0]["values"]
    assert (values["cdr_definition"], values["cdr_h3"]) == ("imgt", "SRWGGDGFYAMDY")


def test_the_cdrs_numbering_is_asked_once_more_with_every_species(tmp_path: Path) -> None:
    run(tmp_path, [protein(1, SEQUENCES["vh"])], "kabat", "contact")

    every, human, cdrs = called(tmp_path)
    assert (every["scheme"], human["scheme"], cdrs["scheme"]) == ("kabat", "kabat", "chothia")
    assert (cdrs["allowed_species"], cdrs["assign_germline"]) == (None, False)


def test_an_scfv_has_both_domains(tmp_path: Path) -> None:
    _, output = run(tmp_path, [protein(1, SEQUENCES["scfv"])])

    values = output["updates"]["protein"][0]["values"]
    assert values["chain_type"] == "heavy_and_light"
    assert (values["cdr_h3"], values["cdr_l3"]) == ("SRWGGDGFYAMDY", "QQHYTTPPT")
    assert (values["heavy_v_gene"], values["light_v_gene"]) == ("IGHV3-66*01", "IGKV1-39*01")
    # The heavy domain, the 15-residue linker, then the light domain.
    assert [(d["chain"], d["start"], d["end"]) for d in values["variable_domains"]] == [
        ("H", 1, 120),
        ("K", 136, 242),
    ]


def test_a_vhh_and_a_lambda_chain(tmp_path: Path) -> None:
    _, output = run(tmp_path, [protein(1, SEQUENCES["vhh"]), protein(2, SEQUENCES["lambda"])])

    vhh, lam = (u["values"] for u in output["updates"]["protein"])
    assert (vhh["chain_type"], vhh["species"]) == ("heavy", "alpaca")
    assert vhh["cdr_h3"] == "AAAGVRAEDGRVRTLPSEYTF"
    # A camelid domain is far from any human germline: what humanizing one changes.
    assert (vhh["heavy_v_gene"], vhh["heavy_germline_identity"]) == ("IGHV3-23*04", 81.6)
    # A human germline, exactly: ANARCI's rhesus model numbers it best, but the closest
    # germline is what says its species.
    assert (lam["chain_type"], lam["species"], lam["cdr_l3"]) == ("lambda", "human", "SSYTSSSTLV")
    assert (lam["light_v_gene"], lam["light_germline_identity"]) == ("IGLV2-14*01", 100.0)


def imgt_cdr3(sequence: str) -> str:
    """CDR3-IMGT by its definition (Lefranc et al. 2003; IMGT's FR-IMGT and CDR-IMGT
    definitions): positions 105-117, between the domain's second cysteine, Cys104 (of
    FR3's YYC or YFC), and Trp118 (of FR4's WGQG or WGKG). Found here by those motifs,
    not by ANARCI."""
    cys = max(sequence.rfind("YYC"), sequence.rfind("YFC")) + 2
    trp = max(sequence.find("WGQG"), sequence.find("WGKG"))
    return sequence[cys + 1 : trp]


def imgt_cdr3_numbers(length: int) -> list[str]:
    """IMGT's numbers for a CDR3 of `length` over 13, in order along the chain: 105-111,
    then the positions IMGT adds at the loop's top in the order 112.1, 111.1, 112.2, 111.2
    and so on (IMGT's unique numbering for V domains), so 111.1 onwards, then 112.n down to
    112.1, then 112-117. ANARCI writes 111.1 as 111A."""
    extra = length - 13
    top_112, top_111 = (extra + 1) // 2, extra // 2
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return (
        [str(n) for n in range(105, 112)]
        + [f"111{letters[i]}" for i in range(top_111)]
        + [f"112{letters[i]}" for i in reversed(range(top_112))]
        + [str(n) for n in range(112, 118)]
    )


def cdr3_numbers(values: dict[str, Any], low: int, high: int) -> list[str]:
    (domain,) = values["variable_domains"]
    return [f"{n}{code}" for n, code, _ in domain["numbering"] if low <= n <= high]


def test_ultralong_cdr_h3s_are_numbered_and_kept_whole_in_imgts_scheme(tmp_path: Path) -> None:
    """Bovine BLV1H12's CDR-H3 (PDB 4K3D) is 63 residues by IMGT's definition and B11's
    (PDB 5IHU) 65, as long as natural ones go; human PG9's (PDB 3U4E) 30."""
    chains = ("blv1h12", "b11", "pg9")
    assert [len(imgt_cdr3(SEQUENCES[c])) for c in chains] == [63, 65, 30]
    assert imgt_cdr3(SEQUENCES["blv1h12"]) == (
        "TSVHQETKKYQSCPDGYRERSDCSNRPACGTSDCCRVSVFGNCLTTLPVSYSYTYNYEWHVDV"
    )
    _, output = run(tmp_path, [protein(i, SEQUENCES[c]) for i, c in enumerate(chains, 1)])

    updates = output["updates"]["protein"]
    assert [u["entity"][-1] for u in updates] == ["1", "2", "3"]
    for chain, update in zip(chains, updates, strict=True):
        values = update["values"]
        assert values["chain_type"] == "heavy"
        assert values["cdr_h3"] == imgt_cdr3(SEQUENCES[chain])
        # Every residue is numbered, the CDR's by IMGT's rule for long ones: B11's 52
        # more than 13 take 111A to 111Z and 112Z to 112A, every single letter.
        assert cdr3_numbers(values, 105, 117) == imgt_cdr3_numbers(len(values["cdr_h3"]))
        (domain,) = values["variable_domains"]
        assert "".join(aa for _, _, aa in domain["numbering"]) == SEQUENCES[chain]
    assert output["messages"] == []


def test_a_cdr_too_long_for_kabats_numbering_leaves_its_chain_as_it_was(tmp_path: Path) -> None:
    """Kabat's numbering puts a CDR-H3's insertions at H100 (H100A, H100B, ...): ANARCI
    numbers PG9's 28 residues (H95-H102, by Kabat's definition) so, but not BLV1H12's 61,
    for which it has too few insertion codes, and gives it no numbers at all. Saved without
    them, BLV1H12 would be a heavy chain with no CDRs; it's left as it was, and PG9, in the
    same run, is numbered."""
    _, output = run(
        tmp_path,
        [protein(1, SEQUENCES["blv1h12"], "BLV1H12"), protein(2, SEQUENCES["pg9"])],
        scheme="kabat",
    )

    (update,) = output["updates"]["protein"]
    assert update["entity"][-1] == "2"
    values = update["values"]
    assert values["cdr_h3"] == imgt_cdr3(SEQUENCES["pg9"])[2:] == "EAGGPDYRNGYNYYDFYDGYYNYHYMDV"
    assert cdr3_numbers(values, 95, 102) == (
        [str(n) for n in range(95, 101)]
        + [f"100{letter}" for letter in "ABCDEFGHIJKLMNOPQRST"]
        + ["101", "102"]
    )
    assert output["messages"] == [
        {
            "level": "warning",
            "text": "BLV1H12 was left as it was: ANARCI found a heavy variable domain at "
            "residues 1-169 but couldn't number it in the kabat scheme, which hasn't the "
            "insertion codes for a CDR so long. The imgt scheme numbers longer ones.",
            "entity": "00000000-0000-0000-0000-000000000001",
        }
    ]


def test_a_cdr_definition_whose_numbering_cant_take_the_cdr_leaves_the_cdrs_empty(
    tmp_path: Path,
) -> None:
    """Numbered in IMGT's scheme, BLV1H12 is a heavy chain; Kabat's CDRs are read in
    Kabat's numbering, which ANARCI can't give it, so they're left empty, saying so."""
    _, output = run(tmp_path, [protein(1, SEQUENCES["blv1h12"], "BLV1H12")], cdr_definition="kabat")

    (update,) = output["updates"]["protein"]
    assert update["values"]["chain_type"] == "heavy"
    assert update["values"]["cdr_h3"] is None
    assert "cdrs" not in update["values"]["variable_domains"][0]
    assert [m["text"] for m in output["messages"]] == [
        "BLV1H12: ANARCI couldn't number a domain in the kabat numbering Kabat's CDRs are "
        "defined in, so its CDRs are left empty."
    ]


def test_a_protein_without_an_antibody_domain_is_left_as_it_was(tmp_path: Path) -> None:
    _, output = run(
        tmp_path, [protein(1, SEQUENCES["lysozyme"], "Lysozyme"), protein(2, SEQUENCES["vh"])]
    )

    assert [u["entity"][-1] for u in output["updates"]["protein"]] == ["2"]
    assert output["messages"] == [
        {
            "level": "warning",
            "text": "Lysozyme has no antibody variable domain that ANARCI found: "
            "it was left as it was.",
            "entity": "00000000-0000-0000-0000-000000000001",
        }
    ]


def test_a_residue_anarci_cant_read_leaves_its_protein_as_it_was(tmp_path: Path) -> None:
    odd = SEQUENCES["vh"][:50] + "U" + SEQUENCES["vh"][51:]
    _, output = run(tmp_path, [protein(1, odd), protein(2, SEQUENCES["vl"])])

    assert [c["sequences"] for c in called(tmp_path)] == [[SEQUENCES["vl"]]] * 2
    assert [u["entity"][-1] for u in output["updates"]["protein"]] == ["2"]
    assert output["messages"][0]["text"] == (
        "PRT-000001 has U, which ANARCI can't number: it was left as it was."
    )


def test_none_readable_asks_anarci_nothing(tmp_path: Path) -> None:
    _, output = run(tmp_path, [protein(1, "MKX-Z")])

    assert not (tmp_path / "called").exists()
    assert output["updates"] == {"protein": []}
    assert len(output["messages"]) == 1


def test_a_run_keeps_at_most_100_messages(tmp_path: Path) -> None:
    _, output = run(tmp_path, [protein(i, SEQUENCES["lysozyme"]) for i in range(1, 151)])

    assert len(output["messages"]) == 100
    assert output["messages"][-1] == {"level": "warning", "text": "And 51 more warnings."}
