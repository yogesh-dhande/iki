"""Entities: creating, editing, revisions and history, provenance, duplicates, archiving."""

import dataclasses
import datetime as dt
from typing import Any

import pytest
from django.db import connection, transaction
from django.db.utils import DatabaseError

from iki_server.accounts.models import User
from iki_server.audit import log
from iki_server.audit.models import AuditEvent, Via
from iki_server.entities import access, services, values
from iki_server.entities.models import Entity, ProjectLink, Revision
from iki_server.org.models import Project, ProjectMembership, ProjectRole, Team
from iki_server.org.services import Refused

pytestmark = pytest.mark.django_db


def person(email: str) -> User:
    return User.objects.create_user(email=email, name=email.split("@")[0])


@pytest.fixture
def lab() -> dict[str, Any]:
    team = Team.objects.create(name="Biologics")
    first = Project.objects.create(team=team, name="Antibodies")
    second = Project.objects.create(team=team, name="Enzymes")
    alice, bob, carol = person("alice@example.org"), person("bob@example.org"), person("c@x.org")
    ProjectMembership.objects.create(project=first, user=alice, role=ProjectRole.EDITOR)
    ProjectMembership.objects.create(project=second, user=bob, role=ProjectRole.EDITOR)
    ProjectMembership.objects.create(project=second, user=alice, role=ProjectRole.VIEWER)
    return {
        "team": team,
        "first": first,
        "second": second,
        "alice": alice,
        "bob": bob,
        "carol": carol,
    }


def dna(lab: dict[str, Any], bases: str = "ACGT", **values: Any) -> services.Saved:
    return services.create(
        lab["alice"], "dna.DNASequence", lab["first"], {"bases": bases, **values}
    )


def test_creating_makes_revision_one_with_history(lab: dict[str, Any]) -> None:
    with log.using(log.Context(via=Via.API)):
        saved = dna(lab, name="pUC19")
    entity = saved.entity
    assert entity.hrid.startswith("DNA-")  # type: ignore[attr-defined]
    assert entity.revision == 1  # type: ignore[attr-defined]
    revision = saved.revision
    assert revision is not None
    assert (revision.number, revision.kind, revision.via) == (1, "created", "api")
    # Defaults count as set: topology defaults to linear.
    assert revision.changed_fields == ["bases", "name", "topology"]
    assert revision.user == lab["alice"]
    assert services.current_values(entity) == {
        "name": "pUC19",
        "bases": "ACGT",
        "topology": "linear",
        "length": None,
        "gc_content": None,
    }
    assert services.values_at(Entity.objects.get(pk=entity.pk), 1)["bases"] == "ACGT"
    assert list(
        ProjectLink.objects.filter(entity_id=entity.pk).values_list("project", flat=True)
    ) == [lab["first"].pk]


def test_edits_make_revisions_and_keep_what_changed(lab: dict[str, Any]) -> None:
    entity = Entity.objects.get(pk=dna(lab, name="pUC19").entity.pk)
    saved = services.update(
        lab["alice"], entity, {"name": "pUC19 v2"}, expected_revision=1, reason="Typo"
    )
    assert saved.revision is not None
    assert (saved.revision.number, saved.revision.changed_fields) == (2, ["name"])
    assert saved.revision.reason == "Typo"
    entity.refresh_from_db()
    assert entity.revision == 2
    assert services.changes(entity, saved.revision) == {"name": ("pUC19", "pUC19 v2")}
    assert services.values_at(entity, 1)["name"] == "pUC19"
    assert services.values_at(entity, 2)["name"] == "pUC19 v2"
    # The same values again change nothing, and make no revision.
    again = services.update(lab["alice"], entity, {"name": "pUC19 v2"}, expected_revision=2)
    assert again.revision is None
    assert Revision.objects.filter(entity=entity).count() == 2


def test_provenance_is_the_revision_that_set_each_value(lab: dict[str, Any]) -> None:
    entity = Entity.objects.get(pk=dna(lab, name="pUC19").entity.pk)
    services.update(lab["alice"], entity, {"topology": "circular"}, expected_revision=1)
    found = services.provenance(entity)
    assert {name: r.number for name, r in found.items()} == {
        "bases": 1,
        "name": 1,
        "topology": 2,
    }


def test_edit_rules(lab: dict[str, Any]) -> None:
    entity = Entity.objects.get(pk=dna(lab).entity.pk)
    with pytest.raises(services.Invalid) as raised:
        services.update(
            lab["alice"],
            entity,
            {"bases": "GGGG", "length": 4, "nope": 1, "topology": "knotted"},
            expected_revision=1,
        )
    assert raised.value.errors == {
        "bases": ["can't change once it's set."],
        "length": ["is computed: only actions set it."],
        "nope": ["dna.DNASequence has no field `nope`."],
        "topology": ["must be one of: `linear`, `circular`."],
    }
    with pytest.raises(services.Invalid) as raised:
        services.create(lab["alice"], "dna.DNASequence", lab["first"], {"gc_content": 50})
    assert raised.value.errors == {
        "gc_content": ["is computed: only actions set it."],
        "bases": ["is required."],
    }
    with pytest.raises(services.Invalid) as raised:
        services.create(lab["alice"], "dna.Sequence", lab["first"], {"name": "x"})
    assert raised.value.errors == {
        "type": ["dna.Sequence is abstract: create one of its subtypes."]
    }


