import textwrap
from pathlib import Path

import pytest

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
        "https://GitHub.com/yogesh-dhande/iki/",
        "git@github.com:yogesh-dhande/iki.git",
        "ssh://git@github.com/yogesh-dhande/iki.git",
    ],
)
def test_iki_plugins_come_from_iki(tmp_path: Path, git: str) -> None:
    entry = f"- {{name: iki/dna, git: '{git}', path: plugins/dna, sha: {SHA}, state: active}}\n"
    assert codes(lock(tmp_path, entry)) == []


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
