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
each with the subclass its prescribing information gives, as are two Fc fusions'
(etanercept, aflibercept) and two bispecifics' chains (emicizumab, amivantamab:
data/fc_fusions_and_bispecifics.fasta). Eculizumab's, an IgG2/IgG4 hybrid, is its
Japanese review report's (PMDA, Soliris, 5 March 2010: data/eculizumab_pmda.fasta),
which says its heavy chain's "CH1, hinge and a part of CH2 regions" are IgG2's and "the
rest of CH2 and CH3 regions" IgG4's. The IGHG3 alleles whose hinges are
shorter than IGHG3*01's are ENA's translations of IMGT's entries for them
(data/ighg3_alleles.fasta): IGHG3*03 (X16110, hinge exons H1, H3 and H4) and IGHG3*04
(X99549, H1 and H4), as IMGT's alignment of the IGHG3 alleles has them.
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
import yaml
from first_party_dependencies import FIRST_PARTY, PLUGINS

from iki_spec import rules
from iki_spec.validate import load_plugin

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


FUSIONS = fasta("fc_fusions_and_bispecifics.fasta")
ECULIZUMAB = fasta("eculizumab_pmda.fasta")
IGHG3 = fasta("ighg3_alleles.fasta")
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
    end = re.search(r"WG[QKE]G[TS]\w{6}", heavy)
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
    """IgG2's CH1 and hinge on IgG4's CH2 and CH3: each domain is one subclass's."""
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


def run_assemble(
    tmp_path: Path, heavy: dict[str, Any], light: dict[str, Any] | None, **params: str
) -> tuple[int, str, dict[str, Any]]:
    """Runs `assemble` on these chains, as a run does."""
    inputs = {"heavy_chain": [heavy], **({"light_chain": [light]} if light else {})}
    (tmp_path / "input.json").write_text(json.dumps({"params": params, "inputs": inputs}))
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


def assemble(tmp_path: Path, heavy_constant: str, **params: str) -> tuple[int, str, dict[str, Any]]:
    """Runs `assemble` on trastuzumab's VH followed by `heavy_constant`, with its light
    chain, as a run does."""
    heavy = chain("heavy", VH + heavy_constant, "H", len(VH))
    return run_assemble(tmp_path, heavy, chain("light", LIGHT, "K", 107), **params)


def test_assembling_takes_the_isotype_from_the_heavy_chain(tmp_path: Path) -> None:
    code, _, output = assemble(tmp_path, constant(HEAVY["D10574"]))  # pembrolizumab's
    assert code == 0
    (made,) = output["creates"]
    assert made["values"]["isotype"] == "igg4"
    said, bonds = output["messages"]
    # An IgG4's light chains bond its heavy chains' C131 (EU), in CH1, and its heavy chains
    # each other at C226 and C229: pembrolizumab's H134, H226 and H229 (KEGG DRUG D10574)
    # behind trastuzumab's VH, which is as long as pembrolizumab's.
    # And each domain's own in each copy: its VH's, CH1's, CH2's and CH3's, and its light
    # chain's VL's and CL's.
    assert bonds["text"] == (
        "Its chains' disulfides, as its subclass places them: H134-L214, H′134-L′214, "
        "H226-H′226, H229-H′229; and 12 within its domains, each domain's own."
    )
    assert len(made["values"]["bonds"]) == 16
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


def test_a_constant_region_no_human_reference_matches_keeps_its_stated_species(
    tmp_path: Path,
) -> None:
    mouse = UNIPROT["P01868"]  # a mouse's IgG1
    code, stdout, _ = assemble(tmp_path, mouse, isotype="igg1")
    assert code == 1
    assert "Give its constant region species with its isotype, igg1" in stdout
    code, _, output = assemble(tmp_path, mouse, isotype="igg1", constant_species="mouse")
    assert code == 0
    values = output["creates"][0]["values"]
    assert (values["isotype"], values["constant_species"]) == ("igg1", "mouse")
    said, bonds = output["messages"]
    assert said["level"] == "warning"
    assert said["text"].endswith("Its isotype, mouse igg1, is as given, unread.")
    # A human subclass's disulfides aren't placed on a mouse's chain: only its variable
    # domains' own and its human light chain's CL's, in each copy.
    placed = output["creates"][0]["values"]["bonds"]
    assert len(placed) == 6
    assert all((b["part"], b["copy"]) == (b["partner"], b["partner_copy"]) for b in placed)
    assert bonds["text"] == (
        "The disulfides between its chains aren't given: its heavy chain's constant region "
        "isn't one human subclass's, so give them yourself if they're known; and 6 within "
        "its domains, each domain's own."
    )


