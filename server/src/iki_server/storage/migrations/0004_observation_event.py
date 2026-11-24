import django.db.models.deletion
import django.db.models.functions.datetime
import django.db.models.lookups
from django.conf import settings
from django.db import migrations, models

import iki_server.core.ids
from iki_server.plugins.operations import AppendOnly

CHECK_PREVIOUS = """
CREATE FUNCTION iki_observation_check_previous() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM iki_storage_observation_event AS prior
        WHERE prior.id = NEW.previous_id
          AND prior.receipt_id IS NOT DISTINCT FROM NEW.receipt_id
          AND prior.reconciliation_id IS NOT DISTINCT FROM NEW.reconciliation_id
          AND prior.line_id IS NOT DISTINCT FROM NEW.line_id
          AND prior.kind = NEW.kind
          AND prior.revision <= NEW.revision
    ) THEN
        RAISE EXCEPTION 'Previous observation must belong to this parent, line and kind'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER iki_observation_previous BEFORE INSERT ON iki_storage_observation_event
FOR EACH ROW WHEN (NEW.previous_id IS NOT NULL) EXECUTE FUNCTION iki_observation_check_previous();
"""
REMOVE_CHECK = """
DROP TRIGGER iki_observation_previous ON iki_storage_observation_event;
DROP FUNCTION iki_observation_check_previous();
"""


class Migration(migrations.Migration):
    """Start recording without attributing any existing working observations retrospectively."""

    dependencies = [
        ("iki_storage", "0003_reconciliation_plan_revision"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ObservationEvent",
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
                ("line_id", models.UUIDField(null=True)),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("outcome", "Receiving outcome"),
                            ("handoff", "Receiving handoff"),
                            ("scan", "Scan"),
                            ("resolution", "Reconciliation decision"),
                            ("status", "Status"),
                        ],
                        max_length=16,
                    ),
                ),
                ("before", models.JSONField(null=True)),
                ("after", models.JSONField(null=True)),
                (
                    "created_at",
                    models.DateTimeField(db_default=django.db.models.functions.datetime.Now()),
                ),
                ("operation", models.UUIDField(db_index=True)),
                ("revision", models.PositiveIntegerField()),
                ("reason", models.CharField(blank=True, default="", max_length=2000)),
                (
                    "actor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "previous",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="iki_storage.observationevent",
                    ),
                ),
                (
                    "receipt",
                    models.ForeignKey(
                        db_index=False,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="observations",
                        to="iki_storage.receipt",
                    ),
                ),
                (
                    "reconciliation",
                    models.ForeignKey(
                        db_index=False,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="observations",
                        to="iki_storage.reconciliation",
                    ),
                ),
            ],
            options={
                "db_table": "iki_storage_observation_event",
                "db_table_comment": "Immutable observations, corrections and removals for receipts and counts.",
                "indexes": [
                    models.Index(
                        condition=models.Q(("receipt__isnull", False)),
                        fields=["receipt", "id"],
                        name="iki_obs_receipt_history",
                    ),
                    models.Index(
                        condition=models.Q(("reconciliation__isnull", False)),
                        fields=["reconciliation", "id"],
                        name="iki_obs_count_history",
                    ),
                    models.Index(
                        condition=models.Q(("receipt__isnull", False)),
                        fields=["receipt", "line_id", "kind", "revision", "id"],
                        name="iki_obs_receipt_line",
                    ),
                    models.Index(
                        condition=models.Q(("reconciliation__isnull", False)),
                        fields=["reconciliation", "line_id", "kind", "revision", "id"],
                        name="iki_obs_count_line",
                    ),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("receipt__isnull", False), ("reconciliation__isnull", True)),
                            models.Q(("receipt__isnull", True), ("reconciliation__isnull", False)),
                            _connector="OR",
                        ),
                        name="iki_observation_one_parent",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("revision__gt", 0)),
                        name="iki_observation_positive_revision",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            receipt__isnull=False, kind="outcome", line_id__isnull=False
                        )
                        | models.Q(receipt__isnull=False, kind="handoff", line_id__isnull=True)
                        | models.Q(
                            reconciliation__isnull=False,
                            kind__in=["scan", "resolution"],
                            line_id__isnull=False,
                        )
                        | models.Q(kind="status", line_id__isnull=True),
                        name="iki_observation_scope",
                    ),
                    models.CheckConstraint(
                        condition=django.db.models.lookups.Exact(
                            models.Func(
                                "before", function="jsonb_typeof", output_field=models.CharField()
                            ),
                            models.Value("object"),
                        ),
                        name="iki_observation_before_object",
                    ),
                    models.CheckConstraint(
                        condition=django.db.models.lookups.Exact(
                            models.Func(
                                "after", function="jsonb_typeof", output_field=models.CharField()
                            ),
                            models.Value("object"),
                        ),
                        name="iki_observation_after_object",
                    ),
                ],
            },
        ),
        migrations.RunSQL(CHECK_PREVIOUS, REMOVE_CHECK),
        AppendOnly(model_name="ObservationEvent"),
    ]
