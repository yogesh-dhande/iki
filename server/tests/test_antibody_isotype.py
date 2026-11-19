"""The antibody plugin reads an antibody's isotype off its heavy chain's constant region.

`assemble` compares the residues after a heavy chain's variable domain with human
constant regions (plugins/antibody/scripts/constant_regions.py), which are IMGT's: the
secreted form of each IGHC gene's first allele, from IMGT's protein display of the human
IGH C-REGIONs (IGHG1*01 from EMBL J00228, IGHG2*01 J00230, IGHG3*01 X03604, IGHG4*01
K01316, IGHA1*01 J00220, IGHA2*01 J00221, IGHM*01 X14940, IGHE*01 J00222, IGHD*01
K02875 to K02879; IMGT data are CC BY 4.0).

They're checked here against UniProt's secreted isoforms of the same genes, as UniProt
gives them (data/ighc_uniprot.fasta, downloaded from rest.uniprot.org on 2026-11-18;
UniProt is CC BY 4.0): P01857-1 (IGHG1), P01859-1 (IGHG2), P01860-1 (IGHG3), P01861-1
(IGHG4), P01876-1 (IGHA1), P01877-1 (IGHA2), P01871-1 (IGHM), P01854-1 (IGHE) and
P01880-1 (IGHD). The same file has two mouse constant regions, P01868 (Ighg1, secreted)
and P01863 (Ighg2a), which no human reference should claim.

Marketed antibodies' heavy chains are KEGG DRUG's (data/antibody_heavy_chains.fasta),
each with the subclass its prescribing information gives.
"""

import importlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "plugins" / "antibody" / "scripts"
DATA = Path(__file__).resolve().parent / "data"


def _module(name: str) -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))
    try:
        return importlib.import_module(name)
    finally:
        sys.path.remove(str(SCRIPTS))


constant_regions = _module("constant_regions")


def fasta(name: str) -> dict[str, str]:
    """A FASTA file's sequences, by the first word of each header."""
    found: dict[str, str] = {}
    header = ""
    for line in (DATA / name).read_text().splitlines():
        if line.startswith(">"):
            header = line[1:].split()[0]
            found[header] = ""
        else:
            found[header] += line.strip()
    return found


UNIPROT = {
    header.split("|")[1]: sequence for header, sequence in fasta("ighc_uniprot.fasta").items()
}
HEAVY = fasta("antibody_heavy_chains.fasta")
#: Where IMGT's *01 allele differs from UniProt's secreted isoform: (position from 1, IMGT's
#: residue, UniProt's). These are allelic: UniProt's sequences are other alleles'.
ALLELIC = {
    "igg1": [],
    "igg2": [(257, "A", "S")],
    "igg3": [],
    "igg4": [],
    "iga1": [],
    "iga2": [
        (93, "P", "S"),
        (102, "P", "R"),
        (279, "F", "Y"),
        (296, "D", "E"),
        (319, "L", "M"),
        (326, "V", "I"),
        (335, "V", "A"),
    ],
    "igm": [(40, "L", "F")],
    "ige": [(43, "C", "W")],
    "igd": [],
}


def reference(isotype: str) -> str:
    return "".join(sequence for _, sequence in constant_regions.REFERENCES[isotype][3])


def domain(isotype: str, name: str) -> str:
    return str(dict(constant_regions.REFERENCES[isotype][3])[name])


def constant(heavy: str) -> str:
    """What follows a heavy chain's variable domain: after its J region's WGxG…VTVSS."""
    end = re.search(r"WG[QK]G[TS]\w{6}", heavy)
    assert end is not None
    return heavy[end.end() :]


@pytest.mark.parametrize("isotype", list(ALLELIC))
def test_each_reference_is_imgts_allele_as_uniprot_has_it(isotype: str) -> None:
    gene, accessions, uniprot, domains = constant_regions.REFERENCES[isotype]
    ours, theirs = reference(isotype), UNIPROT[uniprot]
    assert gene.startswith("IGH") and gene.endswith("*01") and accessions
    assert len(ours) == len(theirs)
    differ = [(i, a, b) for i, (a, b) in enumerate(zip(ours, theirs, strict=True), 1) if a != b]
    assert differ == ALLELIC[isotype]
    # Every domain is there, in order, with the hinge after CH1 where there is one.
    names = [name for name, _ in domains]
    assert names[0] == "CH1" and names[-1] in ("CH3", "CH4")
    assert ("hinge" in names) == (isotype not in ("igm", "ige"))