def domains(name: str, *parts: tuple[str, str]) -> dict[str, Any]:
    """A chain of `parts` end to end, each a (kind, residues): H, K or L for a variable
    domain, which numbering finds where it is, or "" for what isn't one."""
    found, sequence = [], ""
    for kind, residues in parts:
        if kind:
            found.append(
                {"chain": kind, "start": len(sequence) + 1, "end": len(sequence) + len(residues)}
            )
        sequence += residues
    for each in found:
        each |= {"numbering": [], "closest": {}, "human": {}}
    return {
        "id": name,
        "hrid": name,
        "values": {"name": name, "sequence": sequence, "variable_domains": found},
    }


#: Trastuzumab's light chain variable domain, and a (G4S)3 linker.
VL = LIGHT[:107]
LINKER = "GGGGS" * 3


def test_a_tandem_of_scfvs_isnt_an_scfv(tmp_path: Path) -> None:
    """An scFv is one heavy and one light variable domain on one chain. Two scFvs on one
    chain (a tandem scFv, such as blinatumomab, a BiTE) have the same kinds of domain, two
    of each, in either orientation; they're format other."""
    single = domains("single", ("H", VH), ("", LINKER), ("K", VL))
    code, _, output = run_assemble(tmp_path, single, None, format="scfv")
    assert code == 0 and output["creates"][0]["values"]["format"] == "scfv"
    for order in ((("H", VH), ("K", VL)), (("K", VL), ("H", VH))):
        tandem = domains(
            "tandem", *order, ("", LINKER), ("", "GGGGS"), ("H", VH), ("", LINKER), ("K", VL)
        )
        code, stdout, output = run_assemble(tmp_path, tandem, None, format="scfv")
        assert code == 1 and output == {}
        kinds = ", ".join(kind for kind, _ in order)
        assert (
            f"The chain of an scFv has a heavy and a light variable domain, and numbering found "
            f"{kinds}, H, K in tandem, in that order. A chain of more variable domains (a "
            "tandem scFv, a DVD-Ig's) is format other."
        ) in stdout
        code, _, output = run_assemble(tmp_path, tandem, None, format="other")
        assert code == 0 and output["creates"][0]["values"]["format"] == "other"


def test_a_dual_variable_domain_igg_isnt_an_igg(tmp_path: Path) -> None:
    """A DVD-Ig's heavy chain has two heavy variable domains before its constant region,
    and its light chain two light ones before CL: an IgG's have one each."""
    ch = constant(HEAVY["D03257"])  # trastuzumab's IgG1 constant region
    two_heavy = domains("dvd heavy", ("H", VH), ("", "ASTKGP"), ("H", VH), ("", ch))
    one_heavy = domains("heavy", ("H", VH), ("", ch))
    two_light = domains("dvd light", ("K", VL), ("", "TVAAP"), ("K", VL), ("", LIGHT[107:]))
    code, stdout, _ = run_assemble(tmp_path, two_heavy, chain("light", LIGHT, "K", 107))
    assert code == 1
    assert "numbering found H, H in dvd heavy, in that order" in stdout
    code, stdout, _ = run_assemble(tmp_path, one_heavy, two_light)
    assert code == 1
    assert (
        "dvd light has 2 light variable domains, and the light chain of a whole antibody "
        "(IgG) has one. A chain of more (a DVD-Ig's) is format other."
    ) in stdout
    code, _, output = run_assemble(tmp_path, two_heavy, two_light, format="other")
    assert code == 0 and output["creates"][0]["values"]["isotype"] == "igg1"


