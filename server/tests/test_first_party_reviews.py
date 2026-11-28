"""Every first-party reviewed type is reviewed under a declared procedure (ADRs 0301, 0303),
and every step that a second person signs is declared independent of the people before.

A type's review is chosen per project from its plugin's starter, so what's checked here is
each procedure flow a starter offers, as the server compiles it: no flow lets one person
both make a result and sign its review, or sign two steps the procedure keeps apart.
"""

from functools import cache

import pytest
from first_party_dependencies import FIRST_PARTY, PLUGINS, PRODUCT_PLUGINS

from iki_spec.builtin import core_plugin
from iki_spec.model import Exclusion, Plugin, ReviewProcedure, Starter
from iki_spec.validate import load_plugin


@cache
def loaded(name: str) -> Plugin:
    directory = PLUGINS / name
    result = load_plugin(directory, dependencies=list(FIRST_PARTY[directory]))
    errors = [p for p in result.problems if p.severity.value == "error"]
    assert result.plugin is not None and errors == [], errors
    return result.plugin


@cache
def procedures() -> dict[str, ReviewProcedure]:
    found = dict(core_plugin().procedures)
    for name in sorted(d.name for d in PRODUCT_PLUGINS.values()):
        found.update(loaded(name).procedures)
    return found


def starters() -> list[tuple[str, Starter]]:
    return [
        (qualified, starter)
        for name in sorted(d.name for d in PRODUCT_PLUGINS.values())
        for qualified, starter in loaded(name).starters.items()
    ]


def reviewed() -> list[str]:
    """Every first-party type that declares a review of its own, by qualified name."""
    out = []
    for name in sorted(d.name for d in PRODUCT_PLUGINS.values()):
        for qualified, t in loaded(name).types.items():
            if t.review is not None:
                out.append(qualified)
    return out


def test_every_reviewed_type_is_offered_a_procedure() -> None:
    """A reviewed type no starter governs can't be chosen at setup, so records of it can't
    be made unless someone calls the API directly."""
    governed = {s.governs for _, s in starters()}
    # A follower is pinned by the record it follows, whose starter governs it.
    followers = {
        qualified
        for name in sorted(d.name for d in PRODUCT_PLUGINS.values())
        for qualified, t in loaded(name).types.items()
        if t.review is not None and t.review.by is not None
    }
    missing = [t for t in reviewed() if t not in governed | followers]
    assert missing == []


def _signers(exclusions: tuple[Exclusion, ...]) -> set[str]:
    return {s for e in exclusions if e.kind == "signers" for s in e.states}


@pytest.mark.parametrize("offered", [q for q, _ in starters()] or ["none"])
def test_every_step_another_person_signs_is_independent(offered: str) -> None:
    """In each flow a starter offers, a signed state is its maker's (`maker: true`) or
    declares whom its signer must not be: the record's creator, and the signer of every
    earlier signed state on the route."""
    if offered == "none":
        pytest.skip("No first-party plugin declares a starter yet.")
    starter = dict(starters())[offered]
    procedure = procedures()[starter.procedure]
    for flow in starter.flows:
        route = procedure.flows[flow].route
        before: list[str] = []
        for name in route:
            state = procedure.states[name]
            if state.signature is None:
                continue
            if state.maker is not None:
                assert not before, (
                    f"{offered} {flow}: {name} is its maker's, after {before} were signed"
                )
            else:
                kinds = {e.kind for e in state.independent_of}
                assert "creator" in kinds, f"{offered} {flow}: {name}'s signer may be its creator"
                missing = set(before) - _signers(state.independent_of)
                assert not missing, f"{offered} {flow}: {name}'s signer may have signed {missing}"
            before.append(name)
