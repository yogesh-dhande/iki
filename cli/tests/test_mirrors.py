"""Iki's own plugins from mirrors of Iki's repository (ADR 0114).

Each mirror here is a repository on disk holding a copy of Iki's
``plugins/dna``, made from the files git tracks in this checkout, as a
mirror's clone would hold them.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from iki_cli import fetch, workers
from iki_cli.main import main
from iki_spec import first_party, naming
from iki_spec.lockfile import LockedPlugin

ROOT = Path(__file__).parents[2]
CANONICAL = naming.IKI_REPOSITORY

pytestmark = pytest.mark.skipif(not (ROOT / ".git").exists(), reason="needs Iki's repository")
#: A build that succeeds bundles iki/dna pinned, and a pinned plugin's Wasm modules must be
#: the ones its digests name: the Linux x86-64 build (ADR 0200). Elsewhere, such as on a
#: Mac, this checkout's modules are built for the host, and the build rightly refuses them.
builds_release_modules = pytest.mark.skipif(
    not workers._digests_hold_here(),
    reason="bundles a pinned plugin, whose modules only a Linux x86-64 host builds",
)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 - a test's own git commands
        ["git", "-C", str(repo), *args],  # noqa: S607 - git from the PATH, as Iki runs it
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def mirror(tmp_path: Path, name: str, *, change: str | None = None) -> tuple[str, str]:
    """A mirror holding Iki's plugins/dna (with ``change`` appended to its README, if
    given), and its commit."""
    repo = tmp_path / name
    git(tmp_path, "init", "--quiet", "--initial-branch=main", str(repo))
    for relative in git(ROOT, "ls-files", "plugins/dna").splitlines():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    if change is not None:
        readme = repo / "plugins" / "dna" / "README.md"
        readme.write_text(readme.read_text() + change)
    git(repo, "add", ".")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-qm", "mirror")
    return f"file://{repo}", git(repo, "rev-parse", "HEAD")


def deployment(tmp_path: Path, sha: str) -> Path:
    directory = tmp_path / "deployments" / "prod"
    (directory / "settings").mkdir(parents=True)
    (directory / "settings" / "dna.yaml").write_text("ncbi_email: lab@example.org\n")
    (directory / "plugins.lock.yaml").write_text(
        "apiVersion: iki/v1\nkind: PluginLock\nplugins:\n"
        f"  - name: iki/dna\n    git: {CANONICAL}\n    path: plugins/dna\n    sha: {sha}\n"
        "    state: active\n    settings: settings/dna.yaml\n"
    )
    return directory


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """No git config but the test's own; mirrors on disk read as https ones would be."""
    monkeypatch.setenv("NO_COLOR", "1")
    config = tmp_path / "gitconfig"
    config.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(fetch, "PROTOCOLS", ("https", "ssh", "file"))
    return config


def build(tmp_path: Path, sha: str, *mirrors: str) -> int:
    flags = [arg for url in mirrors for arg in ("--mirror", url)]
    out = str(tmp_path / "plugins")
    return main(
        [
            "deploy",
            "build",
            str(deployment(tmp_path, sha)),
            "-o",
            out,
            "--workers",
            str(tmp_path / "workers"),
            *flags,
        ]
    )


@builds_release_modules
def test_ikis_plugin_comes_from_the_mirror_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    url, sha = mirror(tmp_path, "mirror")
    assert build(tmp_path, sha, url) == 0
    printed = capsys.readouterr().out
    assert f"Fetched iki/dna at {sha[:12]} from {url} (a mirror of {CANONICAL})" in printed
    registry = tmp_path / "plugins" / "iki_installed_plugins"
    (entry,) = json.loads((registry / "lock.json").read_text())["plugins"]
    # The lockfile still names Iki's repository; the build records the mirror.
    assert entry["git"] == CANONICAL
    assert entry["sha"] == sha
    assert (registry / "bundles" / f"iki__dna-{sha}.tar.gz").is_file()
    assert (tmp_path / "workers/plugins/iki__dna/plugin.yaml").is_file()
    assert entry["fetched_from"] == url
    assert f"MIRRORS = [{url!r}]" in (registry / "__init__.py").read_text()


