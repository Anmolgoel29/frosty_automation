import logging
import random
import time
from typing import Dict, Any, Optional

from django.db import IntegrityError, transaction
from django.utils import timezone

from linkedin.url_utils import url_to_public_id, public_id_to_url
from linkedin.enums import ProfileState

logger = logging.getLogger(__name__)


def lead_exists(url: str) -> bool:
    """Check if Lead already exists for this LinkedIn URL."""
    from crm.models import Lead

    pid = url_to_public_id(url)
    if not pid:
        return False
    return Lead.objects.filter(public_identifier=pid).exists()


def _coarse_fields_from_profile(profile: Dict[str, Any]) -> Dict[str, str]:
    """Campaign-agnostic coarse facts, cached on Lead at discovery time.

    Feeds the cheap qualification prefilter (ml/qualifier.py:qualify_cheap)
    without a second scrape. Position order mirrors what
    ml/profile_text.py already assumes: LinkedIn returns positions
    reverse-chronological, so index 0 is the current one.

    ``about`` is stored untruncated — it is one of only two fields the
    cheap prefilter reads, so clipping it would blind that stage.
    """
    positions = profile.get("positions") or []
    current = positions[0] if positions else {}
    industry = profile.get("industry") or {}
    return {
        "headline": (profile.get("headline") or "")[:500],
        "about": profile.get("summary") or "",
        "current_title": (current.get("title") or "")[:200],
        "current_company": (current.get("company_name") or "")[:200],
        "industry": (industry.get("name") or "")[:200],
        "location_name": (profile.get("location_name") or "")[:200],
    }


def ensure_coarse_fields(session, lead) -> bool:
    """Backfill a Lead's coarse fields when they predate the coarse-field cache.

    The cheap qualification prefilter reads exactly two columns —
    ``headline`` and ``about`` (ml/qualifier.py:qualify_cheap) — so a lead
    that never had them filled reaches it blank and can never be judged:
    the prompt passes every such lead through to the expensive dossier
    stage, which is precisely the cost the cheap stage exists to avoid.

    Rows discovered before those columns existed, and url-only seeds
    (setup/seeds.py, setup/freemium.py), are both in that state.
    ``coarse_scraped_at`` marks the rows whose coarse fields came from a
    real scrape; anything older gets exactly one repair scrape here.

    One profile fetch, only ever for un-repaired rows, so the cost drains
    away with the legacy backlog. Runs under ``campaign_lock`` like the
    dossier scrape that would otherwise follow it.

    Returns True if the row now carries usable coarse fields.
    """
    if lead.coarse_scraped_at:
        return bool(lead.headline or lead.about)

    if lead.headline or lead.about:
        # Real scrape, just older than the marker column itself.
        lead.coarse_scraped_at = timezone.now()
        lead.save(update_fields=["coarse_scraped_at"])
        return True

    profile = lead.get_profile(session)
    if not profile:
        # Private/deleted/restricted. Leave the marker null and let
        # qualification proceed — the dossier stage disqualifies on the
        # same unreachable profile a moment later.
        logger.warning("Coarse-field repair scrape failed for %s", lead.public_identifier)
        return False

    fields = _coarse_fields_from_profile(profile)
    for name, value in fields.items():
        setattr(lead, name, value)
    lead.coarse_scraped_at = timezone.now()
    lead.save(update_fields=[*fields, "coarse_scraped_at"])

    logger.info("Backfilled coarse fields for %s", lead.public_identifier)
    return bool(lead.headline or lead.about)


def create_enriched_lead(session, url: str, profile: Dict[str, Any]) -> Optional[int]:
    """Create Lead with full profile data and embedding.

    Returns lead PK or None if exists.
    Does NOT create Deal — that comes at qualification.
    """
    from crm.models import Lead

    # Use canonical public_identifier from Voyager response when available.
    canonical_pid = profile.get("public_identifier")
    public_id = canonical_pid or url_to_public_id(url)
    clean_url = public_id_to_url(public_id)

    urn = profile.get("urn") or None

    try:
        with transaction.atomic():
            if Lead.objects.filter(public_identifier=public_id).exists():
                return None
            if urn and Lead.objects.filter(urn=urn).exists():
                logger.info(
                    "Lead with URN %s already exists — skipping duplicate %s",
                    urn, public_id,
                )
                return None
            lead = Lead.objects.create(
                linkedin_url=clean_url, public_identifier=public_id,
                coarse_scraped_at=timezone.now(),
                **_coarse_fields_from_profile(profile),
            )
            _cache_urn_from_profile(lead, profile)
    except IntegrityError:
        # Accounts search in parallel on different keywords and regularly land
        # on the same person, so the existence checks above can both pass
        # before either insert commits. The unique constraint is the real
        # arbiter; losing the race just means someone else created the lead.
        logger.debug("Lead %s created concurrently — skipping", public_id)
        return None

    lead.embed_from_profile(profile)

    logger.debug("Created enriched lead for %s (pk=%d)", public_id, lead.pk)
    return lead.pk


