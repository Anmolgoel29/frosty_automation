"""Swap ChatMessage.owner over to the profile FK and drop the auth.User M2Ms.

Split from ``0003`` because its ``RunPython`` populates ``owner_profile_id``,
and Postgres refuses ``ALTER TABLE`` on a table that still has pending
deferred trigger events from writes in the same transaction.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0003_owner_is_linkedin_profile"),
    ]

    operations = [
        migrations.RemoveField(model_name="chatmessage", name="owner"),
        migrations.RenameField(
            model_name="chatmessage", old_name="owner_profile", new_name="owner",
        ),
        migrations.RemoveField(model_name="chatmessage", name="recipients"),
        migrations.RemoveField(model_name="chatmessage", name="to"),
    ]
