"""Starters a plugin declares (ADR 0303), checked against the procedure they apply and every
type whose records they pin."""

import textwrap
from pathlib import Path

import pytest

from iki_spec.model import plugin_from_dict, plugin_to_dict
from iki_spec.validate import Result, load_plugin

PLUGIN = """\
apiVersion: iki/v1
kind: Plugin
name: acme/qc
version: 1.0.0
"""

TYPES = """\
apiVersion: iki/v1
kind: EntityType
name: Case
schema_version: 1
hrid: { prefix: CAS }
fields:
  lot: { type: string, max_length: 40 }
review: { opens: true }
---
apiVersion: iki/v1
kind: EntityType
name: Certificate
schema_version: 1
hrid: { prefix: CRT }
fields:
  case: { type: ref, to: Case, required: true, immutable: true }
review:
  pinned: { by: case }
  derived: true
  needs: { final: [signed_off, released] }
---
apiVersion: iki/v1
kind: EntityType
name: Assay
schema_version: 1
hrid: { prefix: ASY }
fields:
  value: { type: float }
review:
  needs: { final: [signed_off, released] }
"""

STARTER = """\
apiVersion: iki/v1
kind: Starter
name: release_review
version: 1.0.0
label: Lot release
description: Choose how a lot's release is reviewed, and who signs it.
governs: Case
procedure: iki/core.result_review@1
flows: [research, regulated_qc]
suggested: regulated_qc
summaries:
  research: One reviewer signs the certificate.
slots:
  analyst: { minimum: editor }
  reviewer: { minimum: viewer }
  qa: { minimum: viewer }
"""


def make(tmp_path: Path, starter: str = STARTER) -> Result:
    root = tmp_path / "acme"
    for name, text in {
        "plugin.yaml": PLUGIN,
        "types/types.yaml": TYPES,
        "starters/release.yaml": starter,
    }.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
    return load_plugin(root)


def codes(result: Result) -> list[str]:
    return [p.code for p in result.problems if p.is_error]


def test_a_plugin_declares_a_starter(tmp_path: Path) -> None:
    result = make(tmp_path)
    assert codes(result) == []
    assert result.plugin is not None
    starter = result.plugin.starters["acme/qc.release_review"]
    assert starter.governs == "acme/qc.Case"
    assert starter.slots["analyst"].minimum == "editor"
    assert plugin_from_dict(plugin_to_dict(result.plugin)).starters == result.plugin.starters


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (("governs: Case", "governs: Lot"), "unknown-type"),
        (("governs: Case", "governs: Certificate"), "starter-governs"),
        (("result_review@1", "result_review@2"), "unknown-procedure"),
        (("flows: [research, regulated_qc]", "flows: [research, gmp]"), "unknown-flow"),
        (("suggested: regulated_qc", "suggested: record_only"), "unknown-flow"),
        (("  qa: { minimum: viewer }\n", ""), "starter-slot-minimum"),
        (("  qa: { minimum: viewer }\n", "  qa: { minimum: viewer }\n  sme: {}\n"), "unknown-slot"),
        (("  qa: { minimum: viewer }", "  qa: { role: acme/qc.release_qa }"), "unknown-role"),
    ],
)
def test_a_starter_says_only_what_it_can_mean(
    tmp_path: Path, change: tuple[str, str], code: str
) -> None:
    old, new = change
    assert old in STARTER
    assert code in codes(make(tmp_path, STARTER.replace(old, new, 1)))


def test_a_starter_fits_the_records_that_follow_its_type(tmp_path: Path) -> None:
    """A certificate follows its case's procedure: a flow whose end it can't take as
    complete is refused for the case's starter too."""
    accepted = STARTER.replace("flows: [research, regulated_qc]", "flows: [research, accepted]")
    result = make(tmp_path, accepted)
    found = [p.message for p in result.problems if p.code == "procedure-misfit"]
    assert found == [
        "iki/core.result_review@1 can't review Certificate: its flow `accepted` ends in "
        "`accepted`, and Certificate's readers take only `signed_off` or `released` as "
        "complete.",
        "iki/core.result_review@1 can't review Certificate: its flow `accepted` doesn't end "
        "in a signed state, and Certificate re-issues a signed record.",
    ]


LAB_ROLES = Path(__file__).parents[2] / "plugins" / "lab_roles"


def test_a_plugins_own_generic_role_says_why_its_separate(tmp_path: Path) -> None:
    """A plugin using the shared roles that declares its own `qa` must say why (ADR 0304)."""
    root = tmp_path / "acme"
    root.mkdir()
    plugin = PLUGIN + "dependencies: { iki/lab_roles: {} }\nroles:\n  qa: { label: Lot QA }\n"
    (root / "plugin.yaml").write_text(plugin)
    result = load_plugin(root, dependencies=[LAB_ROLES])
    warned = [p for p in result.problems if p.code == "generic-role"]
    assert [p.is_error for p in warned] == [False]
    (root / "plugin.yaml").write_text(
        plugin.replace(
            "{ label: Lot QA }",
            "{ label: Lot QA, distinct: Lot disposition is given to fewer people. }",
        )
    )
    assert not load_plugin(root, dependencies=[LAB_ROLES]).problems
