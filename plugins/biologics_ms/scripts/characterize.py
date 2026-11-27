"""Compare reported LC-MS observations with registered antibody sequences."""

from __future__ import annotations

import csv
import json
import math
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from weights import AVERAGE, chain_formula

# Average masses: CIAAW's standard atomic weights, the table every Iki plugin weighs by
# (weights.py). Monoisotopic masses: each element's most abundant isotope's, AME2020,
# https://doi.org/10.1088/1674-1137/abddaf.
ATOMS = {
    "average": AVERAGE,
    "monoisotopic": {
        "C": 12.0,
        "H": 1.00782503223,
        "N": 14.00307400443,
        "O": 15.99491461957,
        "S": 31.9720711744,
    },
}
# Incorporated glycan residue formulas (without water): https://www.unimod.org/masses.html
# and https://www.nist.gov/mml/biomolecular-measurement/mass-spectrometry-data-center/nist-glyco-mass-calculator.
GLYCANS = {
    "hex": {"C": 6, "H": 10, "O": 5},
    "hexnac": {"C": 8, "H": 13, "N": 1, "O": 5},
    "dhex": {"C": 6, "H": 10, "O": 4},
    "neuac": {"C": 11, "H": 17, "N": 1, "O": 8},
    "neugc": {"C": 11, "H": 17, "N": 1, "O": 9},
}
MAX_ROWS = 1000
#: The versioned interchange tables each analysis reads (see the plugin README).
PROFILES = {"mass": "iki_mass_table_1", "peptide_map": "iki_peptide_map_1"}
MODIFICATION = re.compile(r"(0|[1-9][0-9]*):UNIMOD:([0-9]+)")


def table(path: Path, fields: set[str]) -> Iterator[tuple[int, dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source, delimiter="\t")
        header = reader.fieldnames or []
        if not fields <= set(header) or len(header) != len(set(header)):
            raise ValueError(
                f"Expected a TSV with distinct headings including {', '.join(sorted(fields))}."
            )
        observations = 0
        for observations, row in enumerate(reader, 1):
            number = observations + 1
            if observations > MAX_ROWS:
                raise ValueError(f"Keep at most {MAX_ROWS} observations in one comparison.")
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"Row {number} has the wrong number of columns.")
            yield number, row
        if not observations:
            raise ValueError("The report has no observations.")


def finite(value: object) -> float:
    try:
        converted = float(str(value))
    except ValueError:
        raise ValueError("expected a finite number") from None
    if not math.isfinite(converted):
        raise ValueError("expected a finite number")
    return converted


def count(value: str) -> int:
    if not re.fullmatch(r"[0-9]{1,6}", value):
        raise ValueError("expected an integer count between 0 and 999999")
    return int(value)


def glycan_mass(composition: dict[str, int], basis: str) -> float:
    atoms = ATOMS[basis]
    return sum(
        composition[name] * sum(atoms[atom] * number for atom, number in formula.items())
        for name, formula in GLYCANS.items()
    )


def identity(record: dict[str, Any]) -> str:
    return str(record.get("entry_id") or record["id"])


def chains_of(antibody: dict[str, Any]) -> tuple[dict[str, str], int, list[dict[str, Any]]]:
    records = antibody.get("included", {}).get("components.part", [])
    components = antibody["values"].get("components") or []
    if len(components) != len(records) or not components:
        raise ValueError("The registered antibody needs its component protein sequences.")
    chains = {}
    cysteines = 0
    for component, record in zip(components, records, strict=True):
        sequence = record["values"].get("sequence")
        if not sequence or not re.fullmatch(r"[ACDEFGHIKLMNPQRSTVWY]+", sequence):
            raise ValueError("Use reference chains with known standard amino acid sequences.")
        chains[component["role"]] = sequence
        cysteines += sequence.count("C") * component["copies"]
    return chains, cysteines, records


