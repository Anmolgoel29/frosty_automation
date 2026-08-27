from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from linkedin.enums import ProfileState


class Outcome(models.TextChoices):
    CONVERTED = "converted"
    NOT_INTERESTED = "not_interested"
    WRONG_FIT = "wrong_fit"
    NO_BUDGET = "no_budget"
    HAS_SOLUTION = "has_solution"
    BAD_TIMING = "bad_timing"
    UNRESPONSIVE = "unresponsive"
    UNKNOWN = "unknown"


class Deal(models.Model):
    class Meta:
        verbose_name = _("Deal")
        verbose_name_plural = _("Deals")
        constraints = [
            models.UniqueConstraint(fields=["lead", "campaign"], name="unique_deal_per_campaign"),
        ]

    lead = models.ForeignKey("Lead", on_delete=models.CASCADE)
    campaign = models.ForeignKey(
        "linkedin.Campaign", on_delete=models.CASCADE, related_name="deals",
    )
    # The LinkedIn account that owns this lead. Null while the deal is still
    # in the campaign-wide pool (search → qualification); stamped when the
    # lead is dealt out round-robin at connect time, and never moved
    # afterwards — the account that sent the invite is the only one that can
    # see the accepted connection and the conversation.
    assigned_profile = models.ForeignKey(
        "linkedin.LinkedInProfile",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="deals",
    )
    state = models.CharField(
        max_length=20,
        choices=[(s.value, s.value) for s in ProfileState],
        default=ProfileState.QUALIFIED,
    )
    outcome = models.CharField(
        max_length=20,
        choices=Outcome.choices,
        blank=True,
        default="",
    )
    reason = models.TextField(blank=True, default="")
    # 1-5 self-rating from the expensive qualification stage (ml/qualifier.py:
    # qualify_with_llm). Null until qualified. Drives both the
    # QUALIFIED -> READY_TO_CONNECT promotion gate (pipeline/ready_pool.py)
    # and connect-order ranking (db/deals.py:get_ready_to_connect_profiles) —
    # the replacement for the old GP posterior on both counts.
    fit_score = models.IntegerField(null=True, blank=True, default=None)
    # Which qualification-cascade stage produced this Deal (pipeline/qualify.py):
    # "cheap" = disqualified by the headline/about prefilter, dossier never scraped.
    # "expensive" = reached the full-dossier LLM call (qualified or rejected there,
    # including a dossier scrape that came back empty). Null for deals that never
    # went through the cascade (freemium kit deals, manually seeded/promoted).
    qualification_stage = models.CharField(
        max_length=20,
        choices=[("cheap", "Cheap"), ("expensive", "Expensive")],
        null=True, blank=True, default=None,
    )
    connect_attempts = models.IntegerField(default=0)
    backoff_hours = models.IntegerField(default=0)
    profile_summary = models.JSONField(null=True, blank=True, default=None)
    chat_summary = models.JSONField(null=True, blank=True, default=None)
    creation_date = models.DateTimeField(default=timezone.now)
    update_date = models.DateTimeField(auto_now=True)

    def __str__(self):
        lead_str = str(self.lead) if self.lead_id else "?"
        return f"{lead_str} [{self.state}]"
