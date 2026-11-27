"""Checks that CI's Python shards ran every test exactly once, and records how long they took.

Each shard (``pytest_shards.py``) leaves a directory with its plan, made from the whole
collection, and the tests it ran. For each Python version, every test of a kind the
version runs must have run in exactly one of its shards, the one the plan gave it, and
every test collected must have run on some version. A shard missing from CI's matrix, one
there twice, or shards that disagree about the suite, all fail here.

    python3 scripts/ci/shard_summary.py REPORTS --workflow .github/workflows/ci.yml \
        [--job python-tests] [--durations OUT] [--markdown OUT]

The shards expected are the workflow's, read from the job's matrix, not learned from the
reports: so a version, a kind or a shard whose reports all went missing fails too.

``--durations`` writes the seconds each file's tests took, the file ``pytest_shards.py``
balances shards by: copy it to ``scripts/ci/test-durations.json`` when the shards drift
apart. Only the standard library, so it runs on any Python 3.12 or later.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

PLAN = "plan.json"
RAN = "ran.json"


class FileDuration(TypedDict):
    tests: int
    seconds: float


@dataclass(frozen=True)
class Report:
    name: str
    python: str
    kind: str
    shard: int
    count: int
    #: Every collected test's kind.
    kinds: dict[str, str]
    #: The shard of each test of this shard's kind.
    shards: dict[str, int]
    #: The tests this shard ran, and the seconds each took.
    seconds: dict[str, float]

    @property
    def label(self) -> str:
        return f"Python {self.python} {self.kind} {self.shard}/{self.count}"


def load(directory: Path) -> tuple[list[Report], list[str]]:
    """The reports in each of ``directory``'s subdirectories, and what's wrong with them."""
    reports: list[Report] = []
    errors: list[str] = []
    for sub in sorted(p for p in directory.iterdir() if p.is_dir()):
        try:
            plan = json.loads((sub / PLAN).read_text(encoding="utf-8"))
            ran = json.loads((sub / RAN).read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            errors.append(f"{sub.name}: no {Path(str(error.filename)).name}")
            continue
        identity = ("python", "kind", "shard", "count")
        if any(plan[k] != ran[k] for k in identity):
            errors.append(f"{sub.name}: its plan and the tests it ran are different shards'")
            continue
        reports.append(
            Report(
                name=sub.name,
                python=plan["python"],
                kind=plan["kind"],
                shard=plan["shard"],
                count=plan["count"],
                kinds=plan["kinds"],
                shards=plan["shards"],
                seconds=ran["seconds"],
            )
        )
    if not reports and not errors:
        errors.append(f"{directory}: no shard reports")
    return reports, errors


#: A shard in the job's matrix: ``- { python: "3.14", kind: plain, shard: 1, shards: 5 }``.
_ENTRY = re.compile(
    r'^\s*-\s*\{\s*python:\s*"(?P<python>[0-9.]+)",\s*kind:\s*(?P<kind>[a-z]+),'
    r"\s*shard:\s*(?P<shard>\d+),\s*shards:\s*(?P<count>\d+)\s*\}\s*$"
)

Shard = tuple[str, str, int, int]


def matrix(workflow: Path, job: str = "python-tests") -> set[Shard]:
    """The shards the workflow's ``job`` runs, as ``(python, kind, shard, count)``, read
    from its matrix's ``include`` entries. Raises :class:`ValueError` if it has none."""
    lines = workflow.read_text(encoding="utf-8").splitlines()
    try:
        start = lines.index(f"  {job}:")
    except ValueError:
        raise ValueError(f"{workflow} has no job {job}") from None
    found: set[Shard] = set()
    for line in lines[start + 1 :]:
        if re.match(r"^  [A-Za-z0-9_-]+:", line):
            break  # the next job
        if entry := _ENTRY.match(line):
            found.add((entry["python"], entry["kind"], int(entry["shard"]), int(entry["count"])))
    if not found:
        raise ValueError(f"{workflow}'s job {job} has no shards in its matrix")
    return found


def expected(reports: list[Report], shards: set[Shard]) -> list[str]:
    """What's missing or extra against the shards the workflow runs."""
    seen = [(r.python, r.kind, r.shard, r.count) for r in reports]
    errors = [
        f"Python {p} {k} {n}/{c}: in the workflow's matrix, but no report"
        for p, k, n, c in sorted(shards - set(seen))
    ]
    errors += [
        f"Python {p} {k} {n}/{c}: a report, but not in the workflow's matrix"
        for p, k, n, c in sorted(set(seen) - shards)
    ]
    return errors


