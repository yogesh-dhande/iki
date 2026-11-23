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

from iki_spec import rules, schemas
from iki_spec.model import EntityType
from iki_spec.validate import load_plugin

PLUGINS = Path(__file__).resolve().parents[2] / "plugins"
PLUGIN = PLUGINS / "example_lims"
#: What it depends on, directly or through iki/lots.
DEPENDENCIES = [PLUGINS / p for p in ("protein", "dna", "lots", "oligo", "plasmid")]
SCRIPTS = PLUGIN / "scripts"
HOST = "lims.acme.example"
TOKEN = "lims-test-token"


def test_the_plugin_is_valid() -> None:
    result = load_plugin(PLUGIN, dependencies=DEPENDENCIES)
    assert result.problems == []
    assert result.plugin is not None
    actions = result.plugin.actions
    assert actions["import_from_lims"].network == ("{settings.lims_host}",)
    assert actions["import_from_lims"].creates[0].match_on == "lims_id"
    assert result.plugin.triggers["import_nightly"].on == "schedule"
    # Its calendar shows bookings in each of a component's places, reading four fields.
    calendar = result.plugin.components["booking_calendar"]
    assert calendar.entities is not None
    assert calendar.entities.type == "iki/example_lims.Booking"
    assert calendar.entities.fields == ("purpose", "instrument", "starts_at", "ends_at")
    assert [(s.slot, s.type) for s in calendar.slots] == [
        ("entity.tab", "iki/example_lims.Instrument"),
        ("search.results", "iki/example_lims.Booking"),
        ("project.page", "iki/example_lims.Booking"),
        ("person.home", "iki/example_lims.Booking"),
    ]
    assert calendar.selects == ""  # It opens bookings; it picks none.
    # A sample's analyst, reviewer and QA each sign for their decision (ADR 0126).
    sample = result.plugin.types["iki/example_lims.Sample"]
    status = sample.fields["status"].states
    signed = {n: [(s.meaning, s.label) for s in st.signatures] for n, st in status.items()}
    assert signed == {
        "registered": [],
        "received": [],
        "in_testing": [],
        "in_review": [("authorship", "Authorship")],
        "approved": [("review", "Review")],
        "released": [("approval", "QA release")],
        "rejected": [("responsibility", "QA rejection")],
    }
    # A sample's volume is drawn by protein mass too, and split into aliquots (ADR 0139).
    amount = sample.amount
    assert amount is not None
    assert (amount.unit, amount.concentration) == ("uL", "concentration")
    assert (amount.aliquots, amount.aliquots_of) == ("iki/example_lims.Aliquot", "sample")
    assert [n for n, st in status.items() if st.holds_amount] == ["registered", "rejected"]
    # A donor's withdrawn consent holds everything derived from them (ADR 0123).
    hold = result.plugin.triggers["hold_withdrawn_consent"]
    assert (hold.on, hold.state_field, hold.states, hold.input) == (
        "entity.state_changed",
        "consent",
        ("withdrawn",),
        "donor",
    )
    assert {name: r.direction for name, r in hold.related.items()} == {
        "aliquots": "downstream",
        "results": "downstream",
    }
    lineage = {
        name.rsplit(".", 1)[1]: [to for f in t.fields.values() if f.type == "ref" for to in f.to]
        for name, t in result.plugin.types.items()
        if name.rsplit(".", 1)[1] in ("Sample", "Aliquot", "Result")
    }
    assert lineage == {
        "Sample": ["iki/protein.Protein", "iki/example_lims.Donor"],
        "Aliquot": ["iki/example_lims.Sample"],
        "Result": ["iki/example_lims.Aliquot"],
    }
    # Its notifications (ADR 0129): QA hears of samples in review, the analyst of one
    # rejected, and whoever a booking is for of its instrument going out of service.
    types = result.plugin.types
    assert types["iki/example_lims.Sample"].hrid_prefix == "SAM"
    review, rejected = types["iki/example_lims.Sample"].notifications.values()
    assert (review.field, review.to_values, review.tell_roles) == (
        "status",
        ("in_review",),
        ("iki/example_lims.qa",),
    )
    assert (rejected.to_values, rejected.tell_fields, rejected.required) == (
        ("rejected",),
        ("analyst",),
        True,
    )
    (down,) = types["iki/example_lims.Booking"].notifications.values()
    assert (down.ref, down.target, down.to_values) == ("instrument", "service", ("out_of_service",))
    assert down.tell_fields == ("booked_by",)
    assert down.where == {"field": "ends_at", "op": "gte", "value": "{{ now }}"}
    # Where its material is kept (ADR 0180): freezer, shelf, rack, box, tube.
    lims = "iki/example_lims."
    holds = {
        name.removeprefix(lims): t.storage.holds
        for name, t in types.items()
        if t.storage is not None and t.storage.holds
    }
    assert holds == {
        "Freezer": (f"{lims}Shelf",),
        "Shelf": (f"{lims}Rack", f"{lims}Box"),
        "Rack": (f"{lims}Box",),
        "Box": (f"{lims}Tube",),
    }
    box, rack, tube = (types[f"{lims}{n}"] for n in ("Box", "Rack", "Tube"))
    assert box.storage is not None and box.storage.grid is not None
    assert (box.storage.grid.rows, box.storage.grid.columns) == ("rows", "columns")
    assert rack.storage is not None and rack.storage.grid is not None
    assert (rack.storage.grid.rows, rack.storage.grid.labels) == (5, "number")
    assert tube.storage is not None and tube.amount is not None
    assert (tube.storage.barcode, tube.storage.confirm) == ("barcode", ("draw",))
    assert tube.amount.follows == ("lot", "sample")
    asked = types[f"{lims}AnalysisRequest"].request
    assert asked is not None
    assert (asked.receives, asked.receives_by) == ("tubes", (f"{lims}coordinator",))


