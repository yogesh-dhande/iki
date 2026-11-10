import shutil
from pathlib import Path

import pytest

from iki_cli.main import main

PLUGINS = Path(__file__).parents[2] / "spec" / "tests" / "plugins"


@pytest.fixture(autouse=True)
def _plain_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")


def test_builds_every_plugin_given(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "out"
    names = ["structure_prediction", "protein", "dna"]
    code = main(["plugin", "build", *(str(PLUGINS / n) for n in names), "--output", str(out)])
    assert code == 0
    assert capsys.readouterr().out == "".join(
        f"Built {out / f'iki_plugin_{n}'}\n" for n in ("dna", "protein", "structure_prediction")
    )
    assert (out / "iki_installed_plugins" / "__init__.py").exists()


def test_missing_dependencies_fail(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["plugin", "build", str(PLUGINS / "protein"), "--output", str(tmp_path)])
    assert code == 1
    assert capsys.readouterr().err == (
        "error: protein needs the dna plugin: give its directory too.\n"
    )
    assert not (tmp_path / "iki_plugin_protein").exists()


def test_invalid_plugins_fail(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plugin = tmp_path / "dna"
    shutil.copytree(PLUGINS / "dna", plugin)
    (plugin / "plugin.yaml").write_text("apiVersion: iki/v1\nkind: Plugin\nname: Dna\n")
    code = main(["plugin", "build", str(plugin), "--output", str(tmp_path / "out")])
    assert code == 1
    assert "error" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()
