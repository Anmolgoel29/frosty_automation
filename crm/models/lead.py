import logging

import numpy as np
from django.db import IntegrityError, models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

logger = logging.getLogger(__name__)


class Lead(models.Model):
    class Meta:
        verbose_name = _("Lead")
        verbose_name_plural = _("Leads")

    linkedin_url = models.URLField(max_length=200, unique=True)
    public_identifier = models.CharField(max_length=200, unique=True)
    urn = models.CharField(max_length=200, null=True, blank=True, unique=True, db_index=True)
    embedding = models.BinaryField(null=True, blank=True)
    # Coarse, campaign-agnostic facts captured once at discovery (see
    # db/leads.py:create_enriched_lead) from the profile dict already in
    # hand — no extra scrape. `headline` + `about` are the only two the
    # cheap qualification prefilter sees (ml/qualifier.py:qualify_cheap);
    # the rest back the admin lead list. `about` is the profile's About
    # section, untruncated, so TextField rather than CharField.
    headline = models.CharField(max_length=500, blank=True, default="")
    about = models.TextField(blank=True, default="")
    current_title = models.CharField(max_length=200, blank=True, default="")
    current_company = models.CharField(max_length=200, blank=True, default="")
    industry = models.CharField(max_length=200, blank=True, default="")
    location_name = models.CharField(max_length=200, blank=True, default="")
    # When the coarse fields above were last filled from a real scrape.
    # Null means "predates the coarse-field cache" — rows discovered before
    # those columns existed, or seeded url-only — and is the signal
    # db/leads.py:ensure_coarse_fields uses to re-scrape them exactly once,
    # on their way into qualification.
    coarse_scraped_at = models.DateTimeField(null=True, blank=True)
    disqualified = models.BooleanField(default=False)
    human_takeover = models.BooleanField(
        default=False,
        verbose_name=_("Human Takeover"),
        help_text=_("If True, AI will stop sending automated messages.")
    )
    creation_date = models.DateTimeField(default=timezone.now)
    update_date = models.DateTimeField(auto_now=True)

    def __str__(self):
        label = self.public_identifier or self.linkedin_url or f"Lead#{self.pk}"
        if self.disqualified:
            return f"({_('Disqualified')}) {label}"
        return label

    # ------------------------------------------------------------------
    # Lazy accessors — re-scrape live on demand, persist only the
    # derived caches we still keep (urn, embedding).
    # ------------------------------------------------------------------

    def get_profile(self, session) -> dict | None:
        """Live Voyager scrape of the parsed profile dict.

        No DB caching: the heavy fields (raw JSON, names, company) live
        only in memory for as long as the caller holds the dict. We do
        opportunistically populate ``self.urn`` if it's still null and
        the scrape returns one.
        """
        from linkedin.api.client import PlaywrightLinkedinAPI
        from linkedin.exceptions import ProfileInaccessibleError

        session.ensure_browser()
        api = PlaywrightLinkedinAPI(session=session)
        try:
            profile, _raw = api.get_profile(public_identifier=self.public_identifier)
        except ProfileInaccessibleError:
            return None
        if not profile:
            return None

        urn = profile.get("urn") or None
        if urn and self.urn != urn:
            if Lead.objects.filter(urn=urn).exclude(pk=self.pk).exists():
                logger.warning("URN %s already owned by another lead — skipping for %s", urn, self.public_identifier)
            else:
                try:
                    self.urn = urn
                    self.save(update_fields=["urn"])
                except IntegrityError:
                    # Another account scraped the same person at the same
                    # moment and claimed the URN first; the check above and
                    # this save are not atomic together.
                    self.refresh_from_db(fields=["urn"])
                    logger.debug("URN %s claimed concurrently for %s", urn, self.public_identifier)

        return profile

    def get_urn(self, session) -> str:
        """LinkedIn URN. Reads cached column; falls back to a live scrape."""
        if self.urn:
            return self.urn
        self.get_profile(session)  # sets self.urn as side-effect
        if self.urn:
            return self.urn
        raise ValueError(f"Lead {self.pk}: could not resolve URN after re-fetch")

    def get_embedding(self, session) -> np.ndarray | None:
        """384-dim embedding. Lazy: scrapes + embeds on first access."""
        if self.embedding is None:
            profile = self.get_profile(session)
            if profile:
                self.embed_from_profile(profile)
        return self.embedding_array

    def embed_from_profile(self, profile: dict) -> None:
        """Compute and persist the 384-dim embedding from an in-hand profile.

        Used by callers that already have a freshly parsed profile dict,
        so they can skip the scrape that ``get_embedding`` would trigger.
        """
        from linkedin.ml.embeddings import embed_text
        from linkedin.ml.profile_text import build_profile_text

        text = build_profile_text({"profile": profile})
        emb = embed_text(text)
        self.embedding = emb.tobytes()
        self.save(update_fields=["embedding"])

    def to_profile_dict(self) -> dict:
        """Standard profile dict shape used by qualifiers and pools.

        The ``profile`` key is intentionally absent — callers that need
        the full Voyager-parsed dict must call ``get_profile(session)``
        themselves (live scrape).
        """
        return {
            "lead_id": self.pk,
            "public_identifier": self.public_identifier,
            "url": self.linkedin_url or "",
            "meta": {},
        }

    @property
    def embedding_array(self) -> np.ndarray | None:
        """384-dim float32 numpy array from stored bytes, or None."""
        if self.embedding is None:
            return None
        return np.frombuffer(bytes(self.embedding), dtype=np.float32).copy()

    @embedding_array.setter
    def embedding_array(self, arr: np.ndarray):
        self.embedding = np.asarray(arr, dtype=np.float32).tobytes()