def check_material(
    antibody: dict[str, Any], adc: dict[str, Any] | None, lot: dict[str, Any] | None
) -> None:
    if adc:
        carriers = adc["values"].get("carrier") or []
        if len(carriers) != 1 or carriers[0] != {
            "kind": "antibody_drug",
            "molecule": identity(antibody),
        }:
            raise ValueError("Select an antibody–drug conjugate of the reference antibody.")
    if lot is None:
        return
    if adc:
        if lot["type"] != "iki/lots.ConjugateLot" or lot["values"].get("conjugate") != identity(
            adc
        ):
            raise ValueError("Select a lot of the chosen antibody–drug conjugate.")
    elif lot["type"] != "iki/lots.ProteinLot" or lot["values"].get("protein") != identity(antibody):
        raise ValueError("Select a protein lot of the chosen antibody.")


def mass_rows(
    path: Path,
    target: dict[str, Any],
    chains: dict[str, str],
    cysteines: int,
    basis: str,
    tolerance: float,
) -> dict[str, Any]:
    if tolerance < 0:
        raise ValueError("Mass tolerance must be nonnegative.")
    mass_field = "molecular_weight" if basis == "average" else "monoisotopic_mass"
    observations = []
    for number, row in table(path, {"name", "analyte", "observed_da", "disulfides", *GLYCANS}):
        try:
            analyte = row["analyte"]
            if not row["name"] or len(row["name"]) > 200:
                raise ValueError("name the observation using 200 or fewer characters")
            if analyte == "intact":
                if row.get("start") or row.get("end"):
                    raise ValueError("intact observations have no chain coordinates")
                base = target["values"].get(mass_field)
                if base is None:
                    raise ValueError(
                        "compute the registered material's composition before comparing"
                    )
                available_cysteines = cysteines
                start = end = None
            else:
                if analyte not in chains:
                    raise ValueError("choose intact or a chain in the registered antibody")
                sequence = chains[analyte]
                start = count(row.get("start") or "1")
                end = count(row.get("end") or str(len(sequence)))
                if not 1 <= start <= end <= len(sequence):
                    raise ValueError("subunit coordinates must fall inside the registered chain")
                subunit = sequence[start - 1 : end]
                # The neutral linear chain: its residues and its ends' water.
                base = sum(ATOMS[basis][e] * n for e, n in chain_formula(subunit).items())
                available_cysteines = subunit.count("C")
            disulfides = count(row["disulfides"])
            if disulfides * 2 > available_cysteines:
                raise ValueError("the retained disulfides exceed available cysteines")
            composition = {name: count(row[name]) for name in GLYCANS}
            delta = finite(row.get("other_delta_da") or 0)
            if delta and not row.get("delta_evidence", "").strip():
                raise ValueError("cite the assignment for a nonzero other mass delta")
            # Cystine formation removes two hydrogen atoms per disulfide.
            # https://www.unimod.org/modifications_view.php?editid1=2020
            glycans = glycan_mass(composition, basis)
            expected = finite(base) - 2 * ATOMS[basis]["H"] * disulfides + glycans + delta
            observed = finite(row["observed_da"])
            if expected <= 0 or observed <= 0:
                raise ValueError("expected and observed neutral masses must be positive")
            error_ppm = 1_000_000 * (observed - expected) / expected
            observations.append(
                {
                    "name": row["name"],
                    "analyte": analyte,
                    "start": start,
                    "end": end,
                    "observed_da": observed,
                    "base_da": base,
                    "expected_da": round(expected, 6),
                    "glycans": composition,
                    "glycan_da": round(glycans, 6),
                    "disulfides": disulfides,
                    "other_delta_da": delta,
                    "delta_evidence": row.get("delta_evidence") or "",
                    "error_ppm": round(error_ppm, 3),
                    "within_tolerance": abs(error_ppm) <= tolerance,
                }
            )
        except ValueError as error:
            raise ValueError(f"Mass row {number}: {error}.") from None
    return {"basis": basis, "tolerance_ppm": tolerance, "peaks": observations}


