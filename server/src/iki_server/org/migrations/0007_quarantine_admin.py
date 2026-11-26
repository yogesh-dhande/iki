from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("iki_org", "0006_legal_hold_release_reason")]

    operations = [
        migrations.AlterField(
            model_name="orgroleassignment",
            name="role",
            field=models.CharField(
                choices=[
                    ("org_admin", "Org Admin"),
                    ("compute_admin", "Compute Admin"),
                    ("auditor", "Auditor"),
                    ("quarantine_admin", "Quarantine Admin"),
                ],
                max_length=32,
            ),
        ),
    ]