def test_an_edit_against_an_old_revision_is_refused(lab: dict[str, Any]) -> None:
    entity = Entity.objects.get(pk=dna(lab).entity.pk)
    services.update(lab["alice"], entity, {"name": "first"}, expected_revision=1)
    with pytest.raises(services.Stale) as raised:
        services.update(lab["alice"], entity, {"name": "second"}, expected_revision=1)
    assert raised.value.current == 2
    entity.refresh_from_db()
    assert services.current_values(services._specific(entity))["name"] == "first"


def test_quantities_convert_into_their_unit(lab: dict[str, Any]) -> None:
    sequence = dna(lab).entity
    saved = services.create(
        lab["alice"],
        "protein.Protein",
        lab["first"],
        {
            "sequence": "MKV",
            "encoded_by": str(sequence.pk),
            "temperature_optimum": {"value": 310.15, "unit": "K"},
            "uniprot_ids": ["P69905"],
        },
    )
    values = services.current_values(saved.entity)
    assert values["temperature_optimum"] == pytest.approx(37.0)
    assert values["encoded_by"] == sequence.pk
    assert values["uniprot_ids"] == ["P69905"]
    with pytest.raises(services.Invalid) as raised:
        services.create(
            lab["alice"],
            "protein.Protein",
            lab["first"],
            {"sequence": "MKV", "temperature_optimum": {"value": 1, "unit": "g"}},
        )
    assert "measure different things" in raised.value.errors["temperature_optimum"][0]


def test_references_only_reach_what_the_caller_can_read(lab: dict[str, Any]) -> None:
    hidden = services.create(lab["bob"], "dna.DNASequence", lab["second"], {"bases": "TTTT"})
    # Alice is a Viewer in the second project: she can refer to it.
    services.create(
        lab["alice"],
        "protein.Protein",
        lab["first"],
        {"sequence": "MKV", "encoded_by": str(hidden.entity.pk)},
    )
    with pytest.raises(services.Invalid) as raised:
        services.create(
            lab["carol"],
            "protein.Protein",
            lab["first"],
            {"sequence": "MKV", "encoded_by": str(hidden.entity.pk)},
        )
    assert raised.value.errors == {"encoded_by": ["refers to no entity you can read."]}
    protein = services.create(lab["alice"], "protein.Protein", lab["first"], {"sequence": "MK"})
    with pytest.raises(services.Invalid) as raised:
        services.create(
            lab["alice"],
            "protein.Protein",
            lab["first"],
            {"sequence": "MKV", "encoded_by": str(protein.entity.pk)},
        )
    assert raised.value.errors == {
        "encoded_by": ["must refer to a dna.DNASequence, not a protein.Protein."]
    }


def test_a_duplicate_is_reused_and_linked(lab: dict[str, Any]) -> None:
    first = dna(lab, "ACGTACGT")
    again = services.create(lab["bob"], "dna.DNASequence", lab["second"], {"bases": "ACGTACGT"})
    # Bob can't read the first project's entity, so he gets his own.
    assert not again.reused
    assert again.entity.pk != first.entity.pk
    # Alice can read both: hers is reused, and linked into the second project.
    ProjectMembership.objects.filter(user=lab["alice"], project=lab["second"]).update(
        role=ProjectRole.EDITOR
    )
    reused = services.create(lab["alice"], "dna.DNASequence", lab["second"], {"bases": "ACGTACGT"})
    assert reused.reused and reused.linked
    assert reused.entity.pk == first.entity.pk
    assert reused.revision is None
    assert ProjectLink.objects.filter(entity_id=first.entity.pk).count() == 2
    # Bob now sees it too, through the second project.
    assert access.can_read(lab["bob"], Entity.objects.get(pk=first.entity.pk))
    assert not access.can_edit(lab["bob"], Entity.objects.get(pk=first.entity.pk))


