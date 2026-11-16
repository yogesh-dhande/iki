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
DOMAINS = Draft202012Validator(
    yaml.safe_load(EXTENSION.read_text())["fields"]["variable_domains"]["schema"]
)

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
    tmp_path: Path, proteins: list[dict[str, Any]], scheme: str | None = "imgt"
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
                "params": {} if scheme is None else {"scheme": scheme},
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


@pytest.mark.parametrize(
    ("scheme", "heavy", "light"),
    [
        # Trastuzumab's, as Kabat and Chothia define them (Carter et al. 1992; 1N8Z).
        (
            "kabat",
            ("DTYIH", "RIYPTNGYTRYADSVKG", "WGGDGFYAMDY"),
            ("RASQDVNTAVA", "SASFLYS", "QQHYTTPPT"),
        ),
        (
            "chothia",
            ("GFNIKDT", "YPTNGY", "WGGDGFYAMDY"),
            ("RASQDVNTAVA", "SASFLYS", "QQHYTTPPT"),
        ),
        ("imgt", ("GFNIKDTY", "IYPTNGYT", "SRWGGDGFYAMDY"), ("QDVNTA", "SAS", "QQHYTTPPT")),
    ],
)
def test_each_scheme_has_its_own_cdrs(
    tmp_path: Path, scheme: str, heavy: tuple[str, ...], light: tuple[str, ...]
) -> None:
    _, output = run(tmp_path, [protein(1, SEQUENCES["vh"]), protein(2, SEQUENCES["vl"])], scheme)

    vh, vl = (u["values"] for u in output["updates"]["protein"])
    assert {c["scheme"] for c in called(tmp_path)} == {scheme}
    assert (vh["cdr_h1"], vh["cdr_h2"], vh["cdr_h3"]) == heavy
    assert (vl["cdr_l1"], vl["cdr_l2"], vl["cdr_l3"]) == light
    assert vh["numbering_scheme"] == vl["numbering_scheme"] == scheme
    assert (vl["chain_type"], vl["light_v_gene"]) == ("kappa", "IGKV1-39*01")


def test_the_scheme_is_imgt_unless_chosen(tmp_path: Path) -> None:
    _, output = run(tmp_path, [protein(1, SEQUENCES["vh"])], scheme=None)

    assert {c["scheme"] for c in called(tmp_path)} == {"imgt"}
    assert output["updates"]["protein"][0]["values"]["cdr_h3"] == "SRWGGDGFYAMDY"


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