# --- alleles, crossovers and Fc fusions -------------------------------------------------


def between(sequence: str, before: str, after: str) -> str:
    """What's between the end of `before` and the start of `after` in `sequence`."""
    start = sequence.index(before) + len(before)
    return sequence[start : sequence.index(after, start)]


def test_the_other_igg3_hinges_are_their_alleles() -> None:
    """IGHG3*04's hinge is ENA's translation of X99549's two hinge exons, and IGHG3*03's
    of X16110's three, between CH1 and CH2, which are IGHG3*01's. IMGT's alignment of the
    IGHG3 alleles gives them as H1 and H4, and H1, H3 and H4, of IGHG3*01's four exons:
    H1's 17 residues, then 15 each."""
    hinges = {
        gene: (accessions, exons, hinge)
        for gene, accessions, exons, hinge in constant_regions.HINGES["igg3"]
    }
    ch1, ch2 = domain("igg3", "CH1"), domain("igg3", "CH2")
    four = domain("igg3", "hinge")
    h1, h2, h3, h4 = four[:17], four[17:32], four[32:47], four[47:]
    assert h1 + h2 + h3 + h4 == four and len(four) == 62
    assert hinges["IGHG3*04"] == (("X99549",), 2, h1 + h4)
    assert hinges["IGHG3*03"] == (("X16110",), 3, h1 + h3 + h4)
    for accession, gene in (("X99549", "IGHG3*04"), ("X16110", "IGHG3*03")):
        sequence = IGHG3[accession]
        assert between(sequence, ch1[1:], ch2) == hinges[gene][2]
    # IGHG3*03's CH3 has IGHG4's converted region: IMGT's M84>V, K88>R, Q98>E and I101>V.
    assert "TTPPVLDSDGSFFLYSRLTVDKSRWQEGNVF" in IGHG3["X16110"]


@pytest.mark.parametrize(
    ("accession", "gene", "exons"),
    [("X03604", None, 4), ("X16110", "IGHG3*03", 3), ("X99549", "IGHG3*04", 2)],
)
def test_an_igg3_allele_is_igg3_whatever_its_hinges_length(
    tmp_path: Path, accession: str, gene: str | None, exons: int
) -> None:
    """IgG3's hinge has two, three or four exons (Dard et al., Eur J Hum Genet 9:765, 2001):
    a short one isn't an IgG1/IgG3 hybrid, nor IgG1's."""
    found = constant_regions.call(IGHG3[accession])
    assert found.isotypes == ("igg3",), found.describe()
    assert not found.hybrid
    nearest = found.matches[0]
    assert nearest.hinge == (None if gene is None else (gene, (accession,), exons))
    assert nearest.differences <= 4  # IGHG3*03's converted region, at most
    assert found.matches[1].differences - nearest.differences >= 30
    if gene is not None:
        assert f"with {gene}'s {exons}-exon hinge ({accession})" in found.describe()
    code, stdout, _ = assemble(tmp_path, IGHG3[accession], isotype="igg1")
    assert code == 1 and "It isn't IgG1's" in stdout and "give igg3" in stdout
    code, _, output = assemble(tmp_path, IGHG3[accession])
    assert code == 0 and output["creates"][0]["values"]["isotype"] == "igg3"


def test_eculizumabs_ch2_is_igg2s_then_igg4s() -> None:
    """Its CH1, hinge and the start of CH2 are IgG2's, and the rest of CH2 and CH3 IgG4's,
    as its review report says: CH2's first residues are IgG2's APPVAG (IgG4's are APEFLGG),
    and its 37th IgG4's Q (VSQED; IgG2's is VSHED). Between them, the two are the same."""
    region = constant(ECULIZUMAB["PMDA-H"])
    found = constant_regions.call(region)
    assert found.hybrid and found.isotypes == ("igg2", "igg4")
    assert found.parts == {"igg2": ("CH1", "hinge", "CH2"), "igg4": ("CH2", "CH3")}
    crossover = found.crossovers["CH2"]
    assert (crossover.first, crossover.then) == ("igg2", "igg4")
    assert (crossover.last_first, crossover.first_then) == (5, 37)
    ch2 = region[region.index("APPVAG") :]
    assert ch2[:5] == "APPVA" and ch2[36] == "Q" and ch2[:37].endswith("VSQ")
    assert found.describe() == (
        "is a hybrid: its CH1 and hinge IgG2's; its CH3 IgG4's; its CH2 IgG2's to its "
        "residue 5 and IgG4's from its residue 37, the two the same between"
    )


