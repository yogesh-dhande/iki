"""Review procedures (ADR 0301): declared once, compiled into guards, paired with the
types whose needs they fit."""

import textwrap
from pathlib import Path

import pytest

from iki_spec import procedures, rules
from iki_spec.builtin import shipped_core
from iki_spec.model import (
    ReviewNeeds,
    ReviewProcedure,
    ReviewSpec,
    plugin_from_dict,
    plugin_to_dict,
)
from iki_spec.validate import Result, load_plugin

PLUGIN = """\
apiVersion: iki/v1
kind: Plugin
name: acme/qms
version: 1.0.0
roles:
  analyst: { label: Analyst, description: Tests. }
  reviewer: { label: Reviewer, description: Reviews. }
  qa: { label: QA, description: Releases. }
"""

# A GMP site's SOP: two reviewers before QA approval, named as the SOP names them.
GMP = """\
apiVersion: iki/v1
kind: ReviewProcedure
name: gmp_result_review
version: 2
label: GMP result review (SOP-QA-012)
slots:
  analyst: { label: Analyst }
  reviewer: { label: Second-person reviewer }
  qa: { label: QA }
states:
  recorded:
    label: In progress
    by: [analyst, reviewer]
    reason: required
  submitted:
    label: Submitted
    by: analyst
    locks: true
    signature: { meaning: authorship }
    maker: true
  first_review:
    label: First review
    by: reviewer
    locks: true
    signature: { meaning: review }
    independent_of: [creator, editors, signers: [submitted]]
  second_review:
    label: Second review
    by: reviewer
    locks: true
    signature: { meaning: review }
    independent_of: [creator, editors, signers: [submitted, first_review]]
  released:
    label: Approved
    by: qa
    locks: true
    signature: { meaning: approval }
    independent_of: [creator, editors, signers: [submitted, first_review, second_review]]
flows:
  gmp:
    label: GMP
    summary: Submitted, reviewed by two people and approved by QA.
    route: [recorded, submitted, first_review, second_review, released]
    moves: { recorded: [submitted, first_review, second_review] }
"""

RESULT = """\
apiVersion: iki/v1
kind: EntityType
name: Result
schema_version: 1
hrid: { prefix: RES }
fields:
  value: { type: float }
  analysts: { type: list, items: { type: person } }
  case: { type: ref, to: Case }
review:
  needs:
    final: [signed_off, released]
    states: [submitted]
    locks: signed
  independent_of: [people: [analysts, case.investigator]]
  guards:
    measured:
      on: [signed]
      check: record.value != null
      message: Record the value before signing.
  statements:
    released: I release this result to the project that asked.
"""

CASE = """\
apiVersion: iki/v1
kind: EntityType
name: Case
schema_version: 1
hrid: { prefix: CAS }
fields:
  investigator: { type: person }
review: { opens: true }
"""


def make(tmp_path: Path, files: dict[str, str], plugin: str = PLUGIN) -> Result:
    root = tmp_path / "acme"
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
    (root / "plugin.yaml").write_text(plugin)
    return load_plugin(root)


def codes(result: Result) -> list[str]:
    return [p.code for p in result.problems if p.is_error]


def core_review() -> ReviewProcedure:
    return shipped_core().procedures["iki/core.result_review@1"]


BINDING = {s: f"iki/lab_roles.{s}" for s in ("analyst", "reviewer", "qa")}


# --- Core's procedure -----------------------------------------------------------------------


def test_core_ships_result_review_with_its_four_flows() -> None:
    procedure = core_review()
    assert procedure.qualified_name == "iki/core.result_review@1"
    assert list(procedure.flows) == ["record_only", "accepted", "research", "regulated_qc"]
    assert procedure.flows["regulated_qc"].final == "released"


def test_regulated_qc_compiles_into_route_maker_and_independence_guards() -> None:
    states = procedures.compile_flow(core_review(), "regulated_qc", field="review", binding=BINDING)
    assert list(states) == ["recorded", "submitted", "reviewed", "released"]
    submitted, reviewed, released = states["submitted"], states["reviewed"], states["released"]
    assert submitted.set_by == ("iki/lab_roles.analyst",)
    assert submitted.guards["route"].check == 'state.from == "recorded"'
    assert "history.review[0].by == actor.person" in submitted.guards["maker"].check
    independent = released.guards["independent"].check
    assert "history.review[0].by != actor.person" in independent
    assert "edits.review.recorded.all(e, e.by != actor.person)" in independent
    assert "last.review.submitted.by != actor.person" in independent
    assert "last.review.reviewed.by != actor.person" in independent
    assert "independent" not in submitted.guards
    assert reviewed.guards["independent"].message == (
        "It must be signed by someone other than whoever created the record, whoever "
        "changed it since it was Recorded and whoever signed it Submitted."
    )
    assert states["recorded"].guards["route"].check == 'state.from in ["submitted", "reviewed"]'


