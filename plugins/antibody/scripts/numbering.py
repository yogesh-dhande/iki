"""The antibody plugin's numbering: ANARCI on each chain, in the numbering-tools image.

It reads the run input from $IKI_INPUT, reports progress with `::progress` lines, and
writes its results to $IKI_OUTPUT, as every action's command does.

ANARCI aligns each protein to hidden Markov models of antibody variable domains, with
HMMER's hmmscan, and numbers the residues of each domain it finds in the scheme asked
for. It finds the germline V and J genes each domain is closest to, from IMGT's (CC BY
4.0), and how much of each region is the same. It runs twice: with every species' models
and germlines, for the numbering and the species the closest V gene is from; then with
human's alone, for the closest human genes, whose V gene's identity is the domain's
humanness. Only antibody domains count (heavy, kappa and lambda): a T-cell receptor's
aren't.

A protein's fields describe its first heavy domain and its first light domain: a heavy
chain has one, a light chain the other, an scFv both. Every field is set, so numbering
a protein again leaves nothing of the last time. A protein with none is left as it was,
and the run warns which.

CDRs are each scheme's own definitions, by residue number, insertions included:

- IMGT: 27-38, 56-65 and 105-117, in both chains;
- Kabat: H1 31-35, H2 50-65, H3 95-102; L1 24-34, L2 50-56, L3 89-97;
- Chothia: H1 26-32, H2 52-56, H3 95-102; light as Kabat's.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from anarci import anarci

#: Each scheme's CDRs, by residue number, for heavy (H) and light (L) domains.
CDRS: dict[str, dict[str, tuple[tuple[int, int], ...]]] = {
    "imgt": {"H": ((27, 38), (56, 65), (105, 117)), "L": ((27, 38), (56, 65), (105, 117))},
    "kabat": {"H": ((31, 35), (50, 65), (95, 102)), "L": ((24, 34), (50, 56), (89, 97))},
    "chothia": {"H": ((26, 32), (52, 56), (95, 102)), "L": ((24, 34), (50, 56), (89, 97))},
}
#: What ANARCI can read: the 20 standard amino acids and X.
READABLE = frozenset("ACDEFGHIKLMNPQRSTVWYX")
#: ANARCI's chain types that are an antibody's: heavy, kappa and lambda.
ANTIBODY = {"H", "K", "L"}
#: A run's output keeps at most 100 messages.
MAX_MESSAGES = 100
#: The species ANARCI has germlines for.
SPECIES = ("human", "mouse", "rat", "rabbit", "rhesus", "pig", "cow", "alpaca")
FIELDS = (
    "chain_type",
    "species",
    "numbering_scheme",
    "cdr_h1",
    "cdr_h2",
    "cdr_h3",
    "cdr_l1",
    "cdr_l2",
    "cdr_l3",
    "heavy_v_gene",
    "heavy_germline_identity",
    "heavy_j_gene",
    "light_v_gene",
    "light_germline_identity",
    "light_j_gene",
    "variable_domains",
)


def unreadable(sequence: str) -> str | None:
    """The first residue ANARCI can't read, if any. A final stop (*) is left out first."""
    return next((r for r in sequence.removesuffix("*").upper() if r not in READABLE), None)


def _percent(found: Any) -> float | None:  # noqa: ANN401 - ANARCI's (gene, identity) or None
    if not found or found[1] is None:
        return None
    return round(float(found[1]) * 100, 1)


def _gene(found: Any) -> str | None:  # noqa: ANN401 - ANARCI's ((species, gene), identity)
    return str(found[0][1]) if found and found[0] else None


def germlines(details: dict[str, Any] | None) -> dict[str, Any]:
    """The V and J genes ANARCI found a domain closest to, and how much is the same."""
    found = (details or {}).get("germlines") or {}
    v, j = found.get("v_gene"), found.get("j_gene")
    genes = {
        "v_gene": _gene(v),
        "v_identity": _percent(v),
        "j_gene": _gene(j),
        "j_identity": _percent(j),
    }
    return {key: value for key, value in genes.items() if value is not None}


def domain(
    positions: list[Any],
    start: int,
    end: int,
    details: dict[str, Any],
    human: dict[str, Any] | None,
) -> dict[str, Any]:
    """One variable domain, as `variable_domains` keeps it: gaps left out, from 1.

    `details` is what ANARCI found with every species' germlines: the closest species is
    its V gene's. `human` is what it found with human germlines alone, for humanness.
    """
    v = ((details.get("germlines") or {}).get("v_gene") or [None])[0]
    out: dict[str, Any] = {
        "chain": details["chain_type"],
        "start": start + 1,
        "end": end + 1,
        "numbering": [[n, insertion.strip(), aa] for (n, insertion), aa in positions if aa != "-"],
        "closest": germlines(details),
        "human": germlines(human),
    }
    if v and v[0] in SPECIES:
        out["species"] = v[0]
    return out


