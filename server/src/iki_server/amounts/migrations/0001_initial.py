import django.db.models.deletion
import django.db.models.functions.datetime
from django.conf import settings
from django.db import migrations, models

import iki_server.core.ids
import iki_server.plugins.operations
from iki_server.graph import sql


class Migration(migrations.Migration):
    """Each record's amount, and the append-only ledger of its movements (ADR 0139), whose
    aliquots and transfers are edges of the graph."""

    initial = True

    dependencies = [
        ("iki_audit", "0005_via_trigger"),
        ("iki_entities", "0017_submitted"),
        ("iki_graph", "0003_amount_edges"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Amount",
            fields=[
                (
                    "entity",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        primary_key=True,
                        related_name="+",
                        serialize=False,
                        to="iki_entities.entity",
                    ),
                ),
                ("unit", models.CharField(db_comment="Its type's unit, in UCUM.", max_length=50)),
                ("value", models.DecimalField(decimal_places=12, max_digits=40)),
                (
                    "updated_at",
                    models.DateTimeField(db_default=django.db.models.functions.datetime.Now()),
                ),
            ],
            options={
                "db_table": "iki_amount",
                "db_table_comment": "How much there is of each record with an amount (ADR 0139).",
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("value__gte", 0)), name="iki_amount_not_negative"
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="Movement",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=iki_server.core.ids.uuid7,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("recorded", "Recorded"),
                            ("drawn", "Drawn"),
                            ("transferred_out", "Transferred to"),
                            ("transferred_in", "Transferred from"),
                            ("aliquoted", "Split into aliquots"),
                            ("aliquot", "Split from"),
                            ("adjusted", "Adjusted"),
                        ],
                        max_length=16,
                    ),
                ),
                (
                    "purpose",
                    models.CharField(
                        choices=[("routine", "Routine use"), ("qa_sampling", "QA sampling")],
                        max_length=16,
                        null=True,
                    ),
                ),
                (
                    "change",
                    models.DecimalField(
                        db_comment="Signed: what it took or gave.", decimal_places=12, max_digits=40
                    ),
                ),
                (
                    "balance",
                    models.DecimalField(
                        db_comment="How much it had after.", decimal_places=12, max_digits=40
                    ),
                ),
                ("unit", models.CharField(max_length=50)),
                ("given_value", models.DecimalField(decimal_places=12, max_digits=40, null=True)),
                ("given_unit", models.CharField(blank=True, default="", max_length=50)),
                ("operation", models.UUIDField(default=iki_server.core.ids.uuid7)),
                ("via", models.CharField(max_length=16)),
                ("client", models.CharField(blank=True, default="", max_length=255)),
                ("reason", models.TextField(blank=True, default="")),
                (
                    "created_at",
                    models.DateTimeField(db_default=django.db.models.functions.datetime.Now()),
                ),
                (
                    "agent",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="iki_audit.agent",
                    ),
                ),
                (
                    "counterpart",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="iki_entities.entity",
                    ),
                ),
                (
                    "entity",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="iki_entities.entity",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "iki_amount_movement",
                "db_table_comment": "Every change to a record's amount (ADR 0139). Append-only.",
                "indexes": [
                    models.Index(
                        fields=["entity", "created_at"], name="iki_amount_movement_entity"
                    ),
                    models.Index(fields=["operation"], name="iki_amount_movement_operation"),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            (
                                "kind__in",
                                [
                                    "recorded",
                                    "drawn",
                                    "transferred_out",
                                    "transferred_in",
                                    "aliquoted",
                                    "aliquot",
                                    "adjusted",
                                ],
                            )
                        ),
                        name="iki_amount_movement_kind",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            kind="drawn",
                            purpose__isnull=False,
                            purpose__in=["routine", "qa_sampling"],
                        )
                        | (~models.Q(kind="drawn") & models.Q(purpose__isnull=True)),
                        name="iki_amount_draw_purpose",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("balance__gte", 0)),
                        name="iki_amount_movement_not_negative",
                    ),
                ],
            },
        ),
        iki_server.plugins.operations.AppendOnly(model_name="Movement"),
        migrations.RunSQL(sql.MOVEMENTS_0139, sql.DROP_MOVEMENTS),
    ]
