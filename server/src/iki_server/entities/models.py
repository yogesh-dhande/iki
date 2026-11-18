"""The core entity row, its revisions, and the projects it belongs to (ADR 0007, ADR 0035).

Every entity, of any type, has one ``Entity`` row: its identity and
bookkeeping. Plugin types are generated models that extend it with
multi-table inheritance, so a protein is an ``iki_entity`` row joined to a
``protein_protein`` row by ``entity_id``.

A type with an identity has **registry entries** and **project records**
(ADR 0115). An entry is an ``iki_entity`` row with a registry and no project,
joined to its type's entry table (``iki__protein_protein__entry``): its identity
and what's derived from it or curated about it, with its ID (``PRT-000123``).
A record is a row in a project, joined to the type's own table: the project's
data about the entry, which it names, with the entry's ID. A type without an
identity has records only. References to a type with an identity hold entries.

Every change to an entity is a numbered :class:`Revision`: who made it, how,
when, why, and which fields it changed. The values at each revision are in
the plugin tables' history tables. Both are append-only.
"""

from typing import Any, ClassVar

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.db import models
from django.db.models import Q
from django.db.models.functions import Now

from iki_server.core.ids import uuid7
from iki_server.entities import hrids, identity, registry
from iki_server.org.models import Project

#: A qualified type name, ``plugin.Type``.
TYPE_PATTERN = r"^[a-z][a-z0-9]*(_[a-z0-9]+)*/[a-z][a-z0-9]*(_[a-z0-9]+)*\.[A-Z][A-Za-z0-9]*$"
#: The constraint a second entity in a project with a unique field's value breaks.
UNIQUE_CONSTRAINT = "iki_unique_value_once"
#: The one a second entry in a registry with a unique fact's value breaks (ADR 0115).
UNIQUE_IN_REGISTRY = "iki_unique_value_once_in_registry"
#: ``PRT-000123``: a type's prefix and a number, zero-padded to the type's digits.
HRID_PATTERN = r"^[A-Z0-9]{2,8}-[0-9]{3,}$"


