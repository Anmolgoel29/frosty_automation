import logging
import sys

from django.core.management import call_command
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the OpenOutreach daemon (onboard, validate, start task queue)."

    def handle(self, *args, **options):
        self._configure_logging(verbose=options["verbosity"] >= 2)
        self._ensure_db()
        self._ensure_onboarded()
        session = self._create_session()
        self._ensure_newsletter(session)

        from linkedin.daemon import run_daemon
        run_daemon(session)

    # -- Steps ---------------------------------------------------------------

    def _configure_logging(self, verbose: bool = False):
        from linkedin.logging import configure_logging

        level = logging.DEBUG if verbose else logging.INFO
        configure_logging(level=level)

    def _ensure_db(self):
        call_command("migrate", "--no-input")

        from linkedin.management.setup_crm import setup_crm
        setup_crm()

    def _ensure_onboarded(self):
        from linkedin.onboarding import apply, collect_from_wizard, missing_keys

        if not missing_keys():
            return

        # collect_from_wizard() is non-interactive: it loads config.json (from
        # the data dir / cwd / home) or falls back to defaults. Apply whatever
        # it finds, then re-check.
        apply(collect_from_wizard())

        missing = missing_keys()
        if missing:
            self.stderr.write(
                f"Onboarding incomplete.\n"
                f"Missing: {', '.join(sorted(missing))}\n"
                f"Provide a config.json (data dir / cwd / ~/.openoutreach/) or "
                f"configure via the Django Admin panel, then the daemon will start."
            )
            sys.exit(1)

    def _create_session(self):
        from linkedin.browser.registry import get_first_active_profile, get_or_create_session
        from linkedin.models import SiteConfig

        if not SiteConfig.load().llm_api_key:
            logger.error("LLM_API_KEY is required. Set it in Site Configuration (Django Admin).")
            sys.exit(1)

        profile = get_first_active_profile()
        if profile is None:
            logger.error("No active LinkedIn profiles found.")
            sys.exit(1)

        session = get_or_create_session(profile)

        if not session.campaigns:
            logger.error("No campaigns found for this user.")
            sys.exit(1)
        campaign = next(
            (c for c in session.campaigns if not c.is_freemium), None,
        ) or session.campaigns[0]
        session.campaign = campaign

        return session

    def _ensure_newsletter(self, session):
        if session.linkedin_profile.newsletter_processed:
            return

        update_fields = []
        if session.linkedin_profile.subscribe_newsletter:
            session.linkedin_profile.subscribe_newsletter = False
            update_fields.append("subscribe_newsletter")

        session.linkedin_profile.newsletter_processed = True
        update_fields.append("newsletter_processed")
        session.linkedin_profile.save(update_fields=update_fields)
        logger.info("Newsletter auto-subscription disabled; skipping signup.")
