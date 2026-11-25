import subprocess
import textwrap
from pathlib import Path

import pytest

from iki_spec import naming
from iki_spec.lockfile import (
    LockedPlugin,
    load_lock,
    lock_to_dict,
    redact_credentials,
    without_credentials,
)

SHA = "4f1c2b9e0d6a7c3b8e5f1a2d9c0b7e6f5a4d3c2b"


def lock(tmp_path: Path, plugins: str, *, settings: tuple[str, ...] = ()) -> Path:
    for name in settings:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("{}\n")
    file = tmp_path / "plugins.lock.yaml"
    file.write_text(
        "apiVersion: iki/v1\nkind: PluginLock\nplugins:\n"
        + textwrap.indent(textwrap.dedent(plugins), "  ")
    )
    return file


def codes(file: Path) -> list[str]:
    return [p.code for p in load_lock(file).problems]


def test_reads_plugins_from_git_and_local_directories(tmp_path: Path) -> None:
    file = lock(
        tmp_path,
        f"""\
        - name: acme/dna
          git: https://github.com/acme/iki-plugins.git
          path: plugins/dna
          sha: {SHA}
          state: active
          settings: settings/dna.yaml
        - name: acme/assays
          git: git@github.com:acme/assays.git
          sha: {"b" * 64}
          state: active
        - name: acme/scratch
          local: ../scratch
          state: active
        """,
        settings=("settings/dna.yaml",),
    )
    result = load_lock(file)
    assert result.problems == []
    assert result.lock is not None
    assert result.lock.plugins[0] == LockedPlugin(
        name="acme/dna",
        state="active",
        git="https://github.com/acme/iki-plugins.git",
        sha=SHA,
        path="plugins/dna",
        settings="settings/dna.yaml",
    )
    assert result.lock.has_local
    assert lock_to_dict(result.lock)["plugins"][2] == {
        "name": "acme/scratch",
        "state": "active",
        "local": "../scratch",
    }


def test_a_plugin_can_be_detached(tmp_path: Path) -> None:
    file = lock(
        tmp_path, f"- {{name: acme/dna, git: https://x.org/p, sha: {SHA}, state: detached}}\n"
    )
    result = load_lock(file)
    assert result.problems == []
    assert result.lock is not None
    assert result.lock.plugins[0].state == "detached"


def test_a_purged_plugin_has_only_its_name(tmp_path: Path) -> None:
    result = load_lock(lock(tmp_path, "- {name: acme/lims, state: purged}\n"))
    assert result.problems == []
    assert result.lock is not None
    assert result.lock.plugins[0].to_dict() == {"name": "acme/lims", "state": "purged"}
    kept = load_lock(
        lock(tmp_path, f"- {{name: acme/lims, git: https://x.org/p, sha: {SHA}, state: purged}}\n")
    )
    assert [(p.code, p.message) for p in kept.problems] == [
        ("purged-source", "acme/lims is purged, so it isn't fetched or built: remove `git`."),
        ("purged-source", "acme/lims is purged, so it isn't fetched or built: remove `sha`."),
    ]


def test_an_empty_deployment(tmp_path: Path) -> None:
    file = tmp_path / "plugins.lock.yaml"
    file.write_text("apiVersion: iki/v1\nkind: PluginLock\nplugins: []\n")
    result = load_lock(file)
    assert result.ok
    assert result.lock is not None
    assert result.lock.plugins == ()