def test_eculizumab_may_be_given_as_either_subclass(tmp_path: Path) -> None:
    heavy = ECULIZUMAB["PMDA-H"]
    vh = len(heavy) - len(constant(heavy))
    chains = (chain("heavy", heavy, "H", vh), chain("light", ECULIZUMAB["PMDA-L"], "K", 107))
    for isotype in ("igg2", "igg4"):
        code, _, output = run_assemble(tmp_path, *chains, isotype=isotype)
        assert code == 0 and output["creates"][0]["values"]["isotype"] == isotype
        assert "its CH2 IgG2's to its residue 5 and IgG4's from" in output["messages"][0]["text"]
    code, _, output = run_assemble(tmp_path, *chains)
    assert code == 0 and "isotype" not in output["creates"][0]["values"]


@pytest.mark.parametrize(
    ("name", "isotype"),
    [("D10821-H1", "igg4"), ("D10821-H2", "igg4"), ("D11894-A", "igg1"), ("D11894-B", "igg1")],
)
def test_a_bispecifics_heavy_chains_are_their_subclass(name: str, isotype: str) -> None:
    """Emicizumab's two heavy chains are IgG4's (S228P, and the charge pairs that make them
    pair with each other), and amivantamab's IgG1's."""
    found = constant_regions.call(constant(FUSIONS[name]))
    assert found.isotypes == (isotype,), found.describe()
    assert not found.crossovers


def unnumbered(name: str, sequence: str) -> dict[str, Any]:
    """A protein as numbering leaves one it finds no variable domain in: as it was."""
    return {"id": name, "hrid": name, "values": {"name": name, "sequence": sequence}}


@pytest.mark.parametrize("kegg", ["D00742", "D09574"])
def test_an_fc_fusions_isotype_is_read_off_the_whole_chain(tmp_path: Path, kegg: str) -> None:
    """Etanercept (TNFR2's extracellular domain on IgG1's hinge, CH2 and CH3) and
    aflibercept (VEGFR domains on the same) have no variable domain, so numbering leaves
    them as they were: an `other` with an isotype compares the whole chain."""
    fusion = unnumbered(kegg, FUSIONS[kegg])
    found = constant_regions.call(FUSIONS[kegg])
    assert found.isotypes == ("igg1",) and found.matches[0].identity > 0.97
    assert not found.has("CH1") and found.has("CH2") and found.has("CH3")
    code, _, output = run_assemble(tmp_path, fusion, None, format="other", isotype="igg1")
    assert code == 0 and output["creates"][0]["values"]["isotype"] == "igg1"
    assert output["messages"][0]["text"].startswith(
        f"Its isotype is igg1, as given: {kegg}, compared whole as it has no numbered "
        "variable domain, is IgG1's"
    )
    code, _, output = run_assemble(tmp_path, fusion, None, format="other")
    assert code == 0 and output["creates"][0]["values"]["isotype"] == "igg1"
    code, stdout, _ = run_assemble(tmp_path, fusion, None, format="other", isotype="igg4")
    assert code == 1 and "It isn't IgG4's" in stdout


