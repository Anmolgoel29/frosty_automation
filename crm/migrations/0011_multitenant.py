"""Per-client leads and profile-assigned deals.

``Lead`` stops being a global table. Uniqueness moves from
``public_identifier``/``linkedin_url``/``urn`` alone to those columns paired
with ``client``, so two clients chasing the same person each hold their own
row — with their own embedding, their own disqualification flag, and their
own conversation. Existing rows all move to the "Default" client.

``Deal.assigned_profile`` records which of a campaign's profiles owns the
outreach. It stays null while the deal sits in the campaign's shared pool
and is set once by the round-robin dispatcher; from PENDING onward it is
sticky, because only the profile that sent the invite can see the reply.
"""
from django.db import migrations, models
import django.db.models.deletion


def forwards(apps, schema_editor):
    Client = apps.get_model("linkedin", "Client")
    Lead = apps.get_model("crm", "Lead")

    if not Lead.objects.exists():
        return

    client, _ = Client.objects.get_or_create(
        name="Default",
        defaults={"notes": "Created automatically when this install became multi-tenant."},
    )
    Lead.objects.filter(client__isnull=True).update(client=client)


def backwards(apps, schema_editor):
    """Collapsing back to a global Lead table can drop rows — refuse."""
    Lead = apps.get_model("crm", "Lead")
    clients_with_leads = Lead.objects.values("client_id").distinct().count()
    if clients_with_leads > 1:
        raise RuntimeError(
            "Cannot reverse: leads belong to more than one client and the "
            "pre-multitenant schema has no way to keep them apart.",
        )


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0010_lead_human_takeover"),
        ("linkedin", "0009_multitenant"),
    ]

    operations = [
        migrations.AddField(
            model_name="lead",
            name="client",
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.CASCADE,
                related_name="leads", to="linkedin.client",
            ),
        ),
        migrations.AddField(
            model_name="deal",
            name="assigned_profile",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="deals", to="linkedin.linkedinprofile",
            ),
        ),
        migrations.AddField(
            model_name="deal",
            name="assigned_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        # Last operation in this migration: writing rows queues deferred FK
        # trigger events that Postgres holds until COMMIT, and ALTER TABLE on a
        # table with pending events fails. The constraints follow in 0012.
        migrations.RunPython(forwards, backwards),
    ]
