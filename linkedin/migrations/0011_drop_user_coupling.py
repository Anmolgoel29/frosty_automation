"""Drop the auth.User coupling from campaigns and profiles.

Runs last of the multi-tenant set: ``linkedin.0009`` has already copied
``Campaign.users`` into ``Campaign.profiles`` and ``chat.0003`` has already
translated ``ChatMessage.owner`` through the ``LinkedInProfile.user`` 1:1,
so nothing still reads these columns.

auth.User stays in INSTALLED_APPS — it is still the admin panel's login
table. It is just no longer part of the outreach data model, which is what
makes "add a profile" a single row in the admin instead of a user, a
profile, and an M2M membership.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("linkedin", "0010_multitenant_constraints"),
        ("chat", "0003_owner_is_linkedin_profile"),
    ]

    operations = [
        migrations.RemoveField(model_name="campaign", name="users"),
        migrations.RemoveField(model_name="linkedinprofile", name="user"),
    ]
