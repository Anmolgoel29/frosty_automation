from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0013_lead_about"),
    ]

    operations = [
        migrations.AddField(
            model_name="lead",
            name="coarse_scraped_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