def test_a_fabs_heavy_chain_has_no_fc(tmp_path: Path) -> None:
    """A Fab's heavy chain ends in the hinge: one that goes on through CH2 has an Fc's
    domain, however few residues it has after its variable domain. IgG1's CH1, hinge and
    CH2 are 223 residues, fewer than a whole IgG's constant region."""
    ch1, hinge, ch2 = (domain("igg1", d) for d in ("CH1", "hinge", "CH2"))
    assert len(ch1 + hinge + ch2) == 223
    code, stdout, output = assemble(tmp_path, ch1 + hinge + ch2, format="fab")
    assert code == 1 and output == {}
    assert (
        "A Fab's heavy chain has CH1, and perhaps some of the hinge, but no Fc, and heavy's "
        "constant region has IgG1's CH1, hinge and CH2: choose other."
    ) in stdout
    code, stdout, _ = assemble(tmp_path, ch1 + hinge + ch2, format="igg")
    assert code == 1
    assert "heavy's constant region has IgG1's CH1, hinge and CH2 but no CH3: choose other." in (
        stdout
    )
    # A Fab' has the whole hinge, and a pepsin F(ab')2 the first few residues of CH2 too.
    for fab in (ch1, ch1 + hinge, ch1 + hinge + ch2[:5]):
        code, _, output = assemble(tmp_path, fab, format="fab")
        assert code == 0 and output["creates"][0]["values"]["isotype"] == "igg1"
    # A whole IgG's heavy chain without its C-terminal lysine is still a whole one.
    code, _, output = assemble(tmp_path, constant(HEAVY["D03257"])[:-1], format="igg")
    assert code == 0


def test_assemble_records_the_aligner_it_runs() -> None:
    """`assemble` aligns with Biopython's PairwiseAligner and BLOSUM62, so what it makes is
    recorded as made with Biopython, at the version its environment pins (ADR 0106)."""
    manifest = yaml.safe_load((SCRIPTS.parent / "actions" / "assemble.yaml").read_text())
    pinned = re.search(
        r"^biopython==(\S+)", (SCRIPTS.parent / "requirements.txt").read_text(), re.M
    )
    assert pinned is not None
    assert manifest["software"] == [
        {"name": "Biopython", "version": pinned.group(1), "url": "https://biopython.org"}
    ]
    assert "from Bio import Align" in (SCRIPTS / "constant_regions.py").read_text()


# --- How much of what bonds it its bonds are -------------------------------------------------


def bonds_of(output: dict[str, Any]) -> list[tuple[str, int, int, str, int, int]]:
    return [
        (b["part"], b["copy"], b["residue"], b["partner"], b["partner_copy"], b["partner_residue"])
        for b in output["creates"][0]["values"].get("bonds") or []
    ]


def test_a_light_chain_without_its_last_cysteine_leaves_its_heavy_chains_unpaired(
    tmp_path: Path,
) -> None:
    """Trastuzumab's light chain (KEGG DRUG D03257) without its C-terminal C214: its bond to
    the heavy chain's C220 (H223) has no cysteine to end on, so it isn't given, and its
    constant domain's own, L134-L194, stays where it is, not moved to bond the heavy chain."""
    heavy = chain("heavy", HEAVY["D03257"], "H", len(VH))
    code, log, output = run_assemble(tmp_path, heavy, chain("light", LIGHT[:-1], "K", 107))
    assert code == 0, log
    made = bonds_of(output)
    assert ("light", 1, 134, "light", 1, 194) in made and ("light", 2, 134, "light", 2, 194) in made
    assert not any(b[3] == "light" and b[0] == "heavy" for b in made)
    assert len(made) == 14
    assert output["creates"][0]["values"]["bond_map"] == "incomplete"
    said = output["messages"][-1]
    assert said["level"] == "warning"
    assert said["text"] == (
        "Not given, as a cysteine one needs isn't where its chain's domains put it: H223's to "
        "its light chain, which has no cysteine where a light chain's bonds its heavy chain, "
        "at its constant domain's end. So its bonds don't include every one it has."
    )
    # The whole chain has KEGG's.
    code, _, output = run_assemble(tmp_path, heavy, chain("light", LIGHT, "K", 107))
    assert ("heavy", 1, 223, "light", 1, 214) in bonds_of(output)
    assert output["creates"][0]["values"]["bond_map"] == "complete"


