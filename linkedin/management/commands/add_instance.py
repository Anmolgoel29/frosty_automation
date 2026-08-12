"""Add an automation instance — a client, a campaign, and/or a profile.

The admin panel can do all of this too; this is the scriptable version, for
onboarding a client without clicking through four forms.

    # New client, new campaign, first profile on it
    python manage.py add_instance --client "Acme" \
        --campaign "Acme Q3 outreach" \
        --objective-file docs/acme.md \
        --email sdr1@acme.com --password '…'

    # Second profile on the same campaign — shares its prospect pool
    python manage.py add_instance --client "Acme" \
        --campaign "Acme Q3 outreach" \
        --email sdr2@acme.com --password '…'

A running worker pool picks the new profile up within one poll interval
(``WORKER_POLL_INTERVAL``); there is no restart step.
"""
from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from linkedin.conf import (
    DEFAULT_CONNECT_DAILY_LIMIT,
    DEFAULT_CONNECT_WEEKLY_LIMIT,
    DEFAULT_FOLLOW_UP_DAILY_LIMIT,
)


class Command(BaseCommand):
    help = "Add a client, campaign, and/or LinkedIn profile."

    def add_arguments(self, parser):
        parser.add_argument("--client", required=True, help="Client name (created if new).")

        parser.add_argument("--campaign", help="Campaign name (created if new, under --client).")
        parser.add_argument("--objective", default="", help="Campaign objective text.")
        parser.add_argument("--objective-file", help="Read the campaign objective from a file.")
        parser.add_argument("--product-docs", default="", help="Product documentation text.")
        parser.add_argument("--product-docs-file", help="Read product docs from a file.")
        parser.add_argument("--booking-link", default="", help="Meeting booking URL.")
        parser.add_argument("--seeds-file", help="File of LinkedIn URLs to seed as QUALIFIED.")

        parser.add_argument("--email", help="LinkedIn login for the new profile.")
        parser.add_argument("--password", help="LinkedIn password for the new profile.")
        parser.add_argument("--profile-name", default="", help="Display label for the profile.")
        parser.add_argument("--timezone", help="IANA timezone for this profile's working hours.")
        parser.add_argument("--connect-daily", type=int, default=DEFAULT_CONNECT_DAILY_LIMIT)
        parser.add_argument("--connect-weekly", type=int, default=DEFAULT_CONNECT_WEEKLY_LIMIT)
        parser.add_argument("--follow-up-daily", type=int, default=DEFAULT_FOLLOW_UP_DAILY_LIMIT)
        parser.add_argument(
            "--accept-legal", action="store_true",
            help="Record that this account's owner accepted LEGAL_NOTICE.md.",
        )

    def handle(self, *args, **options):
        from linkedin.models import Campaign
        from linkedin.onboarding import create_campaign, create_profile, get_or_create_client

        if options["email"] and not options["password"]:
            raise CommandError("--password is required with --email.")

        client = get_or_create_client(options["client"])
        self.stdout.write(self.style.SUCCESS(f"Client: {client.name}"))

        campaign = None
        if options["campaign"]:
            campaign = Campaign.objects.filter(client=client, name=options["campaign"]).first()
            if campaign is None:
                campaign = create_campaign(
                    client,
                    name=options["campaign"],
                    product_docs=_text(options, "product_docs"),
                    objective=_text(options, "objective"),
                    booking_link=options["booking_link"],
                )
                self.stdout.write(self.style.SUCCESS(f"Campaign created: {campaign.name}"))
            else:
                self.stdout.write(f"Campaign exists: {campaign.name}")

            if options["seeds_file"]:
                self._add_seeds(campaign, Path(options["seeds_file"]))

        if options["email"]:
            if campaign is None:
                self.stdout.write(self.style.WARNING(
                    "No --campaign given: the profile will sit idle until it is "
                    "added to one (a profile with no campaigns gets no worker).",
                ))
            profile = create_profile(
                client,
                options["email"],
                options["password"],
                name=options["profile_name"],
                campaigns=[campaign] if campaign else [],
                connect_daily=options["connect_daily"],
                connect_weekly=options["connect_weekly"],
                follow_up_daily=options["follow_up_daily"],
                legal_accepted=options["accept_legal"],
            )
            if options["timezone"]:
                profile.timezone_name = options["timezone"]
                profile.save(update_fields=["timezone_name"])

            self.stdout.write(self.style.SUCCESS(f"Profile created: {profile}"))
            self.stdout.write(
                "A running worker pool will start it within one poll interval.",
            )

    def _add_seeds(self, campaign, path: Path):
        from linkedin.setup.seeds import create_seed_leads, parse_seed_urls

        if not path.exists():
            raise CommandError(f"Seeds file not found: {path}")
        public_ids = parse_seed_urls(path.read_text(encoding="utf-8"))
        created = create_seed_leads(campaign, public_ids)
        self.stdout.write(self.style.SUCCESS(f"{created} seed lead(s) added."))


def _text(options: dict, key: str) -> str:
    """Value of ``--<key>``, or the contents of ``--<key>-file`` if given."""
    file_option = options.get(f"{key}_file")
    if file_option:
        path = Path(file_option)
        if not path.exists():
            raise CommandError(f"File not found: {path}")
        return path.read_text(encoding="utf-8").strip()
    return options.get(key, "")