def test_research_sign_off_is_independent_of_the_maker() -> None:
    states = procedures.compile_flow(core_review(), "research", field="review", binding=BINDING)
    assert list(states) == ["recorded", "signed_off"]
    assert states["signed_off"].guards["independent"].check == (
        "history.review[0].by != actor.person && edits.review.recorded.all(e, e.by != actor.person)"
    )
    # Nothing returns a research result to Recorded, so no one sets it: records start there.
    assert states["recorded"].set_by == ()


def test_compiled_guards_type_check_against_the_record() -> None:
    procedure = core_review()
    for flow in procedure.flows:
        f = procedures.review_field(
            ReviewSpec(), procedure, flow, binding=BINDING, people=lambda _: None
        )
        record = rules.record_schema("Result", {"review": f}, None)
        variables = rules.guard_variables(record, {"review": f}, BINDING.values(), f)
        guards = [g for st in f.states.values() for g in st.guards.values()]
        checked = rules.type_check(variables, [(g.check, rules.CONDITION) for g in guards])
        assert [c.error for c in checked] == [None] * len(guards), flow


def test_staffing_reads_who_must_be_different_people_from_the_declarations() -> None:
    plan = procedures.staffing(core_review(), "regulated_qc")
    assert plan.steps == {
        "submitted": ("analyst",),
        "reviewed": ("reviewer",),
        "released": ("qa",),
    }
    assert plan.distinct_slots == ("analyst", "reviewer", "qa")
    assert procedures.can_staff(plan, {"analyst": {1}, "reviewer": {2}, "qa": {3}})
    assert not procedures.can_staff(plan, {"analyst": {1}, "reviewer": {1}, "qa": {3}})
    # Two people chosen for every slot can still sign as three.
    assert procedures.can_staff(plan, {"analyst": {1, 2}, "reviewer": {1, 2}, "qa": {3}})
    assert not procedures.can_staff(plan, {"analyst": {1, 2}, "reviewer": {1, 2}, "qa": {1, 2}})
    research = procedures.staffing(core_review(), "research")
    assert research.slots == ("reviewer",) and research.distinct_slots == ()
    assert procedures.staffing(core_review(), "record_only").steps == {}


# --- A lab's own procedure ------------------------------------------------------------------


def test_a_lab_declares_its_sop_as_a_procedure(tmp_path: Path) -> None:
    result = make(tmp_path, {"procedures/gmp.yaml": GMP})
    assert codes(result) == []
    assert result.plugin is not None
    procedure = result.plugin.procedures["acme/qms.gmp_result_review@2"]
    plan = procedures.staffing(procedure, "gmp")
    # Two reviews in one slot need two people holding it.
    assert not procedures.can_staff(plan, {"analyst": {1}, "reviewer": {2}, "qa": {3}})
    assert procedures.can_staff(plan, {"analyst": {1}, "reviewer": {2, 4}, "qa": {3}})
    again = plugin_from_dict(plugin_to_dict(result.plugin))
    assert again.procedures == result.plugin.procedures


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (("    maker: true\n", ""), "signer-undeclared"),
        (("    maker: true\n", "    independent_of: [creator]\n"), "authorship-not-maker"),
        (
            (
                "    independent_of: [creator, editors, signers: [submitted]]\n",
                "    independent_of: [creator, people: [analysts]]\n",
            ),
            "people-in-procedure",
        ),
        (
            (
                "    independent_of: [creator, editors, signers: [submitted]]\n",
                "    independent_of: [{creator: true, when: record.value > 0}]\n",
            ),
            "when-in-procedure",
        ),
        (("    by: [analyst, reviewer]\n", "    by: [analyst, approver]\n"), "unknown-slot"),
        (("    reason: required\n", "    reason: required\n    locks: true\n"), "open-state-locks"),
        (("route: [recorded, submitted,", "route: [recorded, submit,"), "unknown-state"),
    ],
)
def test_a_procedure_that_cant_be_enforced_is_refused(
    tmp_path: Path, change: tuple[str, str], code: str
) -> None:
    old, new = change
    assert old in GMP
    result = make(tmp_path, {"procedures/gmp.yaml": GMP.replace(old, new, 1)})
    assert code in codes(result)


