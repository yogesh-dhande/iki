"""The antibody plugin's sequence liabilities: the motifs a developability screen reports
in a chain's variable domains, read off the numbering `number_chains` stored.

It reads the run input from $IKI_INPUT, reports progress with `::progress` lines, and
writes its results to $IKI_OUTPUT, as every action's command does. It's the plugin's own
code, in pure Python: no third-party tool runs, so the action lists no software
(ADR 0106).

The rules are the ones published developability screens use, with the severities they
give them:

- **N-glycosylation** `N-X-S/T`, X not proline: the sequon an N-linked glycan may be added
  at, a potential site. High, anywhere in a variable domain (LAP's `Ngly`, over the
  variable fragment). When the middle residue is unknown (X, which may be proline), the
  sequon is only possible, and it's reported as uncertain.
- **Deamidation** of asparagine: `NG` high, `NS`, `NT` and `NN` medium, in a CDR. NG is
  the most labile of them, and the hierarchy NG > NS > NT is Lu et al.'s (LAP has N[GS]
  high and N[AHNT] medium; this ranks NG above NS, as the measured rates do).
- **Isomerisation** of aspartate: `DG` high, `DS`, `DT` and `DD` medium, in a CDR, the
  same way round (LAP's `Isom`, D[DGHST]; Lu et al.: DG more labile than DS, and DS than
  DD or DT).
- **Noncanonical cysteines**: a cysteine beyond the pair that makes a variable domain's
  conserved disulphide, at IMGT 23 and 104 or a scheme's equivalents. If one of those two
  isn't a cysteine, the other has no canonical partner, so it's reported as well. High,
  anywhere in a variable domain (LAP's `xCys` and `mCys`). Sequence alone can't say
  whether it's free: it may bond another, as a camelid VHH's CDR1 and CDR3 cysteines
  often do (PDB 1MEL's Cys33-Cys109).
- **Oxidation**: a methionine or a tryptophan in a CDR. Medium (LAP's `MetOx` and
  `TrpOx`).
- **Fragmentation** `DP` in a CDR. High (LAP's `FragH`).
- **Pyroglutamate**: a glutamine or a glutamate at the chain's first residue, which
  cyclises to pyroglutamate. Low: it makes charge variants rather than changing what the
  antibody does.

Sources:

- Satława et al., LAP: Liability Antibody Profiler by sequence & structural mapping of
  natural and therapeutic antibodies, PLoS Comput Biol 20(3):e1011881 (2024), Table 1.
- Lu et al., Deamidation and isomerization liability analysis of 131 clinical-stage
  antibodies, mAbs 11(1):45-57 (2019).
- Raybould et al., Five computational developability guidelines for therapeutic antibody
  profiling, PNAS 116(10):4025-4030 (2019), whose Therapeutic Antibody Profiler reports
  such motifs alongside its guidelines.
- Liu et al., N-terminal glutamate to pyroglutamate conversion in vivo for human IgG2
  antibodies, J Biol Chem 286(13):11211-11217 (2011).

What a screen reports isn't what a chain does: a liability is a site to look at, which
needs measuring before it's believed.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

#: The two cysteines that make a variable domain's disulphide, by numbering scheme: IMGT 23
#: and 104; Kabat's, Chothia's and Martin's numbering of the same residues (heavy 22 and
#: 92, light 23 and 88); and AHo's 23 and 106.
DISULPHIDE: dict[str, dict[str, tuple[int, int]]] = {
    "imgt": {"H": (23, 104), "L": (23, 104)},
    "kabat": {"H": (22, 92), "L": (23, 88)},
    "chothia": {"H": (22, 92), "L": (23, 88)},
    "martin": {"H": (22, 92), "L": (23, 88)},
    "aho": {"H": (23, 106), "L": (23, 106)},
}
#: Deamidation motifs, by the residue after the asparagine, and how severe each is.
DEAMIDATION = {"G": "high", "S": "medium", "T": "medium", "N": "medium"}
#: Isomerisation motifs, by the residue after the aspartate.
ISOMERISATION = {"G": "high", "S": "medium", "T": "medium", "D": "medium"}
#: Residues that oxidise, in a CDR.
OXIDISES = frozenset("MW")
#: What cyclises to pyroglutamate at a chain's N-terminus.
CYCLISES = frozenset("QE")
#: A chain keeps at most this many liabilities, as its field's schema says.
MAX_LIABILITIES = 500
#: A run's output keeps at most 100 messages.
MAX_MESSAGES = 100
FIELDS = ("liabilities", "liability_counts", "liability_count")


def _label(entity: dict[str, Any]) -> str:
    return str(entity["values"].get("name") or entity["hrid"])


def regions(domain: dict[str, Any]) -> list[str]:
    """Which region each of a domain's residues is in: a CDR, by its field's name, or
    framework. Its CDRs are where `number_chains` found them, by the definition it used."""
    letter = "h" if domain["chain"] == "H" else "l"
    start = domain["start"]
    spans = [
        (n, span) for n, span in enumerate(domain.get("cdrs") or [], start=1) if span is not None
    ]
    found = []
    for index in range(len(domain["numbering"])):
        position = start + index
        where = "framework"
        for n, (first, last) in spans:
            if first <= position <= last:
                where = f"cdr_{letter}{n}"
        found.append(where)
    return found


def _noncanonical(domain: dict[str, Any], scheme: str) -> set[int]:
    """Which of a domain's residues are cysteines that aren't its conserved disulphide's:
    every cysteine but the two at the canonical positions, and those two as well if either
    position holds something else, which leaves the other without its canonical partner."""
    side = "H" if domain["chain"] == "H" else "L"
    canonical = DISULPHIDE[scheme][side]
    at = {
        number: index
        for index, (number, insertion, _) in enumerate(domain["numbering"])
        if number in canonical and not insertion
    }
    paired = {
        at[number]
        for number in canonical
        if all(n in at and domain["numbering"][at[n]][2] == "C" for n in canonical)
    }
    return {index for index, (_, _, aa) in enumerate(domain["numbering"]) if aa == "C"} - paired


def found_in(domain: dict[str, Any], scheme: str, start: int) -> list[dict[str, Any]]:
    """The liabilities in one variable domain, in order along it. ``start`` is the index in
    the chain's sequence, from 1, of the domain's first residue."""
    numbering = domain["numbering"]
    residues = "".join(aa for _, _, aa in numbering)
    where = regions(domain)
    noncanonical = _noncanonical(domain, scheme)
    found: list[dict[str, Any]] = []

    def report(
        index: int, kind: str, motif: str, severity: str, *, uncertain: bool = False
    ) -> None:
        number, insertion, _ = numbering[index]
        liability = {
            "kind": kind,
            "motif": motif,
            "position": start + index,
            "number": f"{number}{insertion}",
            "region": where[index],
            "severity": severity,
        }
        if uncertain:
            liability["uncertain"] = True
        found.append(liability)

    for index, residue in enumerate(residues):
        cdr = where[index] != "framework"
        after = residues[index + 1 : index + 2]
        if (
            residue == "N"
            and residues[index + 2 : index + 3] in ("S", "T")
            and after not in ("P", "")
        ):
            # An unknown residue (X) may be proline, which would make it no sequon.
            report(
                index,
                "n_glycosylation",
                residues[index : index + 3],
                "high",
                uncertain=after == "X",
            )
        if cdr and residue == "N" and after in DEAMIDATION:
            report(index, "deamidation", residue + after, DEAMIDATION[after])
        if cdr and residue == "D" and after in ISOMERISATION:
            report(index, "isomerisation", residue + after, ISOMERISATION[after])
        if cdr and residue == "D" and after == "P":
            report(index, "fragmentation", "DP", "high")
        if cdr and residue in OXIDISES:
            report(index, "oxidation", residue, "medium")
        if index in noncanonical:
            report(index, "noncanonical_cysteine", "C", "high")
        if start + index == 1 and residue in CYCLISES:
            report(index, "pyroglutamate", residue, "low")
    return found


def liabilities(entity: dict[str, Any]) -> list[dict[str, Any]] | None:
    """A chain's liabilities, in order along it, or None if it hasn't been numbered."""
    domains = entity["values"].get("variable_domains")
    if not domains:
        return None
    scheme = entity["values"].get("numbering_scheme") or "imgt"
    found = [
        liability for domain in domains for liability in found_in(domain, scheme, domain["start"])
    ]
    return sorted(found, key=lambda f: (f["position"], f["kind"]))


def screen(run: dict[str, Any]) -> dict[str, Any]:
    entities = run["inputs"]["protein"]
    messages: list[dict[str, Any]] = []
    updates = []
    for done, entity in enumerate(entities, start=1):
        found = liabilities(entity)
        if found is not None and any(
            d.get("cdrs") is None for d in entity["values"]["variable_domains"]
        ):
            messages.append(
                {
                    "level": "warning",
                    "text": f"{_label(entity)} has a variable domain whose CDRs numbering "
                    "didn't find, so the motifs that count only in a CDR weren't looked for "
                    "in it.",
                    "entity": entity["id"],
                }
            )
        if found is None:
            messages.append(
                {
                    "level": "warning",
                    "text": f"{_label(entity)} hasn't been numbered, so its liabilities "
                    "can't be placed: number its chains first. It was left as it was.",
                    "entity": entity["id"],
                }
            )
        else:
            kept = found[:MAX_LIABILITIES]
            if len(found) > len(kept):
                messages.append(
                    {
                        "level": "warning",
                        "text": f"{_label(entity)} has {len(found):,} liabilities: the "
                        f"first {MAX_LIABILITIES:,} along the chain were kept.",
                        "entity": entity["id"],
                    }
                )
            counts = Counter(f["kind"] for f in kept)
            values: dict[str, Any] = {
                "liabilities": kept,
                "liability_counts": dict(sorted(counts.items())),
                "liability_count": len(kept),
            }
            updates.append({"entity": entity["id"], "values": values})
            shown = ", ".join(f"{kind} {n}" for kind, n in sorted(counts.items())) or "none"
            print(f"{entity['hrid']}: {len(kept)} liabilities ({shown})")
        print(f"::progress {done / len(entities):.2f} {done} of {len(entities)}", flush=True)
    if len(messages) > MAX_MESSAGES:
        more = len(messages) - (MAX_MESSAGES - 1)
        messages = [
            *messages[: MAX_MESSAGES - 1],
            {"level": "warning", "text": f"And {more:,} more warnings."},
        ]
    return {"updates": {"protein": updates}, "messages": messages}


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    output = {"apiVersion": "iki/v1", "kind": "RunOutput", **screen(run)}
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")


if __name__ == "__main__":
    main()
