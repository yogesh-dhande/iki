"""The PR gate builds the server image on both native release platforms."""

import os
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"


def test_amd64_producer_verifies_the_export_before_uploading_it() -> None:
    workflow = yaml.safe_load((WORKFLOWS / "build-server-wasm.yml").read_text())
    job = workflow["jobs"]["build"]
    assert job["runs-on"] == "ubuntu-24.04"
    steps = job["steps"]
    export = next(s for s in steps if s.get("uses", "").startswith("docker/build-push-action@"))
    verify = next(
        s
        for s in steps
        if shlex.split(s.get("run", ""))
        == ["scripts/verify-server-wasm.sh", "$RUNNER_TEMP/server-wasm"]
    )
    upload = next(s for s in steps if s.get("uses", "").startswith("actions/upload-artifact@"))
    assert steps.index(export) < steps.index(verify) < steps.index(upload)
    assert {"if", "continue-on-error"}.isdisjoint(verify)
    assert export["with"]["target"] == "server-wasm-artifacts"
    assert export["with"]["platforms"] == "linux/amd64"
    assert export["with"]["outputs"] == "type=local,dest=" + upload["with"]["path"]
    assert upload["with"]["path"] == "${{ runner.temp }}/server-wasm"
    assert job["outputs"]["artifact-id"] == "${{ steps." + upload["id"] + ".outputs.artifact-id }}"
    events = workflow.get("on", workflow.get(True))
    output = events["workflow_call"]["outputs"]["artifact-id"]
    assert output["value"] == "${{ jobs.build.outputs.artifact-id }}"


@pytest.mark.parametrize(
    ("job_name", "platform"),
    [("server-image", "linux/amd64"), ("server-image-arm64", "linux/arm64")],
)
def test_server_gate_builds_and_runs_the_final_image(job_name: str, platform: str) -> None:
    ci = yaml.safe_load((WORKFLOWS / "ci.yml").read_text())
    release = yaml.safe_load((WORKFLOWS / "publish.yml").read_text())
    matrix = release["jobs"]["build"]["strategy"]["matrix"]["include"]
    server = next(row for row in matrix if row.get("image") == "iki-server")
    native = next(row for row in matrix if row.get("platform") == platform)

    job = ci["jobs"][job_name]
    assert job["runs-on"] == native["runner"]
    assert job["permissions"] == {"contents": "read"}
    assert set(job["needs"]) == {"changes", "server-wasm"}
    server_input = "needs.changes.outputs.server_image == 'true'"
    code_or_server_input = f"needs.changes.outputs.code == 'true' || {server_input}"
    if platform == "linux/arm64":
        assert job["if"] == (
            f"{server_input} && "
            "(github.event_name == 'pull_request' || github.event_name == 'merge_group')"
        )
    else:
        assert job["if"] == code_or_server_input
    # PyYAML reads an unquoted `on` key as True. Neither event is filtered by path.
    events = ci.get("on", ci.get(True))
    for event in ("pull_request", "merge_group"):
        assert {"paths", "paths-ignore"}.isdisjoint(events[event] or {})
    producer = ci["jobs"]["server-wasm"]
    assert producer["if"] == code_or_server_input
    assert producer["uses"] == "./.github/workflows/build-server-wasm.yml"
    assert {job_name, "server-wasm"} <= set(ci["jobs"]["ci-ok"]["needs"])
    assert (
        ci["jobs"]["changes"]["outputs"]["server_image"] == "${{ steps.diff.outputs.server_image }}"
    )

    downloader = next(
        step
        for step in job["steps"]
        if step.get("uses", "").startswith("actions/download-artifact@")
    )
    assert downloader["with"] == {
        "artifact-ids": "${{ needs.server-wasm.outputs.artifact-id }}",
        "path": "${{ runner.temp }}/server-wasm",
    }
    commands = [shlex.split(step.get("run", "")) for step in job["steps"]]
    build = next(parts for parts in commands if parts[:3] == ["docker", "buildx", "build"])
    assert build[build.index("--file") + 1] == server["file"]
    assert build[build.index("--platform") + 1] == platform
    assert build[-1] == server["context"]
    assert "--load" in build and "--target" not in build and "--push" not in build
    artifact = "$RUNNER_TEMP/server-wasm"
    assert build[build.index("--build-context") + 1] == f"server-wasm-artifacts={artifact}"
    verifier = ["scripts/verify-server-wasm.sh", artifact]
    verify_at = commands.index(verifier)
    assert job["steps"].index(downloader) < verify_at < commands.index(build)
    assert {"if", "continue-on-error"}.isdisjoint(job["steps"][verify_at])

    tag = build[build.index("--tag") + 1]
    smoke = ["scripts/ci/smoke-server-image.sh", tag, platform, artifact]
    assert commands.index(build) < commands.index(smoke)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed Git command
        ["git", "-C", str(repo), *args],  # noqa: S607 - Git on PATH
        text=True,
        check=True,
        capture_output=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "config", "user.email", "ci@example.com")
    _git(tmp_path, "config", "user.name", "CI")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    _git(tmp_path, "commit", "--quiet", "--allow-empty", "-m", "Base")
    return tmp_path