class Entity(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    #: The entity's own type, the most specific one, such as ``iki/protein.Protein``.
    type = models.CharField(
        max_length=112, db_comment="The entity's type, such as iki/protein.Protein."
    )
    hrid = models.CharField(
        max_length=32,
        db_comment="Human-readable ID, such as PRT-000123. A record of an entry has the entry's.",
    )
    #: The project a record belongs to, which decides its changes. An entry has none.
    owner_project = models.ForeignKey(
        Project, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: A registry entry's registry (ADR 0115). Records have none: theirs is their project's.
    registry = models.ForeignKey(
        "iki_org.Registry",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        db_comment="For a registry entry: its registry (ADR 0115).",
    )
    #: A project's record of a registry entry: the entry (ADR 0115).
    entry = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="records",
        db_comment="For a project's record of a registry entry: the entry (ADR 0115).",
    )
    #: A registry entry a registrar superseded: the entry that replaced it (ADR 0115).
    superseded_by = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    superseded_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(db_default=Now())
    archived_at = models.DateTimeField(null=True, blank=True)
    #: A registry entry's SHA-256 of its type's identity fields, by which it's found.
    identity_hash = models.CharField(max_length=64, null=True, blank=True)
    #: The latest revision's number. Edits name it, so two people's edits can't cross.
    revision = models.PositiveIntegerField(default=0)
    #: Its state locks it (ADR 0118): nothing changes its values until a transition takes
    #: it out. Kept with its state, which is in its type's table; triggers on the tables
    #: holding its values refuse changes while it's set.
    locked = models.BooleanField(
        default=False,
        db_default=False,
        db_comment="Its state locks it (ADR 0118): its values can't change while this is set.",
    )
    updated_at = models.DateTimeField(db_default=Now())
    #: Its plugin's data was purged (ADR 0092): this row is its tombstone, and its values
    #: are gone. It's in no list or search, and reading it says so.
    purged_at = models.DateTimeField(
        null=True, blank=True, db_comment="When its plugin's data was purged (ADR 0092)."
    )

    class Meta:
        db_table = "iki_entity"
        db_table_comment = "Every entity, of every type. Each type's own table joins on entity_id."
        verbose_name_plural = "entities"
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["type", "created_at"], name="iki_entity_type"),
            models.Index(fields=["type", "updated_at", "id"], name="iki_entity_updated"),
            GinIndex(fields=["hrid"], opclasses=["gin_trgm_ops"], name="iki_entity_hrid_trgm"),
            models.Index(
                fields=["hrid"], name="iki_entity_record_hrid", condition=Q(entry__isnull=False)
            ),
            models.Index(
                fields=["entry"], name="iki_entity_records", condition=Q(entry__isnull=False)
            ),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(condition=Q(type__regex=TYPE_PATTERN), name="iki_entity_type"),
            models.CheckConstraint(condition=Q(hrid__regex=HRID_PATTERN), name="iki_entity_hrid"),
            models.CheckConstraint(
                condition=Q(identity_hash__regex=r"^[0-9a-f]{64}$"),
                name="iki_entity_identity_hash",
            ),
            # An entry has a registry and an identity, and no project; a record has a
            # project, and no registry or identity of its own.
            models.CheckConstraint(
                condition=(
                    # An entry's identity is gone only once its plugin's purged.
                    (
                        Q(registry__isnull=False, owner_project__isnull=True, entry__isnull=True)
                        & (Q(identity_hash__isnull=False) | Q(purged_at__isnull=False))
                    )
                    | Q(
                        registry__isnull=True,
                        owner_project__isnull=False,
                        identity_hash__isnull=True,
                    )
                ),
                name="iki_entity_entry_or_record",
            ),
            models.CheckConstraint(
                condition=Q(superseded_by__isnull=True, superseded_at__isnull=True)
                | Q(
                    registry__isnull=False, superseded_by__isnull=False, superseded_at__isnull=False
                ),
                name="iki_entity_superseded",
            ),
            # An ID names one entry, or one record without an entry. Records of an entry
            # share its ID.
            models.UniqueConstraint(
                fields=["hrid"], condition=Q(entry__isnull=True), name="iki_entity_hrid_once"
            ),
            # One entry per identity in a registry.
            models.UniqueConstraint(
                fields=["registry", "identity_hash"],
                condition=Q(registry__isnull=False),
                name="iki_entity_identity_once",
            ),
            # One record of an entry in a project.
            models.UniqueConstraint(
                fields=["owner_project", "entry"],
                condition=Q(entry__isnull=False),
                name="iki_entity_record_once",
            ),
        ]

    def __str__(self) -> str:
        return self.hrid

    @property
    def is_entry(self) -> bool:
        """Whether it's a registry entry, rather than a project's record (ADR 0115)."""
        return self.registry_id is not None

    def save(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401 - Django's signature
        """A new entity of a plugin type gets its type and HRID here, and a new registry
        entry its identity hash. A record of an entry has the entry's HRID."""
        model = type(self)
        iki_type = getattr(model, "IKI_TYPE", None)
        if self._state.adding and iki_type is not None:
            entering = getattr(model, "IKI_ENTRY", False)
            if not entering or not self.type:
                entity_type = registry.types()[iki_type]
                if entity_type.abstract:
                    raise ValueError(f"{iki_type} is abstract: create one of its subtypes.")
                if self.type and self.type != iki_type:
                    raise ValueError(f"A {iki_type} can't have the type {self.type}.")
                self.type = iki_type
            # An entry's table is its identity's type's, which subtypes share.
            entity_type = registry.types()[self.type]
            if not self.hrid:
                self.hrid = hrids.next_hrid(entity_type)
            if entering and self.identity_hash is None:
                self.identity_hash = identity.hash_for(self, entity_type)
        super().save(*args, **kwargs)


class ProjectLink(models.Model):
    """A project an entity belongs to. Its owner project has one too.

    Reading an entity takes a role in any of its projects. Changing it takes
    Editor in its owner project. An entity reused from another project (a
    duplicate) is linked into the new one rather than copied.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="links")
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="+")
    linked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+"
    )
    linked_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_entity_project"
        db_table_comment = "The projects each entity belongs to, its owner project included."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["entity", "project"], name="iki_entity_project_once"),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["project", "entity"], name="iki_entity_project_by_project"),
        ]

    def __str__(self) -> str:
        return f"{self.entity_id} in {self.project_id}"


class AppDraft(models.Model):
    """An app's spec to look at before saving it (ADR 0090): what an agent drafts for its
    person, say. Only the person it's for can open it, and only until it expires. It's
    never an entity, has no history, and nothing points to it."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="+")
    #: The person it's for, whose reading it shows and who alone can open it.
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+"
    )
    #: The agent that drafted it for them, if one did (ADR 0072).
    agent = models.ForeignKey(
        "iki_audit.Agent", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    name = models.CharField(max_length=200)
    spec = models.JSONField()
    #: For an app about an entity, the one it's shown on.
    about = models.ForeignKey(
        Entity, null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    created_at = models.DateTimeField(db_default=Now())
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        db_table = "iki_app_draft"
        db_table_comment = "Apps' specs to look at before saving, each for one person, for a day."
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["created_by", "created_at"], name="iki_app_draft_by"),
        ]


