"""The example gate discovers a new lab plugin and runs the tests that travel with it."""

import sys
from pathlib import Path

import check_plugin_examples
import pytest


def test_example_uses_its_transitive_dependencies_and_runs_its_python_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    example = tmp_path / "reports"
    tests = example / "tests"
    tests.mkdir(parents=True)
    (example / "README.md").write_text("# Reports\n")
    (example / "plugin.yaml").write_text(
        "apiVersion: iki/v1\nkind: Plugin\nname: lab/reports\nversion: 1.0.0\niki: '>=0.1'\n"
        'dependencies: {iki/protein: {types: {Protein: ">=1"}}}\n'
    )
    # The Python-only example must execute its portable test after validating with
    # both Protein and Protein's DNA dependency, even though it has no ActionTest.
    (tests / "test_report.py").write_text(
        "from pathlib import Path\ndef test_report():\n"
        "    Path(__file__).with_suffix('.ran').touch()\n"
    )
    monkeypatch.setattr(check_plugin_examples, "EXAMPLES", tmp_path)
    monkeypatch.setattr(sys, "argv", ["check_plugin_examples.py"])
    assert check_plugin_examples.main() == 0
    assert (tests / "test_report.ran").exists()

    (tests / "test_report.py").write_text(
        "import pytest\ndef test_report():\n    pytest.skip('no reports')\n"
    )
    assert check_plugin_examples.main() == 1

    (tests / "test_report.py").unlink()
    assert check_plugin_examples.main() == 1
