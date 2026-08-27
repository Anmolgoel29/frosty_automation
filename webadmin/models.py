# webadmin/models.py
"""SQLAlchemy mirror of the tables Django's migrations own.

These are read/write mirrors of the exact tables `crm`, `chat`, and `linkedin`
already create via `manage.py migrate` — Django remains the only thing that
alters schema. Column set here must be hand-kept in sync with
`crm/models/`, `linkedin/models.py`, and `chat/models.py` whenever a new
Django migration changes one of these tables (accepted tradeoff, see
ARCHITECTURE.md).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Table,
    Text,
    UniqueConstraint,
    Column,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# auth_user — read-only mirror, only ever queried for login + FK display.
# ---------------------------------------------------------------------------
class User(Base):
    __tablename__ = "auth_user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(150))
    password: Mapped[str] = mapped_column(String(128))
    email: Mapped[str] = mapped_column(String(254))
    is_staff: Mapped[bool] = mapped_column(Boolean)
    is_active: Mapped[bool] = mapped_column(Boolean)
    is_superuser: Mapped[bool] = mapped_column(Boolean)

    def __str__(self) -> str:
        return self.username


campaign_users = Table(
    "linkedin_campaign_users",
    Base.metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("campaign_id", BigInteger, ForeignKey("linkedin_campaign.id")),
    Column("user_id", Integer, ForeignKey("auth_user.id")),
)


class SiteConfig(Base):
    __tablename__ = "linkedin_siteconfig"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    expensive_llm_provider: Mapped[str] = mapped_column(String(32), default="openai")
    expensive_llm_api_key: Mapped[str] = mapped_column(String(500), default="")
    expensive_ai_model: Mapped[str] = mapped_column(String(200), default="")
    expensive_llm_api_base: Mapped[str] = mapped_column(String(500), default="")
    cheap_llm_provider: Mapped[str] = mapped_column(String(32), default="openai")
    cheap_llm_api_key: Mapped[str] = mapped_column(String(500), default="")
    cheap_ai_model: Mapped[str] = mapped_column(String(200), default="")
    cheap_llm_api_base: Mapped[str] = mapped_column(String(500), default="")

    def __str__(self) -> str:
        return "Site Configuration"


class Campaign(Base):
    __tablename__ = "linkedin_campaign"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    product_docs: Mapped[str] = mapped_column(Text, default="")
    campaign_objective: Mapped[str] = mapped_column(Text, default="")
    booking_link: Mapped[str] = mapped_column(String(500), default="")
    is_freemium: Mapped[bool] = mapped_column(Boolean, default=False)
    action_fraction: Mapped[float] = mapped_column(Float, default=0.2)
    seed_public_ids: Mapped[list] = mapped_column(JSON, default=list)
    assignment_cursor: Mapped[int] = mapped_column(Integer, default=0)

    users: Mapped[list[User]] = relationship(secondary=campaign_users)

    def __str__(self) -> str:
        return self.name


class Lead(Base):
    __tablename__ = "crm_lead"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    linkedin_url: Mapped[str] = mapped_column(String(200), unique=True)
    public_identifier: Mapped[str] = mapped_column(String(200), unique=True)
    urn: Mapped[str | None] = mapped_column(String(200), nullable=True, unique=True)
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    headline: Mapped[str] = mapped_column(String(500), default="")
    about: Mapped[str] = mapped_column(Text, default="")
    current_title: Mapped[str] = mapped_column(String(200), default="")
    current_company: Mapped[str] = mapped_column(String(200), default="")
    industry: Mapped[str] = mapped_column(String(200), default="")
    location_name: Mapped[str] = mapped_column(String(200), default="")
    coarse_scraped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disqualified: Mapped[bool] = mapped_column(Boolean, default=False)
    human_takeover: Mapped[bool] = mapped_column(Boolean, default=False)
    creation_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    update_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    def __str__(self) -> str:
        label = self.public_identifier or self.linkedin_url or f"Lead#{self.id}"
        return f"(Disqualified) {label}" if self.disqualified else label


class LinkedInProfile(Base):
    __tablename__ = "linkedin_linkedinprofile"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # Not unique: several LinkedIn accounts may share one Django user so they
    # all inherit its campaign membership (see linkedin/models.py).
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("auth_user.id"))
    self_lead_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("crm_lead.id"), nullable=True,
    )
    linkedin_username: Mapped[str] = mapped_column(String(200))
    linkedin_password: Mapped[str] = mapped_column(String(200))
    subscribe_newsletter: Mapped[bool] = mapped_column(Boolean, default=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    connect_daily_limit: Mapped[int] = mapped_column(Integer, default=20)
    connect_weekly_limit: Mapped[int] = mapped_column(Integer, default=100)
    follow_up_daily_limit: Mapped[int] = mapped_column(Integer, default=25)
    legal_accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    cookie_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    newsletter_processed: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped[User] = relationship()
    self_lead: Mapped[Lead | None] = relationship()

    def __str__(self) -> str:
        # Deliberately does NOT touch ``self.user``: this is rendered for FK
        # columns (Task, ActionLog, Deal.assigned_profile) from objects loaded
        # without their relationships, and a lazy load there raises
        # MissingGreenlet under the async session. The LinkedIn login is the
        # identifier that matters once several accounts share a campaign.
        return self.linkedin_username or f"Profile#{self.id}"


class SearchKeyword(Base):
    __tablename__ = "linkedin_searchkeyword"
    __table_args__ = (UniqueConstraint("campaign_id", "keyword"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("linkedin_campaign.id"))
    keyword: Mapped[str] = mapped_column(String(500))
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    campaign: Mapped[Campaign] = relationship()

    def __str__(self) -> str:
        return self.keyword


class ActionLog(Base):
    __tablename__ = "linkedin_actionlog"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    linkedin_profile_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("linkedin_linkedinprofile.id"),
    )
    campaign_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("linkedin_campaign.id"))
    action_type: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    linkedin_profile: Mapped[LinkedInProfile] = relationship()
    campaign: Mapped[Campaign] = relationship()

    def __str__(self) -> str:
        return f"{self.action_type} by {self.linkedin_profile} at {self.created_at}"


class Task(Base):
    __tablename__ = "linkedin_task"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_type: Mapped[str] = mapped_column(String(20))
    linkedin_profile_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("linkedin_linkedinprofile.id"),
    )
    status: Mapped[str] = mapped_column(String(20), default="pending")
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    linkedin_profile: Mapped[LinkedInProfile] = relationship()

    def __str__(self) -> str:
        return f"{self.task_type} [{self.status}] scheduled={self.scheduled_at}"


class Deal(Base):
    __tablename__ = "crm_deal"
    __table_args__ = (UniqueConstraint("lead_id", "campaign_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[int] = mapped_column(Integer, ForeignKey("crm_lead.id"))
    campaign_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("linkedin_campaign.id"))
    assigned_profile_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("linkedin_linkedinprofile.id"), nullable=True,
    )
    state: Mapped[str] = mapped_column(String(20), default="Qualified")
    outcome: Mapped[str] = mapped_column(String(20), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    fit_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    qualification_stage: Mapped[str | None] = mapped_column(String(20), nullable=True)
    connect_attempts: Mapped[int] = mapped_column(Integer, default=0)
    backoff_hours: Mapped[int] = mapped_column(Integer, default=0)
    profile_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    chat_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    creation_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    update_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    lead: Mapped[Lead] = relationship()
    campaign: Mapped[Campaign] = relationship()
    assigned_profile: Mapped[LinkedInProfile | None] = relationship()

    def __str__(self) -> str:
        lead_str = str(self.lead) if self.lead_id else "?"
        return f"{lead_str} [{self.state}]"


class ChatMessage(Base):
    __tablename__ = "chat_chatmessage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Always targets Lead in this codebase (confirmed: every ChatMessage.objects.create /
    # ContentType.objects.get_for_model call site uses Lead) — object_id is treated as a
    # crm_lead id directly rather than resolved generically via django_content_type.
    content_type_id: Mapped[int] = mapped_column(Integer)
    object_id: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text, default="")
    owner_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("auth_user.id"), nullable=True)
    answer_to_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("chat_chatmessage.id"), nullable=True,
    )
    topic_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("chat_chatmessage.id"), nullable=True,
    )
    creation_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    linkedin_urn: Mapped[str] = mapped_column(String(300), unique=True)
    is_outgoing: Mapped[bool] = mapped_column(Boolean, default=True)

    owner: Mapped[User | None] = relationship(foreign_keys=[owner_id])

    def __str__(self) -> str:
        return self.content[:70]