@pytest.fixture(scope="module")
def analysis_result() -> EntityType:
    loaded = load_plugin(PLUGIN, dependencies=DEPENDENCIES)
    assert loaded.problems == []
    assert loaded.plugin is not None
    return loaded.plugin.types["iki/example_lims.AnalysisResult"]


def allows(
    result_type: EntityType,
    current: str,
    target: str,
    person: str,
    values: dict[str, Any],
    *,
    reviewed_from: str = "in_review",
    value_editor: str | None = None,
) -> bool:
    """Evaluate the example's guards with the people who signed the latest transitions."""
    review = result_type.fields["review"]
    guards = review.states[target].guards.values()
    record = rules.record_schema(result_type.label, result_type.fields, None)
    variables = rules.guard_variables(record, {"review": review}, [], review)
    history = [{"from": "", "to": "draft", "by": "creator"}]
    if current != "draft" and reviewed_from == "in_review":
        history.append({"from": "draft", "to": "in_review", "by": "analyst"})
    if current in ("reviewed", "released"):
        history.append({"from": reviewed_from, "to": "reviewed", "by": "reviewer"})
    if current == "released":
        history.append({"from": "reviewed", "to": "released", "by": "qa"})
    context = {
        "state": {"field": "review", "from": current, "to": target},
        "record": {**values, "review": current},
        "actor": {"person": person},
        "last": {"review": {change["to"]: change for change in history}},
        "history": {"review": history},
        "edits": {
            "review": {
                "draft": [{"by": value_editor, "fields": ["purity"]}] if value_editor else []
            }
        },
    }
    checks = [
        (f"!({g.when}) || ({g.check})" if g.when else g.check, rules.CONDITION) for g in guards
    ]
    (outcomes,) = rules.evaluate(variables, checks, [context])
    assert all(o.error is None or o.empty for o in outcomes), outcomes
    return all(o.value for o in outcomes)


@pytest.mark.parametrize(
    ("analysis", "measurement"),
    [
        ("sec_purity", "purity"),
        ("intact_mass", "measured_mass"),
        ("peptide_map", "peptide_coverage"),
        ("endotoxin", "endotoxin"),
    ],
)
def test_submitting_an_analysis_requires_its_own_measurement(
    analysis_result: EntityType, analysis: str, measurement: str
) -> None:
    complete = {"analysis": analysis, "sample": "sample-id", "result": "fail", measurement: 0.0}
    assert allows(analysis_result, "draft", "in_review", "analyst", complete)
    for missing in ("sample", "result"):
        incomplete = {**complete, missing: None}
        assert not allows(analysis_result, "draft", "in_review", "analyst", incomplete), missing
    # A different assay's measurement cannot stand in for the one ordered.
    wrong = {"purity": 99.0, "measured_mass": 24000.0, "peptide_coverage": 97.0, "endotoxin": 0.2}
    assert not allows(
        analysis_result, "draft", "in_review", "analyst", {**complete, **wrong, measurement: None}
    )


def test_qa_cannot_release_an_unreviewed_analysis(analysis_result: EntityType) -> None:
    for state in ("draft", "in_review"):
        assert not allows(analysis_result, state, "released", "qa", {})


@pytest.mark.parametrize("person", ["creator", "analyst"])
def test_qa_cannot_release_a_result_they_created_or_submitted(
    analysis_result: EntityType, person: str
) -> None:
    values = {"analysis": "sec_purity", "sample": "sample-id", "result": "pass", "purity": 99.0}
    assert not allows(analysis_result, "reviewed", "released", person, values)


def test_an_older_unsigned_review_needs_fresh_review(analysis_result: EntityType) -> None:
    values = {"analysis": "sec_purity", "sample": "sample-id", "result": "pass", "purity": 99.0}
    assert allows(analysis_result, "reviewed", "draft", "analyst", values, reviewed_from="draft")
    assert not allows(analysis_result, "reviewed", "released", "qa", values, reviewed_from="draft")


