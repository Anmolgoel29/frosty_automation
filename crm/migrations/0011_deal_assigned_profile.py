"""Sticky per-account ownership of a lead, from the connect step onwards.

Backfill: deals that were already contacted (PENDING / CONNECTED, or closed
out of one of those states) belong to the single account existing installs
ran. Deals still in the shared pool (QUALIFIED / READY_TO_CONNECT) stay
unassigned so the round-robin allocator deals them out.
"""
from django.db import migrations, models
import django.db.models.deletion

CONTACTED_STATES = ["Pending", "Connected", "Completed"]


def assign_contacted_deals(apps, schema_editor):
    Campaign = apps.get_model("linkedin", "Campaign")
    Deal = apps.get_model("crm", "Deal")
    LinkedInProfile = apps.get_model("linkedin", "LinkedInProfile")

    for campaign in Campaign.objects.all():
        profile = (
            LinkedInProfile.objects.filter(user__campaigns=campaign)
            .order_by("-active", "pk")
            .first()
        )
        if profile is None:
            continue
        Deal.objects.filter(
            campaign=campaign,
            state__in=CONTACTED_STATES,
            assigned_profile__isnull=True,
        ).update(assigned_profile=profile)


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0010_lead_human_takeover"),
        ("linkedin", "0009_multi_profile_tasks"),
    ]

    operations = [
        migrations.AddField(
            model_name="deal",
            name="assigned_profile",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="deals",
                to="linkedin.linkedinprofile",
            ),
        ),
        migrations.RunPython(
            assign_contacted_deals,
            migrations.RunPython.noop,
        ),
    ]