# --- What a type needs ----------------------------------------------------------------------


def test_a_reviewed_type_gets_its_review_field_and_pinned_fields(tmp_path: Path) -> None:
    result = make(tmp_path, {"types/result.yaml": RESULT, "types/case.yaml": CASE})
    assert codes(result) == []
    assert result.plugin is not None
    result_type = result.plugin.types["acme/qms.Result"]
    review = result_type.fields["review"]
    assert review.procedure and review.initial == "recorded"
    assert list(review.states) == ["recorded", "signed_off", "released", "submitted"]
    for name in ("review_procedure", "review_flow"):
        f = result_type.fields[name]
        assert f.computed and f.immutable and f.indexed
    case = result.plugin.types["acme/qms.Case"]
    assert "review" not in case.fields and "review_procedure" in case.fields
    assert result_type.review is not None
    assert result_type.review.guards["measured"].on == ("signed",)
    # What its people exclusions read, so the server loads them for the guard.
    assert set(result_type.review.reads) == {"analysts", "case"}
    again = plugin_from_dict(plugin_to_dict(result.plugin))
    assert again.types == result.plugin.types


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (
            (
                "  value: { type: float }\n",
                "  value: { type: float }\n  review_flow: { type: string }\n",
            ),
            "review-field-declared",
        ),
        (("      on: [signed]\n", "      on: [approved]\n"), "review-unneeded-state"),
        (("[people: [analysts, case.investigator]]", "[people: [value]]"), "not-a-person"),
        (("[people: [analysts, case.investigator]]", "[creator]"), "review-exclusion"),
        (("    released: I release", "    approved: I release"), "review-unneeded-state"),
        (("  guards:\n    measured:", "  guards:\n    independent:"), "review-guard-name"),
        (
            ("      check: record.value != null", "      check: record.amount != null"),
            "invalid-expression",
        ),
        (("review:\n  needs:", "review:\n  pinned: { by: value }\n  needs:"), "review-pinned-by"),
    ],
)
def test_a_reviewed_type_says_only_what_it_can_mean(
    tmp_path: Path, change: tuple[str, str], code: str
) -> None:
    old, new = change
    assert old in RESULT
    result = make(
        tmp_path, {"types/result.yaml": RESULT.replace(old, new, 1), "types/case.yaml": CASE}
    )
    assert code in codes(result)


def test_a_follower_is_pinned_by_a_reference_to_a_reviewed_record(tmp_path: Path) -> None:
    follower = RESULT.replace(
        "  case: { type: ref, to: Case }\n",
        "  case: { type: ref, to: Case, required: true, immutable: true }\n",
    ).replace("review:\n  needs:", "review:\n  pinned: { by: case }\n  needs:")
    assert codes(make(tmp_path, {"types/result.yaml": follower, "types/case.yaml": CASE})) == []
    loose = follower.replace(", required: true, immutable: true", "")
    assert "review-pinned-by" in codes(
        make(tmp_path / "loose", {"types/result.yaml": loose, "types/case.yaml": CASE})
    )


def test_a_subtype_is_reviewed_as_its_parent(tmp_path: Path) -> None:
    child = """\
    apiVersion: iki/v1
    kind: EntityType
    name: SpecialResult
    extends: Result
    schema_version: 1
    hrid: { prefix: SPR }
    review: { needs: { final: [released] } }
    """
    result = make(
        tmp_path, {"types/result.yaml": RESULT, "types/case.yaml": CASE, "types/child.yaml": child}
    )
    assert "review-inherited" in codes(result)


