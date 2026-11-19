"""HELM (ADR 0144): reading HELM strings, as a HELM field's value would be read, without
saving anything. Each comes back canonical, as an identity reads it, with its polymers'
monomers in order and its connections, or with what's wrong and at which character.

It reads HELM's grammar, not its chemistry: what a monomer ID means is its monomer
library's, which for oligonucleotides is the ``iki/oligo`` plugin's registry of monomers.
"""

from typing import Any, Literal

from django.http import HttpRequest
from ninja import Field, Router, Schema

from iki_server.api.common import caller, require_scope
from iki_server.api.problems import Problem
from iki_spec import helm
from iki_spec.wasm import UnavailableError

router = Router(tags=["helm"])

#: The most HELM strings one request reads.
MAX_READ = 100


class HelmIn(Schema):
    helm: list[str] = Field(
        min_length=1,
        max_length=MAX_READ,
        description="HELM strings, HELM 2 (ending V2.0) or HELM 1, such as "
        "RNA1{[dR](A)[sP].[dR](C)}$$$$V2.0.",
    )


class HelmMonomerOut(Schema):
    position: int = Field(description="Its position in its polymer, from 1, as connections count.")
    id: str = Field(description="Its monomer ID, without brackets, or its SMILES.")
    branch: bool = Field(description="It's a branch monomer: a nucleotide's base.")
    smiles: bool = Field(description="It's given in-line, as SMILES.")
    list: bool = Field(description="It's one of several monomers, or a mixture of them.")
    unit: int = Field(description="The unit it's in (its nucleotide, for RNA), from 1.")


class HelmPolymerOut(Schema):
    id: str = Field(description="Its canonical ID, such as RNA1.")
    type: Literal["PEPTIDE", "RNA", "CHEM", "BLOB"]
    units: int = Field(description="Its units: its nucleotides, for RNA.")
    monomers: list[HelmMonomerOut]
    ambiguous: bool = Field(
        description="It has monomers that aren't known, or a list of them, or a repeat of a "
        "range of counts."
    )


class HelmReadingOut(Schema):
    canonical: str | None = Field(
        None, description="Its canonical form, as an identity reads it. Null if it isn't HELM."
    )
    polymers: list[HelmPolymerOut] = Field(
        default_factory=list, description="Its polymers, in canonical order."
    )
    connections: list[str] = Field(default_factory=list, description="Its connections, canonical.")
    version1: bool = Field(False, description="It was written as HELM 1.")
    error: str | None = Field(None, description="Why it isn't HELM. Null if it is.")
    at: int | None = Field(None, description="The character that's wrong, from 0.")


class HelmOut(Schema):
    readings: list[HelmReadingOut] = Field(description="Each string's reading, in order.")


@router.post("/helm/read", response=HelmOut, summary="Read HELM")
def read_helm(request: HttpRequest, payload: HelmIn) -> dict[str, Any]:
    """Each HELM string read as a HELM field's value is: canonical, with its polymers'
    monomers and its connections, or why it isn't HELM and where. Nothing is saved."""
    require_scope(caller(request), "read")
    try:
        found = helm.read_all(payload.helm)
    except UnavailableError as error:
        raise Problem(503, f"Iki can't read HELM now: {error}", code="helm_unavailable") from None
    return {
        "readings": [
            {"error": r.message, "at": r.at} if isinstance(r, helm.HelmError) else r for r in found
        ]
    }
