"""Which commits the live cloud tests passed on, and whether a cloud is due to be tested again.

The live tests (make check-gcp-live, make check-aws-live) deploy Iki to a real cloud, check
it and tear it down: an hour or two, and a few dollars, each. So they don't run on every
commit, or on CI at all. They run now and then, when something they cover has changed, and
always before a release is tagged.

Each passing run appends an entry to deploy/live-runs.json (the harness writes it; commit
it): the cloud, the commit tested, with the tree clean, when it began, how long it took,
its cost at list prices, the suite's version, and the checks that passed. Nothing about
the account it ran in. A failed run, or one of a tree with changes, records nothing.

RELEVANT lists, for each cloud, the paths a change to which the last passing run didn't
test. Then:

    python deploy/live/live_runs.py due [--strict]   # make live-due
    python deploy/live/live_runs.py gate <ref> [--dry-run]

``due`` says, for each cloud, its last passing run and what relevant paths changed since
(exit 1 for any due only with --strict). ``gate`` is the release workflow's: for a tag,
every cloud needs a passing run of an ancestor of it with no relevant change between them.

Only the standard library and git: the release workflow runs it with the runner's Python.
"""

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
RECORD = REPO / "deploy" / "live-runs.json"

#: What every cloud's run tests: whatever the cloud under it could change the outcome of.
#: Not deploy/live-runs.json: recording a run changes only that.
SHARED = (
    # The chart, the images it builds and their Dockerfiles, and the harness itself.
    ".dockerignore",
    "deploy/helm/",
    "deploy/live/iki_live.py",
    "deploy/plugins.Dockerfile",
    "deploy/worker.Dockerfile",
    "server/Dockerfile",
    "server/iki_wasm.wasm.sha256",
    "server/iki_wasm.amd64.cwasm.sha256",
    "server/iki_wasm.arm64.cwasm.sha256",
    "worker/Dockerfile",
    "scripts/build-dna-annotation-wasm.sh",
    "scripts/build-oligo-checks-wasm.sh",
    "scripts/build-oligo-wasm.sh",
    "scripts/build-plasmid-tools-wasm.sh",
    "scripts/build-server-aot.sh",
    "scripts/verify-server-aot.py",
    "scripts/verify-server-wasm.sh",
    "plugins/plasmid/data/dna-annotation.wasm.sha256",
    "plugins/plasmid/data/plasmid-tools.wasm.sha256",
    # What those images are built from besides the code below: the locked dependencies
    # (the server's Python and its transport to the cloud's APIs; the worker's Rust) and
    # the worker's toolchain and manifest, which `cargo build --locked` reads.
    "uv.lock",
    "worker/.cargo/",
    "worker/Cargo.lock",
    "worker/Cargo.toml",
    "worker/rust-toolchain.toml",
    # `iki deploy build`, which builds the plugins into the server's image and writes the
    # build context of the worker's (deploy/worker.Dockerfile), environments and all.
    "cli/src/iki_cli/deploy.py",
    "cli/src/iki_cli/workers.py",
    # The plugins it installs (iki_live.PLUGINS): their migrations run on the cloud's
    # database, their environments are built into the worker's image, and the backend
    # run's action (iki_live.ACTION) runs their script on the cloud's nodes.
    "plugins/dna/",
    "plugins/protein/",
    # The crates copied into the worker and server Wasm builds.
    "worker/crates/",
    # The server's code the checks reach that's the cloud's to decide: the database's
    # connection over verified TLS, storage (whichever cloud's backend it hands to), its
    # check of the bucket, readiness, and the setup wizard.
    "server/src/iki_server/settings.py",
    "server/src/iki_server/core/postgresql/",
    "server/src/iki_server/files/storage.py",
    "server/src/iki_server/files/management/commands/setup_storage.py",
    "server/src/iki_server/health.py",
    "server/src/iki_server/setup/",
)

