import io

from django.db import migrations


def compress_existing_blobs(apps, schema_editor):
    """Re-dump each Campaign.model_blob with joblib zlib compression.

    joblib.load auto-detects compression, so uncompressed blobs still load.
    Re-dumping with compress=3 shrinks them in place (~3-5x on GP pipelines).

    ``joblib`` is imported here rather than at module scope, and its absence
    is not an error. Django imports *every* migration module to build the
    graph, so a top-level import of a dependency that has since been dropped
    doesn't just break this migration — it breaks `migrate` outright, and with
    it the daemon, which runs `migrate` on startup. joblib went away with the
    GP qualifier, so that is exactly what happened.

    Skipping is safe on every path. An install past this migration never runs
    it again; a fresh install runs it against a table with no rows; and an old
    install still carrying blobs only loses a space optimisation on a column
    that migration 0011 drops a few steps later.
    """
    try:
        import joblib
    except ModuleNotFoundError:
        return

    Campaign = apps.get_model("linkedin", "Campaign")
    for campaign in Campaign.objects.exclude(model_blob=None).iterator():
        pipeline = joblib.load(io.BytesIO(campaign.model_blob))
        buf = io.BytesIO()
        joblib.dump(pipeline, buf, compress=3)
        campaign.model_blob = buf.getvalue()
        campaign.save(update_fields=["model_blob"])


class Migration(migrations.Migration):

    dependencies = [
        ("linkedin", "0003_siteconfig"),
    ]

    operations = [
        migrations.RunPython(compress_existing_blobs, migrations.RunPython.noop),
    ]
