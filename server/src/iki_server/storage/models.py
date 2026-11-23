"""Where records are kept, and the ledger of how they moved (ADR 0180)."""

from typing import ClassVar

from django.conf import settings
from django.db import models
from django.db.models import Deferrable, F, Q
from django.db.models.functions import Now
from django.db.models.lookups import Exact

from iki_server.core.ids import uuid7
from iki_server.entities.models import Entity
from iki_server.org.models import Project


class Labels(models.TextChoices):
    LETTER_NUMBER = "letter_number", "A1 to H12"
    NUMBER = "number", "1 to 96"


class Order(models.TextChoices):
    BY_ROW = "by_row", "By row"
    BY_COLUMN = "by_column", "By column"


class Container(models.Model):
    """A container's shape, kept as it's first used: its type's grid, or its own fields',
    which can't change. Places in it are checked against it by the database."""

    APPEND_ONLY = True

    entity = models.OneToOneField(
        Entity, primary_key=True, on_delete=models.PROTECT, related_name="+"
    )
    #: None for a container without positions, such as a freezer or a shelf.
    rows = models.PositiveSmallIntegerField(null=True)
    columns = models.PositiveSmallIntegerField(null=True)
    labels = models.CharField(max_length=16, choices=Labels.choices, default=Labels.LETTER_NUMBER)
    order = models.CharField(max_length=16, choices=Order.choices, default=Order.BY_ROW)

    class Meta:
        db_table = "iki_storage_container"
        db_table_comment = "Each container's positions, as it was first used (ADR 0180)."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=(Q(rows__isnull=True) & Q(columns__isnull=True))
                | (Q(rows__gte=1, rows__lte=32) & Q(columns__gte=1, columns__lte=48)),
                name="iki_storage_container_grid",
            ),
        ]


class Place(models.Model):
    """Where a record is now: in a container, at a position, or with a person."""

    entity = models.OneToOneField(
        Entity, primary_key=True, on_delete=models.PROTECT, related_name="+"
    )
    #: None once it's out of storage: used up, discarded, shipped, or with its custodian.
    container = models.ForeignKey(
        Entity, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    row = models.PositiveSmallIntegerField(null=True, blank=True)
    column = models.PositiveSmallIntegerField(null=True, blank=True)
    #: Who has it: checked out (its position kept for it), or received and not put away.
    custodian = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    custody_since = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_storage_place"
        db_table_comment = "Where each record kept somewhere is now (ADR 0180)."
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["container"], name="iki_storage_place_container"),
            models.Index(
                fields=["custodian"],
                name="iki_storage_place_custodian",
                condition=Q(custodian__isnull=False),
            ),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # One record per position. Positions are NULL for a container without them, and
            # NULLs are distinct, so this is per position; deferred, so a swap is one change.
            models.UniqueConstraint(
                fields=["container", "row", "column"],
                name="iki_storage_place_one_per_position",
                deferrable=Deferrable.DEFERRED,
            ),
            models.CheckConstraint(
                condition=Q(row__isnull=True, column__isnull=True)
                | Q(container__isnull=False, row__gte=1, column__gte=1),
                name="iki_storage_place_position",
            ),
            models.CheckConstraint(
                condition=~Q(container=F("entity")), name="iki_storage_place_not_in_itself"
            ),
            models.CheckConstraint(
                condition=Q(custodian__isnull=True, custody_since__isnull=True)
                | Q(custodian__isnull=False, custody_since__isnull=False),
                name="iki_storage_place_custody",
            ),
        ]


class EventKind(models.TextChoices):
    #: Put into storage from nowhere.
    PLACED = "placed", "Put away"
    MOVED = "moved", "Moved"
    #: Taken out of storage: used up, discarded or shipped.
    REMOVED = "removed", "Taken out of storage"
    #: To a person, its position kept for it.
    CHECKED_OUT = "checked_out", "Checked out"
    CHECKED_IN = "checked_in", "Checked in"
    #: From one person to another while it's out.
    HANDED_OVER = "handed_over", "Handed over"
    #: Custody taken with a request's receipt.
    RECEIVED = "received", "Received"