def peptide_rows(path: Path, chains: dict[str, str]) -> dict[str, Any]:
    covered: dict[str, set[int]] = {role: set() for role in chains}
    peptides = []
    for number, row in table(path, {"chain", "start", "sequence", "modifications"}):
        role = row["chain"]
        sequence = row["sequence"]
        start = count(row["start"])
        end = start + len(sequence) - 1
        reference = chains.get(role, "")
        if (
            not sequence
            or not start
            or end > len(reference)
            or reference[start - 1 : end] != sequence
        ):
            raise ValueError(
                f"Peptide row {number} does not match its registered chain and coordinates."
            )
        modifications = []
        # Use mzTab's peptide position convention: 0 and length + 1 are the termini.
        # Unimod can name the same chemistry at a terminus and a residue.
        for annotation in filter(None, row["modifications"].split(";")):
            match = MODIFICATION.fullmatch(annotation)
            if not match or int(match[1]) > len(sequence) + 1:
                raise ValueError(
                    f"Peptide row {number} needs position:UNIMOD:id for a residue or terminus."
                )
            position = int(match[1])
            if position == 0:
                site, chain_position = "peptide_n_term", start
            elif position == len(sequence) + 1:
                site, chain_position = "peptide_c_term", end
            else:
                site, chain_position = "residue", start + position - 1
            modifications.append(
                {"name": f"UNIMOD:{match[2]}", "chain_position": chain_position, "site": site}
            )
        covered[role].update(range(start, end + 1))
        peptides.append(
            {
                "chain": role,
                "start": start,
                "end": end,
                "sequence": sequence,
                "modifications": modifications,
            }
        )
    # Sequence coverage is the fraction of reference positions observed at least once.
    # See the MAb peptide mapping coverage in https://doi.org/10.1080/21655979.2020.1814683.
    return {
        "coverage": [
            {
                "chain": role,
                "covered": len(covered[role]),
                "length": len(sequence),
                "percent": round(100 * len(covered[role]) / len(sequence), 3),
            }
            for role, sequence in chains.items()
        ],
        "peptides": peptides,
    }


def result(run: dict[str, Any]) -> dict[str, Any]:
    inputs = run["inputs"]
    params = run["params"]
    antibody = inputs["antibody"][0]
    adc = next(iter(inputs.get("adc", [])), None)
    lot = next(iter(inputs.get("lot", [])), None)
    check_material(antibody, adc, lot)
    chains, cysteines, parts = chains_of(antibody)
    report_id = params["report"]
    report = run["files"][report_id]
    path = Path(report["path"])
    analysis = params["analysis"]
    if analysis == "mass" and params.get("tolerance_ppm") is None:
        raise ValueError("Enter the lab's mass tolerance in ppm for a mass comparison.")
    comparison = (
        mass_rows(
            path,
            adc or antibody,
            chains,
            cysteines,
            params["mass_basis"],
            finite(params.get("tolerance_ppm")),
        )
        if analysis == "mass"
        else peptide_rows(path, chains)
    )
    parents = [row for row in (antibody, adc, lot) if row]
    return {
        "apiVersion": "iki/v1",
        "kind": "RunOutput",
        "creates": [
            {
                "type": "iki/biologics_ms.MSResult",
                "derived_from": [row["id"] for row in parents],
                "values": {
                    "antibody": identity(antibody),
                    "adc": identity(adc) if adc else None,
                    "lot": lot["id"] if lot else None,
                    "analysis": analysis,
                    "report_format": PROFILES[analysis],
                    "report": report_id,
                    "report_sha256": report["sha256"],
                    "comparison": comparison,
                    "input_revisions": [
                        {
                            "id": row["id"],
                            "revision": row["revision"],
                            "entry_revision": row.get("entry_revision"),
                        }
                        for row in (*parents, *parts)
                    ],
                    "review_flow": run.get("settings", {}).get("review_flow", "research"),
                },
            }
        ],
    }


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    try:
        document = json.dumps(result(run), allow_nan=False)
    except (ValueError, OSError, csv.Error) as exc:
        print(f"::error {exc}", flush=True)
        raise SystemExit(1) from None
    Path(os.environ["IKI_OUTPUT"]).write_text(document, encoding="utf-8")


if __name__ == "__main__":
    main()
