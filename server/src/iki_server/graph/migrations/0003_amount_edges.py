from django.db import migrations, models


class Migration(migrations.Migration):
    """An aliquot depends on the record it was split from, and a record on those some of
    its amount was transferred from (ADR 0139)."""

    dependencies = [("iki_graph", "0002_rows")]

    operations = [
        migrations.AlterField(
            model_name="edge",
            name="kind",
            field=models.CharField(
                choices=[
                    ("reference", "Reference"),
                    ("run", "Run"),
                    ("file", "File read by a run"),
                    ("record", "Record of an entry"),
                    ("aliquot", "Aliquot of"),
                    ("transfer", "Transferred from"),
                ],
                max_length=16,
            ),
        ),
        migrations.RemoveConstraint(model_name="edge", name="iki_graph_edge_kind"),
        migrations.AddConstraint(
            model_name="edge",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("kind__in", ["reference", "run", "file", "record", "aliquot", "transfer"])
                ),
                name="iki_graph_edge_kind",
            ),
        ),
    ]
