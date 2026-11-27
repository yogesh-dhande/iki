"""Every plugin weighs by one table of atomic weights (ADR 0271): CIAAW's standard atomic
weights 2024, each plugin's copy the same, and the web packages' too."""

import ast
import importlib
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
COPIES = sorted(
    [
        *ROOT.glob("plugins/*/scripts/weights.py"),
        *ROOT.glob("examples/plugins/*/scripts/weights.py"),
    ]
)
#: The plugins that weigh a molecule's average mass, which must each have the table.
WEIGHERS = {"biologics_ms", "complex", "conjugate", "lots", "oligo", "protein", "small_molecule"}


def table() -> ModuleType:
    sys.path.insert(0, str(COPIES[0].parent))
    try:
        return importlib.import_module("weights")
    finally:
        sys.path.remove(str(COPIES[0].parent))


def test_every_plugin_that_weighs_has_the_same_table() -> None:
    assert {p.parents[1].name for p in COPIES} >= WEIGHERS
    first = COPIES[0].read_bytes()
    assert [p.relative_to(ROOT) for p in COPIES if p.read_bytes() != first] == []


@pytest.mark.parametrize(
    ("element", "weight"),
    [
        # Abridged values of elements whose standard atomic weight is an interval,
        # https://www.ciaaw.org/abridged-atomic-weights.htm (2024).
        ("H", 1.0080), ("C", 12.011), ("N", 14.007), ("O", 15.999), ("S", 32.06),
        ("Cl", 35.45), ("Br", 79.904), ("Mg", 24.305), ("B", 10.81), ("Pb", 207.2),
        # Standard atomic weights, https://www.ciaaw.org/atomic-weights.htm (2024),
        # gadolinium's revised in 2024.
        ("P", 30.973761998), ("F", 18.998403162), ("Na", 22.98976928), ("I", 126.90447),
        ("Se", 78.971), ("Fe", 55.845), ("Zn", 65.38), ("Gd", 157.249), ("U", 238.02891),
    ],
)  # fmt: skip
def test_the_table_is_ciaaws(element: str, weight: float) -> None:
    assert table().AVERAGE[element] == weight


def test_an_element_without_a_standard_atomic_weight_has_none() -> None:
    # Technetium, promethium and every element past uranium but thorium and protactinium.
    assert not {"Tc", "Pm", "Po", "Rn", "Pu"} & table().AVERAGE.keys()
    assert len(table().AVERAGE) == 84


def test_trastuzumab_weighs_the_same_in_every_plugin() -> None:
    """KEGG D03257's formula, by the table: what the complex, conjugate and biologics MS
    plugins all give it now, where they gave 145,529.81, 145,532.15 and 145,531.86:
    6470 × 12.011 + 10012 × 1.008 + 1726 × 14.007 + 2013 × 15.999 + 42 × 32.06."""
    formula = {"C": 6470, "H": 10012, "N": 1726, "O": 2013, "S": 42}
    assert table().average_mass(formula) == pytest.approx(145531.855, abs=1e-6)


def _ts_record(source: str, name: str) -> dict[str, str]:
    block = re.search(rf"export const {name}[^=]*= \{{(.*?)\}};", source, re.S)
    assert block, name
    return dict(re.findall(r"^\s*([A-Za-z]+): \"?([^\",]+)\"?,", block[1], re.M))


def test_the_web_packages_weigh_by_the_same_table() -> None:
    source = (ROOT / "web/packages/science/src/molecular-data.ts").read_text()
    weights = table()
    average = _ts_record(source, "AVERAGE")
    assert {k: float(v) for k, v in average.items()} == {k: weights.AVERAGE[k] for k in average}
    residues = _ts_record(source, "RESIDUES")
    hill = {
        k: "".join(f"{e}{n if n > 1 else ''}" for e, n in v.items())
        for k, v in weights.RESIDUES.items()
    }
    assert residues == hill
    assert (
        "AVERAGE_H = AVERAGE["
        in (ROOT / "web/packages/science/src/composition-mass.ts").read_text()
    )


def test_no_plugin_weighs_by_another_table() -> None:
    """RDKit's and Biopython's own tables weigh by other atomic weights: a plugin's average
    mass is never theirs."""
    other = re.compile(
        r"Descriptors\.MolWt\(|GetAtomicWeight\(|\bmolecular_weight\(|ProteinAnalysis\([^)]*\)\.molecular_weight"
    )
    found = [
        f"{path.relative_to(ROOT)}:{node.lineno}"
        for path in [
            *ROOT.glob("plugins/*/scripts/*.py"),
            *ROOT.glob("examples/plugins/*/scripts/*.py"),
        ]
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Call) and other.search(ast.unparse(node.func) + "(")
    ]
    assert found == []