@pytest.mark.parametrize(
    ("entry", "code"),
    [
        ("- {name: acme/dna, git: https://x.org/p, sha: main, state: active}", "schema"),
        (f"- {{name: acme/dna, git: https://x.org/p, sha: {SHA[:12]}, state: active}}", "schema"),
        (f"- {{name: acme/dna, git: 'file:///srv/p', sha: {SHA}, state: active}}", "schema"),
        (f"- {{name: acme/dna, git: 'ext::sh -c id', sha: {SHA}, state: active}}", "schema"),
        (f"- {{name: acme/dna, git: https://x.org/p, sha: {SHA}, state: removed}}", "schema"),
        (f"- {{name: acme/dna, git: https://x.org/p, sha: {SHA}}}", "schema"),
        ("- {name: acme/dna, git: https://x.org/p, state: active}", "missing-sha"),
        ("- {name: acme/dna, state: active}", "missing-source"),
        (f"- {{name: acme/dna, local: ../dna, sha: {SHA}, state: active}}", "mixed-source"),
        (
            f"- {{name: acme/dna, git: https://x.org/p, sha: {SHA}, path: a/../../b, "
            "state: active}",
            "path-outside",
        ),
        (
            f"- {{name: iki/core, git: https://x.org/p, sha: {SHA}, state: active}}",
            "core-is-builtin",
        ),
        (
            f"- {{name: acme/dna, git: https://x.org/p, sha: {SHA}, state: active, "
            "settings: s.yaml}",
            "missing-settings-file",
        ),
        (
            f"- {{name: acme/dna, git: https://x.org/p, sha: {SHA}, state: active, "
            "settings: /s.yaml}",
            "absolute-path",
        ),
    ],
)
def test_refuses(tmp_path: Path, entry: str, code: str) -> None:
    assert codes(lock(tmp_path, entry)) == [code]


def test_each_plugin_once(tmp_path: Path) -> None:
    file = lock(
        tmp_path,
        "- {name: acme/dna, local: a, state: active}\n"
        "- {name: acme/dna, local: b, state: active}\n",
    )
    problems = load_lock(file).problems
    assert [p.code for p in problems] == ["duplicate-plugin"]
    assert problems[0].location is not None
    assert problems[0].location.line == 5


def test_explains_a_branch_given_as_a_sha(tmp_path: Path) -> None:
    file = lock(tmp_path, "- {name: acme/dna, git: https://x.org/p, sha: main, state: active}")
    (problem,) = load_lock(file).problems
    assert problem.hint is not None
    assert "git ls-remote" in problem.hint


def test_a_sha_yaml_reads_as_a_number(tmp_path: Path) -> None:
    number = "1234567890" * 4
    file = lock(
        tmp_path, f"- {{name: acme/dna, git: https://x.org/p, sha: {number}, state: active}}"
    )
    (problem,) = load_lock(file).problems
    assert problem.hint == "YAML read this SHA as a number: put it in quotes."


def test_not_a_lockfile(tmp_path: Path) -> None:
    file = tmp_path / "plugins.lock.yaml"
    file.write_text("apiVersion: iki/v1\nkind: Plugin\nname: dna\n")
    assert [p.code for p in load_lock(file).problems] == ["not-a-lockfile"]
    assert [p.code for p in load_lock(tmp_path / "nope.yaml").problems] == ["missing-lockfile"]


def test_plugins_are_named_in_a_namespace(tmp_path: Path) -> None:
    (problem,) = load_lock(lock(tmp_path, "- {name: assays, local: a, state: active}\n")).problems
    assert problem.code == "unqualified-name"
    assert problem.message == "`assays` needs its namespace, as its plugin.yaml names it."
    assert problem.hint == "Write `<namespace>/assays`, such as `acme/assays`."
    (problem,) = load_lock(lock(tmp_path, "- {name: dna, local: a, state: active}\n")).problems
    assert problem.hint == "Write `iki/dna`."


@pytest.mark.parametrize(
    "git",
    [
        "https://github.com/yogesh-dhande/iki.git",
        "https://github.com/yogesh-dhande/iki",
        "https://GitHub.com:443/yogesh-dhande/iki.git/",
        "git@github.com:yogesh-dhande/iki.git",
        "ssh://git@github.com/yogesh-dhande/iki.git",
    ],
)
def test_iki_plugins_come_from_iki(tmp_path: Path, git: str) -> None:
    entry = f"- {{name: iki/dna, git: '{git}', path: plugins/dna, sha: {SHA}, state: active}}\n"
    assert codes(lock(tmp_path, entry)) == []


