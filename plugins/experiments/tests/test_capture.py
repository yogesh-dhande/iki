"""An experiment's capture pins the exact registered and unregistered result revisions read."""

import importlib.util
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "capture.py"


@pytest.fixture
def capture() -> Any:
    spec = importlib.util.spec_from_file_location("experiment_capture", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.capture


def run(*results: dict[str, Any], **values: Any) -> dict[str, Any]:
    return {
        "settings": {"review_flow": "research"},
        "run": {"id": "capture-run"},
        "inputs": {
            "experiment": [{"id": "experiment-id", "values": values}],
            "results": list(results),
        },
    }


def test_pins_project_and_registry_independently(capture: Any) -> None:
    registered = {"id": "first", "revision": 3, "entry_id": "shared", "entry_revision": 11}
    plain = {"id": "second", "revision": 8}
    doc = run(registered, plain, result_pins=[{"result": "an old selection"}])
    [update] = capture(doc)["updates"]["experiment"]
    assert update == {
        "entity": "experiment-id",
        "require_unchanged": True,
        "values": {
            "review_flow": "research",
            "capture_id": "capture-run",
            "result_pins": [
                {
                    "result": "first",
                    "record_id": "first",
                    "record_revision": 3,
                    "entry_id": "shared",
                    "entry_revision": 11,
                },
                {"result": "second", "record_id": "second", "record_revision": 8},
            ],
        },
    }


@pytest.mark.parametrize("incomplete", [{"entry_id": "shared"}, {"entry_revision": 11}])
def test_requires_both_parts_of_a_registry_pin(capture: Any, incomplete: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="complete registry pin"):
        capture(run({"id": "a", "revision": 3, **incomplete}))


def test_refuses_self_reference(capture: Any) -> None:
    with pytest.raises(ValueError, match="its own result"):
        capture(run({"id": "experiment-id", "revision": 4}))


def test_direct_attachment_clears_an_old_selection(capture: Any) -> None:
    doc = run(attachments=["file-id"], result_pins=[{"result": "old"}])
    assert capture(doc)["updates"]["experiment"][0]["values"]["result_pins"] == []
