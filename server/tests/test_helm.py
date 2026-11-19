"""Reading HELM (ADR 0144) through the API: canonical, as an identity reads it, or what's
wrong and where. Nothing is saved."""

import pytest
from org_world import World, build_world
from test_api_entities import api_as

pytestmark = [pytest.mark.django_db, pytest.mark.wasm]


@pytest.fixture
def world() -> World:
    return build_world()


def test_it_reads_helm_canonical_or_says_whats_wrong_and_where(world: World) -> None:
    api = api_as(world, "viewer")
    found = api.post(
        "/helm/read",
        {
            "helm": [
                # 5'-biotin on a DNA primer, written as HELM 1 with its conjugate first.
                "CHEM1{[5Bio]}|RNA1{[dR](A)[sP].[dR](C)[sP]'2'.[dR](G)}$RNA1,CHEM1,1:R1-1:R2$$$",
                "RNA1{[dR](A)[sP].}$$$$V2.0",
            ]
        },
    ).json()["readings"]
    assert found[0]["canonical"] == (
        "RNA1{[dR](A)[sP].[dR](C)[sP].[dR](C)[sP].[dR](G)}|CHEM1{[5Bio]}"
        "$RNA1,CHEM1,1:R1-1:R2$$$V2.0"
    )
    assert found[0]["version1"] is True
    assert [p["id"] for p in found[0]["polymers"]] == ["RNA1", "CHEM1"]
    assert found[0]["polymers"][0]["units"] == 4
    assert [m["id"] for m in found[0]["polymers"][0]["monomers"][:3]] == ["dR", "A", "sP"]
    assert found[0]["connections"] == ["RNA1,CHEM1,1:R1-1:R2"]
    assert found[1]["canonical"] is None
    assert found[1]["error"] == "Expected a monomer, but found “}”."
    assert found[1]["at"] == 17


def test_it_reads_at_most_a_hundred(world: World) -> None:
    api = api_as(world, "viewer")
    assert api.post("/helm/read", {"helm": ["RNA1{R}$$$$"] * 101}).status_code == 422
    assert api.post("/helm/read", {"helm": []}).status_code == 422
