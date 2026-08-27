from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0014_lead_coarse_scraped_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="deal",
            name="qualification_stage",
            field=models.CharField(
                blank=True,
                choices=[("cheap", "Cheap"), ("expensive", "Expensive")],
                default=None,
                max_length=20,
                null=True,
            ),
        ),
    ]
