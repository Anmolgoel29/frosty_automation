"""Per-client Lead uniqueness, once ``0011``'s backfill has committed.

Split from ``0011`` because ``ALTER TABLE`` cannot run in the same
transaction as the row writes that populate ``Lead.client`` — Postgres
rejects it with ``pending trigger events``.
"""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0011_multitenant"),
    ]

    operations = [
        migrations.AlterField(
            model_name="lead",
            name="client",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="leads", to="linkedin.client",
            ),
        ),
        # ── Global uniqueness → per-client uniqueness ──
        migrations.AlterField(
            model_name="lead",
            name="linkedin_url",
            field=models.URLField(max_length=200),
        ),
        migrations.AlterField(
            model_name="lead",
            name="public_identifier",
            field=models.CharField(db_index=True, max_length=200),
        ),
        migrations.AlterField(
            model_name="lead",
            name="urn",
            field=models.CharField(blank=True, db_index=True, max_length=200, null=True),
        ),
        migrations.AddConstraint(
            model_name="lead",
            constraint=models.UniqueConstraint(
                fields=("client", "public_identifier"),
                name="unique_lead_public_id_per_client",
            ),
        ),
        migrations.AddConstraint(
            model_name="lead",
            constraint=models.UniqueConstraint(
                fields=("client", "linkedin_url"), name="unique_lead_url_per_client",
            ),
        ),
        migrations.AddConstraint(
            model_name="lead",
            constraint=models.UniqueConstraint(
                fields=("client", "urn"), name="unique_lead_urn_per_client",
            ),
        ),
        migrations.AddIndex(
            model_name="deal",
            index=models.Index(
                fields=["campaign", "state", "assigned_profile"],
                name="crm_deal_campaig_69e3cb_idx",
            ),
        ),
    ]
