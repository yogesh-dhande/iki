"""Shared registration actions cannot stand in for a missing product seed scenario."""

import runpy
from pathlib import Path

import pytest

from iki_server.accounts.models import User
from iki_server.entities.models import Entity
from iki_server.org.models import Project, Team
from iki_server.runs.models import ActionRun, EffectKind, RunEffect, RunStatus


@pytest.mark.django_db
def test_plasmid_and_oligo_seeds_require_distinct_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    atlas = Project.objects.create(team=Team.objects.create(name="Lab"), name="Atlas")
    user = User.objects.create_user("scientist@example.com")
    monkeypatch.setenv("IKI_SEED_PROJECT", str(atlas.pk))
    checker = Path(__file__).resolve().parents[2] / "deploy/dev/check_seed.py"

    def run(number: int, action: str, **params: str) -> ActionRun:
        return ActionRun.objects.create(
            hrid=f"RUN-{number:06d}",
            action=action,
            project=atlas,
            requested_by=user,
            spec={},
            params=params,
            tier="backend",
            status=RunStatus.SUCCEEDED,
            timeout=600,
            max_attempts=1,
        )

    def missing() -> str:
        # This database contains only the two scenarios under test.
        with pytest.raises(SystemExit) as error:
            runpy.run_path(str(checker))
        return str(error.value)

    run(1, "iki/plasmid.register_plasmid")
    register = "iki/oligo.register_oligonucleotide"
    strand = Entity.objects.create(
        type="iki/oligo.Oligonucleotide", owner_project=atlas, hrid="OLI-001"
    )
    other = Entity.objects.create(type=strand.type, owner_project=atlas, hrid="OLI-002")
    shared_monomer = Entity.objects.create(
        type="iki/oligo.Monomer", owner_project=atlas, hrid="MON-001"
    )
    named = run(2, register, name="nusinersen")
    RunEffect.objects.create(run=named, kind=EffectKind.CREATED, entity=strand)
    RunEffect.objects.create(run=named, kind=EffectKind.REUSED, entity=shared_monomer)
    unrelated = run(3, register)
    RunEffect.objects.create(run=unrelated, kind=EffectKind.REUSED, entity=other)
    RunEffect.objects.create(run=unrelated, kind=EffectKind.REUSED, entity=shared_monomer)
    absent = missing()
    assert "no successful run for iki/plasmid.assemble" in absent
    assert "no reused nusinersen oligonucleotide" in absent

    run(4, "iki/plasmid.assemble")
    RunEffect.objects.create(run=run(5, register), kind=EffectKind.REUSED, entity=strand)
    present = missing()
    assert "no successful run for iki/plasmid.assemble" not in present
    assert "no reused nusinersen oligonucleotide" not in present
