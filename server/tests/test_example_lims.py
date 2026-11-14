"""The example LIMS plugin: its manifests, and its scripts run against a fake LIMS, as a
worker runs them (an input.json in, an output.json out)."""

import importlib
import io
import json
import sys
import urllib.error
import urllib.parse
from collections.abc import Iterator
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from iki_spec import schemas
from iki_spec.validate import load_plugin

PLUGINS = Path(__file__).resolve().parents[2] / "plugins"
PLUGIN = PLUGINS / "example_lims"
SCRIPTS = PLUGIN / "scripts"
HOST = "lims.acme.example"
TOKEN = "lims-test-token"


def test_the_plugin_is_valid() -> None:
    result = load_plugin(PLUGIN, dependencies=[PLUGINS / "protein", PLUGINS / "dna"])
    assert result.problems == []
    assert result.plugin is not None
    actions = result.plugin.actions
    assert actions["import_from_lims"].network == ("{settings.lims_host}",)
    assert actions["import_from_lims"].creates[0].match_on == "lims_id"
    assert result.plugin.triggers["import_nightly"].on == "schedule"


class FakeLims:
    """The LIMS's API: proteins, a page at a time, and changes to them."""

    def __init__(self) -> None:
        self.proteins = [
            {
                "id": "P-1",
                "name": "ABL1",
                "sequence": "mkvl eic",
                "modified_at": "2026-11-01T09:00:00Z",
            },
            {"id": "P-2", "name": "", "sequence": "MKKK", "modified_at": "2026-11-12T15:30:00Z"},
            {
                "id": "P-3",
                "name": "SRC",
                "sequence": "MGSNK",
                "modified_at": "2026-11-13T08:00:00Z",
            },
        ]
        self.page = 2
        self.asked: list[tuple[str, str, dict[str, list[str]], Any]] = []
        self.failures: list[int] = []

    def __call__(self, request: Any, timeout: float) -> Any:
        url = urllib.parse.urlsplit(request.full_url)
        assert (url.scheme, url.netloc) == ("https", HOST)
        assert request.get_header("Authorization") == f"Bearer {TOKEN}"
        query = urllib.parse.parse_qs(url.query)
        body = json.loads(request.data) if request.data else None
        self.asked.append((request.get_method(), url.path, query, body))
        if self.failures:
            code = self.failures.pop(0)
            raise urllib.error.HTTPError(request.full_url, code, "No", Message(), None)
        if request.get_method() == "PATCH":
            return io.BytesIO(b"{}")
        since = query.get("modified_since", [""])[0]
        start = int(query.get("cursor", ["0"])[0])
        changed = [p for p in self.proteins if p["modified_at"] > since]
        items = changed[start : start + self.page]
        more = start + self.page < len(changed)
        answer = {"items": items, "next_cursor": str(start + self.page) if more else None}
        return io.BytesIO(json.dumps(answer).encode())