@pytest.mark.parametrize(
    "path",
    [
        "examples/plugins/provider",
        "./plugin-template/",
        "spec/tests/data/signed_report",
        "spec/tests/plugins/dna",
        "server/tests/fixtures/plugins/execution",
        "deploy/dev/fixtures/plugins/worker_execution",
        "deploy/live/plugins/live_check",
        "plugins/../examples/plugins/connector",
    ],
)
def test_official_teaching_and_fixture_pins_need_a_test_build(tmp_path: Path, path: str) -> None:
    mirror = "https://git.lab.example/release/iki.git"
    file = lock(
        tmp_path,
        f"- name: acme/a\n  git: {naming.IKI_REPOSITORY}\n  path: {path}\n"
        f"  sha: {SHA}\n  state: detached\n"
        f"- name: lab/b\n  git: https://GIT.lab.example:443/release/iki/\n  path: {path}\n"
        f"  sha: {SHA}\n  state: active\n",
    )
    problems = load_lock(file, mirrors=[mirror]).problems
    assert [(p.code, p.path) for p in problems] == [
        ("test-only-source", ("plugins", 0, "path")),
        ("test-only-source", ("plugins", 1, "path")),
    ]
    assert all(p.location and "--test-only" in (p.hint or "") for p in problems)
    assert load_lock(file, mirrors=[mirror], test_only=True).ok


@pytest.mark.parametrize(
    ("git", "path"),
    [
        ("https://git.lab.example/iki.git", "examples/plugins/provider"),
        ("https://git.lab.example/custom/iki.git", "plugin-template"),
        (naming.IKI_REPOSITORY, "examples/plugins_extra/provider"),
        (naming.IKI_REPOSITORY, "deploy/live/plugins/checks"),
    ],
)
def test_distribution_rule_matches_both_repository_and_directory(
    tmp_path: Path, git: str, path: str
) -> None:
    file = lock(
        tmp_path, f"- {{name: acme/provider, git: {git}, sha: {SHA}, path: {path}, state: active}}"
    )
    assert load_lock(file, mirrors=["https://git.lab.example/release/iki.git"]).ok


@pytest.mark.parametrize(
    ("mirror", "pin", "test_only"),
    [
        ("ssh://git@git.lab.example:22/Team/Iki.git", "git@GIT.lab.example:/Team/Iki/", True),
        (
            "ssh://git@git.lab.example:2222/Team/Iki",
            "ssh://git@git.lab.example/2222/Team/Iki",
            False,
        ),
        ("https://git.lab.example/Team/Iki", "https://git.lab.example/team/Iki", False),
        ("https://git.lab.example/Team/Iki.git", "https://git.lab.example/Team/Iki.git.git", False),
        ("https://git.lab.example/Team/Iki", "ssh://git@git.lab.example/Team/Iki", False),
        ("git@git.lab.example:Team/Iki", "ssh://git@git.lab.example/~/Team/Iki", True),
        ("git@git.lab.example:Team/Iki", "ssh://git@git.lab.example/Team/Iki", False),
        ("git@git.lab.example:Team/Iki", "lab@git.lab.example:Team/Iki", False),
    ],
)
def test_distribution_uses_the_configured_mirror_identity(
    tmp_path: Path, mirror: str, pin: str, test_only: bool
) -> None:
    file = lock(
        tmp_path,
        f"- {{name: lab/a, git: '{pin}', sha: {SHA}, path: spec/tests/plugins/a, state: active}}",
    )
    problems = load_lock(file, mirrors=[mirror]).problems
    assert [problem.code for problem in problems] == (["test-only-source"] if test_only else [])


@pytest.mark.parametrize(
    "url", ["https://git.lab.example:bad/Team/Iki", "ssh://git.lab.example%3A22/Team/Iki"]
)
def test_fixture_pins_need_a_recognized_source_in_production(tmp_path: Path, url: str) -> None:
    file = lock(
        tmp_path,
        f"- {{name: lab/a, git: '{url}', sha: {SHA}, path: spec/tests/plugins/a, state: active}}",
    )
    assert codes(file) == ["unrecognized-source"]
    assert load_lock(file, test_only=True).ok


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:yogesh-dhande/iki",
        "ssh://git@GITHUB.com:22/yogesh-dhande/iki.git",
        "https://github.com:443/Yogesh-Dhande/Iki.git",
        "https://github.com./yogesh-dhande/%69ki.git",
        "https://%67ithub.com/yogesh-dhande/iki.git",
        "https://github.com/yogesh-dhande/./iki.git",
        "ssh://%67it@github.com/yogesh-dhande/iki.git",
    ],
)
def test_official_repository_spellings(tmp_path: Path, url: str) -> None:
    file = lock(
        tmp_path,
        f"- {{name: acme/a, git: '{url}', sha: {SHA}, path: plugin-template, state: active}}",
    )
    assert codes(file) == ["test-only-source"]


