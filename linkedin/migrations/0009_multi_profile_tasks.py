"""Route every Task to the LinkedIn account that must execute it.

Existing installs ran a single account, so every historical task belongs to
it. If no LinkedIn account exists yet the queue is simply dropped —
``scheduler.reconcile()`` rebuilds it from CRM state on the next daemon
cycle, so nothing durable is lost.
"""
from django.db import migrations, models
import django.db.models.deletion


def assign_tasks_to_first_profile(apps, schema_editor):
    LinkedInProfile = apps.get_model("linkedin", "LinkedInProfile")
    Task = apps.get_model("linkedin", "Task")

    profile = LinkedInProfile.objects.order_by("-active", "pk").first()
    if profile is None:
        Task.objects.all().delete()
        return
    Task.objects.filter(linkedin_profile__isnull=True).update(linkedin_profile=profile)


class Migration(migrations.Migration):

    dependencies = [
        ("linkedin", "0008_siteconfig_split_chat_task_llm"),
    ]

    operations = [
        migrations.AddField(
            model_name="campaign",
            name="assignment_cursor",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="task",
            name="linkedin_profile",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="tasks",
                to="linkedin.linkedinprofile",
            ),
        ),
        migrations.RunPython(
            assign_tasks_to_first_profile,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="task",
            name="linkedin_profile",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="tasks",
                to="linkedin.linkedinprofile",
            ),
        ),
    ]
