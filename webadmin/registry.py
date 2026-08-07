# webadmin/registry.py
"""Declarative admin config — one ModelAdmin per model, analogous to Django's
admin.ModelAdmin subclasses in the old admin.py/crm/admin.py, but plain data
consumed by the generic views in views.py rather than a class hierarchy.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class AdminAction:
    name: str
    label: str
    # Runs against the selected ids; returns a flash message to show the user.
    handler: Callable[[AsyncSession, list[int]], Awaitable[str]]


@dataclass
class ModelAdmin:
    model: type
    slug: str
    label: str
    list_display: list[str]
    list_filter: list[str] = field(default_factory=list)
    search_fields: list[str] = field(default_factory=list)
    date_hierarchy: str | None = None
    readonly_fields: list[str] = field(default_factory=list)
    # FK fields (relationship attribute name, not the "_id" column) rendered as a
    # plain id input with the related object's label shown alongside — this is the
    # only FK widget the engine has (see webadmin/views.py); Django's raw_id_fields
    # vs. plain dropdown distinction isn't replicated, deliberately, to avoid a
    # <select> listing every Lead row.
    raw_id_fields: list[str] = field(default_factory=list)
    # Many-to-many fields rendered as a native <select multiple>.
    m2m_fields: list[str] = field(default_factory=list)
    # Columns hand-excluded from add/edit forms entirely (e.g. auto_now timestamps
    # that Django's ModelForm would silently omit — Lead.update_date, Deal.update_date).
    exclude_fields: list[str] = field(default_factory=list)
    choices: dict[str, type[Enum]] = field(default_factory=dict)
    # Choice fields where an empty selection is valid (Django blank=True on a
    # CharField with choices, e.g. Deal.outcome) — everything else in `choices`
    # is treated as required.
    blank_choice_fields: list[str] = field(default_factory=list)
    actions: list[AdminAction] = field(default_factory=list)
    can_add: bool = True
    can_delete: bool = True
    hidden_from_nav: bool = False

    def __post_init__(self) -> None:
        self.actions_by_name = {a.name: a for a in self.actions}


REGISTRY: dict[str, ModelAdmin] = {}


def register(admin: ModelAdmin) -> ModelAdmin:
    REGISTRY[admin.slug] = admin
    return admin
