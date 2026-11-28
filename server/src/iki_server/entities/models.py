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
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import (
    ArrayField,
    DateTimeRangeField,
    DecimalRangeField,
    RangeOperators,
)
from django.contrib.postgres.indexes import GinIndex
from django.db import models
from django.db.models import Q
from django.db.models.functions import Coalesce, Now

from iki_server.core.ids import uuid7
from iki_server.entities import hrids, identity, registry
from iki_server.org.models import Project

#: A qualified type name, ``plugin.Type``.
TYPE_PATTERN = r"^[a-z][a-z0-9]*(_[a-z0-9]+)*/[a-z][a-z0-9]*(_[a-z0-9]+)*\.[A-Z][A-Za-z0-9]*$"
#: The constraint a second record in a project breaks with a ``unique`` rule's values
#: (ADR 0127), a unique field's among them (ADR 0082).
UNIQUE_CONSTRAINT = "iki_unique_value_once"
#: The one a second entry in a registry breaks with a ``within: registry`` rule's (ADR 0115).
UNIQUE_IN_REGISTRY = "iki_unique_value_once_in_registry"
#: The unique indexes a second option of a lab list in a registry with a label in another
#: case breaks start with this (ADR 0270), one for each list.
LIST_LABEL_INDEX = "iki_list_label__"
#: The constraints two of a project's records break whose ranges a rule says mustn't
#: overlap (ADR 0127): of dates and datetimes, and of numbers.
RANGE_CONSTRAINT = "iki_range_value_apart"
SPAN_CONSTRAINT = "iki_range_value_span_apart"
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
    #: A registry entry's SHA-256 of its type's identity fields, by which it's found. An
    #: entry superseded by one of the same identity as another type (ADR 0165) has handed
    #: it to its replacement, which registering that identity finds now.
    identity_hash = models.CharField(max_length=64, null=True, blank=True)
    #: The latest revision's number. Edits name it, so two people's edits can't cross.
    revision = models.PositiveIntegerField(default=0)
    #: The state fields whose states lock it (ADR 0125): while any does, its other fields
    #: can't change. Kept with the fields, which are in its type's tables; triggers on the
    #: tables holding its values refuse changes while it's locked.
    locked_by = ArrayField(
        models.CharField(max_length=50),
        default=list,
        db_default=models.Value([], output_field=ArrayField(models.CharField(max_length=50))),
        db_comment="The state fields whose states lock it (ADR 0125).",
    )
    #: Remains true after reopening, including when a plugin migration changes its lock.
    has_been_locked = models.BooleanField(default=False, db_default=False)
    #: A signed result delivered to its requester holds this reviewed revision for good.
    released_review_revision = models.PositiveIntegerField(null=True, blank=True)
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
            # When records changed, of every type: the lists' default order, newest first, which
            # names no type (ADR 0176). Read backwards, a page reads its rows and no more.
            models.Index(fields=["updated_at", "id"], name="iki_entity_changed"),
            # A type's entities by ID, as a page sorted by it reads them (ADR 0103). Records
            # share their entry's, so neither index of IDs below orders them all.
            models.Index(fields=["type", "hrid", "id"], name="iki_entity_hrid_sorted"),
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
                    # An entry's identity is gone only once its plugin's purged, or once it
                    # was superseded by an entry of its identity as another type (ADR 0165).
                    (
                        Q(registry__isnull=False, owner_project__isnull=True, entry__isnull=True)
                        & (
                            Q(identity_hash__isnull=False)
                            | Q(purged_at__isnull=False)
                            | Q(superseded_by__isnull=False)
                        )
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
            models.CheckConstraint(
                condition=Q(released_review_revision__isnull=True)
                | (Q(revision=models.F("released_review_revision") + 2) & ~Q(locked_by=[])),
                name="iki_released_review_locked",
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
    def locked(self) -> bool:
        """Whether a state field locks it (ADR 0125): its other values can't change."""
        return bool(self.locked_by) or self.released_review_revision is not None

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
    # No index of their own: each leads one of the two below, which serve its lookups too.
    entity = models.ForeignKey(
        Entity, on_delete=models.PROTECT, related_name="links", db_index=False
    )
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="+", db_index=False)
    linked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+"
    )
    linked_at = models.DateTimeField(db_default=Now())
    #: The request it came with (ADR 0132): the request itself, sent to its service; a
    #: record it sends; or a result released back to the asking project. Readable there,
    #: but not passed on to other projects.
    via_request = models.ForeignKey(
        Entity,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        db_comment="The request it came with, if a request linked it here (ADR 0132).",
    )

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


