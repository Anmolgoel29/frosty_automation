"""Retarget ChatMessage.owner from auth.User to linkedin.LinkedInProfile.

A message's owner is the mailbox it was synced from, which is a LinkedIn
profile — routing that through auth.User only mattered when every profile
needed a Django user. Owning it directly is also what lets a conversation
be scoped to one profile when two profiles of the same client end up
holding the same Lead row.

Runs before ``linkedin.0010`` drops ``LinkedInProfile.user``, since the
backfill reads that 1:1 to translate user ids into profile ids.

``recipients`` and ``to`` (M2M to auth.User) are dropped: never written,
never read, and the last remaining reason for this app to know about users.
"""
from django.db import migrations, models
import django.db.models.deletion


def forwards(apps, schema_editor):
    ChatMessage = apps.get_model("chat", "ChatMessage")
    LinkedInProfile = apps.get_model("linkedin", "LinkedInProfile")

    profile_id_by_user_id = dict(
        LinkedInProfile.objects.values_list("user_id", "pk"),
    )
    for message in ChatMessage.objects.exclude(owner_id=None).only("id", "owner_id"):
        profile_id = profile_id_by_user_id.get(message.owner_id)
        if profile_id is not None:
            ChatMessage.objects.filter(pk=message.pk).update(owner_profile_id=profile_id)


def backwards(apps, schema_editor):
    ChatMessage = apps.get_model("chat", "ChatMessage")
    LinkedInProfile = apps.get_model("linkedin", "LinkedInProfile")

    user_id_by_profile_id = dict(
        LinkedInProfile.objects.values_list("pk", "user_id"),
    )
    for message in ChatMessage.objects.exclude(owner_profile_id=None).only("id", "owner_profile_id"):
        user_id = user_id_by_profile_id.get(message.owner_profile_id)
        ChatMessage.objects.filter(pk=message.pk).update(owner_id=user_id)


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0002_add_linkedin_sync_fields"),
        ("linkedin", "0009_multitenant"),
    ]

    operations = [
        migrations.AddField(
            model_name="chatmessage",
            name="owner_profile",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                related_name="chat_messages", to="linkedin.linkedinprofile",
                verbose_name="Owner",
            ),
        ),
        # Last operation here: the column swap is DDL and can't share a
        # transaction with these writes (Postgres: pending trigger events).
        migrations.RunPython(forwards, backwards),
    ]
