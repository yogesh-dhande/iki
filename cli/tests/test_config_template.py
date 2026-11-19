"""The example config repository (deploy/config-template) stays valid as Iki changes."""

import os
import re
import subprocess
from importlib.metadata import version
from pathlib import Path

import pytest
import yaml

from iki_spec.lockfile import load_lock

ROOT = Path(__file__).parents[2]
TEMPLATE = ROOT / "deploy" / "config-template"
DEPLOYMENTS = sorted(p.name for p in (TEMPLATE / "deployments").iterdir())


def test_has_staging_and_production() -> None:
    assert DEPLOYMENTS == ["production", "staging"]


@pytest.mark.parametrize("deployment", DEPLOYMENTS)
def test_lockfiles_are_valid(deployment: str) -> None:
    result = load_lock(TEMPLATE / "deployments" / deployment / "plugins.lock.yaml")
    assert result.problems == []
    assert result.lock is not None
    assert not result.lock.has_local


@pytest.mark.parametrize("deployment", DEPLOYMENTS)
def test_names_this_iki_release(deployment: str) -> None:
    iki = yaml.safe_load((TEMPLATE / "deployments" / deployment / "iki.yaml").read_text())
    release = version("iki-cli").split(".dev")[0]
    assert iki["version"] == release
    assert iki["chart"] == release
    assert re.fullmatch(
        rf"ghcr\.io/yogesh-dhande/iki-server:{re.escape(release)}@sha256:[0-9a-f]{{64}}",
        iki["serverImage"],
    )
    assert re.fullmatch(
        rf"ghcr\.io/yogesh-dhande/iki-worker:{re.escape(release)}@sha256:[0-9a-f]{{64}}",
        iki["workerImage"],
    )


@pytest.mark.parametrize("dockerfile", ["plugins.Dockerfile", "worker.Dockerfile"])
def test_uses_ikis_own_dockerfiles(dockerfile: str) -> None:
    assert (TEMPLATE / dockerfile).read_text() == (ROOT / "deploy" / dockerfile).read_text()


def test_actions_are_pinned_by_commit() -> None:
    workflow = (TEMPLATE / ".github" / "workflows" / "build.yml").read_text()
    uses = re.findall(r"uses: (\S+)", workflow)
    assert uses
    for action in uses:
        assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", action), action
    parsed = yaml.safe_load(workflow)
    assert parsed["permissions"] == {}


# --- What CI's build gives git --------------------------------------------------------------

TOKEN = "github_pat_11TEST"
#: What each expression the build job's steps use is, on a pull request's run.
EXPRESSIONS = {
    "matrix.deployment": "staging",
    "steps.iki.outputs.version": "0.1.0",
    "github.server_url": "https://github.com",
}
#: Stands in for `iki deploy build`: asks git for credentials as its fetch of a private
#: plugin does, from this GitHub and from another host, and writes what git answered.
ASKS = """#!/bin/sh
for host in github.com git.example.org; do
  printf 'protocol=https\\nhost=%s\\npath=acme/assays.git\\n\\n' "$host" |
    git credential fill > "$ANSWERS/$host" 2>/dev/null || echo none > "$ANSWERS/$host"
done
"""


def expand(text: str, secrets: dict[str, str]) -> str:
    def value(match: re.Match[str]) -> str:
        name = match.group(1)
        if name.startswith("secrets."):
            return secrets.get(name.removeprefix("secrets."), "")
        return EXPRESSIONS[name]

    return re.sub(r"\$\{\{\s*([\w.-]+)\s*\}\}", value, text)


def run_build_job(tmp_path: Path, secrets: dict[str, str]) -> dict[str, str]:
    """The build job's steps up to and including the plugins' build, as a runner runs
    them: each in a shell of its own with its own env, git's global config shared, the
    tools stubbed. Then a later step's git asks too. What git answered, by host."""
    job = yaml.safe_load((TEMPLATE / ".github" / "workflows" / "build.yml").read_text())
    job = job["jobs"]["build"]
    bin_dir, answers = tmp_path / "bin", tmp_path / "answers"
    for directory in (bin_dir, answers, tmp_path / "home"):
        directory.mkdir()
    for name, script in {"yq": "#!/bin/sh\n", "uvx": ASKS}.items():
        (bin_dir / name).write_text(script)
        (bin_dir / name).chmod(0o755)
    base = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "GITHUB_SERVER_URL": "https://github.com",
        "ANSWERS": str(answers),
        **{k: expand(str(v), secrets) for k, v in job.get("env", {}).items()},
    }

    def step(script: str, env: dict[str, str]) -> None:
        subprocess.run(  # noqa: S603 - the workflow's own step
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],  # noqa: S607
            env={**base, **env},
            cwd=tmp_path,
            check=True,
        )

    for each in job["steps"]:
        if "run" not in each:
            continue
        step(
            expand(each["run"], secrets),
            {k: expand(str(v), secrets) for k, v in each.get("env", {}).items()},
        )
        if "iki deploy build" in each["run"]:
            break
    else:
        pytest.fail("no step runs `iki deploy build`")
    found = {host: (answers / host).read_text() for host in ("github.com", "git.example.org")}
    step(ASKS.removeprefix("#!/bin/sh\n"), {})
    found["a later step"] = (answers / "github.com").read_text()
    return found


def test_the_plugins_build_gives_git_the_token_for_github_alone(tmp_path: Path) -> None:
    """A private plugin over https: the step that fetches it has the token, for git to give
    this GitHub when it asks, and to no other host, nor to any later step."""
    answers = run_build_job(tmp_path, {"PLUGIN_REPOS_TOKEN": TOKEN})
    assert f"username=x-access-token\npassword={TOKEN}\n" in answers["github.com"]
    assert answers["git.example.org"] == "none\n"
    assert answers["a later step"] == "none\n"


def test_without_the_token_git_is_given_no_password(tmp_path: Path) -> None:
    """Public plugins need no token: git gets no empty password to fail with."""
    answers = run_build_job(tmp_path, {})
    assert set(answers.values()) == {"none\n"}
