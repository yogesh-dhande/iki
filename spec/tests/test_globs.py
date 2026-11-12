import pytest

from iki_spec.globs import matches, valid_path


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("ranking_debug.json", "ranking_debug.json", True),
        ("ranking_debug.json", "x/ranking_debug.json", False),
        ("*.a3m", "msa.a3m", True),
        ("*.a3m", "msas/msa.a3m", False),
        ("msas/**", "msas/a.a3m", True),
        ("msas/**", "msas/deep/b.a3m", True),
        ("msas/**", "msas", True),
        ("msas/**", "other/a.a3m", False),
        ("**/*.log", "run.log", True),
        ("**/*.log", "a/b/run.log", True),
        ("model_?.pdb", "model_1.pdb", True),
        ("model_?.pdb", "model_10.pdb", False),
        ("model_[0-4].pdb", "model_3.pdb", True),
        ("model_[!0-4].pdb", "model_3.pdb", False),
        ("alignment.*", "alignment.txt", True),
        ("a*b*c", "aXbYbc", True),
        ("a*b*c", "aXbY", False),
        ("[ab", "[ab", True),
    ],
)
def test_globs_match_a_segment_at_a_time(pattern: str, path: str, expected: bool) -> None:
    assert matches(pattern, path) is expected


@pytest.mark.parametrize(
    "path", ["", "/abs", "a//b", "./a", "a/../b", "..", "a\\b", "a\nb", "x" * 501]
)
def test_paths_outside_or_odd_arent_valid(path: str) -> None:
    assert not valid_path(path)


def test_ordinary_paths_are_valid() -> None:
    assert valid_path("msas/uniref90 hits.a3m") and valid_path("é.txt")
