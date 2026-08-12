"""Tighten the multi-tenant columns, once ``0009``'s backfill has committed.

Split from ``0009`` deliberately: its ``RunPython`` writes rows, which queues
deferred FK trigger events that Postgres holds until COMMIT, and
``ALTER TABLE`` on a table with pending trigger events fails with
``cannot ALTER TABLE … because it has pending trigger events``. Django runs
each migration in its own transaction, so putting the DDL in a separate file
is what lets the backfill commit first.
"""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("linkedin", "0009_multitenant"),
    ]

    operations = [
        migrations.AlterField(
            model_name="campaign",
            name="client",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="campaigns", to="linkedin.client",
            ),
        ),
        migrations.AlterField(
            model_name="linkedinprofile",
            name="client",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="profiles", to="linkedin.client",
            ),
        ),
        migrations.AlterField(
            model_name="task",
            name="linkedin_profile",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="tasks", to="linkedin.linkedinprofile",
            ),
        ),
        migrations.AlterField(
            model_name="task",
            name="campaign",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="tasks", to="linkedin.campaign",
            ),
        ),
        # ── Campaign names are unique per client, not globally ──
        migrations.AlterField(
            model_name="campaign",
            name="name",
            field=models.CharField(max_length=200),
        ),
        migrations.AddConstraint(
            model_name="campaign",
            constraint=models.UniqueConstraint(
                fields=("client", "name"), name="unique_campaign_name_per_client",
            ),
        ),
        # ── Drop the dead freemium + newsletter columns ──
        migrations.RemoveField(model_name="campaign", name="is_freemium"),
        migrations.RemoveField(model_name="campaign", name="action_fraction"),
        migrations.RemoveField(model_name="linkedinprofile", name="subscribe_newsletter"),
        migrations.RemoveField(model_name="linkedinprofile", name="newsletter_processed"),
        # ── Ordering + the index the per-profile queue reads on every poll ──
        migrations.AlterModelOptions(
            name="linkedinprofile",
            options={"ordering": ["client__name", "pk"]},
        ),
        migrations.AddIndex(
            model_name="task",
            index=models.Index(
                fields=["linkedin_profile", "status", "scheduled_at"],
                name="linkedin_ta_linkedi_b0c832_idx",
            ),
        ),
    ]