@pytest.mark.parametrize("isotype", list(ALLELIC))
def test_a_constant_region_is_the_isotype_whose_reference_it_is(isotype: str) -> None:
    found = constant_regions.call(UNIPROT[constant_regions.REFERENCES[isotype][2]])
    assert found.isotypes == (isotype,)
    assert found.matches[0].identity > 0.97  # an allele apart, at most
    # And so is its CH1 alone, as a Fab's heavy chain has it.
    assert constant_regions.call(domain(isotype, "CH1")).isotypes == (isotype,)


@pytest.mark.parametrize(
    ("kegg", "isotype"),
    [
        ("D02597", "igg1"),  # adalimumab
        ("D03257", "igg1"),  # trastuzumab
        ("D04603", "igg1"),  # ipilimumab
        ("D10773", "igg1"),  # atezolizumab, N297A
        ("D11380", "igg1"),  # nirsevimab, M252Y/S254T/T256E (YTE)
        ("D09575", "igg4"),  # ibalizumab
        ("D10316", "igg4"),  # nivolumab, S228P
        ("D10354", "igg4"),  # dupilumab, S228P
        ("D10574", "igg4"),  # pembrolizumab, S228P
    ],
)
def test_marketed_antibodies_are_their_subclasses_whole_and_as_fabs(
    kegg: str, isotype: str
) -> None:
    region = constant(HEAVY[kegg])
    found = constant_regions.call(region)
    assert found.isotypes == (isotype,), found.describe()
    assert found.matches[1].differences - found.matches[0].differences >= 15
    # A Fab's heavy chain: CH1 and the start of the hinge.
    assert constant_regions.call(region[:103]).isotypes == (isotype,)


def test_a_hybrids_domains_are_each_their_own_subclasss() -> None:
    """As eculizumab's are: CH1 and the hinge IgG2's, CH2 and CH3 IgG4's."""
    hybrid = "".join(
        [
            domain("igg2", "CH1"),
            domain("igg2", "hinge"),
            domain("igg4", "CH2"),
            domain("igg4", "CH3"),
        ]
    )
    found = constant_regions.call(hybrid)
    assert found.hybrid
    assert found.parts == {"igg2": ("CH1", "hinge"), "igg4": ("CH2", "CH3")}
    assert found.describe() == "is a hybrid: its CH1 and hinge IgG2's; its CH2 and CH3 IgG4's"


@pytest.mark.parametrize("accession", ["P01868", "P01863"])
def test_a_mouse_constant_region_isnt_called_a_human_one(accession: str) -> None:
    found = constant_regions.call(UNIPROT[accession])
    assert found.isotypes == ()
    assert found.matches[0].identity < 0.7
    assert found.describe().startswith("matches no human constant region: the nearest is ")


# --- assemble -----------------------------------------------------------------------------

VH = "EVQLVESGGGLVQPGGSLRLSCAASGFNIKDTYIHWVRQAPGKGLEWVARIYPTNGYTRYADSVKGRFTISADTSKNTAYLQMNSLRAEDTAVYYCSRWGGDGFYAMDYWGQGTLVTVSS"  # noqa: E501 - trastuzumab's, KEGG D03257
LIGHT = (
    "DIQMTQSPSSLSASVGDRVTITCRASQDVNTAVAWYQQKPGKAPKLLIYSASFLYSGVPSRFSGSRSGTDFTLTISSLQPEDFATYY"
    "CQQHYTTPPTFGQGTKVEIKRTVAAPSVFIFPPSDEQLKSGTASVVCLLNNFYPREAKVQWKVDNALQSGNSQESVTEQDSKDSTYS"
    "LSSTLTLSKADYEKHKVYACEVTHQGLSSPVTKSFNRGEC"
)


def chain(name: str, sequence: str, kind: str, end: int) -> dict[str, Any]:
    domains = [{"chain": kind, "start": 1, "end": end, "numbering": [], "closest": {}, "human": {}}]
    return {
        "id": name,
        "hrid": name,
        "values": {"name": name, "sequence": sequence, "variable_domains": domains},
    }