@pytest.fixture
def policy(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Gives DNASequence another ``on_duplicate`` policy."""

    def use(name: str) -> None:
        form = values.shape("dna.DNASequence")
        changed = values.Shape(dataclasses.replace(form.entity_type, on_duplicate=name), form.parts)
        monkeypatch.setattr(services, "shape", lambda _: changed)

    return use


def test_warn_refuses_a_duplicate_unless_told_to_go_ahead(lab: dict[str, Any], policy: Any) -> None:
    policy("warn")
    first = dna(lab, "GGCC")
    with pytest.raises(services.Duplicate) as raised:
        dna(lab, "GGCC")
    assert [e.pk for e in raised.value.existing] == [first.entity.pk]
    second = services.create(
        lab["alice"], "dna.DNASequence", lab["first"], {"bases": "GGCC"}, allow_duplicate=True
    )
    assert not second.reused
    assert second.entity.pk != first.entity.pk


def test_allow_always_creates_another(lab: dict[str, Any], policy: Any) -> None:
    policy("allow")
    assert dna(lab, "GGCC").entity.pk != dna(lab, "GGCC").entity.pk


def test_an_archived_entity_isnt_reused(lab: dict[str, Any]) -> None:
    first = dna(lab, "GGCC")
    services.set_archived(lab["alice"], Entity.objects.get(pk=first.entity.pk), True)
    assert not dna(lab, "GGCC").reused


def test_archived_entities_cant_change_until_restored(lab: dict[str, Any]) -> None:
    entity = Entity.objects.get(pk=dna(lab).entity.pk)
    archived = services.set_archived(lab["alice"], entity, True, reason="Contaminated")
    assert archived.revision is not None
    assert (archived.revision.kind, archived.revision.reason) == ("archived", "Contaminated")
    with pytest.raises(Refused):
        services.update(lab["alice"], entity, {"name": "x"}, expected_revision=2)
    # An archived entity isn't reused for a new duplicate.
    assert not dna(lab).reused
    restored = services.set_archived(lab["alice"], entity, False)
    assert restored.revision is not None
    assert restored.revision.number == 3
    services.update(lab["alice"], entity, {"name": "x"}, expected_revision=3)


def test_linking_is_audited_and_the_owner_project_cant_be_unlinked(lab: dict[str, Any]) -> None:
    entity = Entity.objects.get(pk=dna(lab).entity.pk)
    assert services.link(lab["alice"], entity, lab["second"])
    assert not services.link(lab["alice"], entity, lab["second"])
    assert services.unlink(lab["alice"], entity, lab["second"])
    assert not services.unlink(lab["alice"], entity, lab["second"])
    with pytest.raises(Refused):
        services.unlink(lab["alice"], entity, lab["first"])
    audited = AuditEvent.objects.filter(target_id=str(entity.pk)).order_by("pk")
    assert [(e.action, e.project_id, e.actor_id) for e in audited] == [
        ("entity.linked", lab["second"].pk, lab["alice"].pk),
        ("entity.unlinked", lab["second"].pk, lab["alice"].pk),
    ]


def test_extension_fields_get_their_own_row_and_history(lab: dict[str, Any]) -> None:
    from iki_server.files.models import Blob, File

    blob = Blob.objects.create(sha256="a" * 64, size=1, storage_uri="s3://b/a")
    pdb = File.objects.create(
        blob=blob, name="a.pdb", media_type="chemical/x-pdb", project=lab["first"]
    )
    from iki_plugin_protein.models import ProteinStructure  # type: ignore[import-not-found]

    structure = ProteinStructure.objects.create(owner_project=lab["first"], structure=pdb)
    ProjectLink.objects.create(entity_id=structure.pk, project=lab["first"])
    Entity.objects.filter(pk=structure.pk).update(revision=1)
    entity = Entity.objects.get(pk=structure.pk)
    with pytest.raises(services.Invalid) as raised:
        services.update(lab["alice"], entity, {"plddt_mean": 90}, expected_revision=1)
    assert raised.value.errors == {"plddt_mean": ["is computed: only actions set it."]}
    saved = services.update(lab["alice"], entity, {"ranking": {"rank": 1}}, expected_revision=1)
    assert saved.revision is not None
    assert saved.revision.changed_fields == ["ranking"]
    assert services.current_values(saved.entity)["ranking"] == {"rank": 1}
    assert services.values_at(entity, 2)["ranking"] == {"rank": 1}
    assert services.values_at(entity, 1)["ranking"] is None
    with pytest.raises(services.Invalid) as raised:
        services.update(lab["alice"], entity, {"ranking": {"rank": "first"}}, expected_revision=2)
    assert list(raised.value.errors) == ["ranking"]


def test_revisions_cant_be_changed_by_anyone(lab: dict[str, Any]) -> None:
    dna(lab)
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("UPDATE iki_revision SET reason = 'rewritten'")
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("DELETE FROM iki_revision")


def test_datetimes_need_a_time_zone() -> None:
    from iki_server.entities.values import Refs, to_db
    from iki_spec.model import Field

    f = Field(name="at", type="datetime")
    assert to_db(f, "2026-03-01T09:30:00+01:00", Refs(Entity.objects.none())) == dt.datetime(
        2026, 3, 1, 8, 30, tzinfo=dt.UTC
    )
    with pytest.raises(ValueError, match="time zone"):
        to_db(f, "2026-03-01T09:30:00", Refs(Entity.objects.none()))


def test_values_of_many_entities_match_each_ones_values(lab: dict[str, Any]) -> None:
    first = dna(lab, "AAAA", name="a")
    second = dna(lab, "CCCC")
    protein = services.create(
        lab["alice"],
        "protein.Protein",
        lab["first"],
        {"sequence": "MKV", "encoded_by": str(first.entity.pk), "uniprot_ids": ["P12345"]},
    )
    entities = list(Entity.objects.filter(pk__in=[s.entity.pk for s in (first, second, protein)]))
    many = services.current_values_of(entities)
    assert set(many) == {e.pk for e in entities}
    for entity in entities:
        assert many[entity.pk] == services.current_values(services._specific(entity))
    assert many[protein.entity.pk]["encoded_by"] == first.entity.pk