class ProjectDashboard(models.Model):
    """The app a project's page opens with (ADR 0090), chosen by its Admins from its own
    apps. The app shows what each person viewing it may read, as it does anywhere."""

    project = models.OneToOneField(
        Project, on_delete=models.CASCADE, primary_key=True, related_name="+"
    )
    app = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    set_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+"
    )
    set_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_project_dashboard"
        db_table_comment = "The app each project's page opens with, chosen by its Admins."

    def __str__(self) -> str:
        return f"{self.app_id} on {self.project_id}"


class UniqueValue(models.Model):
    """A value of a unique field (ADR 0082), held once per project, or for a registry
    fact, once per registry (ADR 0115).

    Triggers on the plugin tables keep these rows, so whatever writes a value,
    a person, a run, a bulk import or SQL, a second entity in the project (or
    entry in the registry) with the same value is refused by the database.
    ``iki-server sync_plugins`` installs the triggers and fills in values
    already there.
    """

    id = models.BigAutoField(primary_key=True)
    #: A record's owner project: values are unique within it.
    project = models.ForeignKey(
        Project, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: A registry entry's registry: values are unique within it.
    registry = models.ForeignKey(
        "iki_org.Registry", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: The field, as ``table.column``, such as ``lims_protein.lims_id``.
    key = models.CharField(max_length=127)
    value = models.CharField(max_length=600)
    entity = models.ForeignKey(Entity, on_delete=models.CASCADE, related_name="+")

    class Meta:
        db_table = "iki_unique_value"
        db_table_comment = (
            "Each unique field's values, once per project. Kept by triggers on the plugin tables."
        )
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["project", "key", "value"],
                condition=Q(project__isnull=False),
                name=UNIQUE_CONSTRAINT,
            ),
            models.UniqueConstraint(
                fields=["registry", "key", "value"],
                condition=Q(registry__isnull=False),
                name=UNIQUE_IN_REGISTRY,
            ),
            models.UniqueConstraint(fields=["entity", "key"], name="iki_unique_value_entity"),
            models.CheckConstraint(
                condition=Q(project__isnull=False, registry__isnull=True)
                | Q(project__isnull=True, registry__isnull=False),
                name="iki_unique_value_scope",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["key", "value"], name="iki_unique_value_lookup"),
        ]

    def __str__(self) -> str:
        return f"{self.key}={self.value}"


class RevisionKind(models.TextChoices):
    CREATED = "created", "Created"
    EDITED = "edited", "Edited"
    ARCHIVED = "archived", "Archived"
    RESTORED = "restored", "Restored"
    #: Moved from one state to another (ADR 0118).
    TRANSITIONED = "transitioned", "Transitioned"
    #: A registrar changed a registry entry's facts (ADR 0115).
    CURATED = "curated", "Curated"
    #: A registry entry a registrar superseded with another (ADR 0115).
    SUPERSEDED = "superseded", "Superseded"
    #: A record moved from a superseded entry to the one that replaced it.
    MOVED = "moved", "Moved"


