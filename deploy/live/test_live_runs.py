"""The record of passing live runs, what's due, and the release gate: checked against a
repository made here, and against this one's record and its server's cloud modules."""

import dataclasses
import json
import re
import subprocess
from pathlib import Path

import iki_live
import live_runs
import pytest
import yaml

REPO = live_runs.REPO


def git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 - argv built here
        ["git", "-C", str(repo), *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def commit(repo: Path, files: dict[str, str], message: str) -> str:
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


def entry(cloud: str, sha: str, started: str = "2026-11-17T10:00:00Z") -> live_runs.Entry:
    return live_runs.Entry(
        cloud=cloud,
        commit=sha,
        started=started,
        duration_seconds=3600,
        cost_usd=2.5,
        suite_version="1",
        checks=["test_web_pods_are_ready_with_their_database"],
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    return tmp_path


def test_a_cloud_is_due_after_a_relevant_change_and_not_after_another(repo: Path) -> None:
    tested = commit(
        repo, {"deploy/aws/main.tf": "a", "deploy/gcp/main.tf": "a", "README.md": "a"}, "1"
    )
    record = repo / "runs.json"
    live_runs.append(entry("aws", tested), record)
    live_runs.append(entry("gcp", tested), record)
    commit(repo, {"README.md": "b", "runs.json": record.read_text()}, "docs, and the record")
    verdicts = {v.cloud: v for v in live_runs.due("HEAD", repo, record)}
    assert not verdicts["aws"].due and not verdicts["gcp"].due

    commit(repo, {"deploy/aws/main.tf": "b"}, "aws")
    verdicts = {v.cloud: v for v in live_runs.due("HEAD", repo, record)}
    assert verdicts["aws"].due and verdicts["aws"].paths == ["deploy/aws/main.tf"]
    assert "deploy/aws/main.tf" in verdicts["aws"].text()
    assert not verdicts["gcp"].due

    # The chart is every cloud's.
    commit(repo, {"deploy/helm/iki/values.yaml": "x"}, "chart")
    assert all(v.due for v in live_runs.due("HEAD", repo, record))


def test_a_change_to_what_the_backend_run_runs_makes_every_cloud_due(repo: Path) -> None:
    """The action's script runs on each cloud's nodes, in the worker the run tests too."""
    script = "plugins/protein/scripts/proteins.py"
    worker = "worker/crates/iki-worker/src/files.rs"
    tested = commit(repo, {script: "a", worker: "a", "plugins/other/x.py": "a"}, "1")
    record = repo / "runs.json"
    for cloud in live_runs.CLOUDS:
        live_runs.append(entry(cloud, tested), record)
    commit(repo, {"plugins/other/x.py": "b"}, "a plugin no run installs")
    assert not any(v.due for v in live_runs.due("HEAD", repo, record))
    for path in (script, worker):
        commit(repo, {path: path}, path)
        assert all(v.due and path in (v.paths or []) for v in live_runs.due("HEAD", repo, record))


def test_a_cloud_never_tested_is_due(repo: Path) -> None:
    commit(repo, {"a": "a"}, "1")
    (gcp, aws) = sorted(
        live_runs.due("HEAD", repo, repo / "none.json"), key=lambda v: v.cloud, reverse=True
    )
    assert gcp.due and aws.due and gcp.entry is None
    assert "no passing run" in gcp.text()


def test_only_a_run_of_an_ancestor_counts(repo: Path) -> None:
    base = commit(repo, {"a": "a"}, "1")
    git(repo, "checkout", "-qb", "side")
    side = commit(repo, {"deploy/aws/x.tf": "side"}, "side")
    git(repo, "checkout", "-q", "main")
    commit(repo, {"b": "b"}, "2")
    record = repo / "runs.json"
    live_runs.append(entry("aws", side), record)
    (aws,) = [v for v in live_runs.due("HEAD", repo, record) if v.cloud == "aws"]
    assert aws.due and aws.entry is None
    # Nor of a commit the repository doesn't have.
    live_runs.append(entry("aws", "f" * 40), record)
    (aws,) = [v for v in live_runs.due("HEAD", repo, record) if v.cloud == "aws"]
    assert aws.due and aws.entry is None
    live_runs.append(entry("aws", base, "2026-11-16T10:00:00Z"), record)
    (aws,) = [v for v in live_runs.due("HEAD", repo, record) if v.cloud == "aws"]
    assert not aws.due and aws.entry is not None and aws.entry.commit == base


def test_the_gate_names_what_is_missing_and_a_dry_run_never_fails(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tested = commit(repo, {"deploy/gcp/x.tf": "a"}, "1")
    record = repo / "runs.json"
    live_runs.append(entry("gcp", tested), record)
    commit(repo, {"runs.json": record.read_text()}, "record")
    git(repo, "tag", "v1.0.0")
    monkeypatch.setattr(live_runs, "REPO", repo)
    monkeypatch.setattr(live_runs, "RECORD", record)
    assert live_runs.main(["gate", "v1.0.0"]) == 1
    out = capsys.readouterr().out
    assert "missing: aws" in out and "gcp: not due" in out
    assert live_runs.main(["gate", "v1.0.0", "--dry-run"]) == 0
    assert "A dry run" in capsys.readouterr().out
    live_runs.append(entry("aws", tested), record)
    assert live_runs.main(["gate", "v1.0.0"]) == 0
    assert live_runs.main(["due", "--strict"]) == 0


def test_a_run_of_a_tree_with_changes_tested_no_commit(repo: Path) -> None:
    sha = commit(repo, {"a": "a"}, "1")
    assert live_runs.clean_head(repo) == sha
    (repo / "a").write_text("changed")
    assert live_runs.clean_head(repo) is None
    (repo / "a").write_text("a")
    (repo / "new").write_text("untracked")
    assert live_runs.clean_head(repo) is None


def test_an_entry_holds_only_what_it_should(tmp_path: Path) -> None:
    record = tmp_path / "runs.json"
    with pytest.raises(ValueError, match="full SHA"):
        live_runs.append(entry("aws", "abc"), record)
    with pytest.raises(ValueError, match="cloud"):
        live_runs.append(entry("azure", "a" * 40), record)
    record.write_text(json.dumps([{**dataclasses.asdict(entry("aws", "a" * 40)), "account": "1"}]))
    with pytest.raises(ValueError, match="exactly"):
        live_runs.load(record)


def test_the_record_is_written_as_prettier_writes_it(tmp_path: Path) -> None:
    record = tmp_path / "runs.json"
    live_runs.append(entry("aws", "a" * 40), record)
    text = record.read_text()
    assert text.startswith('[\n  {\n    "cloud": "aws",\n')
    assert '    "checks": ["test_web_pods_are_ready_with_their_database"]\n' in text
    long = [f"test_{c * 30}" for c in "abc"]
    assert live_runs.pretty({"checks": long}) == (
        '{\n  "checks": [\n' + ",\n".join(f'    "{c}"' for c in long) + "\n  ]\n}"
    )
    # The line's end counts: 80 columns fit, 81 don't.
    fits = {"k": ["x" * 69]}
    assert live_runs.pretty(fits) == '{\n  "k": ["' + "x" * 69 + '"]\n}'
    assert "\n    " in live_runs.pretty({"k": ["x" * 70]})


def test_this_repositorys_record_is_well_formed() -> None:
    """Every entry has exactly the fields, a full SHA, and nothing of an account's."""
    text = live_runs.RECORD.read_text()
    entries = live_runs.load()
    for e in entries:
        assert not e.problems(), e
    assert text == live_runs.pretty([dataclasses.asdict(e) for e in entries]) + "\n"
    # No AWS account ID, email address or token, in anything but the commits' SHAs.
    rest = json.dumps([{**dataclasses.asdict(e), "commit": ""} for e in entries])
    assert not re.search(r"\d{12}|@|ya29\.|eyJ", rest)


#: Server modules that touch a cloud's SDK but are every cloud's, and why.
SHARED_SERVER_MODULES = {
    # Turns the storage backend's errors into API responses; nothing cloud-specific.
    "server/src/iki_server/api/v1.py",
    # The provisioner's command: it runs whichever fleets are configured.
    "server/src/iki_server/runs/management/commands/provision.py",
}


def cloud_modules() -> dict[str, set[str]]:
    """The server's and the worker's code that's one cloud's: what imports its SDK, or is
    named for its services (ec2_*, s3_*, gce_*, gcs_*), or reads its metadata server."""
    found: dict[str, set[str]] = {"aws": set(), "gcp": set()}
    for path in sorted((REPO / "server/src").rglob("*.py")):
        name = path.relative_to(REPO).as_posix()
        text = path.read_text()
        if re.search(r"^\s*(import|from) (boto3|botocore)\b", text, re.M) or re.match(
            r"(ec2|s3)(_|\.py$)", path.name
        ):
            found["aws"].add(name)
        if re.search(r"^\s*(import|from) google\b", text, re.M) or re.match(
            r"(gce|gcs)(_|\.py$)", path.name
        ):
            found["gcp"].add(name)
    for path in sorted((REPO / "worker/crates").rglob("*.rs")):
        name = path.relative_to(REPO).as_posix()
        text = path.read_text()
        if "169.254.169.254" in text:
            found["aws"].add(name)
        if "metadata.google.internal" in text:
            found["gcp"].add(name)
    return found


def covered(name: str, cloud: str) -> bool:
    return any(
        name == p or (p.endswith("/") and name.startswith(p)) for p in live_runs.RELEVANT[cloud]
    )


def test_every_module_of_one_cloud_is_relevant_to_its_live_test() -> None:
    """A new module for a cloud (a provisioner, a storage backend) changes what that cloud's
    live test should cover: list it in live_runs.RELEVANT, so make live-due and the release
    gate see changes to it."""
    for cloud, modules in cloud_modules().items():
        missing = sorted(m for m in modules - SHARED_SERVER_MODULES if not covered(m, cloud))
        assert not missing, f"list these in live_runs.RELEVANT[{cloud!r}]: {missing}"


def test_what_the_runs_install_and_run_is_relevant_to_every_cloud() -> None:
    """Every plugin the harness installs, with the script of the action it runs: a plugin
    added to the runs, or another action run, would otherwise change nothing that's due."""
    plugins = [f"plugins/{name}/plugin.yaml" for name in iki_live.PLUGINS]
    plugin, action = iki_live.ACTION.removeprefix("iki/").split(".")
    manifest = yaml.safe_load(
        (REPO / "plugins" / plugin / "actions" / f"{action}.yaml").read_text()
    )
    scripts = [
        f"plugins/{plugin}/{arg.removeprefix('./')}"
        for arg in manifest["command"]
        if arg.startswith("./")
    ]
    assert scripts, manifest["command"]
    for path in [*plugins, *scripts]:
        assert (REPO / path).is_file(), path
        for cloud in live_runs.CLOUDS:
            assert covered(path, cloud), f"list {path} in live_runs.RELEVANT[{cloud!r}]"


def test_every_relevant_path_exists() -> None:
    """A path renamed away would quietly cover nothing."""
    for cloud, paths in live_runs.RELEVANT.items():
        for path in paths:
            assert (REPO / path).exists(), f"{cloud}: {path} doesn't exist"
    assert all(not p.startswith("deploy/live-runs") for p in live_runs.SHARED)
