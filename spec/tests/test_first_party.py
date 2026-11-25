"""The trees of Iki's own plugins, which a mirror's copies are checked against (ADR 0114)."""

import subprocess
import sys
from pathlib import Path

import pytest

from iki_spec import first_party, naming

ROOT = Path(__file__).parents[2]


@pytest.mark.skipif(not (ROOT / ".git").exists(), reason="needs Iki's git repository")
def test_every_plugins_current_tree_is_listed() -> None:
    result = subprocess.run(  # noqa: S603 - this interpreter, Iki's own script
        [sys.executable, str(ROOT / "scripts" / "first_party_trees.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_each_fetched_plugin_has_trees_and_the_core_plugin_none() -> None:
    for name in naming.FIRST_PARTY - {naming.CORE}:
        assert first_party.trees(name), name
        assert all(len(t) == 40 and int(t, 16) >= 0 for t in first_party.trees(name))
    # It ships with the server; nothing fetches it.
    assert first_party.trees(naming.CORE) == frozenset()
    assert first_party.trees("acme/dna") == frozenset()


def test_only_the_active_first_party_plugins_are_in_the_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(naming, "FIRST_PARTY", frozenset({naming.CORE, "iki/protein"}))
    directory = tmp_path / "protein"
    directory.mkdir()
    manifest = directory / "plugin.yaml"
    with pytest.raises(ValueError, match="Missing first-party manifest"):
        first_party.catalog(tmp_path)
    manifest.write_text("name: acme/protein\n")
    with pytest.raises(ValueError, match="must declare the first-party plugin iki/protein"):
        first_party.catalog(tmp_path)
    manifest.write_text("name: iki/protein\n")
    assert first_party.catalog(tmp_path) == {"iki/protein": directory}

    extra = directory / "fixtures"
    extra.mkdir()
    (extra / "plugin.yaml").write_text("name: acme/fixture\n")
    with pytest.raises(ValueError, match="Unexpected plugin manifest"):
        first_party.catalog(tmp_path)