@builds_release_modules
def test_mirrors_are_tried_in_order(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    empty, other = (f"file://{tmp_path / name}" for name in ("empty", "other"))
    for name in ("empty", "other"):
        git(tmp_path, "init", "--quiet", "--bare", str(tmp_path / name))
    url, sha = mirror(tmp_path, "second")
    assert build(tmp_path, sha, empty, url) == 0
    assert f"from {url} (a mirror of" in capsys.readouterr().out
    # None has it: each says why, and Iki's repository isn't tried.
    assert build(tmp_path / "again", sha, empty, other) == 1
    err = capsys.readouterr().err
    assert f"iki/dna: no mirror of Iki's repository had commit {sha[:12]}:" in err
    assert f"from {empty}:" in err and f"from {other}:" in err
    assert CANONICAL not in err


def test_a_mirror_cant_serve_a_commit_of_its_own(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    url, sha = mirror(tmp_path, "fork", change="\nChanged by the lab.\n")
    assert build(tmp_path, sha, url) == 1
    err = capsys.readouterr().err
    assert f"`plugins/dna` at commit {sha[:12]} from the mirror {url} isn't one Iki's" in err
    assert "give it your namespace, such as `acme/dna`" in err
    assert not (tmp_path / "plugins").exists()


def test_selecting_a_mirror_checks_its_tree_even_when_the_lock_names_that_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, sha = mirror(tmp_path, "cached")
    plugin = LockedPlugin("iki/dna", "active", git=CANONICAL, sha=sha, path="plugins/dna")
    # The build has this commit in its cache, but its CLI has no approved tree for it.
    monkeypatch.setattr(fetch, "_cache", lambda cache, source: tmp_path / "cached" / ".git")
    monkeypatch.setattr(first_party, "trees", lambda name: frozenset())
    with pytest.raises(fetch.FetchError, match="isn't one Iki's repository has had"):
        fetch.fetch(plugin, tmp_path, tmp_path / "out", protocols=("file",), mirrors=[CANONICAL])


def test_the_list_holds_this_checkouts_tree(tmp_path: Path) -> None:
    _, sha = mirror(tmp_path, "mirror")
    tree = git(tmp_path / "mirror", "rev-parse", f"{sha}:plugins/dna")
    assert tree in first_party.trees("iki/dna")


def test_gits_own_config_cant_quietly_send_ikis_repository_elsewhere(
    tmp_path: Path, _environment: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    url, sha = mirror(tmp_path, "mirror")
    _environment.write_text(f'[url "{url}"]\n\tinsteadOf = {CANONICAL}\n')
    assert build(tmp_path, sha) == 1
    err = capsys.readouterr().err
    assert f"git's config sends {CANONICAL} to {url}" in err
    assert "Name a mirror of Iki's repository" in err


@pytest.mark.parametrize(
    "effective",
    [
        "ssh://git@git.example.org/2222/Org/Repo.git",
        "ssh://git@git.example.org:2222/org/Repo.git",
        "https://git.example.org:2222/Org/Repo.git",
    ],
)
def test_fetch_checks_the_scheme_port_and_path_git_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, effective: str
) -> None:
    mirror_url = "ssh://git@git.example.org:2222/Org/Repo.git"
    plugin = LockedPlugin("iki/dna", "active", git=CANONICAL, sha="a" * 40)
    git_client = fetch._Git(tmp_path / "cache", fetch.PROTOCOLS)
    monkeypatch.setattr(git_client, "effective", lambda url: effective)
    with pytest.raises(fetch.FetchError, match="git's config sends"):
        git_client.check_not_rewritten(plugin, mirror_url)


@pytest.mark.parametrize(
    ("mirror_url", "effective"),
    [
        ("ssh://git@git.example.org:22/Org/Repo.git/", "git@GIT.example.org:/Org/Repo"),
        ("https://git.example.org/Org/Repo.git", "https://ci@GIT.example.org:443/Org/Repo"),
    ],
)
def test_fetch_accepts_credential_and_spelling_changes_to_the_same_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mirror_url: str, effective: str
) -> None:
    plugin = LockedPlugin("iki/dna", "active", git=CANONICAL, sha="a" * 40)
    git_client = fetch._Git(tmp_path / "cache", fetch.PROTOCOLS)
    monkeypatch.setattr(git_client, "effective", lambda url: effective)
    git_client.check_not_rewritten(plugin, mirror_url)


@pytest.mark.parametrize(
    ("url", "why"),
    [
        ("https://x:ghp_EXAMPLE0SECRET@git.acme.example/iki.git", "has a password in it"),
        ("https://ghp_EXAMPLE0SECRET@git.acme.example/iki.git", "has a user or token in it"),
        ("ssh://git:ghp_EXAMPLE0SECRET@git.acme.example/iki.git", "has a password in it"),
        ("http://git.acme.example/iki.git", "isn't an https or ssh"),
        ("ext::sh -c touch% /tmp/x", "isn't an https or ssh"),
        ("https://git.acme.example/iki.git?ref=main", "has a query or fragment"),
    ],
)
def test_a_mirror_is_an_https_or_ssh_url_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    url: str,
    why: str,
) -> None:
    monkeypatch.setattr(fetch, "PROTOCOLS", ("https", "ssh"))
    assert build(tmp_path, "a" * 40, url) == 1
    err = capsys.readouterr().err
    assert why in err
    assert "ghp_EXAMPLE0SECRET" not in err


def test_check_mirror_takes_ssh_in_both_forms() -> None:
    assert fetch.check_mirror("ssh://git@git.acme.example:2222/mirrors/iki.git") is None
    assert fetch.check_mirror("git@git.acme.example:mirrors/iki.git") is None
    assert fetch.check_mirror("https://git.acme.example/mirrors/iki.git") is None
