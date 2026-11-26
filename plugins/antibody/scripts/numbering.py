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
chain has one, a light chain the other, an scFv both. A chain with a domain ANARCI finds
but can't number in the scheme (a CDR with more insertions than it has codes for, as a
cow's ultralong CDR-H3 has in Kabat's) is left as it was, and the run warns which: saved
without that domain, it would say it's another kind of chain. Every field is set, so numbering
a protein again leaves nothing of the last time. A chain whose liabilities were found
(find_liabilities) on another scheme or CDR definition has that screen cleared, since each
liability's number and region are its numbering's: the screen is run again, by whoever
chooses its rule set, rather than reapplied here. Numbered again the same way, its numbering
is the same, so its screen stays. A protein with none is left as it was, and the run warns
which.

The closest species and germline genes are assignments against the germlines this ANARCI
release carries (IMGT's), which each chain records as its germline set. They say what the
sequence resembles, not the species the antibody came from.

The numbering scheme (IMGT, Kabat, Chothia, Martin or AHo) says how residues are
numbered. The CDR definition says where the CDRs are, by residue number in the numbering
its boundaries are published in, so each definition finds the same residues whichever
scheme numbers the chain: ANARCI numbers the chain again in that numbering when it isn't
the chain's. Each domain keeps its CDRs as where they are in the chain, from 1. A CDR
holds its endpoints' insertions too:

- IMGT (IMGT numbering; Lefranc et al. 2003): 27-38, 56-65 and 105-117, in both chains;
- Kabat (Kabat numbering): H1 H31-H35B (and H35C onwards, where ANARCI puts a longer
  CDR-H1's residues, before the conserved W36), H2 H50-H65, H3 H95-H102; L1 L24-L34, L2
  L50-L56, L3 L89-L97;
- Chothia (Chothia numbering): H1 H26-H32, H2 H52-H56, H3 H95-H102; light as Kabat's;
- AbM (Chothia numbering): H1 H26-H35, H2 H50-H58, H3 H95-H102; light as Kabat's;
- Contact (Chothia numbering; MacCallum et al. 1996): H1 H30-H35, H2 H47-H58, H3
  H93-H101; L1 L30-L36, L2 L46-L55, L3 L89-L96.

Kabat's, AbM's and Contact's are as A. C. R. Martin tabulates them (bioinf.org.uk/abs,
"Antibody numbering and CDR definitions"; Martin, in Antibody Engineering vol. 2, 2010,
Table 3.4). Chothia's are the wider set that table gave until 2021, and that SAbDab
still uses; it now gives a narrower consensus (H26-H32, H52-H56, H96-H101; L26-L32,
L50-L52, L91-L96), Martin's own reading of Chothia papers that disagree, which isn't
implemented: no primary source defines it (the README says why). Martin's numbering
differs from Chothia's only in framework, so its CDRs are Chothia's unless another is
chosen; AHo's numbering (Honegger and Plückthun 2001) defines no CDRs of its own, so
its are IMGT's unless another is chosen.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from anarci import anarci
from liabilities import OWN_DEFINITION

#: A CDR's first and last residue numbers, each with its insertions.
Bounds = tuple[int, int]

#: ANARCI's numbered domains for each chain, and what it found about them.
Answer = tuple[list[Any], list[Any]]
#: The numbering schemes ANARCI numbers in.
SCHEMES = ("imgt", "kabat", "chothia", "martin", "aho")
#: Each CDR definition: the numbering its boundaries are given in, and its three CDRs for
#: heavy (H) and light (L) domains (see the module's docstring for the sources).
DEFINITIONS: dict[str, tuple[str, dict[str, tuple[Bounds, Bounds, Bounds]]]] = {
    "imgt": (
        "imgt",
        {
            "H": ((27, 38), (56, 65), (105, 117)),
            "L": ((27, 38), (56, 65), (105, 117)),
        },
    ),
    "kabat": (
        "kabat",
        {
            "H": ((31, 35), (50, 65), (95, 102)),
            "L": ((24, 34), (50, 56), (89, 97)),
        },
    ),
    "chothia": (
        "chothia",
        {
            "H": ((26, 32), (52, 56), (95, 102)),
            "L": ((24, 34), (50, 56), (89, 97)),
        },
    ),
    "abm": (
        "chothia",
        {
            "H": ((26, 35), (50, 58), (95, 102)),
            "L": ((24, 34), (50, 56), (89, 97)),
        },
    ),
    "contact": (
        "chothia",
        {
            "H": ((30, 35), (47, 58), (93, 101)),
            "L": ((30, 36), (46, 55), (89, 96)),
        },
    ),
}
#: Each CDR definition's name, as people write it.
NAMES = {"imgt": "IMGT", "kabat": "Kabat", "chothia": "Chothia", "abm": "AbM", "contact": "Contact"}
#: What ANARCI can read: the 20 standard amino acids and X.
READABLE = frozenset("ACDEFGHIKLMNPQRSTVWYX")
#: ANARCI's chain types that are an antibody's: heavy, kappa and lambda.
ANTIBODY = {"H", "K", "L"}
#: ANARCI's chain types, as people say them.
CHAINS = {"H": "heavy", "K": "kappa", "L": "lambda"}
#: A run's output keeps at most 100 messages.
MAX_MESSAGES = 100
#: The germlines its species and genes are assigned against: those this ANARCI release
#: carries, from IMGT (the action's software and the image's requirements pin it).
GERMLINE_SET = "ANARCI 2026.2.13.2, IMGT germlines"
#: The fields a liability screen writes, which a new numbering clears.
SCREEN = ("liability_screen", "liabilities", "liability_counts", "liability_count")
#: The species ANARCI has germlines for.
SPECIES = ("human", "mouse", "rat", "rabbit", "rhesus", "pig", "cow", "alpaca")
FIELDS = (
    "chain_type",
    "species",
    "germline_set",
    "numbering_scheme",
    "cdr_definition",
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


def cdr_spans(
    positions: list[Any], start: int, chain: str, definition: str
) -> list[list[int] | None]:
    """Where a domain's three CDRs are in its chain, from 1, first and last residue
    included, from ANARCI's numbering of it in the definition's own numbering; None for a
    CDR with no residues. ``start`` is the domain's first residue's index, from 0."""
    bounds = DEFINITIONS[definition][1]["H" if chain == "H" else "L"]
    spans: list[list[int] | None] = []
    for low, high in bounds:
        inside = []
        index = start
        for (n, _), aa in positions:
            if aa == "-":
                continue
            index += 1
            if low <= n <= high:
                inside.append(index)
        spans.append([inside[0], inside[-1]] if inside else None)
    return spans


def domain(
    positions: list[Any],
    start: int,
    end: int,
    details: dict[str, Any],
    human: dict[str, Any] | None,
    cdrs: list[list[int] | None] | None,
) -> dict[str, Any]:
    """One variable domain, as `variable_domains` keeps it: gaps left out, from 1.

    `details` is what ANARCI found with every species' germlines: the closest species is
    its V gene's. `human` is what it found with human germlines alone, for humanness.
    `cdrs` is where its CDRs are, or None if ANARCI couldn't number it in its CDR
    definition's numbering.
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
    if cdrs is not None:
        out["cdrs"] = cdrs
    if v and v[0] in SPECIES:
        out["species"] = v[0]
    return out


def fields(
    domains: list[dict[str, Any]], sequence: str, scheme: str, definition: str
) -> tuple[dict[str, Any], list[str]]:
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
    values["cdr_definition"] = definition
    values["germline_set"] = GERMLINE_SET
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
        for n, span in enumerate(first.get("cdrs") or [], start=1):
            values[f"cdr_{letter}{n}"] = sequence[span[0] - 1 : span[1]] if span else None
        values[f"{prefix}_v_gene"] = first["human"].get("v_gene")
        values[f"{prefix}_germline_identity"] = first["human"].get("v_identity")
        values[f"{prefix}_j_gene"] = first["human"].get("j_gene")
    return values, notes


def _matching(
    positions: tuple[Any, int, int], details: dict[str, Any], other: tuple[Any, Any]
) -> tuple[Any, dict[str, Any]] | None:
    """The same domain in another of ANARCI's answers for the chain, if it found it: its
    numbering, start and end, and what it found about it."""
    _, start, end = positions
    for found, about in zip(other[0] or [], other[1] or [], strict=True):
        _, other_start, other_end = found
        same = about["chain_type"] == details["chain_type"]
        if same and other_start <= end and start <= other_end:
            return found, dict(about)
    return None


def _definition(params: dict[str, Any], scheme: str) -> str:
    return str(params.get("cdr_definition") or OWN_DEFINITION[scheme])


def number(run: dict[str, Any]) -> dict[str, Any]:
    params = run.get("params") or {}
    scheme = params.get("scheme") or "imgt"
    definition = _definition(params, scheme)
    reference = DEFINITIONS[definition][0]
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
    print(
        f"Numbering {len(readable):,} chains in the {scheme} scheme, with {definition} CDRs",
        flush=True,
    )
    every: Answer = ([], [])
    human: Answer = ([], [])
    cdr_numbering: Answer = ([], [])
    if readable:
        sequences = [(e["id"], s) for e, s in readable]
        ncpu = max(1, min(os.cpu_count() or 1, 8))
        # With every species' models and germlines, for the numbering and the closest
        # species; then with human germlines alone, for humanness.
        every, human = (
            anarci(
                sequences,
                scheme=scheme,
                allow=ANTIBODY,
                assign_germline=True,
                allowed_species=species,
                ncpu=ncpu,
            )[:2]
            for species in (None, ["human"])
        )
        # The CDRs are read in the numbering their definition gives them in.
        cdr_numbering = (
            every
            if reference == scheme
            else anarci(
                sequences,
                scheme=reference,
                allow=ANTIBODY,
                assign_germline=False,
                allowed_species=None,
                ncpu=ncpu,
            )[:2]
        )
    updates = []
    for done, ((entity, sequence), found, about, *others) in enumerate(
        zip(readable, *every, *human, *cdr_numbering, strict=True), start=1
    ):
        found_human, about_human, found_cdrs, about_cdrs = others
        domains = []
        unnumbered = []
        for positions, d in zip(found or [], about or [], strict=True):
            if d["chain_type"] not in ANTIBODY:
                continue
            if not positions[0]:
                # ANARCI found the domain but gave it no numbers: it has more insertions
                # than the scheme has codes for (a bovine CDR-H3 of 60 in Kabat's).
                unnumbered.append(
                    f"a {CHAINS[d['chain_type']]} variable domain at residues "
                    f"{positions[1] + 1}-{positions[2] + 1}"
                )
                continue
            for_human = _matching(positions, d, (found_human, about_human))
            for_cdrs = _matching(positions, d, (found_cdrs, about_cdrs))
            if for_cdrs is not None and not for_cdrs[0][0]:
                for_cdrs = None
            cdrs = None
            if for_cdrs is not None:
                cdr_positions, cdr_start, _ = for_cdrs[0]
                cdrs = cdr_spans(cdr_positions, cdr_start, d["chain_type"], definition)
            else:
                messages.append(
                    {
                        "level": "warning",
                        "text": f"{_label(entity)}: ANARCI couldn't number a domain in "
                        f"the {reference} numbering {NAMES[definition]}'s CDRs are defined "
                        "in, so its CDRs are left empty.",
                        "entity": entity["id"],
                    }
                )
            numbered, start, end = positions
            domains.append(
                domain(numbered, start, end, d, for_human[1] if for_human else None, cdrs)
            )
        if unnumbered:
            # Numbered without it, the chain would say it's something else (an scFv a light
            # chain), so none of it is.
            messages.append(
                {
                    "level": "warning",
                    "text": f"{_label(entity)} was left as it was: ANARCI found "
                    f"{' and '.join(unnumbered)} but couldn't number "
                    f"{'it' if len(unnumbered) == 1 else 'them'} in the {scheme} scheme, "
                    "which hasn't the insertion codes for a CDR so long. The imgt scheme "
                    "numbers longer ones.",
                    "entity": entity["id"],
                }
            )
        elif not domains:
            messages.append(
                {
                    "level": "warning",
                    "text": f"{_label(entity)} has no antibody variable domain that ANARCI "
                    "found: it was left as it was.",
                    "entity": entity["id"],
                }
            )
        else:
            values, notes = fields(domains, sequence, scheme, definition)
            cleared = None
            screen = entity["values"].get("liability_screen") or {}
            if entity["values"].get("liability_count") is not None and (
                screen.get("numbering_scheme"),
                screen.get("cdr_definition"),
            ) != (scheme, definition):
                # Its liabilities were found on the numbering this replaces: their numbers
                # and regions are that numbering's, so the screen goes with it.
                values |= dict.fromkeys(SCREEN)
                cleared = {
                    "level": "info",
                    "text": f"{_label(entity)}'s liability screen read another numbering, so "
                    "it was cleared: find its liabilities again on this one.",
                    "entity": entity["id"],
                }
            updates.append({"entity": entity["id"], "values": values})
            messages += [
                {"level": "warning", "text": f"{_label(entity)}: {note}.", "entity": entity["id"]}
                for note in notes
            ]
            messages += [cleared] if cleared else []
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
