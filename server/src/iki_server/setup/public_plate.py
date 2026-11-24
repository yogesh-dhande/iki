"""Offline Tox21 firefly luciferase plate shared by the starter and the dev seed."""

import json
from pathlib import Path
from typing import Any

DATA = Path(__file__).with_name("data")
READER_FILE = DATA / "aid-1224835-activity.csv"
ASSAY = "PubChem AID 1224835"
PLATE_NAME = "Tox21 firefly luciferase inhibition"
with (DATA / "aid-1224835.json").open() as archive:
    SOURCE = json.load(archive)
SERIES: dict[str, dict[str, Any]] = SOURCE["series"]

# Canonical Photinus pyralis luciferase, UniProt P08659, sequence version 1.
LUCIFERASE = (
    "MEDAKNIKKGPAPFYPLEDGTAGEQLHKAMKRYALVPGTIAFTDAHIEVNITYAEYFEMSVRLAEAMKRYGLNTNHRIVV"
    "CSENSLQFFMPVLGALFIGVAVAPANDIYNERELLNSMNISQPTVVFVSKKGLQKILNVQKKLPIIQKIIIMDSKTDYQG"
    "FQSMYTFVTSHLPPGFNEYDFVPESFDRDKTIALIMNSSGSTGLPKGVALPHRTACVRFSHARDPIFGNQIIPDTAILSV"
    "VPFHHGFGMFTTLGYLICGFRVVLMYRFEEELFLRSLQDYKIQSALLVPTLFSFFAKSTLIDKYDLSNLHEIASGGAPLS"
    "KEVGEAVAKRFHLPGIRQGYGLTETTSAILITPEGDDKPGAVGKVVPFFEAKVVDLDTGKTLGVNQRGELCVRGPMIMSG"
    "YVNNPEATNALIDKDGWLHSGDIAYWDEDEHFFIVDRLKSLIKYKGYQVAPAELESILLQHPNIFDAGVAGLPDDDAGEL"
    "PAAVVVLEHGKTMTEKEIVDYVASQVTTAKKLRGGVVFVDEVPKGLTGKLDARKIREILIKAKKGGKSKL"
)


def compound_values(key: str) -> dict[str, str]:
    series = SERIES[key]
    return {
        "name": series["name"],
        "smiles": series["smiles"],
        "inchikey": series["inchikey"],
        "pubchem_cid": str(series["cid"]),
    }


def assignments(compounds: dict[str, str]) -> list[dict[str, Any]]:
    rows = []
    for key, series in SERIES.items():
        for replicate, letter in enumerate(series["rows"], 1):
            for column, dose in enumerate(SOURCE["concentrations_uM"], 1):
                rows.append(
                    {
                        "well": f"{letter}{column}",
                        "sample": compounds[key],
                        "concentration": dose,
                        "concentration_unit": "uM",
                        "replicate": replicate,
                    }
                )
    rows.extend(
        {key: value for key, value in c.items() if key != "sid"} for c in SOURCE["controls"]
    )
    return rows