def cdrs(found: dict[str, Any], scheme: str) -> list[str]:
    """A domain's three CDRs, in the scheme it was numbered in."""
    side = "H" if found["chain"] == "H" else "L"
    return [
        "".join(aa for n, _, aa in found["numbering"] if low <= n <= high)
        for low, high in CDRS[scheme][side]
    ]


def fields(domains: list[dict[str, Any]], scheme: str) -> tuple[dict[str, Any], list[str]]:
    """A protein's fields from its domains, and what to warn about."""
    heavy = [d for d in domains if d["chain"] == "H"]
    light = [d for d in domains if d["chain"] in ("K", "L")]
    notes = [
        f"it has {len(found)} {side} domains: its fields describe the first"
        for side, found in (("heavy", heavy), ("light", light))
        if len(found) > 1
    ]
    values: dict[str, Any] = dict.fromkeys(FIELDS)
    values["numbering_scheme"] = scheme
    values["variable_domains"] = domains
    if heavy and light:
        values["chain_type"] = "heavy_and_light"
    elif heavy:
        values["chain_type"] = "heavy"
    else:
        values["chain_type"] = "kappa" if light[0]["chain"] == "K" else "lambda"
    values["species"] = (heavy or light)[0].get("species")
    for found, prefix, letter in ((heavy, "heavy", "h"), (light, "light", "l")):
        if not found:
            continue
        first = found[0]
        for n, cdr in enumerate(cdrs(first, scheme), start=1):
            values[f"cdr_{letter}{n}"] = cdr or None
        values[f"{prefix}_v_gene"] = first["human"].get("v_gene")
        values[f"{prefix}_germline_identity"] = first["human"].get("v_identity")
        values[f"{prefix}_j_gene"] = first["human"].get("j_gene")
    return values, notes


def _matching(
    positions: tuple[Any, int, int], details: dict[str, Any], human: tuple[Any, Any]
) -> dict[str, Any] | None:
    """What ANARCI found with human germlines alone for the same domain, if it did."""
    _, start, end = positions
    for (_, other_start, other_end), about in zip(human[0] or [], human[1] or [], strict=True):
        same = about["chain_type"] == details["chain_type"]
        if same and other_start <= end and start <= other_end:
            return dict(about)
    return None


def number(run: dict[str, Any]) -> dict[str, Any]:
    scheme = (run.get("params") or {}).get("scheme") or "imgt"
    entities = run["inputs"]["protein"]
    messages: list[dict[str, Any]] = []
    readable = []
    for entity in entities:
        sequence = entity["values"]["sequence"]
        residue = unreadable(sequence)
        if residue is not None:
            messages.append(
                {
                    "level": "warning",
                    "text": f"{_label(entity)} has {residue}, which ANARCI can't number: "
                    "it was left as it was.",
                    "entity": entity["id"],
                }
            )
        else:
            readable.append((entity, sequence.removesuffix("*").upper()))
    print(f"Numbering {len(readable):,} chains in the {scheme} scheme", flush=True)
    every, human = ([], []), ([], [])
    if readable:
        sequences = [(e["id"], s) for e, s in readable]
        # With every species' models and germlines, for the numbering and the closest
        # species; then with human germlines alone, for humanness.
        every, human = (
            anarci(
                sequences,
                scheme=scheme,
                allow=ANTIBODY,
                assign_germline=True,
                allowed_species=species,
                ncpu=max(1, min(os.cpu_count() or 1, 8)),
            )[:2]
            for species in (None, ["human"])
        )
    updates = []
    for done, ((entity, _), found, about, found_human, about_human) in enumerate(
        zip(readable, *every, *human, strict=True), start=1
    ):
        domains = [
            domain(*positions, d, _matching(positions, d, (found_human, about_human)))
            for positions, d in zip(found or [], about or [], strict=True)
            if d["chain_type"] in ANTIBODY
        ]
        if not domains:
            messages.append(
                {
                    "level": "warning",
                    "text": f"{_label(entity)} has no antibody variable domain that ANARCI "
                    "found: it was left as it was.",
                    "entity": entity["id"],
                }
            )
        else:
            values, notes = fields(domains, scheme)
            updates.append({"entity": entity["id"], "values": values})
            messages += [
                {"level": "warning", "text": f"{_label(entity)}: {note}.", "entity": entity["id"]}
                for note in notes
            ]
            shown = ", ".join(
                f"{k} {values[k]}"
                for k in ("chain_type", "species", "cdr_h3", "cdr_l3")
                if values[k]
            )
            print(f"{entity['hrid']}: {shown}")
        print(f"::progress {done / len(readable):.2f} {done} of {len(readable)}", flush=True)
    if len(messages) > MAX_MESSAGES:
        more = len(messages) - (MAX_MESSAGES - 1)
        messages = [
            *messages[: MAX_MESSAGES - 1],
            {"level": "warning", "text": f"And {more:,} more warnings."},
        ]
    return {"updates": {"protein": updates}, "messages": messages}


def _label(entity: dict[str, Any]) -> str:
    return str(entity["values"].get("name") or entity["hrid"])


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    output = {"apiVersion": "iki/v1", "kind": "RunOutput", **number(run)}
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")


if __name__ == "__main__":
    main()
