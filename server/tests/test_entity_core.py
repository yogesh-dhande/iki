"""Entities: creating, editing, revisions and history, provenance, duplicates, archiving."""

import datetime as dt
from typing import Any

import pytest
from django.db import connection, transaction
from django.db.utils import DatabaseError

from iki_server.accounts.models import User
from iki_server.audit import log
from iki_server.audit.models import AuditEvent, Via
from iki_server.entities import access, services
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
        lab["alice"], "acme/dna.DNASequence", lab["first"], {"bases": bases, **values}
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
    # Defaults count as set: topology defaults to linear. Its bases are its registry
    # entry's (ADR 0115), set by the entry's first revision.
    assert revision.changed_fields == ["name", "topology"]
    assert saved.entry_revision is not None
    assert saved.entry_revision.changed_fields == ["bases"]
    assert revision.user == lab["alice"]
    assert services.current_values(entity) == {
        "name": "pUC19",
        "description": None,
        "organism": None,
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
        "nope": ["acme/dna.DNASequence has no field `nope`."],
        "topology": ["must be one of: `linear`, `circular`."],
    }
    with pytest.raises(services.Invalid) as raised:
        services.create(lab["alice"], "acme/dna.DNASequence", lab["first"], {"gc_content": 50})
    assert raised.value.errors == {
        "gc_content": ["is computed: only actions set it."],
        "bases": ["is required."],
    }
    with pytest.raises(services.Invalid) as raised:
        services.create(lab["alice"], "acme/dna.Sequence", lab["first"], {"name": "x"})
    assert raised.value.errors == {
        "type": ["acme/dna.Sequence is abstract: create one of its subtypes."]
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
        "acme/protein.Protein",
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
    # A reference holds the registry entry (ADR 0115).
    assert values["encoded_by"] == sequence.entry_id  # type: ignore[attr-defined]
    assert values["uniprot_ids"] == ["P69905"]
    with pytest.raises(services.Invalid) as raised:
        services.create(
            lab["alice"],
            "acme/protein.Protein",
            lab["first"],
            {"sequence": "MKV", "temperature_optimum": {"value": 1, "unit": "g"}},
        )
    assert "measure different things" in raised.value.errors["temperature_optimum"][0]


def test_references_only_reach_what_the_caller_can_read(lab: dict[str, Any]) -> None:
    hidden = services.create(lab["bob"], "acme/dna.DNASequence", lab["second"], {"bases": "TTTT"})
    # Alice is a Viewer in the second project: she reads its record, but a reference
    # holds the registry entry (ADR 0115), which her project must have first.
    with pytest.raises(services.Invalid) as raised:
        services.create(
            lab["alice"],
            "acme/protein.Protein",
            lab["first"],
            {"sequence": "MKV", "encoded_by": str(hidden.entity.pk)},
        )
    assert raised.value.errors == {
        "encoded_by": [
            f"refers to {hidden.entity.hrid}, which isn't in Antibodies: add it to the "  # type: ignore[attr-defined]
            "project first, by registering it there."
        ]
    }
    services.create(lab["alice"], "acme/dna.DNASequence", lab["first"], {"bases": "TTTT"})
    made = services.create(
        lab["alice"],
        "acme/protein.Protein",
        lab["first"],
        {"sequence": "MKV", "encoded_by": str(hidden.entity.pk)},
    )
    assert services.current_values(made.entity)["encoded_by"] == hidden.entity.entry_id  # type: ignore[attr-defined]
    with pytest.raises(services.Invalid) as raised:
        services.create(
            lab["carol"],
            "acme/protein.Protein",
            lab["first"],
            {"sequence": "MKVA", "encoded_by": str(hidden.entity.pk)},
        )
    assert raised.value.errors == {"encoded_by": ["refers to no entity you can read."]}
    protein = services.create(
        lab["alice"], "acme/protein.Protein", lab["first"], {"sequence": "MK"}
    )
    with pytest.raises(services.Invalid) as raised:
        services.create(
            lab["alice"],
            "acme/protein.Protein",
            lab["first"],
            {"sequence": "MKVL", "encoded_by": str(protein.entity.pk)},
        )
    assert raised.value.errors == {
        "encoded_by": ["must refer to a acme/dna.DNASequence, not a acme/protein.Protein."]
    }


def test_registering_again_gives_each_project_its_own_record(lab: dict[str, Any]) -> None:
    first = dna(lab, "ACGTACGT")
    assert first.registered and not first.reused
    again = services.create(
        lab["bob"], "acme/dna.DNASequence", lab["second"], {"bases": "ACGTACGT"}
    )
    # The same registry entry, and its ID; a record of the second project's own.
    assert not again.reused and not again.registered
    assert again.entity.pk != first.entity.pk
    mine, theirs = Entity.objects.get(pk=first.entity.pk), Entity.objects.get(pk=again.entity.pk)
    assert theirs.entry_id == mine.entry_id is not None
    assert theirs.hrid == mine.hrid
    # Bob can't read the first project's record, nor change it.
    assert not access.can_read(lab["bob"], Entity.objects.get(pk=first.entity.pk))
    # Registering it again where it has a record is that record, as it is.
    reused = dna(lab, "ACGTACGT")
    assert reused.reused and reused.entity.pk == first.entity.pk
    assert reused.revision is None


def test_an_archived_record_comes_back_archived(lab: dict[str, Any]) -> None:
    first = dna(lab, "GGCC")
    services.set_archived(lab["alice"], Entity.objects.get(pk=first.entity.pk), True)
    again = dna(lab, "GGCC")
    assert again.reused and again.entity.pk == first.entity.pk
    assert Entity.objects.get(pk=again.entity.pk).archived_at is not None


def test_archived_entities_cant_change_until_restored(lab: dict[str, Any]) -> None:
    entity = Entity.objects.get(pk=dna(lab).entity.pk)
    archived = services.set_archived(lab["alice"], entity, True, reason="Contaminated")
    assert archived.revision is not None
    assert (archived.revision.kind, archived.revision.reason) == ("archived", "Contaminated")
    with pytest.raises(Refused):
        services.update(lab["alice"], entity, {"name": "x"}, expected_revision=2)
    # Registering it again is the same record, still archived.
    assert dna(lab).reused
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
    from scan_fixtures import clean_blob

    from iki_server.files.models import File

    blob = clean_blob(sha256="a" * 64, size=1, storage_uri="s3://b/a")
    pdb = File.objects.create(
        blob=blob, name="a.pdb", media_type="chemical/x-pdb", project=lab["first"]
    )
    from iki_plugin_acme__protein.models import ProteinStructure  # type: ignore[import-not-found]

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
        "acme/protein.Protein",
        lab["first"],
        {"sequence": "MKV", "encoded_by": str(first.entity.pk), "uniprot_ids": ["P12345"]},
    )
    entities = list(Entity.objects.filter(pk__in=[s.entity.pk for s in (first, second, protein)]))
    many = services.current_values_of(entities)
    assert set(many) == {e.pk for e in entities}
    for entity in entities:
        assert many[entity.pk] == services.current_values(services._specific(entity))
    assert many[protein.entity.pk]["encoded_by"] == first.entity.entry_id  # type: ignore[attr-defined]