class Event(models.Model):
    """One change to where a record is, or who has it. Append-only."""

    APPEND_ONLY = True

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    kind = models.CharField(max_length=16, choices=EventKind.choices)
    from_container = models.ForeignKey(
        Entity, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    from_row = models.PositiveSmallIntegerField(null=True, blank=True)
    from_column = models.PositiveSmallIntegerField(null=True, blank=True)
    to_container = models.ForeignKey(
        Entity, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    to_row = models.PositiveSmallIntegerField(null=True, blank=True)
    to_column = models.PositiveSmallIntegerField(null=True, blank=True)
    #: Who had it before, and who has it after.
    from_custodian = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    custodian = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: Who handed it over, as the receipt names them: a courier and a waybill, or a person.
    handed_over_by = models.CharField(max_length=200, blank=True, default="")
    #: The code scanned to confirm it, when its type asks for one (ADR 0180).
    confirmed = models.CharField(max_length=200, blank=True, default="")
    #: One per request: a bulk move's, a receipt's and a reconciliation's events share it.
    operation = models.UUIDField(default=uuid7)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+"
    )
    via = models.CharField(max_length=16)
    client = models.CharField(max_length=255, blank=True, default="")
    agent = models.ForeignKey(
        "iki_audit.Agent", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_storage_event"
        db_table_comment = "Every change to where a record is, or who has it (ADR 0180)."
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["entity", "created_at"], name="iki_storage_event_entity"),
            models.Index(fields=["operation"], name="iki_storage_event_operation"),
            models.Index(fields=["from_container", "created_at"], name="iki_storage_event_from"),
            models.Index(fields=["to_container", "created_at"], name="iki_storage_event_to"),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(kind__in=EventKind.values), name="iki_storage_event_kind"
            ),
        ]


# --- Receiving a request's material ------------------------------------------------------------


class ReceiptStatus(models.TextChoices):
    OPEN = "open", "Open"
    COMPLETED = "completed", "Completed"
    CANCELLED = "cancelled", "Cancelled"


class Outcome(models.TextChoices):
    MATCHED = "matched", "Matched"
    MISSING = "missing", "Missing"
    EXTRA = "extra", "Extra"
    DAMAGED = "damaged", "Damaged"
    REJECTED = "rejected", "Rejected"


class Receipt(models.Model):
    """A request's material checked in by its service against what it expected."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    request = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    #: The service receiving it.
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(
        max_length=16, choices=ReceiptStatus.choices, default=ReceiptStatus.OPEN
    )
    handed_over_by = models.CharField(max_length=200, blank=True, default="")
    note = models.TextField(blank=True, default="")
    started_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    started_at = models.DateTimeField(db_default=Now())
    #: Who received it: whoever completed it, and when.
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    #: The operation its events share, once it's completed.
    operation = models.UUIDField(default=uuid7)

    class Meta:
        db_table = "iki_storage_receipt"
        db_table_comment = "A request's material checked in by its service (ADR 0180)."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # One open receipt per request at a time: two receivers would tick one box twice.
            models.UniqueConstraint(
                fields=["request"],
                condition=Q(status="open"),
                name="iki_storage_receipt_one_open",
            ),
        ]


class ReceiptLine(models.Model):
    """One thing a receipt expected, or found: its outcome, who checked it, and when."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    receipt = models.ForeignKey(Receipt, on_delete=models.PROTECT, related_name="lines")
    #: The record, if Iki knows it: always for what was expected; for an extra, the record
    #: its code names, if the service can read it.
    entity = models.ForeignKey(
        Entity, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: What was scanned, or the code it was expected with.
    code = models.CharField(max_length=200, blank=True, default="")
    expected = models.BooleanField()
    outcome = models.CharField(max_length=16, choices=Outcome.choices, null=True, blank=True)
    note = models.TextField(blank=True, default="")
    checked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    checked_at = models.DateTimeField(null=True, blank=True)
    #: Where it was put away, once the receipt is completed.
    put_in = models.ForeignKey(
        Entity, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    put_row = models.PositiveSmallIntegerField(null=True, blank=True)
    put_column = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta:
        db_table = "iki_storage_receipt_line"
        db_table_comment = "What a receipt expected or found, and its outcome (ADR 0180)."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["receipt", "entity"], name="iki_storage_receipt_line_once"
            ),
            models.CheckConstraint(
                condition=Q(expected=False, outcome="extra")
                | Q(expected=False, outcome__isnull=True)
                | (Q(expected=True) & ~Q(outcome="extra")),
                name="iki_storage_receipt_line_extra",
            ),
            models.CheckConstraint(
                condition=Q(expected=False) | Q(entity__isnull=False),
                name="iki_storage_receipt_line_expected",
            ),
        ]