def check(reports: list[Report]) -> list[str]:
    """What's wrong: each test must run in exactly one shard of each version that runs its
    kind, and on at least one version."""
    errors: list[str] = []
    by_python: dict[str, list[Report]] = defaultdict(list)
    for report in reports:
        by_python[report.python].append(report)
    collected: set[str] = set()
    ran_anywhere: set[str] = set()
    for python, shards in sorted(by_python.items()):
        first = shards[0]
        errors.extend(
            f"{other.label} collected a different suite from {first.label}: "
            f"{_some(set(other.kinds) ^ set(first.kinds))}"
            for other in shards[1:]
            if other.kinds != first.kinds
        )
        collected |= set(first.kinds)
        by_kind: dict[str, list[Report]] = defaultdict(list)
        for report in shards:
            by_kind[report.kind].append(report)
        for kind, group in sorted(by_kind.items()):
            counts = {r.count for r in group}
            if len(counts) > 1:
                errors.append(f"Python {python} {kind}: shards disagree on how many there are")
                continue
            (count,) = counts
            numbers = sorted(r.shard for r in group)
            if numbers != list(range(1, count + 1)):
                errors.append(
                    f"Python {python} {kind}: expected shards 1 to {count}, found {numbers}"
                )
            errors.extend(
                f"{other.label} split the tests unlike {group[0].label}"
                for other in group[1:]
                if other.shards != group[0].shards
            )
            plan = group[0].shards
            ran_by: dict[str, list[Report]] = defaultdict(list)
            for report in group:
                for nodeid in report.seconds:
                    ran_by[nodeid].append(report)
            # Grouped, so a missing shard is one line, not one for each of its tests.
            problems: dict[str, set[str]] = defaultdict(set)
            for nodeid, test_kind in first.kinds.items():
                if test_kind != kind:
                    continue
                where = ran_by.get(nodeid, [])
                if not where:
                    problems[f"ran in no {kind} shard"].add(nodeid)
                elif len(where) > 1:
                    problems[f"ran in {', '.join(r.label for r in where)}"].add(nodeid)
                elif where[0].shard != plan.get(nodeid):
                    problems[f"ran in {where[0].label}, planned for another"].add(nodeid)
            errors.extend(
                f"Python {python}: {len(nodeids)} tests {problem}: {_some(nodeids)}"
                for problem, nodeids in sorted(problems.items())
            )
            for report in group:
                strays = {n for n in report.seconds if first.kinds.get(n) != kind}
                if strays:
                    errors.append(f"{report.label} ran tests not of its kind: {_some(strays)}")
                ran_anywhere |= set(report.seconds)
    never = collected - ran_anywhere
    if never:
        errors.append(f"{len(never)} tests ran on no Python version: {_some(never)}")
    return errors


def durations(reports: list[Report]) -> dict[str, dict[str, dict[str, FileDuration]]]:
    """How many tests each file ran and their total seconds, by Python version and kind."""
    times: dict[str, dict[str, dict[str, list[float]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    for report in reports:
        for nodeid, seconds in report.seconds.items():
            times[report.python][report.kind][nodeid.split("::", 1)[0]].append(seconds)
    return {
        python: {
            kind: {
                f: FileDuration(tests=len(observed), seconds=round(sum(observed), 1))
                for f, observed in sorted(files.items())
            }
            for kind, files in sorted(kinds.items())
        }
        for python, kinds in sorted(times.items())
    }


def markdown(reports: list[Report]) -> str:
    lines = [
        "### Python test shards",
        "",
        "| Shard | Files | Tests | Test time (all workers) |",
        "| --- | ---: | ---: | ---: |",
    ]
    for r in sorted(reports, key=lambda r: (r.python, r.kind, r.shard)):
        files = {n.split("::", 1)[0] for n in r.seconds}
        minutes = sum(r.seconds.values()) / 60
        lines.append(f"| {r.label} | {len(files)} | {len(r.seconds)} | {minutes:.1f} min |")
    return "\n".join(lines) + "\n"


def _some(nodeids: set[str], limit: int = 5) -> str:
    shown = sorted(nodeids)[:limit]
    more = len(nodeids) - len(shown)
    return ", ".join(shown) + (f" and {more} more" if more else "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("reports", type=Path, help="a directory of the shards' report directories")
    parser.add_argument(
        "--workflow", type=Path, required=True, help="the workflow whose matrix runs the shards"
    )
    parser.add_argument("--job", default="python-tests", help="its job that runs them")
    parser.add_argument("--durations", type=Path, help="write each file's recorded seconds here")
    parser.add_argument("--markdown", type=Path, help="append a table of the shards here")
    args = parser.parse_args(argv)
    reports, errors = load(args.reports)
    try:
        errors += expected(reports, matrix(args.workflow, args.job))
    except (OSError, ValueError) as unread:
        errors.append(str(unread))
    errors += check(reports)
    if args.durations:
        args.durations.write_text(
            json.dumps(durations(reports), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    if args.markdown:
        with args.markdown.open("a", encoding="utf-8") as out:
            out.write(markdown(reports))
    prefix = "::error::" if os.environ.get("GITHUB_ACTIONS") else "error: "
    for error in errors:
        sys.stderr.write(f"{prefix}{error}\n")
    if errors:
        return 1
    tests = sum(len(r.seconds) for r in reports)
    sys.stdout.write(
        f"{len(reports)} shards, the workflow's, ran {tests} tests: each test once on every"
        " Python version that runs its kind, and on at least one.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
