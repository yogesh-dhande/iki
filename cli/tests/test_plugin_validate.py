import json
import shutil
from pathlib import Path

import pytest

from iki_cli.main import main

PLUGINS = Path(__file__).parents[2] / "spec" / "tests" / "plugins"


@pytest.fixture(autouse=True)
def _plain_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")


@pytest.fixture
def broken(tmp_path: Path) -> Path:
    plugin = tmp_path / "protein"
    shutil.copytree(PLUGINS / "protein", plugin)
    types = plugin / "types" / "protein.yaml"
    types.write_text(types.read_text().replace("unit: u,", "unit: Da,"))
    return plugin


def test_valid_plugins_pass(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["plugin", "validate", str(PLUGINS / "protein"), "--with", str(PLUGINS / "dna")])
    assert code == 0
    assert capsys.readouterr().out == (
        "✓ protein 3.0.0 is valid: 2 entity types, 0 extensions, 0 settings, 5 actions, "
        "1 environment, 5 workflows.\n"
    )


def test_validates_the_current_directory_by_default(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(PLUGINS / "dna")
    assert main(["plugin", "validate"]) == 0
    assert (
        "✓ dna 1.2.0 is valid: 2 entity types, 0 extensions, 2 settings, 0 actions, "
        "0 environments, 0 workflows." in capsys.readouterr().out
    )


def test_problems_show_where_and_how_to_fix_them(
    broken: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(broken.parent)
    code = main(["plugin", "validate", "protein", "--with", str(PLUGINS / "dna")])
    assert code == 1
    out = capsys.readouterr().out
    assert out.splitlines() == [
        "protein/types/protein.yaml:12:45: error: `Da` isn't a UCUM unit. [unknown-unit]",
        " 12 |   molecular_weight: { type: quantity, unit: Da, display_unit: ku, computed: true,"
        " indexed: true }",
        "    |                                             ^",
        "  hint: `u` is the unified atomic mass unit (the dalton); `g/mol` is molar mass.",
        "",
        "1 error in protein.",
    ]


def test_each_problem_names_its_own_file(
    broken: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for script in ("fetch.py", "imports.py"):
        (broken / "scripts" / script).unlink()
    monkeypatch.chdir(broken.parent)
    assert main(["plugin", "validate", "protein", "--with", str(PLUGINS / "dna")]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert sorted(line.split(":")[0] for line in lines if ": error: " in line) == [
        "protein/actions/fetch_proteins.yaml",
        "protein/actions/import_proteins.yaml",
        "protein/types/protein.yaml",
    ]


def test_warnings_pass_unless_strict(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plugin = tmp_path / "acme"
    plugin.mkdir()
    (plugin / "plugin.yaml").write_text(
        "apiVersion: iki/v1\nkind: Plugin\nname: acme\nversion: 1.0.0\n"
        "settings:\n  api_key: { type: string }\n"
    )
    assert main(["plugin", "validate", str(plugin)]) == 0
    out = capsys.readouterr().out
    assert "warning: `api_key` looks like a secret." in out
    assert out.endswith(
        "✓ acme 1.0.0 is valid: 0 entity types, 0 extensions, 1 setting, 0 actions, "
        "0 environments, 0 workflows, with 1 warning.\n"
    )
    assert main(["plugin", "validate", str(plugin), "--strict"]) == 1


def test_checks_settings_values(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    values = tmp_path / "dna.yaml"
    values.write_text("ncbi_email: lab@example.com\n")
    assert main(["plugin", "validate", str(PLUGINS / "dna"), "--settings", str(values)]) == 0
    values.write_text("ncbi_emial: lab@example.com\n")
    assert main(["plugin", "validate", str(PLUGINS / "dna"), "--settings", str(values)]) == 1
    out = capsys.readouterr().out
    assert "error: The dna plugin has no setting `ncbi_emial`. [unknown-setting]" in out
    assert "hint: Did you mean `ncbi_email`?" in out
    assert "error: The dna plugin needs `ncbi_email` to be set. [missing-setting]" in out


def test_json_output(broken: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        ["plugin", "validate", str(broken), "--with", str(PLUGINS / "dna"), "--format", "json"]
    )
    assert code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["valid"] is False
    assert report["plugin"] is None
    assert report["errors"] == 1
    assert report["problems"] == [
        {
            "code": "unknown-unit",
            "severity": "error",
            "message": "`Da` isn't a UCUM unit.",
            "hint": "`u` is the unified atomic mass unit (the dalton); `g/mol` is molar mass.",
            "file": "types/protein.yaml",
            "line": 12,
            "column": 45,
            "path": ["fields", "molecular_weight", "unit"],
        }
    ]
    assert main(["plugin", "validate", str(PLUGINS / "dna"), "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["plugin"] == {
        "name": "dna",
        "version": "1.2.0",
        "types": ["dna.DNASequence", "dna.Sequence"],
        "extensions": [],
        "settings": ["max_sequence_length", "ncbi_email"],
        "actions": [],
        "environments": [],
        "workflows": [],
    }


def test_github_annotations(
    broken: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(broken.parent)
    main(["plugin", "validate", "protein", "--with", str(PLUGINS / "dna"), "--format", "github"])
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "::error file=protein/types/protein.yaml,line=12,col=45,title=unknown-unit::"
        "`Da` isn't a UCUM unit.%0A`u` is the unified atomic mass unit (the dalton); "
        "`g/mol` is molar mass.",
        "1 error in protein.",
    ]


def test_problems_without_a_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["plugin", "validate", str(tmp_path / "nope"), "--format", "github"]) == 1
    assert capsys.readouterr().out.startswith("::error file=")
    assert main(["plugin", "validate", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "error: There's no plugin.yaml here, so this isn't a plugin." in out


def test_colors_when_asked(
    broken: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("FORCE_COLOR", "1")
    main(["plugin", "validate", str(broken), "--with", str(PLUGINS / "dna")])
    assert "\033[1;31merror\033[0m" in capsys.readouterr().out
    main(["plugin", "validate", str(PLUGINS / "dna")])
    assert "\033[1;32m✓\033[0m" in capsys.readouterr().out


def test_plugin_alone_prints_its_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["plugin"]) == 0
    assert "validate" in capsys.readouterr().out