def test_local_pin_follows_its_own_checkout_and_resolves_links(tmp_path: Path) -> None:
    repo = tmp_path / "checkout"
    fixture = repo / "examples" / "plugins" / "provider"
    fixture.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)  # noqa: S603, S607
    (tmp_path / "provider").symlink_to(fixture, target_is_directory=True)
    file = lock(tmp_path, "- {name: acme/provider, local: provider, state: detached}")
    mirror = "https://git.lab.example/iki.git"
    for origin in (
        "https://GIT.lab.example:443/iki.git/",
        "https://git.lab.example/./%69ki.git",
        naming.IKI_REPOSITORY,
    ):
        subprocess.run(  # noqa: S603
            ["git", "-C", str(repo), "config", "remote.origin.url", origin],  # noqa: S607
            check=True,
        )
        result = load_lock(file, mirrors=[mirror])
        assert [(p.code, p.path) for p in result.problems] == [
            ("test-only-source", ("plugins", 0, "local"))
        ]
        assert load_lock(file, test_only=True).ok
    for origin, problems in (
        ("https://git.lab.example/custom", []),
        (str(tmp_path / "lab-fork.git"), []),
        ("https://git.lab.example:bad/iki", ["unrecognized-source"]),
    ):
        subprocess.run(  # noqa: S603
            ["git", "-C", str(repo), "config", "remote.origin.url", origin],  # noqa: S607
            check=True,
        )
        result = load_lock(file, mirrors=[mirror])
        assert [p.code for p in result.problems] == problems


def test_local_pin_in_an_iki_source_archive(tmp_path: Path) -> None:
    source = tmp_path / "source"
    marker = source / "spec/src/iki_spec/lockfile.py"
    marker.parent.mkdir(parents=True)
    marker.touch()
    (source / "plugin-template").mkdir()
    file = lock(tmp_path, "- {name: acme/example, local: source/plugin-template, state: active}")
    assert codes(file) == ["test-only-source"]
    assert load_lock(file, test_only=True).ok


@pytest.mark.parametrize(
    "git",
    [
        "https://github.com/yogesh-dhande/IKI.git",
        "https://github.com:8443/yogesh-dhande/iki.git",
    ],
)
def test_a_first_party_lock_checks_the_repository_path_and_port(tmp_path: Path, git: str) -> None:
    entry = f"- {{name: iki/dna, git: '{git}', path: plugins/dna, sha: {SHA}, state: active}}"
    assert codes(lock(tmp_path, entry)) == ["first-party-source"]


def test_the_iki_namespace_is_reserved(tmp_path: Path) -> None:
    # A plugin Iki doesn't have.
    entry = f"- {{name: iki/assays, git: https://x.org/p, sha: {SHA}, state: active}}\n"
    (problem,) = load_lock(lock(tmp_path, entry)).problems
    assert problem.code == "reserved-namespace"
    assert problem.message == "Iki has no plugin `iki/assays`: the namespace `iki` is Iki's own."
    # One of Iki's, from somewhere else: a fork is another plugin, in its own namespace.
    fork = (
        "- {name: iki/dna, git: https://git.acme.example/lab/iki.git, path: plugins/dna, "
        f"sha: {SHA}, state: active}}\n"
    )
    assert codes(lock(tmp_path, fork)) == ["first-party-source"]
    # Or from another directory of Iki's repository.
    elsewhere = (
        "- {name: iki/dna, git: https://github.com/yogesh-dhande/iki.git, path: plugins/protein, "
        f"sha: {SHA}, state: active}}\n"
    )
    assert codes(lock(tmp_path, elsewhere)) == ["first-party-source"]
    # A local directory is for development, and a purged plugin isn't fetched.
    assert codes(lock(tmp_path, "- {name: iki/dna, local: ../dna, state: active}\n")) == []
    assert codes(lock(tmp_path, "- {name: iki/dna, state: purged}\n")) == []


