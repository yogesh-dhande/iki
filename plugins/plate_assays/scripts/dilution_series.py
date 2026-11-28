"""Assign nominal sample concentrations and replicates to empty wells on a plate."""

import math
from typing import Any

import iki_action as iki
from reader import DIMENSIONS, WELL


def coordinates(well: str) -> tuple[int, int]:
    match = WELL.fullmatch(well.strip().upper())
    if not match:
        raise ValueError(f"Invalid well position {well!r}.")
    return ord(match[1]) - ord("A"), int(match[2]) - 1


def assign(run: dict[str, Any]) -> dict[str, Any]:
    plate = run["inputs"]["plate"][0]
    values = plate["values"]
    if values.get("committed"):
        raise ValueError("The measurements for this plate have already been committed.")
    params = run["params"]
    dose = params["start_concentration"]
    fold = params.get("fold_dilution", 2)
    if not (math.isfinite(dose) and dose > 0 and math.isfinite(fold) and fold > 1):
        raise ValueError("Enter a positive starting concentration and a dilution factor above one.")
    rows, cols = DIMENSIONS[int(values["size"].removeprefix("plate_"))]
    first_row, first_col = coordinates(params["start_well"])
    assignments = list(values.get("assignments") or [])
    occupied = {coordinates(a["well"]) for a in assignments}
    sample = run["inputs"]["sample"][0]["id"]
    across = params.get("direction", "across") == "across"
    for step in range(params["steps"]):
        if dose == 0:
            raise ValueError("This series dilutes beyond the precision of the concentration field.")
        for repeat in range(params.get("replicates", 1)):
            dr, dc = (repeat, step) if across else (step, repeat)
            row, col = first_row + dr, first_col + dc
            if row >= rows or col >= cols:
                raise ValueError("The dilution series extends beyond the plate.")
            well = f"{chr(ord('A') + row)}{col + 1}"
            if (row, col) in occupied:
                raise ValueError(f"{well} is already assigned. Choose an empty block of wells.")
            assignments.append(
                {
                    "well": well,
                    "sample": sample,
                    "replicate": repeat + 1,
                    "concentration": dose,
                    "concentration_unit": params.get("concentration_unit", "nM"),
                }
            )
        # NIST J. Res. 98, 653 (1993), doi:10.6028/jres.098.043.
        # A fold dilution is the ratio C(before)/C(after); repeat the same ratio per step.
        dose /= fold
    return {
        "entity": plate["id"],
        "require_unchanged": True,
        "values": {"assignments": assignments, "preview": None},
    }


if __name__ == "__main__":
    iki.main(lambda run: {"updates": {"plate": [assign(run)]}}, refuse=ValueError)