# --- Reconciling a container -------------------------------------------------------------------


class Finding(models.TextChoices):
    MATCHED = "matched", "Matched"
    MISSING = "missing", "Missing"
    UNEXPECTED = "unexpected", "Unexpected"
    WRONG_SLOT = "wrong_slot", "In the wrong position"


class Resolution(models.TextChoices):
    #: Moved to where it was found.
    MOVED = "moved", "Moved to where it was found"
    #: Taken out of storage: it isn't there.
    REMOVED = "removed", "Taken out of storage"
    #: Put here, at the position it was found in, from wherever it was recorded.
    PLACED = "placed", "Put where it was found"
    #: Left as recorded, with a note.
    KEPT = "kept", "Left as recorded"


class Reconciliation(models.Model):
    """A container's contents scanned and compared with what's recorded in it."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    container = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(
        max_length=16, choices=ReceiptStatus.choices, default=ReceiptStatus.OPEN
    )
    plan_revision = models.PositiveIntegerField(default=1, db_default=1)
    note = models.TextField(blank=True, default="")
    started_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    started_at = models.DateTimeField(db_default=Now())
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    operation = models.UUIDField(default=uuid7)

    class Meta:
        db_table = "iki_storage_reconciliation"
        db_table_comment = "A container's contents scanned against its records (ADR 0180)."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(plan_revision__gt=0),
                name="iki_storage_reconciliation_positive_revision",
            ),
            models.UniqueConstraint(
                fields=["container"],
                condition=Q(status="open"),
                name="iki_storage_reconciliation_one_open",
            ),
        ]


class ReconciliationLine(models.Model):
    """One record, or one code, in a reconciliation: where it was recorded, where it was
    found, and what was done about it."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    reconciliation = models.ForeignKey(
        Reconciliation, on_delete=models.PROTECT, related_name="lines"
    )
    entity = models.ForeignKey(
        Entity, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    code = models.CharField(max_length=200, blank=True, default="")
    #: It was checked out when the reconciliation started: its position is kept for it, and
    #: it isn't expected there.
    checked_out = models.BooleanField(default=False)
    #: Where it's recorded in this container, when the reconciliation started.
    expected_row = models.PositiveSmallIntegerField(null=True, blank=True)
    expected_column = models.PositiveSmallIntegerField(null=True, blank=True)
    #: Where it was scanned in it, if it was.
    found_row = models.PositiveSmallIntegerField(null=True, blank=True)
    found_column = models.PositiveSmallIntegerField(null=True, blank=True)
    scanned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    scanned_at = models.DateTimeField(null=True, blank=True)
    resolution = models.CharField(max_length=16, choices=Resolution.choices, null=True, blank=True)
    note = models.TextField(blank=True, default="")
    # The observed slot the receiver approved, or the expected slot for a missing record.
    # An older open decision without a recorded position needs review before completion.
    resolved_position = models.CharField(max_length=16, null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "iki_storage_reconciliation_line"
        db_table_comment = "A record or code in a reconciliation, and its resolution (ADR 0180)."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["reconciliation", "entity"], name="iki_storage_reconciliation_line_once"
            ),
            models.UniqueConstraint(
                fields=["reconciliation", "found_row", "found_column"],
                name="iki_storage_reconciliation_line_found_once",
            ),
        ]


