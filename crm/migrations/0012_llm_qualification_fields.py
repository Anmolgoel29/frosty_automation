from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0011_deal_assigned_profile"),
    ]

    operations = [
        migrations.AddField(
            model_name="lead",
            name="headline",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
        migrations.AddField(
            model_name="lead",
            name="current_title",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="lead",
            name="current_company",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="lead",
            name="industry",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="lead",
            name="location_name",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="deal",
            name="fit_score",
            field=models.IntegerField(blank=True, default=None, null=True),
        ),
    ]
