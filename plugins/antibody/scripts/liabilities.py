"""The antibody plugin's sequence liabilities: the motifs a developability screen reports
in a chain's variable domains, read off the numbering `number_chains` stored.

It reads the run input from $IKI_INPUT, reports progress with `::progress` lines, and
writes its results to $IKI_OUTPUT, as every action's command does. It's the plugin's own
code, in pure Python: no third-party tool runs, so the action lists no software
(ADR 0106).

Its one rule set, `developability_v1`, applies the rules published developability screens use,
with the severities they give them. Each chain records the rule set and the numbering
it was read off (`liability_screen`), so a later rule set's results can't pass for these:

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
  isn't a cysteine, the other has no canonical partner, so it's reported as well; a
  fragment that doesn't reach one of them says nothing of its partner. High,
  anywhere in a variable domain (LAP's `xCys` and `mCys`). Sequence alone can't say
  whether it's free: it may bond another, as a camelid VHH's CDR1 and CDR3 cysteines
  often do (PDB 1MEL's Cys33-Cys109).
- **Oxidation**: a methionine or a tryptophan in a CDR. Medium (LAP's `MetOx` and
  `TrpOx`).
- **Fragmentation** `DP` in a CDR. High (LAP's `FragH`).
- **Pyroglutamate**: a glutamine or a glutamate at the chain's first residue, which
  cyclises to pyroglutamate, whether or not a variable domain starts there (one after a
  tag has no number, and its region is `outside`). Low: it makes charge variants rather
  than changing what the antibody does.

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
#: The rule sets a screen can apply, each with the motifs and severities above. A new rule
#: set is a new name, so a chain's screen always says which rules found its liabilities.
RULE_SETS = ("developability_v1",)
#: The CDR definition each scheme's numbering is read with, unless another is chosen.
#: Martin's numbering is Chothia's in the CDRs; AHo's defines no CDRs of its own.
OWN_DEFINITION = {
    "imgt": "imgt",
    "kabat": "kabat",
    "chothia": "chothia",
    "martin": "chothia",
    "aho": "imgt",
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
    framework. Its CDRs are where `number_chains` found them, by the definition it used;
    in a domain whose CDRs it couldn't find, every residue's region is unknown, as a
    loop's residues aren't framework's."""
    if domain.get("cdrs") is None:
        return ["unknown"] * len(domain["numbering"])
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
    every cysteine but the two at the canonical positions, and those two as well if the
    other position holds something else, which leaves it without its canonical partner. A
    partner position the domain doesn't reach, as a fragment that starts or ends past it
    doesn't, says nothing either way; one it reaches but has no residue at is a loss."""
    side = "H" if domain["chain"] == "H" else "L"
    canonical = DISULPHIDE[scheme][side]
    numbering = domain["numbering"]
    at = {
        number: index
        for index, (number, insertion, _) in enumerate(numbering)
        if number in canonical and not insertion
    }
    first, last = (numbering[0][0], numbering[-1][0]) if numbering else (0, -1)

    def lost(number: int) -> bool:
        return first <= number <= last and (number not in at or numbering[at[number]][2] != "C")

    paired = {
        at[number]
        for number in canonical
        if number in at
        and numbering[at[number]][2] == "C"
        and not any(lost(other) for other in canonical if other != number)
    }
    return {index for index, (_, _, aa) in enumerate(numbering) if aa == "C"} - paired


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
        cdr = where[index].startswith("cdr_")
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
    # The chain's first residue cyclises whether or not a variable domain starts there: one
    # after a tag or a linker isn't numbered, so it has no number and no region.
    first = entity["values"]["sequence"][:1].upper()
    if first in CYCLISES and not any(int(d["start"]) == 1 for d in domains):
        found.append(
            {
                "kind": "pyroglutamate",
                "motif": first,
                "position": 1,
                "region": "outside",
                "severity": "low",
            }
        )
    return sorted(found, key=lambda f: (f["position"], f["kind"]))


def screened(
    entity: dict[str, Any], found: list[dict[str, Any]], rule_set: str = RULE_SETS[0]
) -> tuple[dict[str, Any], str]:
    """A screened chain's fields, from its liabilities and the rule set and numbering that
    found them, and what to say if some weren't kept."""
    kept = found[:MAX_LIABILITIES]
    note = ""
    if len(found) > len(kept):
        note = (
            f"{_label(entity)} has {len(found):,} liabilities: the first "
            f"{MAX_LIABILITIES:,} along the chain were kept."
        )
    counts = Counter(f["kind"] for f in kept)
    scheme = entity["values"].get("numbering_scheme") or "imgt"
    values = {
        "liability_screen": {
            "rule_set": rule_set,
            "numbering_scheme": scheme,
            "cdr_definition": entity["values"].get("cdr_definition") or OWN_DEFINITION[scheme],
        },
        "liabilities": kept,
        "liability_counts": dict(sorted(counts.items())),
        "liability_count": len(kept),
    }
    return values, note


def screen(run: dict[str, Any]) -> dict[str, Any]:
    entities = run["inputs"]["protein"]
    rule_set = (run.get("params") or {}).get("rule_set") or RULE_SETS[0]
    if rule_set not in RULE_SETS:
        raise ValueError(f"{rule_set} isn't a rule set this screen has.")
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
            values, note = screened(entity, found, rule_set)
            if note:
                messages.append({"level": "warning", "text": note, "entity": entity["id"]})
            counts = values["liability_counts"]
            kept = values["liabilities"]
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