def test_the_same_name_in_two_namespaces(tmp_path: Path) -> None:
    file = lock(
        tmp_path,
        "- {name: acme/assays, local: a, state: active}\n"
        "- {name: globex/assays, local: b, state: active}\n",
    )
    result = load_lock(file)
    assert result.problems == []
    assert result.lock is not None
    assert [p.name for p in result.lock.plugins] == ["acme/assays", "globex/assays"]


# --- A lockfile holds no credentials (ADR 0034) ------------------------------------------

SECRET = "ghp_EXAMPLE0SECRET"


@pytest.mark.parametrize(
    ("name", "git", "what"),
    [
        ("acme/assays", f"https://bob:{SECRET}@github.com/acme/assays.git", "a password"),
        ("acme/assays", f"https://{SECRET}@github.com/acme/assays.git", "a user or token"),
        ("acme/assays", f"https://x-access-token:{SECRET}@github.com/a/b.git", "a password"),
        ("acme/assays", f"https://github.com/acme/a.git?private_token={SECRET}", "a query"),
        ("acme/assays", f"https://github.com/acme/a.git#{SECRET}", "a query or fragment"),
        ("acme/assays", f"ssh://git:{SECRET}@git.acme.example/assays.git", "a password"),
        ("iki/dna", f"https://user:{SECRET}@[invalid/Org/Repo", "malformed user information"),
        ("iki/dna", f"https://[invalid/Org/Repo?value={SECRET}", "a query or fragment"),
        # Messages for Iki's own plugins and for a missing commit name the URL: none may.
        ("iki/dna", f"https://bob:{SECRET}@git.acme.example/mirrors/iki.git", "a password"),
        ("iki/dna", f"https://{SECRET}@github.com/yogesh-dhande/iki.git", "a user or token"),
    ],
)
def test_a_git_url_with_credentials_is_refused_without_repeating_them(
    tmp_path: Path, name: str, git: str, what: str
) -> None:
    file = lock(
        tmp_path,
        f"""\
        - name: {name}
          git: "{git}"
          path: plugins/dna
          state: active
        """,
    )
    result = load_lock(file)
    assert result.lock is None
    assert [p.code for p in result.problems] == ["git-credentials"]
    (problem,) = result.problems
    assert what in problem.message
    assert "credential helper" in (problem.hint or "")
    assert problem.location is not None and problem.location.line == 5
    assert SECRET not in str(problem.as_dict())


@pytest.mark.parametrize(
    "git",
    [
        "ssh://git@git.acme.example:2222/assays.git",
        "git@github.com:acme/assays.git",
        "https://github.com/acme/assays.git",
    ],
)
def test_an_ssh_user_isnt_a_credential(tmp_path: Path, git: str) -> None:
    file = lock(tmp_path, f"- name: acme/assays\n  git: {git}\n  sha: {SHA}\n  state: active\n")
    assert codes(file) == []


def test_urls_are_shown_without_credentials() -> None:
    assert without_credentials(f"https://[invalid/Org/Repo?value={SECRET}") == "[invalid Git URL]"
    assert SECRET not in without_credentials(f"https://user:{SECRET}@host:bad/Org/Repo")
    assert (
        without_credentials(f"https://bob:{SECRET}@GitHub.com:8443/a/b.git?t={SECRET}")
        == "https://github.com:8443/a/b.git"
    )
    assert without_credentials(f"ssh://git:{SECRET}@host/a.git") == "ssh://git@host/a.git"
    assert without_credentials("https://GitHub.com/a/b.git") == "https://GitHub.com/a/b.git"
    assert without_credentials("git@github.com:a/b.git") == "git@github.com:a/b.git"
    message = (
        f"unable to access 'https://bob:{SECRET}@github.com/a/b.git/': 403, "
        f"and ssh://git:{SECRET}@host/x, and https://{SECRET}@host/y"
    )
    assert redact_credentials(message) == (
        "unable to access 'https://github.com/a/b.git/': 403, "
        "and ssh://git@host/x, and https://host/y"
    )
