from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0012_llm_qualification_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="lead",
            name="about",
            field=models.TextField(blank=True, default=""),
        ),
    ]
