"""Multi-tenancy: Client, per-client campaigns/profiles, profile-scoped tasks.

Existing single-tenant installs are folded into one client named "Default":
every campaign, profile, and (in ``crm.0011``) lead is attached to it, so an
upgrade keeps running exactly as before with one tenant.

``Campaign.users`` (M2M to auth.User) becomes ``Campaign.profiles`` (M2M to
LinkedInProfile) — the profiles working a campaign are what the round-robin
dispatcher rotates over, and routing through auth.User only ever obscured
that. The old membership is copied across here; the ``users`` column itself
is dropped in ``0010`` once ``chat.0003`` has finished reading it.

Every Task row is deleted rather than backfilled: the queue is a pure
reflection of CRM state, and ``scheduler.reconcile()`` rebuilds it (now with
the owning profile attached) on the first idle cycle after startup.
"""
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone

import linkedin.conf


def forwards(apps, schema_editor):
    Client = apps.get_model("linkedin", "Client")
    Campaign = apps.get_model("linkedin", "Campaign")
    LinkedInProfile = apps.get_model("linkedin", "LinkedInProfile")
    Task = apps.get_model("linkedin", "Task")

    has_data = Campaign.objects.exists() or LinkedInProfile.objects.exists()
    if has_data:
        client, _ = Client.objects.get_or_create(
            name="Default",
            defaults={"notes": "Created automatically when this install became multi-tenant."},
        )
        Campaign.objects.filter(client__isnull=True).update(client=client)
        LinkedInProfile.objects.filter(client__isnull=True).update(client=client)

        for profile in LinkedInProfile.objects.select_related("user"):
            if not profile.name and profile.user_id:
                profile.name = profile.user.username
                profile.save(update_fields=["name"])

        # Campaign.users → Campaign.profiles, via the User↔LinkedInProfile 1:1.
        profile_by_user = {p.user_id: p for p in LinkedInProfile.objects.all()}
        for campaign in Campaign.objects.prefetch_related("users"):
            members = [
                profile_by_user[u.pk]
                for u in campaign.users.all()
                if u.pk in profile_by_user
            ]
            if members:
                campaign.profiles.set(members)

    # The queue is derived state; reconcile() rebuilds it profile-scoped.
    Task.objects.all().delete()


def backwards(apps, schema_editor):
    Task = apps.get_model("linkedin", "Task")
    Task.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("linkedin", "0008_siteconfig_split_chat_task_llm"),
    ]

    operations = [
        migrations.CreateModel(
            name="Client",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=200, unique=True)),
                ("active", models.BooleanField(
                    default=True,
                    help_text="Uncheck to pause every profile belonging to this client.",
                )),
                ("notes", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={"ordering": ["name"]},
        ),
        # ── New columns, nullable for the backfill ──
        migrations.AddField(
            model_name="campaign",
            name="client",
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.CASCADE,
                related_name="campaigns", to="linkedin.client",
            ),
        ),
        migrations.AddField(
            model_name="campaign",
            name="dispatch_cursor",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="linkedinprofile",
            name="client",
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.CASCADE,
                related_name="profiles", to="linkedin.client",
            ),
        ),
        migrations.AddField(
            model_name="linkedinprofile",
            name="name",
            field=models.CharField(
                blank=True, default="", max_length=200,
                help_text="Display label for this profile, e.g. the person it belongs to.",
            ),
        ),
        migrations.AddField(
            model_name="linkedinprofile",
            name="active_hours_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="linkedinprofile",
            name="active_start_hour",
            field=models.PositiveSmallIntegerField(default=9, help_text="Inclusive, local time."),
        ),
        migrations.AddField(
            model_name="linkedinprofile",
            name="active_end_hour",
            field=models.PositiveSmallIntegerField(default=19, help_text="Exclusive, local time."),
        ),
        migrations.AddField(
            model_name="linkedinprofile",
            name="timezone_name",
            field=models.CharField(
                default=linkedin.conf.default_timezone, max_length=64,
                help_text="IANA timezone name, e.g. Europe/Madrid.",
            ),
        ),
        migrations.AddField(
            model_name="linkedinprofile",
            name="rest_days",
            field=models.JSONField(
                blank=True, default=linkedin.conf.default_rest_days,
                help_text="Weekday numbers to skip: 0=Mon … 6=Sun.",
            ),
        ),
        migrations.AddField(
            model_name="campaign",
            name="profiles",
            field=models.ManyToManyField(
                blank=True, related_name="campaigns", to="linkedin.linkedinprofile",
                help_text=(
                    "Every profile listed here works this campaign. They share one "
                    "prospect pool and take assignments round-robin."
                ),
            ),
        ),
        migrations.AddField(
            model_name="task",
            name="linkedin_profile",
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.CASCADE,
                related_name="tasks", to="linkedin.linkedinprofile",
            ),
        ),
        migrations.AddField(
            model_name="task",
            name="campaign",
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.CASCADE,
                related_name="tasks", to="linkedin.campaign",
            ),
        ),
        # ── Backfill ──
        #
        # This is the last operation in the migration on purpose. Writing rows
        # queues deferred FK trigger events that live until COMMIT, and Postgres
        # refuses ALTER TABLE on a table with pending events. The NOT NULL
        # tightening therefore lives in 0010, which runs in its own transaction.
        migrations.RunPython(forwards, backwards),
    ]
