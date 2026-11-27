"""`design_guides`: where a CRISPR nuclease can cut a sequence, and how particular each
place is (ADR 0205).

It reads the run and writes its results with Iki's action helper, iki_action.py, and
reports progress with `::progress` lines.

`scripts/guides.py` is the science: the motifs, the cut sites, what is known to make a
guide work less well, and the off-target search and its scoring. What this does is the
run: read the design, give the search the sequences it names, and record the guides.
"""

from __future__ import annotations

from typing import Any

import guides
import iki_action as iki

#: Each nuclease's own protospacer length, used where a design doesn't name one.
DEFAULT_SPACER = {"spcas9": 20, "sacas9": 21, "ascas12a": 23, "fncas12a": 23}


class Stop(Exception):  # noqa: N818 - reads as "stop: why"
    """A run that can't go on, said in words a lab can act on."""


def progress(done: int, total: int) -> None:
    print(f"::progress {done / total:.2f} {done} of {total}", flush=True)


def values_of(entity: dict[str, Any]) -> dict[str, Any]:
    found: dict[str, Any] = entity.get("values") or {}
    return found


def one_included(entity: dict[str, Any], name: str, what: str) -> dict[str, Any]:
    found = (entity.get("included") or {}).get(name) or []
    if len(found) != 1:
        raise Stop(f"{entity['hrid']} has no {what}: its {name} names {len(found)} sequences.")
    one: dict[str, Any] = found[0]
    return one


def bases_of(entity: dict[str, Any]) -> str:
    found = (values_of(entity).get("bases") or "").upper()
    if not found:
        raise Stop(f"{entity['hrid']} has no bases.")
    return str(found)


def named(entity: dict[str, Any]) -> str:
    return str(values_of(entity).get("name") or entity["hrid"])


def region_of(values: dict[str, Any], length: int) -> tuple[int, int] | None:
    first, last = values.get("region_start"), values.get("region_end")
    if first is None and last is None:
        return None
    if first is None or last is None:
        raise Stop(
            "A region needs both ends: give the first base the cut may fall on and the "
            "last, or neither to read the whole sequence."
        )
    first, last = int(first), int(last)
    if first < 1 or last > length or last < first:
        raise Stop(f"The region {first:,} to {last:,} isn't inside the target's {length:,} bases.")
    return first, last


def said(guide: guides.Guide, kept: int) -> dict[str, Any]:
    """One guide as the record keeps it: everything the run worked out, with its
    off-targets nearest first and the furthest ones left off a very long list."""
    return {
        "protospacer": guide.protospacer,
        "pam": guide.pam,
        "strand": guide.strand,
        "start": guide.start,
        "end": guide.end,
        "cuts": list(guide.cuts),
        "gc": guide.gc,
        "flags": list(guide.flags),
        "specificity": guide.specificity,
        "off_targets": len(guide.hits),
        "hits": [
            {
                "where": hit.where,
                "protospacer": hit.protospacer,
                "pam": hit.pam,
                "strand": hit.strand,
                "start": hit.start,
                "mismatched": list(hit.mismatched),
                "score": None if hit.score is None else round(hit.score, 2),
            }
            for hit in guide.hits[:kept]
        ],
    }


def how(nuclease: guides.Nuclease, spacer: int, most: int, searching: bool) -> str:
    where = "after" if nuclease.pam_after else "before"
    cut = (
        f"cutting bluntly {nuclease.cuts[0]} bases from the PAM"
        if nuclease.blunt
        else (
            f"cutting staggered after the {nuclease.cuts[0]}th and {nuclease.cuts[1]}rd "
            "bases of the protospacer, leaving a 5-base 5' overhang"
        )
    )
    parts = [
        f"{nuclease.label}, reading {nuclease.pam} {where} a {spacer}-base protospacer, {cut}",
        nuclease.cited,
        f"off-targets searched to {most} mismatches, on {', '.join(nuclease.cleaves)}",
    ]
    if searching and nuclease.name == "spcas9":
        parts.append(
            "specificity by the aggregate formula the CRISPR Design tool published at "
            "crispr.mit.edu, over that tool's own single-site formula and position "
            "weights, which are the site's documentation and not Hsu et al. 2013"
        )
    elif searching:
        parts.append(
            "no specificity computed: the CRISPR Design tool's weights were fitted for "
            "SpCas9's 20-base protospacer and say nothing about this nuclease"
        )
    else:
        parts.append("no specificity computed: the design named nothing to search")
    return "; ".join(parts)


def run(given: dict[str, Any]) -> dict[str, Any]:
    params = given["params"]
    designs = given["inputs"]["design"]
    kept_hits = int(params.get("most_off_targets_kept") or 100)
    updates: list[dict[str, Any]] = []
    messages: list[dict[str, str]] = []
    total_guides = 0
    for done, design in enumerate(designs, start=1):
        values = values_of(design)
        target = one_included(design, "target", "target")
        bases = bases_of(target)
        circular = values_of(target).get("topology") == "circular"
        name = str(values.get("nuclease") or "spcas9")
        nuclease = guides.NUCLEASES[name]
        spacer = int(values.get("spacer_length") or DEFAULT_SPACER[name])
        region = region_of(values, len(bases))
        searching = [
            (named(each), bases_of(each), values_of(each).get("topology") == "circular")
            for each in (design.get("included") or {}).get("search_in") or []
        ]
        if not searching:
            messages.append(
                {
                    "level": "warning",
                    "text": (
                        f"{design['hrid']} names nothing to search, so its guides have no "
                        "specificity: give the genome the cut is to be made in, and "
                        "anything else in the cell, to find where else each could cut."
                    ),
                    "entity": design["id"],
                }
            )
        band = (
            float(params.get("gc_min") or guides.GC_BAND[0]),
            float(params.get("gc_max") or guides.GC_BAND[1]),
        )
        try:
            found = guides.designed(
                bases,
                circular,
                nuclease,
                spacer,
                region,
                searching,
                int(
                    values.get("most_mismatches")
                    if values.get("most_mismatches") is not None
                    else 3
                ),
                band,
            )
        except guides.Nothing as why:
            raise Stop(f"{design['hrid']}: {why}") from why
        most = int(values.get("most_guides") or 50)
        updates.append(
            {
                "entity": design["id"],
                "values": {
                    "guides_found": len(found),
                    "guides": [said(guide, kept_hits) for guide in found[:most]],
                    "searched": (
                        ", ".join(f"{each[0]} ({len(each[1]):,} bp)" for each in searching)
                        or "nothing: no sequence was given to search"
                    )[:600],
                    "designed_with": how(
                        nuclease, spacer, int(values.get("most_mismatches") or 3), bool(searching)
                    )[:600],
                },
            }
        )
        total_guides += len(found)
        print(
            f"{design['hrid']}: {len(found)} {nuclease.label} site(s) in {target['hrid']}, "
            f"keeping {min(len(found), most)}"
        )
        progress(done, len(designs))
    return {
        "updates": {"design": updates},
        "messages": messages,
        "metrics": {
            "guides": total_guides,
            "off_targets": sum(
                sum(guide["off_targets"] for guide in update["values"]["guides"])
                for update in updates
            ),
        },
    }


if __name__ == "__main__":
    iki.main(run, refuse=(Stop, guides.Nothing))