#: For each cloud, what its run tests beyond SHARED: its deploy/<cloud>/ (modules, roots,
#: scripts and its own live test), and the server's code for that cloud: the storage
#: backend (S3's is storage.py, in SHARED, since it hands to GCS's), the VM tier's
#: provisioner and its fleets and volumes. test_live_runs.py fails if
#: a module of the server that's one cloud's isn't listed for it.
RELEVANT: dict[str, tuple[str, ...]] = {
    "gcp": (
        *SHARED,
        "deploy/gcp/",
        "server/src/iki_server/files/gcs.py",
        "server/src/iki_server/runs/gce.py",
        "server/src/iki_server/runs/gce_fleet.py",
        "server/src/iki_server/runs/gce_volumes.py",
        # Its VM-tier run (48h): the live check plugin it installs and runs on Compute
        # Engine, EC2's boot script and host config its machines share, what every
        # machine's worker is given (its settings, the CA's certificates), the provisioner
        # that starts them, setup builds and the volumes they make, and the artifacts a
        # run keeps.
        "deploy/live/plugins/live_check/",
        "server/src/iki_server/runs/ec2.py",
        "server/src/iki_server/runs/ec2_fleet.py",
        "server/src/iki_server/runs/machines.py",
        "server/src/iki_server/runs/provisioner.py",
        "server/src/iki_server/runs/setups.py",
        "server/src/iki_server/runs/volumes.py",
        "server/src/iki_server/runs/artifacts.py",
    ),
    "aws": (
        *SHARED,
        "deploy/aws/",
        "server/src/iki_server/runs/ec2.py",
        "server/src/iki_server/runs/ec2_fleet.py",
        "server/src/iki_server/runs/ec2_volumes.py",
    ),
}
CLOUDS = tuple(RELEVANT)
#: What an entry holds, and nothing more: no account, project, email or token.
FIELDS = ("cloud", "commit", "started", "duration_seconds", "cost_usd", "suite_version", "checks")


@dataclass(frozen=True)
class Entry:
    cloud: str
    commit: str
    started: str
    duration_seconds: int
    cost_usd: float | None
    suite_version: str
    checks: list[str]

    def problems(self) -> list[str]:
        found = []
        if self.cloud not in CLOUDS:
            found.append(f"cloud {self.cloud!r} isn't one of {', '.join(CLOUDS)}")
        if not re.fullmatch(r"[0-9a-f]{40}", self.commit):
            found.append(f"commit {self.commit!r} isn't a full SHA")
        if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", self.started):
            found.append(f"started {self.started!r} isn't a UTC time such as 2026-11-17T10:00:00Z")
        if self.duration_seconds <= 0:
            found.append("duration_seconds isn't positive")
        if self.cost_usd is not None and self.cost_usd < 0:
            found.append("cost_usd is negative")
        if not self.checks or not all(re.fullmatch(r"test_\w+", c) for c in self.checks):
            found.append(f"checks {self.checks!r} aren't the tests' names")
        return found


def load(path: Path = RECORD) -> list[Entry]:
    """The record's entries, oldest first. A missing file is no runs."""
    if not path.exists():
        return []
    raw = json.loads(path.read_text())
    if not isinstance(raw, list):
        raise ValueError(f"{path} isn't a JSON list")
    entries = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != set(FIELDS):
            raise ValueError(f"{path}: an entry must have exactly {', '.join(FIELDS)}: {item!r}")
        entries.append(Entry(**item))
    return entries


def append(entry: Entry, path: Path = RECORD) -> None:
    problems = entry.problems()
    if problems:
        raise ValueError("; ".join(problems))
    entries = [asdict(e) for e in load(path)] + [asdict(entry)]
    path.write_text(pretty(entries) + "\n")


def pretty(value: Any, indent: int = 0, lead: int = 0, trail: int = 0, width: int = 80) -> str:
    """JSON as Prettier prints it: an object one member a line; a list of no objects or
    lists on one line if that line fits in width, else one item a line. lead and trail are
    the columns before the value on its line (its key) and after it (a comma)."""
    pad = " " * indent
    inner = " " * (indent + 2)
    if isinstance(value, dict):
        if not value:
            return "{}"
        last = len(value) - 1
        members = []
        for i, (k, v) in enumerate(value.items()):
            key = f"{inner}{json.dumps(k)}: "
            members.append(key + pretty(v, indent + 2, len(key), int(i < last), width))
        return "{\n" + ",\n".join(members) + f"\n{pad}}}"
    if isinstance(value, list):
        if not value:
            return "[]"
        flat = "[" + ", ".join(json.dumps(v) for v in value) + "]"
        if not any(isinstance(v, dict | list) for v in value) and lead + len(flat) + trail <= width:
            return flat
        last = len(value) - 1
        items = [
            inner + pretty(v, indent + 2, len(inner), int(i < last), width)
            for i, v in enumerate(value)
        ]
        return "[\n" + ",\n".join(items) + f"\n{pad}]"
    return json.dumps(value)


# --- git ---------------------------------------------------------------------------------


def git(*args: str, repo: Path = REPO) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - argv built here
        ["git", "-C", str(repo), *args],  # noqa: S607 - git from the PATH
        capture_output=True,
        text=True,
        check=False,
    )


def clean_head(repo: Path = REPO) -> str | None:
    """HEAD's full SHA if the tree has no changes (staged, unstaged or untracked, ignored
    files aside), else None: a run of a tree with changes didn't test any commit."""
    status = git("status", "--porcelain", repo=repo)
    if status.returncode != 0 or status.stdout.strip():
        return None
    head = git("rev-parse", "HEAD", repo=repo)
    return head.stdout.strip() if head.returncode == 0 else None


