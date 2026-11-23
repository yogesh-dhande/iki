from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("iki_org", "0005_lab_roles")]

    operations = [
        migrations.AddField(
            model_name="legalhold",
            name="release_reason",
            field=models.TextField(blank=True, default="", max_length=2000),
        ),
    ]
