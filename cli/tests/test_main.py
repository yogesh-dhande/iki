import pytest

from iki_cli import __version__
from iki_cli.main import main


def test_version_flag_prints_version_and_exits_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out == f"iki {__version__}\n"


def test_no_arguments_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "usage: iki" in capsys.readouterr().out


def test_unknown_argument_is_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--nope"])
    assert excinfo.value.code == 2
    assert "unrecognized arguments: --nope" in capsys.readouterr().err