class Request(models.Model):
    """A request its record's project sent to another project's service (ADR 0132).

    The asking project owns the record and its own fields; the service may change only
    the fields its type gives the service, and sees only what the request sends. Sent
    once, to one service, for good: withdrawing or declining it is its type's states'.
    """

    entity = models.OneToOneField(
        Entity, primary_key=True, on_delete=models.PROTECT, related_name="iki_request"
    )
    #: The project serving it, which it's linked into.
    service = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="+")
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    #: The agent that submitted it for them, if one did (ADR 0072).
    agent = models.ForeignKey(
        "iki_audit.Agent", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    submitted_at = models.DateTimeField(db_default=Now())
    #: The request's revision when it was sent.
    revision = models.PositiveIntegerField()

    class Meta:
        db_table = "iki_request"
        db_table_comment = (
            "Requests sent from their records' projects to other projects' services (ADR 0132)."
        )
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["service", "submitted_at"], name="iki_request_queue"),
        ]

    def __str__(self) -> str:
        return f"{self.entity_id} to {self.service_id}"


class Intake(models.Model):
    """What a project takes as a service (ADR 0132): the request types it accepts, and from
    which projects in its registry. Its Admins set it."""

    project = models.OneToOneField(
        Project, primary_key=True, on_delete=models.CASCADE, related_name="iki_intake"
    )
    #: The request types it accepts, by qualified name; their subtypes too.
    types = ArrayField(models.CharField(max_length=112), default=list, blank=True)
    #: Every project in its registry may ask; otherwise only those in ``sources``.
    from_registry = models.BooleanField(default=False)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+"
    )
    updated_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_intake"
        db_table_comment = "The request types each service project accepts (ADR 0132)."

    def __str__(self) -> str:
        return f"intake of {self.project_id}"


