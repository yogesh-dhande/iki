from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("iki_runs", "0048_server_tier")]

    operations = [
        migrations.AddField(
            model_name="actionrun",
            name="environment_image",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
        migrations.AddField(
            model_name="actionrun",
            name="environment_image_pull_secret",
            field=models.CharField(blank=True, default="", max_length=253),
        ),
        migrations.AddField(
            model_name="launch",
            name="image_pull_secret",
            field=models.CharField(blank=True, default="", max_length=253),
        ),
        migrations.AddField(
            model_name="setupbuild",
            name="image_pull_secret",
            field=models.CharField(blank=True, default="", max_length=253),
        ),
    ]
