"""Let several LinkedIn accounts share one Django user.

The one-to-one meant a second account could not reuse the user that already
carries the campaign membership (``Campaign.users``) — adding one in the
admin failed on ``linkedin_linkedinprofile_user_id_key``. Dropping the
unique constraint is what makes "same user, several accounts, one campaign"
work; nothing reads the old reverse ``user.linkedin_profile`` accessor.
"""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("linkedin", "0009_multi_profile_tasks"),
    ]

    operations = [
        migrations.AlterField(
            model_name="linkedinprofile",
            name="user",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="linkedin_profiles",
                to="auth.user",
            ),
        ),
    ]