class IntakeSource(models.Model):
    """A project a service accepts requests from (ADR 0132)."""

    id = models.BigAutoField(primary_key=True)
    intake = models.ForeignKey(Intake, on_delete=models.CASCADE, related_name="sources")
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="+")

    class Meta:
        db_table = "iki_intake_source"
        db_table_comment = "The projects each service accepts requests from (ADR 0132)."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["intake", "project"], name="iki_intake_source_once"),
            models.CheckConstraint(
                condition=~Q(intake_id=models.F("project_id")), name="iki_intake_not_itself"
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["project"], name="iki_intake_source_by_project"),
        ]

    def __str__(self) -> str:
        return f"{self.project_id} asks {self.intake_id}"


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
    """A record's values under a ``unique`` rule (ADR 0127), a unique field's among them
    (ADR 0082): held once per project, or for a ``within: registry`` rule, once per
    registry (ADR 0115).

    Triggers on the plugin tables keep these rows, so whatever writes the values, a
    person, a run, a bulk import or SQL, a second record in the project (or entry in the
    registry) with the same ones is refused by the database. ``iki-server sync_plugins``
    installs the triggers and fills in rows for records already there.
    """

    id = models.BigAutoField(primary_key=True)
    #: A ``within: project`` rule's: the record's owner project, where its values are
    #: unique. Exactly one of this and ``registry`` is set: the rule's scope.
    project = models.ForeignKey(
        Project, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: A ``within: registry`` rule's: the entry's registry.
    registry = models.ForeignKey(
        "iki_org.Registry", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: The rule, as ``table:rule``, such as ``lims_protein:lims_id``.
    key = models.CharField(max_length=127)
    #: Its fields' values, as a JSON array of their text, such as ``["LIMS-1"]``.
    value = models.TextField()
    #: The SHA-256 of ``value``'s UTF-8, which is what's unique: any length of value fits
    #: the index.
    digest = models.BinaryField(max_length=32)
    entity = models.ForeignKey(Entity, on_delete=models.CASCADE, related_name="+")

    class Meta:
        db_table = "iki_unique_value"
        db_table_comment = (
            "Each record's values under each unique rule, once per project or registry. Kept "
            "by triggers on the plugin tables."
        )
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["project", "key", "digest"],
                condition=Q(project__isnull=False),
                name=UNIQUE_CONSTRAINT,
            ),
            models.UniqueConstraint(
                fields=["registry", "key", "digest"],
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
            models.Index(fields=["key", "digest"], name="iki_unique_value_lookup"),
        ]

    def __str__(self) -> str:
        return f"{self.key}={self.value}"


class RevisionKind(models.TextChoices):
    CREATED = "created", "Created"
    EDITED = "edited", "Edited"
    MIGRATED = "migrated", "Migrated"
    ARCHIVED = "archived", "Archived"
    RESTORED = "restored", "Restored"
    #: One of its state fields set to another state (ADR 0125).
    STATE_CHANGED = "state_changed", "State changed"
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
    # The database stamps every writer, including bulk imports and SQL migrations.
    history_seen = models.PositiveBigIntegerField(
        db_default=models.Func(function="iki_history_seen")
    )
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
    #: A record's first, of a registry entry its identity found (ADR 0138): the identity
    #: values as given, where they're spelled other than the entry's (a plasmid from
    #: another origin, a protein with its stop), so what was submitted is kept.
    submitted = models.JSONField(null=True, blank=True)
    #: For a state change (ADR 0125): the state field set, and the states it went from
    #: and to. A plugin's migration moving records out of a state it removed makes one by no
    #: one, ``via`` the system.
    state_field = models.CharField(max_length=50, blank=True, default="", db_default="")
    state_from = models.CharField(max_length=50, blank=True, default="", db_default="")
    state_to = models.CharField(max_length=50, blank=True, default="", db_default="")
    #: For a state set by signing for it (ADR 0126): the signatures that set it, in order.
    signatures = ArrayField(models.UUIDField(), default=list, blank=True, db_default=[])
    #: For a record that moved (ADR 0115): the superseded entry it held before. For a
    #: registry entry's first, when a registrar registered it in place of an entry of
    #: another type (ADR 0165): that entry.
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
            models.CheckConstraint(
                condition=~Q(kind="migrated") | Q(via="system", user__isnull=True, reason__gt=""),
                name="iki_revision_migration_reason",
            ),
            # A state change names its field and states; nothing else does.
            models.CheckConstraint(
                condition=Q(kind="state_changed", state_field__gt="", state_to__gt="")
                | (~Q(kind="state_changed") & Q(state_field="", state_from="", state_to="")),
                name="iki_revision_state",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            # What someone changed lately, newest first: what searching everything shows
            # before anything is typed (ADR 0176).
            models.Index(fields=["user", "id"], name="iki_revision_by_user"),
            # A record's state history, field by field: who set which state when.
            models.Index(
                fields=["entity", "state_field", "number"],
                name="iki_revision_states",
                condition=Q(kind="state_changed"),
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


class RangeValue(models.Model):
    """A record's range under a rule that no two of a project's records' ranges overlap
    (ADR 0127), such as a booking's time on an instrument.

    Triggers on the plugin tables keep these rows, as they keep unique values (ADR 0082),
    so the database refuses an overlap whatever writes it: a person, a run, a bulk import
    or SQL. ``iki-server sync_plugins`` installs the triggers and fills in ranges already
    there.
    """

    id = models.BigAutoField(primary_key=True)
    #: The record's owner project: ranges are apart within it.
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="+")
    #: The rule, as ``table:rule``.
    key = models.CharField(max_length=127)
    #: What the ranges are compared per, as a JSON array of its fields' values: the same
    #: instrument, say. ``[]`` when the rule compares all of a project's records.
    scope = models.CharField(max_length=600)
    #: A range of dates (from midnight UTC on the first to midnight after the last) or of
    #: datetimes: from where it starts, up to where it ends.
    during = DateTimeRangeField(null=True, blank=True)
    #: A range of numbers: from where it starts, up to where it ends.
    span = DecimalRangeField(null=True, blank=True)
    entity = models.ForeignKey(Entity, on_delete=models.CASCADE, related_name="+")

    class Meta:
        db_table = "iki_range_value"
        db_table_comment = (
            "Each record's range under a rule that ranges mustn't overlap (ADR 0127). Kept by "
            "triggers on the plugin tables."
        )
        constraints: ClassVar[list[models.BaseConstraint]] = [
            ExclusionConstraint(
                name=RANGE_CONSTRAINT,
                expressions=[
                    ("project", RangeOperators.EQUAL),
                    ("key", RangeOperators.EQUAL),
                    ("scope", RangeOperators.EQUAL),
                    ("during", RangeOperators.OVERLAPS),
                ],
                condition=Q(during__isnull=False),
            ),
            ExclusionConstraint(
                name=SPAN_CONSTRAINT,
                expressions=[
                    ("project", RangeOperators.EQUAL),
                    ("key", RangeOperators.EQUAL),
                    ("scope", RangeOperators.EQUAL),
                    ("span", RangeOperators.OVERLAPS),
                ],
                condition=Q(span__isnull=False),
            ),
            models.UniqueConstraint(fields=["entity", "key"], name="iki_range_value_entity"),
            models.CheckConstraint(
                condition=Q(during__isnull=False) | Q(span__isnull=False),
                name="iki_range_value_range",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.key} {self.scope}"


class RuleHold(models.Model):
    """A record held by a rule a save of it broke (ADR 0127): the save was kept, with
    why, as a result out of its specification must be, and the record is held until
    someone with one of the rule's lab roles, other than whoever held it, reviews it.
    While it's held, it can't be set to a state that locks it.

    The rule's label, message and roles are kept as they were, so a plugin that drops
    the rule later doesn't leave a hold no one can review.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="holds")
    #: The rule's key among its type's rules (``Rule.key``).
    rule = models.CharField(max_length=160)
    label = models.CharField(max_length=100)
    #: What the save that held it was told.
    message = models.TextField()
    #: Qualified lab roles: someone with one of them, in the record's project, reviews it.
    roles = ArrayField(models.CharField(max_length=112))
    #: The revision that held it, and the latest that broke the rule while it was held.
    revision = models.PositiveIntegerField()
    last_revision = models.PositiveIntegerField()
    #: Who saved it, or None for a deploy's check of records saved before the rule.
    held_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+"
    )
    #: The agent that saved it for ``held_by``, if one did (ADR 0072).
    held_agent = models.ForeignKey(
        "iki_audit.Agent", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    held_at = models.DateTimeField(db_default=Now())
    #: Why it was saved anyway, as whoever saved it said, or as a run's results say.
    reason = models.TextField()
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_reason = models.TextField(blank=True, default="")

    class Meta:
        db_table = "iki_rule_hold"
        db_table_comment = (
            "Records held by rules their saves broke (ADR 0127), and who reviewed each, when "
            "and why."
        )
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # One open hold of a rule on a record: breaking it again while it's held adds
            # to that one.
            models.UniqueConstraint(
                fields=["entity", "rule"],
                condition=Q(reviewed_at__isnull=True),
                name="iki_rule_hold_open",
            ),
            models.CheckConstraint(
                condition=Q(reviewed_at__isnull=True, reviewed_by__isnull=True)
                | Q(reviewed_at__isnull=False, reviewed_by__isnull=False),
                name="iki_rule_hold_review",
            ),
            models.CheckConstraint(condition=~Q(reason=""), name="iki_rule_hold_reason"),
            models.CheckConstraint(
                condition=Q(reviewed_at__isnull=True) | ~Q(review_reason=""),
                name="iki_rule_hold_review_reason",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["entity"],
                name="iki_rule_hold_held",
                condition=Q(reviewed_at__isnull=True),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.entity_id} held by {self.rule}"


class RuleHoldOccurrence(models.Model):
    """The evidence of one failing observation, preserved for the hold's review."""

    APPEND_ONLY = True

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    hold = models.ForeignKey(RuleHold, on_delete=models.PROTECT, related_name="occurrences")
    revision = models.PositiveIntegerField()
    entry = models.ForeignKey(Entity, null=True, on_delete=models.PROTECT, related_name="+")
    entry_revision = models.PositiveIntegerField(null=True)
    label = models.CharField(max_length=100)
    message = models.TextField(null=True)
    reason = models.TextField(null=True)
    held_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+"
    )
    held_agent = models.ForeignKey(
        "iki_audit.Agent", null=True, on_delete=models.PROTECT, related_name="+"
    )
    held_at = models.DateTimeField(null=True, db_default=Now())
    #: Only old aggregated failures had a gap in their evidence. This is when it closed.
    legacy_gap_ended_at = models.DateTimeField(null=True)

    class Meta:
        db_table = "iki_rule_hold_occurrence"
        db_table_comment = "Each failing observation that a rule held, and why it was kept."
        ordering = ("revision", Coalesce("held_at", "legacy_gap_ended_at"), "id")


class RuleBreach(models.Model):
    """A record that breaks a rule it was saved before (ADR 0127): one its plugin added
    or changed later, one a bundle brought from another deployment, or one a change to its
    registry entry's values broke (ADR 0115). It's reported, not refused, until a save that
    checks the rule finds it kept."""

    id = models.BigAutoField(primary_key=True)
    entity = models.ForeignKey(Entity, on_delete=models.CASCADE, related_name="breaches")
    rule = models.CharField(max_length=160)
    label = models.CharField(max_length=100)
    message = models.TextField()
    found_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_rule_breach"
        db_table_comment = "Records that break a rule added or changed after they were saved."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["entity", "rule"], name="iki_rule_breach_once"),
        ]

    def __str__(self) -> str:
        return f"{self.entity_id} breaks {self.rule}"