class Revision(models.Model):
    """One change to an entity. Append-only, for every role (ADR 0035).

    A field's provenance is the latest revision that changed it: who or what
    set its current value, how, and when.
    """

    APPEND_ONLY = True

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="revisions")
    #: 1 for the entity's creation, then 2, 3, ...
    number = models.PositiveIntegerField()
    kind = models.CharField(max_length=16, choices=RevisionKind.choices)
    #: The fields whose values this revision set.
    changed_fields = ArrayField(models.CharField(max_length=63), default=list, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+"
    )
    #: How the change was made: web, api, mcp, cli or system (as in the audit log), or run
    #: for a run's results (its RunEffect names the run).
    via = models.CharField(max_length=16)
    #: The OAuth app it came through, if any, as the audit log names it: an MCP client,
    #: say, acting for ``user``.
    client = models.CharField(max_length=255, blank=True, default="")
    #: The agent that made it for ``user``, if one did (ADR 0072).
    agent = models.ForeignKey(
        "iki_audit.Agent", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: Why, if the person said.
    reason = models.TextField(blank=True, default="")
    #: For a transition (ADR 0118): its name, or ``iki.moved`` for a plugin's migration
    #: moving records out of a state it removed; and the states it went from and to.
    transition = models.CharField(max_length=63, blank=True, default="", db_default="")
    state_from = models.CharField(max_length=50, blank=True, default="", db_default="")
    state_to = models.CharField(max_length=50, blank=True, default="", db_default="")
    #: For a record that moved (ADR 0115): the superseded entry it held before.
    moved_from = models.ForeignKey(
        Entity, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: For a registry entry's revision (ADR 0115): the project it was made through, by
    #: registering the entry or filling in its values. Only that project's members see who
    #: made it and when. None for a registrar's.
    project = models.ForeignKey(
        Project, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_revision"
        db_table_comment = "Every change to every entity: who, how, when, why, and which fields."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["entity", "number"], name="iki_revision_number"),
            models.CheckConstraint(condition=Q(number__gte=1), name="iki_revision_number_min"),
            models.CheckConstraint(
                condition=Q(kind__in=RevisionKind.values), name="iki_revision_kind"
            ),
            # A transition names itself and its states; nothing else does.
            models.CheckConstraint(
                condition=Q(kind="transitioned", transition__gt="", state_to__gt="")
                | (~Q(kind="transitioned") & Q(transition="", state_from="", state_to="")),
                name="iki_revision_transition",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            # Separation of duties reads who made a record's latest transitions.
            models.Index(
                fields=["entity", "transition", "number"],
                name="iki_revision_transitions",
                condition=Q(kind="transitioned"),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.entity_id} r{self.number}"


class Tombstone(models.Model):
    """What a purge keeps of an entity (ADR 0092), besides its core row: the purge that
    deleted it, and a SHA-256 of its last values, which proves what existed without
    keeping it. Only ever added to."""

    APPEND_ONLY = True

    entity = models.OneToOneField(
        Entity, primary_key=True, on_delete=models.PROTECT, related_name="tombstone"
    )
    purge = models.ForeignKey("iki_plugins.PluginPurge", on_delete=models.PROTECT, related_name="+")
    #: Of its last revision's values, as canonical JSON: see ``plugins.purge.fingerprint``.
    content_sha256 = models.CharField(max_length=64)
    revision = models.PositiveIntegerField()
    purged_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_tombstone"
        db_table_comment = "What a purge keeps of each entity it deleted (ADR 0092)."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(content_sha256__regex=r"^[0-9a-f]{64}$"), name="iki_tombstone_sha256"
            ),
        ]

    def __str__(self) -> str:
        return f"Tombstone of {self.entity_id}"
