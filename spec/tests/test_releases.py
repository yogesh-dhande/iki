import pytest

import iki_spec
from iki_spec import releases


def test_a_release_is_its_numbers() -> None:
    assert releases.release("0.2") == (0, 2, 0)
    assert releases.release("1") == (1, 0, 0)
    assert releases.release("0.2.0.dev3") == (0, 2, 0)
    assert releases.release("1.4.2+local") == (1, 4, 2)
    with pytest.raises(ValueError, match="isn't a version"):
        releases.release("next")


def test_a_build_counts_as_its_release(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(iki_spec, "__version__", "0.2.0.dev3")
    assert releases.iki() == "0.2.0"
    assert releases.fits(">=0.2")  # PEP 440 would put 0.2.0.dev3 before 0.2
    assert not releases.fits("<0.2")


@pytest.mark.parametrize(
    ("requirement", "version", "fits"),
    [
        (">=0.1,<0.3", "0.2.5", True),
        (">=0.1,<0.3", "0.3.0", False),
        (" >= 0.1 , < 0.3 ", "0.1.0", True),
        ("==0.2", "0.2.0", True),
        ("==0.2", "0.2.1", False),
        ("!=0.2.1", "0.2.1", False),
        (">1", "1.0.1", True),
        ("<=1.2", "1.2.0", True),
        ("<=1.2", "1.2.1", False),
        (">0.9", "0.10.0", True),  # numbers, not text
    ],
)
def test_requirements(requirement: str, version: str, fits: bool) -> None:
    assert releases.fits(requirement, version) is fits


def test_a_requirement_is_comparisons() -> None:
    with pytest.raises(ValueError, match="isn't a list of comparisons"):
        releases.fits("~0.2", "0.2.0")
