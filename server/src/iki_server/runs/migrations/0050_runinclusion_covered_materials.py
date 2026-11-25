from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("iki_runs", "0049_deployment_images")]

    operations = [
        migrations.AddField(
            model_name="runinclusion",
            name="covered_materials",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