#: Avelumab (KEGG DRUG D10817), an IgG1 with a lambda light chain.
AVELUMAB_HEAVY = (
    "EVQLLESGGGLVQPGGSLRLSCAASGFTFSSYIMMWVRQAPGKGLEWVSSIYPSGGITFYADTVKGRFTISRDNSKNTLYLQMNSLRAED"
    "TAVYYCARIKLGTVTTVDYWGQGTLVTVSSASTKGPSVFPLAPSSKSTSGGTAALGCLVKDYFPEPVTVSWNSGALTSGVHTFPAVLQSS"
    "GLYSLSSVVTVPSSSLGTQTYICNVNHKPSNTKVDKKVEPKSCDKTHTCPPCPAPELLGGPSVFLFPPKPKDTLMISRTPEVTCVVVDVS"
    "HEDPEVKFNWYVDGVEVHNAKTKPREEQYNSTYRVVSVLTVLHQDWLNGKEYKCKVSNKALPAPIEKTISKAKGQPREPQVYTLPPSRDE"
    "LTKNQVSLTCLVKGFYPSDIAVEWESNGQPENNYKTTPPVLDSDGSFFLYSKLTVDKSRWQQGNVFSCSVMHEALHNHYTQKSLSLSPGK"
)
AVELUMAB_LIGHT = (
    "QSALTQPASVSGSPGQSITISCTGTSSDVGGYNYVSWYQQHPGKAPKLMIYDVSNRPSGVSNRFSGSKSGNTASLTISGLQAEDEADYYC"
    "SSYTSSSTRVFGTGTKVTVLGQPKANPTVTLFPPSSEELQANKATLVCLISDFYPGAVTVAWKADGSPVKAGVETTKPSKQSNNKYAASS"
    "YLSLTPEQWKSHRSYSCQVTHEGSTVEKTVAPTECS"
)


def test_a_lambda_chains_cysteines_are_where_its_constant_domain_puts_them(
    tmp_path: Path,
) -> None:
    """Avelumab's lambda chain bonds its heavy chain from C215, next to its last residue,
    and its constant domain's own disulfide is L138-L197, as KEGG DRUG D10817 lists them
    (H223-L215, and L138-L197, which it prints as H138-L197)."""
    heavy = chain("heavy", AVELUMAB_HEAVY, "H", AVELUMAB_HEAVY.index("ASTKGP"))
    light = chain("light", AVELUMAB_LIGHT, "L", AVELUMAB_LIGHT.index("GQPKANP"))
    code, log, output = run_assemble(tmp_path, heavy, light)
    assert code == 0, log
    made = bonds_of(output)
    assert ("heavy", 1, 223, "light", 1, 215) in made
    assert ("light", 1, 138, "light", 1, 197) in made
    assert ("light", 1, 22, "light", 1, 90) in made
    assert len(made) == 16
    assert output["creates"][0]["values"]["bond_map"] == "complete"


def test_a_native_igg4s_hinge_bonds_are_its_predominant_forms(tmp_path: Path) -> None:
    """A native IgG4's hinge, S228 (EU), bonds within each heavy chain in some of its
    molecules (Angal et al., Mol Immunol 30:105, 1993; Bloom et al., Protein Sci 6:407,
    1997): IMGT's IGHG4*01 behind trastuzumab's VH. Pembrolizumab's (KEGG DRUG D10574)
    has S228P, which keeps them between its chains."""
    code, _, output = assemble(tmp_path, reference("igg4"))
    assert code == 0
    assert ("heavy", 1, 226, "heavy", 2, 226) in bonds_of(output)
    assert output["creates"][0]["values"]["bond_map"] == "predominant"
    said = output["messages"][-1]
    assert said["level"] == "warning"
    assert said["text"].startswith("Its hinge is a native IgG4's, S228 (EU)")
    code, _, output = assemble(tmp_path, constant(HEAVY["D10574"]))
    assert code == 0
    assert output["creates"][0]["values"]["bond_map"] == "complete"


