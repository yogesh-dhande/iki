from django.db import migrations, models


class Migration(migrations.Migration):
    """A type's entities by ID, in an index: a page sorted by ID reads a page's worth of
    them, however many there are (ADR 0103). Records share their entry's ID (ADR 0115), so
    the indexes that keep IDs unique each hold only some of them."""

    dependencies = [("iki_entities", "0017_submitted")]

    operations = [
        migrations.AddIndex(
            model_name="entity",
            index=models.Index(fields=["type", "hrid", "id"], name="iki_entity_hrid_sorted"),
        ),
    ]
