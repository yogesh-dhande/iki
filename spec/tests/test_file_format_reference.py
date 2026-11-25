"""The published format reference reflects every shipped declaration and action."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_file_format_reference_is_current() -> None:
    result = subprocess.run(  # noqa: S603 - this interpreter and the checked-in generator
        [sys.executable, str(ROOT / "scripts/build_format_reference.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