class RulesChecked(models.Model):
    """What a type's records were last checked against by a deploy (ADR 0127): a digest of
    its rules and its plugins' versions. A deploy that changes it checks them again."""

    type = models.CharField(max_length=160, primary_key=True)
    digest = models.CharField(max_length=64)
    checked_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_rules_checked"
        db_table_comment = "What each type's records were last checked against (ADR 0127)."

    def __str__(self) -> str:
        return f"{self.type} {self.digest[:12]}"


class RecordExportState(models.TextChoices):
    QUEUED = "queued", "Queued"
    WRITING = "writing", "Writing"
    READY = "ready", "Ready"
    FAILED = "failed", "Failed"
    #: Its file was deleted when it expired; the record of it stays.
    EXPIRED = "expired", "Expired"


class RecordExport(models.Model):
    """Many records as one file of a format each exports (ADR 0229), asked for by someone
    who can read them all: each at the revision it had when they asked, written in the
    background, and kept for them to download for a week."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    agent = models.ForeignKey(
        "iki_audit.Agent", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    via = models.CharField(max_length=16)
    #: Iki's format, or a plugin's writer by its qualified ID (ADR 0230).
    format = models.CharField(max_length=200)
    #: What was picked, in words: "12 plasmids picked", or the search.
    selection = models.TextField()
    #: The search it's from, as given, if it's a table's; None for records picked.
    search = models.JSONField(null=True, blank=True)
    #: Each record, in the file's order, at the revision it had when asked for:
    #: [[its UUID, its HRID, its revision], …].
    records = models.JSONField()
    #: The fields an SDF carries as data, in order; empty for other formats.
    fields = ArrayField(models.CharField(max_length=100), default=list, blank=True)
    #: The projects its records belong to, so a legal hold on one keeps its file.
    projects = ArrayField(models.UUIDField(), default=list, blank=True)
    #: A project its requester read each record through, when they asked: it's theirs to
    #: see while they can read each of these (ADR 0152).
    read_in = ArrayField(models.UUIDField(), default=list, blank=True)
    #: Every record read to write it, its own and what they refer to: whoever downloads it
    #: must still be able to read them all.
    references = ArrayField(models.UUIDField(), null=True, blank=True)
    #: The limits of the token it was asked for with (ADR 0152), which writing it keeps
    #: to in the background as it would while the request waited; None when it was asked
    #: for in the browser.
    limits = models.JSONField(null=True, blank=True)
    state = models.CharField(
        max_length=16, choices=RecordExportState.choices, default=RecordExportState.QUEUED
    )
    #: Written by a background task, which tries again if its runner stops; not while its
    #: request waits, where nothing would if the request's process stopped partway.
    background = models.BooleanField(default=False)
    #: Why it failed, said to the person who asked.
    error = models.TextField(blank=True, default="")
    #: How many of its records are written so far.
    done = models.PositiveIntegerField(default=0)
    name = models.CharField(max_length=200, db_comment="The file's name.")
    sha256 = models.CharField(max_length=64, blank=True, default="")
    size = models.BigIntegerField(null=True, blank=True, db_comment="Bytes.")
    #: What the file couldn't hold, by each record's HRID, as the file says it.
    notes = models.JSONField(default=dict, blank=True)
    #: Records its writer refused, by HRID, and why: it failed, so no file has fewer
    #: records than were asked for.
    refused = models.JSONField(default=dict, blank=True)
    #: Which code wrote it: a built-in writer's name and Iki's version (ADR 0230).
    writer = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(db_default=Now())
    finished_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "iki_record_export"
        db_table_comment = "Many records asked for as one file, such as FASTA or SDF."
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["requested_by", "-created_at"], name="iki_record_export_by"),
            models.Index(
                fields=["expires_at"],
                name="iki_record_export_expiring",
                condition=Q(state="ready"),
            ),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(state__in=RecordExportState.values), name="iki_record_export_state"
            ),
            models.CheckConstraint(
                condition=~Q(state="ready") | Q(sha256__regex=r"^[0-9a-f]{64}$"),
                name="iki_record_export_ready_sha256",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name}"[:254]