def changed(tested: str, ref: str, cloud: str, repo: Path = REPO) -> list[str] | None:
    """The relevant paths that differ between tested and ref, excluding documentation.
    Return None if tested isn't a commit this repository has."""
    if git("cat-file", "-e", f"{tested}^{{commit}}", repo=repo).returncode != 0:
        return None
    diff = git(
        "diff",
        "--name-only",
        "--no-renames",
        "-z",
        tested,
        ref,
        "--",
        *RELEVANT[cloud],
        ":(exclude,glob)**/*.md",
        ":(exclude,glob)**/docs/**",
        repo=repo,
    )
    if diff.returncode != 0:
        raise RuntimeError(f"git diff {tested} {ref}: {diff.stderr.strip()}")
    return [path for path in diff.stdout.split("\0") if path]


def is_ancestor(commit: str, ref: str, repo: Path = REPO) -> bool:
    return git("merge-base", "--is-ancestor", commit, ref, repo=repo).returncode == 0


# --- What's due, and the release gate ---------------------------------------------------------


@dataclass
class Verdict:
    cloud: str
    entry: Entry | None
    #: Relevant paths changed since entry's commit; None if there's no usable entry.
    paths: list[str] | None

    @property
    def due(self) -> bool:
        return self.paths is None or bool(self.paths)

    def text(self) -> str:
        if self.entry is None or self.paths is None:
            return f"{self.cloud}: due: no passing run recorded of a commit this branch has"
        run = (
            f"last passed at {self.entry.commit[:12]} ({self.entry.started}, "
            f"{_cost(self.entry.cost_usd)}, suite {self.entry.suite_version})"
        )
        if not self.paths:
            return f"{self.cloud}: not due: {run}, and nothing it tests has changed since"
        shown = "\n".join(f"    {p}" for p in self.paths[:40])
        more = f"\n    ... and {len(self.paths) - 40} more" if len(self.paths) > 40 else ""
        return f"{self.cloud}: due: {run}; since then, changed:\n{shown}{more}"


def _cost(usd: float | None) -> str:
    return "cost unknown" if usd is None else f"${usd:.2f} at list prices"


def verdict(cloud: str, ref: str, entries: list[Entry], repo: Path = REPO) -> Verdict:
    """The cloud's best passing run for ref: of an ancestor of ref (or ref itself), with
    the fewest relevant changes since, the latest among those."""
    best: Verdict | None = None
    for entry in sorted(
        (e for e in entries if e.cloud == cloud), key=lambda e: e.started, reverse=True
    ):
        if not is_ancestor(entry.commit, ref, repo):
            continue
        paths = changed(entry.commit, ref, cloud, repo)
        if paths is None:
            continue
        if not paths:
            return Verdict(cloud, entry, paths)
        if best is None:
            best = Verdict(cloud, entry, paths)
    return best or Verdict(cloud, None, None)


def due(ref: str = "HEAD", repo: Path | None = None, record: Path | None = None) -> list[Verdict]:
    """Each cloud's verdict for ref: this repository and its record, unless given others."""
    entries = load(record or RECORD)
    return [verdict(cloud, ref, entries, repo or REPO) for cloud in CLOUDS]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    due_parser = commands.add_parser("due", help="which clouds' live tests are due, and why")
    due_parser.add_argument("--strict", action="store_true", help="exit 1 if any is due")
    gate_parser = commands.add_parser("gate", help="the release gate: every cloud passed for ref")
    gate_parser.add_argument("ref", help="the commit or tag to be released")
    gate_parser.add_argument("--dry-run", action="store_true", help="report, but never fail")
    args = parser.parse_args(argv)

    if args.command == "due":
        verdicts = due()
        for v in verdicts:
            print(v.text())
        late = [v.cloud for v in verdicts if v.due]
        if late:
            runs = " and ".join(f"make check-{cloud}-live" for cloud in late)
            print(f"\nDue: {', '.join(late)}. Run {runs}: each bills its cloud.")
        return 1 if late and args.strict else 0

    verdicts = due(args.ref)
    for v in verdicts:
        print(v.text())
    missing = [v for v in verdicts if v.due]
    if not missing:
        print(f"\nEvery cloud's live test passed for {args.ref}.")
        return 0
    names = ", ".join(v.cloud for v in missing)
    print(
        f"\nThe release needs a passing live run of each cloud, of a commit {args.ref} descends "
        f"from with nothing they test changed since; missing: {names}. Run make "
        "check-<cloud>-live on the release commit and commit deploy/live-runs.json."
    )
    if args.dry_run:
        print("(A dry run: not failing.)")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