def _commit(repo: Path, path: str) -> str:
    dest = repo / path
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("Changed by the CI test.", encoding="utf-8")
    _git(repo, "add", "--", path)
    _git(repo, "commit", "--quiet", "-m", f"Change {path}")
    return _git(repo, "rev-parse", "HEAD")


def _classify(repo: Path, base: str, head: str, event: str = "pull_request") -> dict[str, str]:
    command = ROOT / "scripts" / "ci" / "changes.sh"
    output = subprocess.run(  # noqa: S603 - the repository's own script
        [str(command)],
        cwd=repo,
        env={**os.environ, "BASE": base, "HEAD": head, "EVENT": event, "GITHUB_OUTPUT": os.devnull},
        text=True,
        check=True,
        capture_output=True,
    ).stdout
    return dict(line.split("=", 1) for line in output.splitlines())


@pytest.mark.parametrize(
    ("path", "code", "server_image"),
    [
        ("server/Dockerfile", "true", "true"),
        ("server/README.md", "false", "true"),
        ("cli/README.md", "false", "true"),
        ("spec/backend/build.py", "true", "true"),
        ("codegen/README.md", "false", "true"),
        ("sdk/python/README.md", "false", "true"),
        ("server/src/iki_server/web/navigation.json", "true", "true"),
        ("web/app/package.json", "true", "true"),
        ("worker/.cargo/config.toml", "true", "true"),
        ("worker/crates/iki-wasm/README.md", "false", "true"),
        ("uv.lock", "true", "true"),
        ("pnpm-lock.yaml", "true", "true"),
        (".dockerignore", "true", "true"),
        ("scripts/verify-server-wasm.sh", "true", "true"),
        ("scripts/ci/smoke-server-image.sh", "true", "true"),
        ("plugins/plasmid/data/dna-annotation.wasm.sha256", "true", "true"),
        (".github/workflows/build-server-wasm.yml", "true", "true"),
        ("server-build.toml", "true", "true"),
        ("SERVER-PACKAGE.md", "false", "true"),
        ("new-build-assets/entrypoint.sh", "true", "true"),
        ("new-package/README.md", "false", "true"),
        ("docs/guide.md", "false", "false"),
        ("README.md", "false", "false"),
        ("deploy/dev/sso.py", "true", "false"),
    ],
)
def test_server_inputs_trigger_image_jobs(
    repo: Path, path: str, code: str, server_image: str
) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, path)
    assert _classify(repo, base, head) == {"code": code, "server_image": server_image}


def test_moving_an_input_out_of_the_build_context_triggers_the_arm_gate(repo: Path) -> None:
    base = _commit(repo, "server/README.md")
    _git(repo, "mv", "server/README.md", "README.md")
    _git(repo, "commit", "--quiet", "-m", "Move the server README")
    head = _git(repo, "rev-parse", "HEAD")
    assert _classify(repo, base, head, "merge_group") == {"code": "false", "server_image": "true"}


def test_an_unknown_base_runs_both_job_groups(repo: Path) -> None:
    head = _git(repo, "rev-parse", "HEAD")
    assert _classify(repo, "0000000", head) == {"code": "true", "server_image": "true"}
