"""The worked examples' values, computed by the example plugins' own code on their data.

The site never states a plugin's result by hand. Each example here runs the code a
worker runs, on the data the plugin's tests use, and the site's pages read what it
returns (``docs/site/src/generated/examples/``):

- ``puc19-reads``: ``iki/plasmid_sequencing``'s whole-plasmid check of 41 public MinION
  reads of pUC19 (SRA run SRR17915342, every 900th read) against GenBank L09137.2, with
  minimap2's answers replayed from the plugin's recording, as its tests do (ADR 0208).
- ``dose-response``: ``iki/plate_assays``' import of the synthetic MARS export and its
  four-parameter fit, with every sample well and with A4 excluded, as the first
  dose-response guide does (ADR 0188, ADR 0198).
"""

from __future__ import annotations

import csv
import importlib.util
import json
import runpy
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

from sitegen.repo import GENERATED, ROOT

PLASMID_SEQUENCING = ROOT / "plugins" / "plasmid_sequencing"
PLATE_ASSAYS = ROOT / "plugins" / "plate_assays"
SERIES = PLATE_ASSAYS / "tests" / "data" / "mars-96-series.csv"
#: The dilution series the guide assigns: A1 to A8, doubling from 1 nM to 128 nM.
DOSES_NM = [1, 2, 4, 8, 16, 32, 64, 128]
CONTROLS = {"A9": "blank", "A10": "reference"}
#: The exclusion the guide enters, as the fit action's exclusions parameter takes it.
EXCLUSION = ("A4", "Simulated dispensing error")
#: Any record ID: the reader joins wells to a sample's UUID, and the fit uses only doses.
SAMPLE = "00000000-0000-7000-8003-000000000077"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Can't load {path}")
    module = importlib.util.module_from_spec(spec)
    # Dataclasses look their module up while they're made.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def run_length(bases: str, position: int) -> int:
    """How long the run of one base is that the 1-based ``position`` lies in, on a circle."""
    size = len(bases)
    base = bases[position - 1]
    length = 1
    for step in (1, -1):
        offset = step
        while length < size and bases[(position - 1 + offset) % size] == base:
            length += 1
            offset += step
    return length


def puc19_reads() -> dict[str, Any]:
    """The whole-plasmid alignment of the public pUC19 reads, as the worker records it."""
    replay = _load("sitegen_plasmid_replay", PLASMID_SEQUENCING / "tests" / "replay.py")
    script = replay.module()
    answers = replay.replaying()
    script.minimap2 = answers
    script.minimap2_version = lambda: answers.version
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp) / "reads"
        given = replay.reads_run(replay.READS)
        output = replay.run(script, given, out_dir)
        with (out_dir / "coverage.tsv").open(encoding="utf-8") as handle:
            uncalled = [
                int(row["position"])
                for row in csv.DictReader(handle, delimiter="\t")
                if row["consensus"] == "N"
            ]
    (created,) = output["creates"]
    values: dict[str, Any] = created["values"]
    reference = replay.puc19()
    return {
        "type": created["type"],
        "values": values,
        "uncalled": [
            {"position": p, "base": reference[p - 1], "run": run_length(reference, p)}
            for p in uncalled
        ],
        "data": {
            "reads": "plugins/plasmid_sequencing/tests/data/SRR17915342-every-900th.fastq.gz",
            "reference": "plugins/plasmid_sequencing/tests/data/L09137.2.fasta",
            "run": "SRR17915342",
            "biosample": "SAMN25333912",
            "accession": "L09137.2",
        },
    }


def _plate_script(name: str) -> Callable[..., Any]:
    found: Callable[..., Any] = runpy.run_path(str(PLATE_ASSAYS / "scripts" / f"{name}.py"))[
        {"reader": "preview_reader", "four_pl": "fit_curve"}[name]
    ]
    return found


def layout() -> list[dict[str, Any]]:
    """The guide's plate map: the sample's series in A1 to A8, the controls in A9 and A10."""
    wells: list[dict[str, Any]] = [
        {
            "well": f"A{index}",
            "sample": SAMPLE,
            "replicate": 1,
            "concentration": dose,
            "concentration_unit": "nM",
        }
        for index, dose in enumerate(DOSES_NM, 1)
    ]
    wells += [{"well": well, "control": name, "replicate": 1} for well, name in CONTROLS.items()]
    return wells


def dose_response() -> dict[str, Any]:
    """The synthetic plate read as the reader previews it, and the fit with and without A4."""
    preview = _plate_script("reader")(SERIES.read_bytes(), 96, layout())
    if preview["issues"]:
        raise ValueError(f"The example plate doesn't import: {preview['issues']}")
    fit_curve = _plate_script("four_pl")
    samples = [r for r in preview["readings"] if "sample" in r]
    excluded_well, _ = EXCLUSION

    def fit(excluding: str | None) -> dict[str, Any]:
        points = [(r["concentration"], r["value"]) for r in samples if r["well"] != excluding]
        found: dict[str, Any] = fit_curve(points, "IC50")
        if found["fit_status"] != "fitted":
            raise ValueError(f"The example's fit failed: {found.get('fit_reason')}")
        return {**found, "excluded": [excluding] if excluding else []}

    return {
        "data": {
            "file": SERIES.relative_to(ROOT).as_posix(),
            "channel": preview["channel"],
            "unit": preview["unit"],
        },
        "readings": [
            {
                "well": r["well"],
                "role": "sample" if "sample" in r else r["control"],
                "concentration": r.get("concentration"),
                "concentration_unit": r.get("concentration_unit"),
                "value": r["value"],
                "source_row": r["source_row"],
            }
            for r in preview["readings"]
        ],
        "exclusion": {"well": EXCLUSION[0], "reason": EXCLUSION[1]},
        "fits": {"all": fit(None), "excluded": fit(excluded_well)},
    }


EXAMPLES: dict[str, Callable[[], dict[str, Any]]] = {
    "puc19-reads": puc19_reads,
    "dose-response": dose_response,
}


def write(directory: Path = GENERATED / "examples") -> list[Path]:
    """Writes each example's JSON; the same code and data write the same bytes."""
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, make in EXAMPLES.items():
        path = directory / f"{name}.json"
        path.write_text(json.dumps(make(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(path)
    return written
