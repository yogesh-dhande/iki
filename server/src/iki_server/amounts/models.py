"""A record's amount, and the ledger of its movements (ADR 0139)."""

from typing import ClassVar

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Now

from iki_server.core.ids import uuid7
from iki_server.entities.models import Entity

#: Digits an amount keeps: 28 before the point and 12 after, so a picolitre counts in
#: litres and a genome's copies in units.
DIGITS = 40
PLACES = 12
#: What's asked for is kept as it was given, in its own unit, which can be finer or coarser
#: than the record's: 0.0000000000001 L is exactly 0.0000001 uL. So to 40 places each
#: side of the point, which no conversion between UCUM's units a record keeps outgrows.
GIVEN_PLACES = 40


class Amount(models.Model):
    """How much of a record there is now: its ledger's last balance."""

    entity = models.OneToOneField(
        Entity, primary_key=True, on_delete=models.PROTECT, related_name="+"
    )
    unit = models.CharField(max_length=50, db_comment="Its type's unit, in UCUM.")
    value = models.DecimalField(max_digits=DIGITS, decimal_places=PLACES)
    updated_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_amount"
        db_table_comment = "How much there is of each record with an amount (ADR 0139)."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(condition=Q(value__gte=0), name="iki_amount_not_negative"),
        ]


class LowStockAlert(models.Model):
    """A recipient already told during this catalogue item's current low-stock episode."""

    item = models.ForeignKey(Entity, on_delete=models.CASCADE, related_name="+")
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+"
    )

    class Meta:
        db_table = "iki_amount_low_stock_alert"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["item", "recipient"], name="iki_low_stock_alert_once"),
        ]


class MovementKind(models.TextChoices):
    #: How much it had when it was recorded.
    RECORDED = "recorded", "Recorded"
    #: Used up: in an assay, a reaction, a shipment out of the lab.
    DRAWN = "drawn", "Drawn"
    #: Some went to another record (``counterpart``), or came from one.
    TRANSFERRED_OUT = "transferred_out", "Transferred to"
    TRANSFERRED_IN = "transferred_in", "Transferred from"
    #: Split into aliquots, each a new record; and each aliquot's first movement.
    ALIQUOTED = "aliquoted", "Split into aliquots"
    ALIQUOT = "aliquot", "Split from"
    #: Set to what it was found to be, as a recount or a correction.
    ADJUSTED = "adjusted", "Adjusted"


class DrawPurpose(models.TextChoices):
    ROUTINE = "routine", "Routine use"
    QA_SAMPLING = "qa_sampling", "QA sampling"


class Movement(models.Model):
    """One change to a record's amount. Append-only: a mistake is corrected by another."""

    APPEND_ONLY = True

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    kind = models.CharField(max_length=16, choices=MovementKind.choices)
    purpose = models.CharField(max_length=16, choices=DrawPurpose.choices, null=True)
    change = models.DecimalField(
        max_digits=DIGITS, decimal_places=PLACES, db_comment="Signed: what it took or gave."
    )
    balance = models.DecimalField(
        max_digits=DIGITS, decimal_places=PLACES, db_comment="How much it had after."
    )
    unit = models.CharField(max_length=50)
    #: What was asked for, in the unit it was asked in (50 ug of a sample kept in uL),
    #: exactly as it was given.
    given_value = models.DecimalField(
        max_digits=2 * GIVEN_PLACES, decimal_places=GIVEN_PLACES, null=True
    )
    given_unit = models.CharField(max_length=50, blank=True, default="")
    #: The other record: where a transfer went or came from, an aliquot's source.
    counterpart = models.ForeignKey(
        Entity, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: What a draw went into, such as an analysis or an experiment (ADR 0180): an edge of the
    #: graph, so it depends on what was drawn.
    used_in = models.ForeignKey(
        Entity, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: Where a container's material came from, as its type's ``follows`` says: its lot.
    source = models.ForeignKey(
        Entity, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: The code scanned to confirm it, when its type asks for one.
    confirmed = models.CharField(max_length=200, blank=True, default="")
    #: One per request: a transfer's two movements, a split's and its aliquots', share it.
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
        db_table = "iki_amount_movement"
        db_table_comment = "Every change to a record's amount (ADR 0139). Append-only."
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["entity", "created_at"], name="iki_amount_movement_entity"),
            models.Index(fields=["operation"], name="iki_amount_movement_operation"),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(kind__in=MovementKind.values), name="iki_amount_movement_kind"
            ),
            models.CheckConstraint(
                condition=Q(
                    kind=MovementKind.DRAWN, purpose__isnull=False, purpose__in=DrawPurpose.values
                )
                | (~Q(kind=MovementKind.DRAWN) & Q(purpose__isnull=True)),
                name="iki_amount_draw_purpose",
            ),
            models.CheckConstraint(
                condition=Q(balance__gte=0), name="iki_amount_movement_not_negative"
            ),
            models.CheckConstraint(
                condition=Q(kind=MovementKind.DRAWN) | Q(used_in__isnull=True),
                name="iki_amount_used_in_a_draw",
            ),
        ]