def assemble(tmp_path: Path, heavy_constant: str, **params: str) -> tuple[int, str, dict[str, Any]]:
    """Runs `assemble` on trastuzumab's VH followed by `heavy_constant`, with its light
    chain, as a run does."""
    (tmp_path / "input.json").write_text(
        json.dumps(
            {
                "params": params,
                "inputs": {
                    "heavy_chain": [chain("heavy", VH + heavy_constant, "H", len(VH))],
                    "light_chain": [chain("light", LIGHT, "K", 107)],
                },
            }
        )
    )
    output = tmp_path / "output.json"
    output.unlink(missing_ok=True)
    result = subprocess.run(  # noqa: S603 - our own script
        [sys.executable, str(SCRIPTS / "antibodies.py"), "assemble"],
        env={
            "PATH": os.environ["PATH"],
            "IKI_INPUT": str(tmp_path / "input.json"),
            "IKI_OUTPUT": str(output),
        },
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    return (
        result.returncode,
        result.stdout,
        json.loads(output.read_text()) if output.exists() else {},
    )


def test_assembling_takes_the_isotype_from_the_heavy_chain(tmp_path: Path) -> None:
    code, _, output = assemble(tmp_path, constant(HEAVY["D10574"]))  # pembrolizumab's
    assert code == 0
    (made,) = output["creates"]
    assert made["values"]["isotype"] == "igg4"
    (said,) = output["messages"]
    assert said["level"] == "info"
    assert said["text"].startswith(
        "Its isotype is igg4, from its heavy chain: heavy's constant region is IgG4's, 99.7% "
        "identical to IMGT's IGHG4*01 (K01316) over 327 residues; the next nearest, IgG2's, is"
    )


def test_an_isotype_the_heavy_chain_isnt_is_refused(tmp_path: Path) -> None:
    code, stdout, output = assemble(tmp_path, constant(HEAVY["D10574"]), isotype="igg1")
    assert code == 1 and output == {}
    assert "It isn't IgG1's (91.2% identical to it): give igg4, or leave the isotype out" in stdout
    # A Fab's CH1 says it as well.
    code, stdout, _ = assemble(
        tmp_path, constant(HEAVY["D10574"])[:103], format="fab", isotype="igg2"
    )
    assert code == 1
    assert "It isn't IgG2's" in stdout and "give igg4" in stdout


def test_an_igg_whose_constant_region_is_another_class_is_refused(tmp_path: Path) -> None:
    code, stdout, _ = assemble(tmp_path, UNIPROT["P01876-1"])  # IgA1's
    assert code == 1
    assert "is IgA1's" in stdout and "so choose other" in stdout
    code, _, output = assemble(tmp_path, UNIPROT["P01876-1"], format="other")
    assert code == 0 and output["creates"][0]["values"]["isotype"] == "iga1"


def test_a_hybrid_may_be_given_as_either_subclass_and_isnt_guessed(tmp_path: Path) -> None:
    hybrid = domain("igg2", "CH1") + domain("igg2", "hinge") + domain("igg4", "CH2")
    hybrid += domain("igg4", "CH3")
    code, _, output = assemble(tmp_path, hybrid, isotype="igg4")
    assert code == 0 and output["creates"][0]["values"]["isotype"] == "igg4"
    assert output["messages"][0]["level"] == "warning"
    assert "a hybrid" in output["messages"][0]["text"]
    code, _, output = assemble(tmp_path, hybrid)
    assert code == 0 and "isotype" not in output["creates"][0]["values"]
    assert output["messages"][0]["text"].endswith("give igg2 or igg4, whichever it's called.")
    code, stdout, _ = assemble(tmp_path, hybrid, isotype="igg1")
    assert code == 1 and "give igg2 or igg4" in stdout


def test_a_constant_region_no_human_reference_matches_is_unchecked(tmp_path: Path) -> None:
    code, _, output = assemble(tmp_path, UNIPROT["P01868"], isotype="igg1")  # a mouse's IgG1
    assert code == 0 and output["creates"][0]["values"]["isotype"] == "igg1"
    (said,) = output["messages"]
    assert said["level"] == "warning"
    assert said["text"].endswith("Its isotype, igg1, is as given, unchecked.")
