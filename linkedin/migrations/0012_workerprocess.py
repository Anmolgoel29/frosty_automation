"""The WorkerProcess control plane.

One row per LinkedIn profile, holding what the operator wants
(``desired_state``, written by the admin panel) alongside what is actually
true (status/pid/heartbeat, written by the supervisor). The supervisor
reconciles the second to the first.

Rows are created on demand by the supervisor, so there is nothing to
backfill: an existing install grows one per eligible profile on its first
reconcile pass, defaulting to running.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('linkedin', '0011_drop_user_coupling'),
    ]

    operations = [
        migrations.CreateModel(
            name='WorkerProcess',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('desired_state', models.CharField(choices=[('running', 'Running'), ('stopped', 'Stopped')], default='running', help_text='Set to Stopped to park this profile without deleting it.', max_length=16)),
                ('status', models.CharField(choices=[('stopped', 'Stopped'), ('starting', 'Starting'), ('running', 'Running'), ('stopping', 'Stopping'), ('crashed', 'Crashed')], default='stopped', max_length=16)),
                ('pid', models.IntegerField(blank=True, null=True)),
                ('host', models.CharField(blank=True, default='', max_length=200)),
                ('boot_id', models.CharField(blank=True, default='', max_length=64)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('stopped_at', models.DateTimeField(blank=True, null=True)),
                ('last_heartbeat', models.DateTimeField(blank=True, null=True)),
                ('restart_count', models.PositiveIntegerField(default=0)),
                ('last_exit_code', models.IntegerField(blank=True, null=True)),
                ('last_error', models.TextField(blank=True, default='')),
                ('linkedin_profile', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='worker', to='linkedin.linkedinprofile')),
            ],
            options={
                'verbose_name': 'Worker Process',
                'verbose_name_plural': 'Worker Processes',
                'ordering': ['linkedin_profile__client__name', 'linkedin_profile__pk'],
            },
        ),
    ]