@transaction.atomic
def promote_lead_to_deal(session, public_id: str, reason: str = "", fit_score: int | None = None):
    """Create a QUALIFIED Deal for a Lead.

    Returns the Deal.
    """
    from crm.models import Lead, Deal

    lead = Lead.objects.filter(public_identifier=public_id).first()
    if not lead:
        raise ValueError(f"No Lead for {public_id}")

    deal = Deal.objects.create(
        lead=lead,
        campaign=session.campaign,
        state=ProfileState.QUALIFIED,
        reason=reason,
        fit_score=fit_score,
        # Only ever called from the expensive stage of the qualification
        # cascade (pipeline/qualify.py) — there's no other path to QUALIFIED.
        qualification_stage="expensive",
    )

    from termcolor import colored
    logger.info("%s %s", public_id, colored("QUALIFIED", "green", attrs=["bold"]))
    return deal


def get_leads_for_qualification(session) -> list:
    """Leads eligible for qualification in the current campaign.

    Returns profile dicts for leads that are not permanently disqualified
    and have no Deal in this campaign.

    ``.defer("embedding")`` keeps the 384-dim blobs out of this read — the
    callers only need identifiers, and pulling every candidate's embedding
    here moved megabytes per call once the pool grew.
    """
    from crm.models import Lead

    leads = Lead.objects.filter(
        disqualified=False,
    ).exclude(
        deal__campaign=session.campaign,
    ).defer("embedding")

    return [lead.to_profile_dict() for lead in leads]


def update_lead_slug(old_public_id: str, new_public_id: str):
    """Update a Lead after LinkedIn redirected its vanity URL."""
    from crm.models import Lead

    new_url = public_id_to_url(new_public_id)
    updated = Lead.objects.filter(public_identifier=old_public_id).update(
        public_identifier=new_public_id,
        linkedin_url=new_url,
    )
    if updated:
        logger.info("Lead slug updated: %s → %s", old_public_id, new_public_id)
    return updated


def disqualify_lead(public_id: str):
    """Set Lead.disqualified = True (account-level, permanent, cross-campaign)."""
    from crm.models import Lead

    lead = Lead.objects.filter(public_identifier=public_id).first()
    if not lead:
        logger.warning("disqualify_lead: no Lead for %s", public_id)
        return
    lead.disqualified = True
    lead.save(update_fields=["disqualified"])


def discover_and_enrich(session, urls):
    """For each new URL, call Voyager API, create enriched Lead (with embedding).

    Skips URLs that already have a Lead, caps at enrich_max_per_page (DOM
    order — LinkedIn's own relevance), and pauses a human-ish
    [enrich_min_delay_seconds, enrich_max_delay_seconds] between scrapes.
    """
    from linkedin.api.client import PlaywrightLinkedinAPI
    from linkedin.conf import CAMPAIGN_CONFIG

    new_urls = [u for u in urls if not lead_exists(u)]
    if not new_urls:
        return

    max_per_page = CAMPAIGN_CONFIG["enrich_max_per_page"]
    if len(new_urls) > max_per_page:
        new_urls = new_urls[:max_per_page]

    logger.info("Discovered %d new profiles (%d total on page)", len(new_urls), len(urls))

    min_delay = CAMPAIGN_CONFIG["enrich_min_delay_seconds"]
    max_delay = CAMPAIGN_CONFIG["enrich_max_delay_seconds"]
    session.ensure_browser()
    api = PlaywrightLinkedinAPI(session=session)
    enriched = 0

    for url in new_urls:
        public_id = url_to_public_id(url)
        if not public_id:
            continue

        try:
            profile, _raw = api.get_profile(profile_url=url)
        except Exception:
            logger.warning("Voyager API failed for %s — skipping", url)
            continue

        if not profile:
            logger.warning("Empty profile for %s — skipping", url)
            continue

        if create_enriched_lead(session, url, profile) is not None:
            enriched += 1

        time.sleep(random.uniform(min_delay, max_delay))

    logger.info("Enriched %d/%d new profiles", enriched, len(new_urls))


def _cache_urn_from_profile(lead, profile: Dict[str, Any]):
    """Promote ``profile['urn']`` onto the Lead row if not already cached.

    The only durable field we extract from a fresh scrape — everything
    else lives in memory for the lifetime of the caller's dict.
    """
    urn = profile.get("urn") or None
    if urn and lead.urn != urn:
        lead.urn = urn
        lead.save(update_fields=["urn"])