@pytest.fixture
def lims(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Any]:
    """The plugin's scripts, loaded here with the LIMS faked."""
    import time
    import urllib.request

    monkeypatch.syspath_prepend(str(SCRIPTS))
    names = ("lims", "sync", "send")
    for name in names:
        sys.modules.pop(name, None)
    fake = FakeLims()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    monkeypatch.setenv("IKI_INPUT", str(tmp_path / "input.json"))
    monkeypatch.setenv("IKI_OUTPUT", str(tmp_path / "out.json"))
    (tmp_path / "out").mkdir()
    monkeypatch.setenv("IKI_OUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("LIMS_TOKEN", TOKEN)
    yield SimpleNamespace(
        sync=importlib.import_module("sync"),
        send=importlib.import_module("send"),
        fake=fake,
        slept=slept,
        dir=tmp_path,
    )
    for name in names:
        sys.modules.pop(name, None)


def ran(lims: Any, script: Any, run_input: dict[str, Any]) -> dict[str, Any]:
    run_input = {"settings": {"lims_host": HOST}, **run_input}
    (lims.dir / "input.json").write_text(json.dumps(run_input))
    script.main()
    output: dict[str, Any] = json.loads((lims.dir / "out.json").read_text())
    assert not list(schemas.check("run-output", output))
    return output


def test_imports_every_record_a_page_at_a_time(lims: Any) -> None:
    output = ran(lims, lims.sync, {"run": {"hrid": "RUN-000001"}})
    assert [(m, p, q) for m, p, q, _ in lims.fake.asked] == [
        ("GET", "/api/v1/proteins", {}),
        ("GET", "/api/v1/proteins", {"cursor": ["2"]}),
    ]
    first, second, third = output["creates"]
    assert first["type"] == "protein.Protein"
    assert first["values"] == {"lims_id": "P-1", "sequence": "MKVLEIC", "name": "ABL1"}
    assert second["values"] == {"lims_id": "P-2", "sequence": "MKKK"}
    assert third["values"]["lims_id"] == "P-3"
    origin = first["origin"]
    assert {k: origin[k] for k in ("source", "id", "url", "version")} == {
        "source": "LIMS",
        "id": "P-1",
        "url": f"https://{HOST}/proteins/P-1",
        "version": "modified 2026-11-01T09:00:00Z",
    }
    assert output["metrics"] == {"records": 3}


def test_a_scheduled_run_asks_for_what_changed_since_the_last(lims: Any) -> None:
    trigger = {
        "name": "example_lims.import_nightly",
        "scheduled_for": "2026-11-14T02:00:00Z",
        "since": "2026-11-12T02:00:00Z",
    }
    output = ran(lims, lims.sync, {"run": {"hrid": "RUN-000002", "trigger": trigger}})
    assert [q for _, _, q, _ in lims.fake.asked] == [{"modified_since": ["2026-11-12T02:00:00Z"]}]
    assert [c["values"]["lims_id"] for c in output["creates"]] == ["P-2", "P-3"]


def test_many_records_go_to_a_creates_file(lims: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lims.sync, "INLINE", 2)
    output = ran(lims, lims.sync, {"run": {}})
    assert output["creates_file"] == {"$file": "creates.jsonl"}
    lines = (lims.dir / "out" / "creates.jsonl").read_text().splitlines()
    assert [json.loads(line)["values"]["lims_id"] for line in lines] == ["P-1", "P-2", "P-3"]


def test_waits_and_asks_again_when_the_lims_is_busy(lims: Any) -> None:
    lims.fake.failures = [429, 503]
    output = ran(lims, lims.sync, {"run": {}})
    assert lims.slept == [2, 4]
    assert output["metrics"] == {"records": 3}


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"failures": [401]}, "::error The LIMS refused the token (401): check `LIMS_TOKEN`."),
        ({"failures": [404]}, "::error The LIMS answered 404 No."),
        ({"bad": "M1K"}, "::error P-1's sequence isn't one-letter amino acid codes."),
    ],
)
def test_what_the_lims_refuses_fails_the_run(
    lims: Any, capsys: pytest.CaptureFixture[str], change: dict[str, Any], error: str
) -> None:
    lims.fake.failures = change.get("failures", [])
    if "bad" in change:
        lims.fake.proteins[0]["sequence"] = change["bad"]
    (lims.dir / "input.json").write_text(json.dumps({"settings": {"lims_host": HOST}}))
    with pytest.raises(SystemExit):
        lims.sync.main()
    assert error in capsys.readouterr().out.splitlines()


def test_without_a_token_or_a_host_it_says_which(
    lims: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LIMS_TOKEN")
    (lims.dir / "input.json").write_text(json.dumps({"settings": {"lims_host": HOST}}))
    with pytest.raises(SystemExit):
        lims.sync.main()
    assert "::error The deployment hasn't set the `LIMS_TOKEN` secret." in capsys.readouterr().out
    (lims.dir / "input.json").write_text(json.dumps({"settings": {}}))
    with pytest.raises(SystemExit):
        lims.send.main()
    assert "::error Set the plugin's `lims_host`" in capsys.readouterr().out


def protein(i: int, **values: Any) -> dict[str, Any]:
    return {"id": f"00000000-0000-0000-0000-{i:012d}", "hrid": f"PRT-{i:06d}", "values": values}


def test_sends_each_proteins_iki_id_and_name_to_its_record(lims: Any) -> None:
    proteins = [protein(1, lims_id="P-1", name="ABL1 kinase"), protein(2, name="Not in the LIMS")]
    output = ran(lims, lims.send, {"inputs": {"proteins": proteins}})
    assert lims.fake.asked == [
        ("PATCH", "/api/v1/proteins/P-1", {}, {"iki_id": "PRT-000001", "name": "ABL1 kinase"})
    ]
    assert output["metrics"] == {"records": 1}
    assert output["messages"] == [
        {"level": "warning", "text": "PRT-000002 has no LIMS record: skipped."}
    ]
    sent = json.loads((lims.dir / "out" / "sent.json").read_text())
    assert sent == [{"lims_id": "P-1", "fields": {"iki_id": "PRT-000001", "name": "ABL1 kinase"}}]


def test_ids_are_quoted_in_paths(lims: Any) -> None:
    ran(lims, lims.send, {"inputs": {"proteins": [protein(1, lims_id="A/../B", name="X")]}})
    assert lims.fake.asked[0][1] == "/api/v1/proteins/A%2F..%2FB"