def test_a_procedure_fits_a_type_only_where_it_meets_its_needs(tmp_path: Path) -> None:
    result = make(
        tmp_path,
        {"types/result.yaml": RESULT, "types/case.yaml": CASE, "procedures/gmp.yaml": GMP},
    )
    assert result.plugin is not None
    spec = result.plugin.types["acme/qms.Result"].review
    assert spec is not None
    core = core_review()
    assert procedures.misfits(spec, "Result", core, ["research", "regulated_qc"]) == []
    refused = procedures.misfits(spec, "Result", core, ["record_only", "accepted"])
    assert refused == [
        "iki/core.result_review@1 can't review Result: its flow `record_only` ends in "
        "`recorded`, and Result's readers take only `signed_off` or `released` as complete.",
        "iki/core.result_review@1 can't review Result: its flow `accepted` ends in "
        "`accepted`, and Result's readers take only `signed_off` or `released` as complete.",
    ]
    gmp = result.plugin.procedures["acme/qms.gmp_result_review@2"]
    # The lab's SOP ends in `released` and has a `submitted` state, so it fits.
    assert procedures.misfits(spec, "Result", gmp, ["gmp"]) == []
    assert procedures.misfits(spec, "Result", gmp, ["research"]) == [
        "acme/qms.gmp_result_review@2 can't review Result: it has no `research` flow."
    ]


def test_the_types_people_and_guards_join_the_compiled_states(tmp_path: Path) -> None:
    result = make(tmp_path, {"types/result.yaml": RESULT, "types/case.yaml": CASE})
    assert result.plugin is not None
    spec = result.plugin.types["acme/qms.Result"].review
    assert spec is not None
    people = {"analysts": True, "case.investigator": False}.get
    states = procedures.compile_flow(
        core_review(), "regulated_qc", field="review", binding=BINDING, review=spec, people=people
    )
    reviewed = states["reviewed"].guards
    assert "!(actor.person in record.analysts)" in reviewed["independent"].check
    assert "record.case.investigator != actor.person" in reviewed["independent"].check
    assert reviewed["measured"].check == "record.value != null"
    assert "measured" in states["submitted"].guards
    assert "measured" not in states["recorded"].guards
    # Its signers attest in its own words where it gives them, and the procedure's elsewhere.
    (release,) = states["released"].signatures
    assert (release.meaning, release.label) == ("approval", "QA release")
    assert release.statement == "I release this result to the project that asked."
    (review,) = states["reviewed"].signatures
    procedures_own = core_review().states["reviewed"].signature
    assert procedures_own is not None and review.statement == procedures_own.statement


def test_a_derived_record_takes_its_flows_final_signature_alone() -> None:
    derived = ReviewSpec(derived=True, needs=ReviewNeeds(final=("signed_off", "released")))
    states = procedures.compile_flow(
        core_review(), "regulated_qc", field="review", binding=BINDING, review=derived
    )
    assert list(states) == ["recorded", "released"]
    assert states["released"].guards["route"].check == 'state.from == "recorded"'
    assert "independent" not in states["released"].guards
    assert procedures.misfits(derived, "Certificate", core_review(), ["research"]) == []
    assert procedures.misfits(derived, "Certificate", core_review(), ["accepted"]) != []


# --- Declared independence on any state field ------------------------------------------------

LOT = """\
apiVersion: iki/v1
kind: EntityType
name: Batch
schema_version: 1
hrid: { prefix: BAT }
fields:
  source: { type: enum, values: [made, bought] }
  made_by: { type: person }
  disposition:
    type: state
    initial: quarantined
    states:
      quarantined: { label: Quarantined, set_by: [qa], reason: required }
      retest_due: { label: Retest due, set_by: [qa] }
      released:
        label: Released
        set_by: [qa]
        locks: true
        signatures: [{ meaning: approval }]
        independent_of:
          - { creator: true, when: record.source == "made" }
          - { editors: { in: [quarantined, retest_due] } }
          - { people: [made_by] }
"""


def test_an_ordinary_state_declares_its_independence(tmp_path: Path) -> None:
    result = make(tmp_path, {"types/batch.yaml": LOT})
    assert codes(result) == []
    assert result.plugin is not None
    released = result.plugin.types["acme/qms.Batch"].fields["disposition"].states["released"]
    check = released.guards["independent"].check
    assert check.startswith(
        '(!(record.source == "made") || (history.disposition[0].by != actor.person))'
    )
    assert "(edits.disposition.quarantined + edits.disposition.retest_due).all(" in check
    assert "(record.made_by == null || record.made_by != actor.person)" in check
    assert released.independent_of[0].when == 'record.source == "made"'


def test_an_ordinary_states_editors_name_their_window(tmp_path: Path) -> None:
    loose = LOT.replace("{ editors: { in: [quarantined, retest_due] } }", "editors")
    assert "editors-without-window" in codes(make(tmp_path, {"types/batch.yaml": loose}))
