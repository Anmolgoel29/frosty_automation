import logging
import sys

from django.core.management import call_command
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the OpenOutreach daemon (onboard, validate, start task queue)."

    def handle(self, *args, **options):
        self._configure_logging(verbose=options["verbosity"] >= 2)
        self._setup_tracing()
        self._ensure_db()
        self._ensure_onboarded()
        sessions = self._create_sessions()
        for session in sessions.values():
            self._ensure_newsletter(session)

        from linkedin.daemon import run_daemon
        run_daemon(sessions)

    # -- Steps ---------------------------------------------------------------

    def _configure_logging(self, verbose: bool = False):
        from linkedin.logging import configure_logging

        level = logging.DEBUG if verbose else logging.INFO
        configure_logging(level=level)

    def _setup_tracing(self):
        from linkedin.tracing import setup_tracing

        setup_tracing()

    def _ensure_db(self):
        call_command("migrate", "--no-input")

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

    def _create_sessions(self):
        """One AccountSession (and later one browser) per active LinkedIn account.

        Returns a ``{linkedin_profile_pk: AccountSession}`` map — the daemon
        routes each task to the account that must execute it. Accounts that
        aren't attached to any campaign are skipped with a warning rather
        than blocking the rest.
        """
        from linkedin.browser.registry import get_active_profiles, get_or_create_session
        from linkedin.models import SiteConfig

        cfg = SiteConfig.load()
        if not cfg.expensive_llm_api_key or not cfg.cheap_llm_api_key:
            logger.error(
                "EXPENSIVE_LLM_API_KEY and CHEAP_LLM_API_KEY are both required. "
                "Set them in Site Configuration (Django Admin)."
            )
            sys.exit(1)

        profiles = get_active_profiles()
        if not profiles:
            logger.error("No active LinkedIn profiles found.")
            sys.exit(1)

        sessions = {}
        for profile in profiles:
            session = get_or_create_session(profile)
            if not session.campaigns:
                logger.warning(
                    "%s belongs to no campaign — skipping. Attach it with "
                    "`manage.py add_profile --email %s --campaign <name>`.",
                    profile, profile.linkedin_username,
                )
                continue
            session.campaign = next(
                (c for c in session.campaigns if not c.is_freemium), None,
            ) or session.campaigns[0]
            sessions[profile.pk] = session

        if not sessions:
            logger.error("No campaigns found for any active LinkedIn profile.")
            sys.exit(1)

        logger.info(
            "Running %d LinkedIn account(s): %s",
            len(sessions),
            ", ".join(s.linkedin_profile.linkedin_username for s in sessions.values()),
        )
        return sessions

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