# --- Observation history for receiving and reconciliation ----------------------------------------


class ObservationKind(models.TextChoices):
    OUTCOME = "outcome", "Receiving outcome"
    HANDOFF = "handoff", "Receiving handoff"
    SCAN = "scan", "Scan"
    RESOLUTION = "resolution", "Reconciliation decision"
    STATUS = "status", "Status"


class ObservationEvent(models.Model):
    """A person's observation or amendment, kept even if the working line is later removed."""

    APPEND_ONLY = True

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    receipt = models.ForeignKey(
        Receipt, null=True, on_delete=models.PROTECT, related_name="observations", db_index=False
    )
    reconciliation = models.ForeignKey(
        Reconciliation,
        null=True,
        on_delete=models.PROTECT,
        related_name="observations",
        db_index=False,
    )
    #: A physical line's ID survives its deletion; a parent-wide observation has none.
    line_id = models.UUIDField(null=True)
    kind = models.CharField(max_length=16, choices=ObservationKind.choices)
    before = models.JSONField(null=True)
    after = models.JSONField(null=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(db_default=Now())
    operation = models.UUIDField(db_index=True)
    revision = models.PositiveIntegerField()
    previous = models.ForeignKey("self", null=True, on_delete=models.PROTECT, related_name="+")
    reason = models.CharField(max_length=2000, blank=True, default="")

    class Meta:
        db_table = "iki_storage_observation_event"
        db_table_comment = (
            "Immutable observations, corrections and removals for receipts and counts."
        )
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["receipt", "id"],
                condition=Q(receipt__isnull=False),
                name="iki_obs_receipt_history",
            ),
            models.Index(
                fields=["reconciliation", "id"],
                condition=Q(reconciliation__isnull=False),
                name="iki_obs_count_history",
            ),
            models.Index(
                fields=["receipt", "line_id", "kind", "revision", "id"],
                condition=Q(receipt__isnull=False),
                name="iki_obs_receipt_line",
            ),
            models.Index(
                fields=["reconciliation", "line_id", "kind", "revision", "id"],
                condition=Q(reconciliation__isnull=False),
                name="iki_obs_count_line",
            ),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(receipt__isnull=False, reconciliation__isnull=True)
                | Q(receipt__isnull=True, reconciliation__isnull=False),
                name="iki_observation_one_parent",
            ),
            models.CheckConstraint(
                condition=Q(revision__gt=0), name="iki_observation_positive_revision"
            ),
            models.CheckConstraint(
                condition=Q(
                    receipt__isnull=False, kind=ObservationKind.OUTCOME, line_id__isnull=False
                )
                | Q(receipt__isnull=False, kind=ObservationKind.HANDOFF, line_id__isnull=True)
                | Q(
                    reconciliation__isnull=False,
                    kind__in=[ObservationKind.SCAN, ObservationKind.RESOLUTION],
                    line_id__isnull=False,
                )
                | Q(kind=ObservationKind.STATUS, line_id__isnull=True),
                name="iki_observation_scope",
            ),
            models.CheckConstraint(
                condition=Exact(
                    models.Func("before", function="jsonb_typeof", output_field=models.CharField()),
                    models.Value("object"),
                ),
                name="iki_observation_before_object",
            ),
            models.CheckConstraint(
                condition=Exact(
                    models.Func("after", function="jsonb_typeof", output_field=models.CharField()),
                    models.Value("object"),
                ),
                name="iki_observation_after_object",
            ),
        ]
