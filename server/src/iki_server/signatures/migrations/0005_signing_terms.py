from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("iki_signatures", "0004_reviewed_entry_revision")]
    operations = [
        migrations.AddField(
            model_name="signing",
            name="terms_digest",
            field=models.CharField(default="", max_length=64),
            preserve_default=False,
        ),
    ]
