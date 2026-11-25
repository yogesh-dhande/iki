"""Repository identities used by first-party lockfiles, fetches and mirror admission."""

import pytest

from iki_spec.lockfile import Repository, repository


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        (
            "https://GIT.example:8443/Team/Repo.git/",
            Repository("https", "git.example", 8443, "/Team/Repo", None),
        ),
        (
            "https://git.example/8443/Team/Repo",
            Repository("https", "git.example", 443, "/8443/Team/Repo", None),
        ),
        (
            "ssh://build@[2001:db8::1]:2222/Team/Repo",
            Repository("ssh", "2001:db8::1", 2222, "/Team/Repo", "build"),
        ),
        ("git@git.example:Team/Repo.git", Repository("ssh", "git.example", 22, "Team/Repo", "git")),
        ("file:///Build/Repo.git", Repository("file", "", None, "/Build/Repo", None)),
    ],
)
def test_repository_preserves_the_endpoint_and_path(address: str, expected: Repository) -> None:
    assert repository(address) == expected


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("https://GIT.example:443/Team/Repo.git/", "https://git.example/Team/Repo"),
        ("https://build@git.example/Team/Repo", "https://git.example/Team/Repo"),
        ("git@GIT.example:/Team/Repo", "ssh://git@git.example:22/Team/Repo.git"),
        ("ssh://git@[2001:db8::1]/Team/Repo", "git@[2001:db8::1]:/Team/Repo"),
    ],
)
def test_equivalent_remote_spellings(left: str, right: str) -> None:
    assert repository(left) is not None and repository(left) == repository(right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("https://git.example/Team/Repo", "https://git.example/team/Repo"),
        ("https://git.example/Team/Repo.GIT", "https://git.example/Team/Repo.git"),
        ("ssh://git.example/Team/Repo", "https://git.example/Team/Repo"),
        ("ssh://git.example:2222/Team/Repo", "ssh://git.example/2222/Team/Repo"),
        ("https://git.example/Team//Repo", "https://git.example/Team/Repo"),
        ("https://git.example/Team/./Repo", "https://git.example/Team/Repo"),
        ("git@git.example:Team/Repo", "ssh://git@git.example/Team/Repo"),
        ("git@git.example:/Team/A%2FB", "ssh://git@git.example/Team/A%2FB"),
        ("alice@git.example:Team/Repo", "bob@git.example:Team/Repo"),
    ],
)
def test_distinct_remote_identities(left: str, right: str) -> None:
    assert repository(left) is not None and repository(right) is not None
    assert repository(left) != repository(right)


@pytest.mark.parametrize(
    "address",
    [
        "https://git.example:abc/repo",
        "ssh://git.example:65536/repo",
        "https://git.example/repo?other",
        "https:///repo",
        "git@[not-an-ip]:/Team/Repo",
    ],
)
def test_invalid_addresses_have_no_identity(address: str) -> None:
    assert repository(address) is None