def test_an_iggs_heavy_chain_has_all_of_ch3(tmp_path: Path) -> None:
    """IgG1's CH1, hinge and CH2, and ten residues of its CH3, which leave out both of
    CH3's cysteines: some of CH3 isn't a whole IgG's heavy chain."""
    ch1, hinge, ch2, ch3 = (domain("igg1", d) for d in ("CH1", "hinge", "CH2", "CH3"))
    code, stdout, output = assemble(tmp_path, ch1 + hinge + ch2 + ch3[:10])
    assert code == 1 and output == {}
    assert (
        "A whole antibody's heavy chain has all of CH1, CH2 and CH3, and heavy's constant "
        "region has 10 of 107 CH3 residues of IgG1's: if its sequence is the whole chain, "
        "choose other."
    ) in stdout
    code, _, _ = assemble(tmp_path, ch1 + hinge + ch2 + ch3)
    assert code == 0


#: A mouse IgG1's constant region (UniProt P01868): CH1 1-97, the hinge 98-110, CH2
#: 111-217, CH3 218-324.
MOUSE_IGG1 = UNIPROT["P01868"]


def test_a_mouse_fabs_domains_are_read_off_the_nearest_human_one(tmp_path: Path) -> None:
    """A mouse IgG1's CH1, hinge and CH2 aren't a Fab's, though no human isotype matches
    them and they're fewer residues than a whole IgG's constant region: they align with
    human IgG2's domain by domain, 66% identical. Its CH1 and hinge alone are a Fab's."""
    code, stdout, output = assemble(tmp_path, MOUSE_IGG1[:217], format="fab")
    assert code == 1 and output == {}
    assert (
        "A Fab's heavy chain has CH1, and perhaps some of the hinge, but no Fc, and heavy's "
        "constant region aligns with IgG2's CH1, hinge and CH2, the nearest human one's "
        "(65.8% identical): choose other."
    ) in stdout
    code, _, output = assemble(tmp_path, MOUSE_IGG1[:110], format="fab")
    assert code == 0 and output["creates"][0]["values"]["format"] == "fab"
    code, _, output = assemble(
        tmp_path, MOUSE_IGG1, format="igg", isotype="igg1", constant_species="mouse"
    )
    assert code == 0


def test_a_constant_region_unlike_any_antibodys_isnt_a_fabs(tmp_path: Path) -> None:
    """Hen lysozyme (UniProt P00698, its 129 mature residues) behind a VH aligns with no
    human constant region well enough for its domains to be told."""
    lysozyme = (
        "KVFGRCELAAAMKRHGLDNYRGYSLGNWVCAAKFESNFNTQATNRNTDGSTDYGILQINSRWWCNDGRTPGSRNLCNIPCSALLSSD"
        "ITASVNCAKKIVSDGNGMNAWVAWRNRCKGTDVQAWIRGCRL"
    )
    code, stdout, _ = assemble(tmp_path, lysozyme, format="fab")
    assert code == 1
    assert "constant region is too unlike every human one for its domains to be told" in stdout


def test_only_a_chain_whose_ch3_pairs_is_two_copies(tmp_path: Path) -> None:
    """An isolated CH2 is a monomer (Prabakaran et al., Acta Crystallogr D 64:1062, 2008),
    and an Fc fusion's CH3 pairs it with another, as etanercept's two chains are."""
    ch2 = unnumbered("CH2", domain("igg1", "CH2"))
    code, _, output = run_assemble(tmp_path, ch2, None, format="other")
    assert code == 0
    assert output["creates"][0]["values"]["components"][0]["copies"] == 1
    fusion = unnumbered("D00742", FUSIONS["D00742"])
    code, _, output = run_assemble(tmp_path, fusion, None, format="other")
    assert code == 0
    assert output["creates"][0]["values"]["components"][0]["copies"] == 2


def test_an_igm_is_one_subunit_of_its_pentamer(tmp_path: Path) -> None:
    """Human IgM's secreted constant region (UniProt P01871-1) makes one subunit, two heavy
    and two light chains; its pentamer, with a J chain, or hexamer is a protein complex."""
    code, _, output = assemble(tmp_path, UNIPROT["P01871-1"], format="other")
    assert code == 0
    made = output["creates"][0]["values"]
    assert made["isotype"] == "igm" and made["components"][0]["copies"] == 2
    assert any(
        m["text"].startswith("An IgM is secreted as a pentamer, 10 heavy chains, 10 light chains")
        and "iki/complex.ProteinComplex" in m["text"]
        for m in output["messages"]
    )