@pytest.mark.parametrize(
    ("current", "target"), [("in_review", "reviewed"), ("reviewed", "released")]
)
def test_a_value_editor_cannot_sign_for_someone_elses_work(
    analysis_result: EntityType, current: str, target: str
) -> None:
    assert not allows(analysis_result, current, target, "editor", {}, value_editor="editor")
    assert allows(analysis_result, current, target, "independent", {}, value_editor="editor")


def test_correcting_a_result_before_release_reopens_its_draft(analysis_result: EntityType) -> None:
    states = analysis_result.fields["review"].states
    assert {name: state.on_edit for name, state in states.items() if state.on_edit} == {
        "in_review": "draft",
        "reviewed": "draft",
    }


def test_correcting_an_approved_sample_reopens_testing_but_claiming_it_keeps_review() -> None:
    loaded = load_plugin(PLUGIN, dependencies=[PLUGINS / "protein", PLUGINS / "dna"])
    assert loaded.plugin is not None
    sample = loaded.plugin.types["iki/example_lims.Sample"]
    status = sample.fields["status"]
    assert {name: state.on_edit for name, state in status.states.items() if state.on_edit} == {
        "approved": "in_testing",
    }
    schema = rules.record_schema(sample.label, sample.fields, None)
    variables = rules.guard_variables(schema, {"status": status}, [], status)
    for target in ("approved", "released"):
        guard = status.states[target].guards["independent_measurement"]
        for field in ("purity", "concentration", "result", "notes", "reviewer"):
            context = {
                "actor": {"person": "signer"},
                "edits": {"status": {"in_testing": [{"by": "signer", "fields": [field]}]}},
            }
            ((outcome,),) = rules.evaluate(variables, [(guard.check, rules.CONDITION)], [context])
            assert outcome.error is None
            assert outcome.value is (field == "reviewer")


def test_an_analysis_requires_independent_review_and_signed_release(
    analysis_result: EntityType,
) -> None:
    review = analysis_result.fields["review"]
    states = review.states
    assert review.initial == "draft"
    assert not states["draft"].locks
    assert states["reviewed"].locks
    assert states["draft"].reason == "required"
    for state, role, meaning in [
        ("in_review", "analyst", "authorship"),
        ("reviewed", "reviewer", "review"),
        ("released", "qa", "approval"),
    ]:
        assert states[state].locks
        assert states[state].set_by == (f"iki/example_lims.{role}",)
        assert [s.meaning for s in states[state].signatures] == [meaning]
    values = {"analysis": "sec_purity", "sample": "sample-id", "result": "fail", "purity": 90.0}
    assert not allows(analysis_result, "draft", "reviewed", "reviewer", values)
    assert not allows(analysis_result, "in_review", "reviewed", "analyst", values)
    assert not allows(analysis_result, "in_review", "reviewed", "creator", values)
    assert allows(analysis_result, "in_review", "reviewed", "reviewer", values)
    assert not allows(analysis_result, "reviewed", "released", "reviewer", values)
    assert allows(analysis_result, "reviewed", "released", "qa", values)
    # Returning it for correction requires a fresh submission and review before QA can sign.
    for state in ("in_review", "reviewed"):
        assert allows(analysis_result, state, "draft", "analyst", values)
    for state, person in [("draft", "analyst"), ("in_review", "analyst"), ("reviewed", "reviewer")]:
        assert not allows(analysis_result, "released", state, person, values)


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
    names = ("lims", "sync", "send", "hold")
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
        hold=importlib.import_module("hold"),
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
    assert first["type"] == "iki/protein.Protein"
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
        "name": "iki/example_lims.import_nightly",
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


def entity(hrid: str, **values: Any) -> dict[str, Any]:
    return {"id": f"id-{hrid}", "hrid": hrid, "values": values}


def test_holds_what_was_derived_from_a_donor_who_withdrew(lims: Any) -> None:
    """What's on hold with a reason keeps it; nothing is taken off hold."""
    run = {
        "inputs": {
            "donor": [entity("DNR-000001", code="D-17", consent="withdrawn")],
            "aliquots": [
                entity("ALQ-000001"),
                entity("ALQ-000002", on_hold=True, hold_reason="Haemolysed."),
            ],
            "results": [entity("RES-000001", on_hold=True)],
        }
    }
    output = ran(lims, lims.hold, run)
    why = "D-17 withdrew their consent."
    assert output["updates"] == {
        "aliquots": [{"entity": "id-ALQ-000001", "values": {"on_hold": True, "hold_reason": why}}],
        "results": [{"entity": "id-RES-000001", "values": {"on_hold": True, "hold_reason": why}}],
    }


def test_with_nothing_derived_it_holds_nothing(lims: Any) -> None:
    output = ran(lims, lims.hold, {"inputs": {"donor": [entity("DNR-000002", code="D-18")]}})
    assert output["updates"] == {}
