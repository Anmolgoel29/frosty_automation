"""Attach another LinkedIn account to a campaign.

A campaign runs one connect loop per attached account, so this is how you
scale a campaign horizontally: the accounts share one lead pool for search
and qualification, and the ready leads are dealt out between them
round-robin (see linkedin/pipeline/allocation.py).

    manage.py add_profile --email alice@corp.com --password '...'
    manage.py add_profile --email bob@corp.com --password '...' --campaign Outreach
    manage.py add_profile --list
"""
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Add a LinkedIn account to one or more campaigns (multi-account outreach)."

    def add_arguments(self, parser):
        parser.add_argument("--email", help="LinkedIn login email.")
        parser.add_argument("--password", default="", help="LinkedIn password.")
        parser.add_argument(
            "--user",
            dest="username",
            help="Attach to this existing Django user instead of creating a "
                 "dedicated one (several LinkedIn accounts may share a user).",
        )
        parser.add_argument(
            "--campaign",
            action="append",
            dest="campaigns",
            metavar="NAME",
            help="Campaign to attach to (repeatable). Default: every campaign.",
        )
        parser.add_argument(
            "--update-password",
            action="store_true",
            help="If the account already exists, overwrite its stored password "
                 "(clears saved cookies so the next run logs in fresh).",
        )
        parser.add_argument("--connect-daily", type=int, help="Daily connect limit.")
        parser.add_argument("--connect-weekly", type=int, help="Weekly connect limit.")
        parser.add_argument("--follow-up-daily", type=int, help="Daily follow-up limit.")
        parser.add_argument(
            "--list",
            action="store_true",
            help="List the accounts on each campaign and exit.",
        )

    def handle(self, *args, **options):
        from linkedin.models import Campaign

        if options["list"]:
            self._list(Campaign)
            return

        email = options["email"]
        if not email:
            raise CommandError("--email is required (or use --list).")

        campaigns = self._resolve_campaigns(Campaign, options["campaigns"])

        from linkedin.conf import (
            DEFAULT_CONNECT_DAILY_LIMIT,
            DEFAULT_CONNECT_WEEKLY_LIMIT,
            DEFAULT_FOLLOW_UP_DAILY_LIMIT,
        )
        from linkedin.onboarding import ensure_account

        profile, created = ensure_account(
            email,
            options["password"],
            campaigns=campaigns,
            username=options["username"],
            update_credentials=options["update_password"],
            connect_daily=options["connect_daily"] or DEFAULT_CONNECT_DAILY_LIMIT,
            connect_weekly=options["connect_weekly"] or DEFAULT_CONNECT_WEEKLY_LIMIT,
            follow_up_daily=options["follow_up_daily"] or DEFAULT_FOLLOW_UP_DAILY_LIMIT,
        )

        verb = "Created" if created else "Updated"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} account {profile.linkedin_username} (django user '{profile.user.username}') "
            f"on: {', '.join(c.name for c in campaigns)}"
        ))
        self.stdout.write(
            "Restart the daemon to pick it up — it opens one browser per account.",
        )

    def _resolve_campaigns(self, Campaign, names):
        if names:
            campaigns = list(Campaign.objects.filter(name__in=names))
            missing = set(names) - {c.name for c in campaigns}
            if missing:
                raise CommandError(f"No such campaign(s): {', '.join(sorted(missing))}")
        else:
            campaigns = list(Campaign.objects.all())
        if not campaigns:
            raise CommandError("No campaigns exist yet — onboard one first.")
        return campaigns

    def _list(self, Campaign):
        for campaign in Campaign.objects.all().order_by("pk"):
            profiles = campaign.active_profiles()
            self.stdout.write(f"{campaign.name} — {len(profiles)} active account(s)")
            for profile in profiles:
                self.stdout.write(f"    {profile.linkedin_username} ({profile.user.username})")
            if not profiles:
                self.stdout.write("    (none — this campaign cannot run)")