def test_a_mouse_igg2a_is_recorded_as_its_species_names_it(tmp_path: Path) -> None:
    """Mouse Ighg2a (UniProt P01863) is IgG2a, a subclass a human hasn't."""
    mouse = UNIPROT["P01863"]
    code, _, output = assemble(tmp_path, mouse, isotype="igg2a", constant_species="mouse")
    assert code == 0
    values = output["creates"][0]["values"]
    assert (values["format"], values["isotype"], values["constant_species"]) == (
        "igg",
        "igg2a",
        "mouse",
    )
    code, stdout, _ = assemble(tmp_path, mouse, isotype="igg4", constant_species="mouse")
    assert code == 1
    assert "A mouse constant region has no igg4: give one of igg1, igg2a, igg2b, igg2c, igg3." in (
        stdout
    )
    code, stdout, _ = assemble(tmp_path, mouse, isotype="igg2a", constant_species="human")
    assert code == 1
    assert "A human constant region has no igg2a: give one of igg1, igg2, igg3, igg4." in stdout
    code, stdout, _ = assemble(tmp_path, mouse, isotype="igg1", constant_species="human")
    assert code == 1
    assert "so it isn't a human constant region: give the species it is" in stdout
    code, stdout, _ = assemble(tmp_path, mouse, isotype="igg", constant_species="rabbit")
    assert code == 0  # the species is the registrant's statement; IgG is a rabbit's one IgG


def test_a_human_constant_region_is_human_and_no_other_species(tmp_path: Path) -> None:
    human = constant(HEAVY["D03257"])  # trastuzumab's IgG1
    code, _, output = assemble(tmp_path, human)
    values = output["creates"][0]["values"]
    assert code == 0 and (values["isotype"], values["constant_species"]) == ("igg1", "human")
    # Its disulfides are IgG1's pattern, not read from this molecule (ADR 0267).
    assert values["bonds"] and values["bonds_from"] == "pattern"
    code, stdout, _ = assemble(tmp_path, human, constant_species="mouse")
    assert code == 1
    assert "a human constant region, not a mouse one: leave the species out" in stdout
    code, stdout, _ = assemble(tmp_path, human, isotype="igg2a")
    assert code == 1


def test_the_species_rule_holds_at_every_save() -> None:
    plugin = PLUGINS / "antibody"
    loaded = load_plugin(plugin, dependencies=FIRST_PARTY[plugin])
    assert loaded.plugin is not None, loaded.problems
    antibody = loaded.plugin.types["iki/antibody.Antibody"]
    schema = rules.rule_variables(rules.record_schema(antibody.label, antibody.fields, None))
    named = ("an_iggs_isotype_is_a_subclass", "the_isotype_is_its_species")
    checks = [antibody.rules[name] for name in named]
    cases = [
        ({"format": "igg", "isotype": "igg2a", "constant_species": "mouse"}, [True, True]),
        ({"format": "igg", "isotype": "igg2a", "constant_species": "human"}, [True, False]),
        ({"format": "igg", "isotype": "igg4", "constant_species": "mouse"}, [True, False]),
        ({"format": "igg", "isotype": "igg", "constant_species": "rabbit"}, [True, True]),
        ({"format": "igg", "isotype": "igm", "constant_species": "rabbit"}, [False, True]),
        ({"format": "fab", "isotype": "igd", "constant_species": "rabbit"}, [True, False]),
        ({"format": "igg", "isotype": "igg1", "constant_species": None}, [True, True]),
    ]
    for record, wanted in cases:
        found = []
        for rule in checks:
            assert rule.check is not None
            sources = [(rule.check, rules.CONDITION)]
            if rule.when:
                sources.append((rule.when, rules.CONDITION))
            (outcomes,) = rules.evaluate(schema, sources, [{"record": record}])
            applies = outcomes[1].value is True if rule.when else True
            found.append(not applies or outcomes[0].value is True)
        assert found == wanted, record
