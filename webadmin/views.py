# webadmin/views.py
"""Generic list/add/edit/delete/action routes, driven entirely by a
ModelAdmin config (webadmin/registry.py). One call to build_router(admin)
per registered model produces the full CRUD surface for it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import Boolean, DateTime, Float, Integer, LargeBinary, Text, delete, func, inspect, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import JSON

from webadmin.auth import require_login
from webadmin.db import get_session
from webadmin.registry import ModelAdmin
from webadmin.templates_env import templates

PAGE_SIZE = 50

# The one many-to-many field in the whole schema (Campaign.users) — see
# webadmin/models.py:campaign_users. Not worth a generic M2M-via-secondary
# reflection layer for a single field.
from webadmin.models import User, campaign_users  # noqa: E402


@dataclass
class FieldSpec:
    name: str  # attribute name used in forms/urls (relationship name for FK)
    column_name: str  # underlying DB column (== name, except for FK fields)
    kind: str  # text|textarea|number|float|checkbox|datetime|json|binary|select|fk
    label: str
    choices: type[Enum] | None = None
    related_model: type | None = None
    is_raw_id: bool = False
    nullable: bool = True
    has_default: bool = False


def build_field_specs(admin: ModelAdmin) -> list[FieldSpec]:
    mapper = inspect(admin.model)
    pk_name = mapper.primary_key[0].name

    fk_by_column: dict[str, Any] = {}
    for rel in mapper.relationships:
        if rel.uselist:
            continue  # many-to-many — not covered here, see admin.m2m_fields
        local_cols = list(rel.local_columns)
        if len(local_cols) == 1:
            fk_by_column[local_cols[0].name] = rel

    specs: list[FieldSpec] = []
    for column in mapper.columns:
        name = column.name
        if name == pk_name:
            continue
        if name in fk_by_column:
            rel = fk_by_column[name]
            specs.append(FieldSpec(
                name=rel.key,
                column_name=name,
                kind="fk",
                label=rel.key.replace("_", " "),
                related_model=rel.mapper.class_,
                is_raw_id=rel.key in admin.raw_id_fields,
                nullable=column.nullable,
                has_default=column.default is not None,
            ))
            continue

        label = name.replace("_", " ")
        if name in admin.choices:
            specs.append(FieldSpec(name, name, "select", label, choices=admin.choices[name],
                                    nullable=name in admin.blank_choice_fields,
                                    has_default=column.default is not None))
        elif isinstance(column.type, LargeBinary):
            specs.append(FieldSpec(name, name, "binary", label, nullable=True))
        elif isinstance(column.type, JSON):
            specs.append(FieldSpec(name, name, "json", label,
                                    nullable=column.nullable, has_default=column.default is not None))
        elif isinstance(column.type, Boolean):
            specs.append(FieldSpec(name, name, "checkbox", label, nullable=False, has_default=True))
        elif isinstance(column.type, DateTime):
            specs.append(FieldSpec(name, name, "datetime", label,
                                    nullable=column.nullable, has_default=column.default is not None))
        elif isinstance(column.type, Float):
            specs.append(FieldSpec(name, name, "float", label,
                                    nullable=column.nullable, has_default=column.default is not None))
        elif isinstance(column.type, Integer):
            specs.append(FieldSpec(name, name, "number", label,
                                    nullable=column.nullable, has_default=column.default is not None))
        elif isinstance(column.type, Text):
            specs.append(FieldSpec(name, name, "textarea", label,
                                    nullable=column.nullable, has_default=column.default is not None))
        else:
            specs.append(FieldSpec(name, name, "text", label,
                                    nullable=column.nullable, has_default=column.default is not None))

    for m2m_name in admin.m2m_fields:
        specs.append(FieldSpec(m2m_name, m2m_name, "m2m", m2m_name, related_model=User))

    return specs


def form_specs(admin: ModelAdmin, specs: list[FieldSpec]) -> list[FieldSpec]:
    return [s for s in specs if s.name not in admin.exclude_fields]


async def display_value(session: AsyncSession, obj: Any, field_name: str, specs_by_name: dict[str, FieldSpec]) -> str:
    if field_name == "__str__":
        return str(obj)
    spec = specs_by_name.get(field_name)
    if spec and spec.kind == "fk":
        fk_id = getattr(obj, spec.column_name)
        if fk_id is None:
            return "—"
        related = await session.get(spec.related_model, fk_id)
        return str(related) if related else f"#{fk_id} (missing)"

    value = getattr(obj, field_name, None)
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, bytes):
        return f"({len(value)} bytes)"
    if isinstance(value, (dict, list)):
        text = json.dumps(value)
        return text if len(text) <= 80 else text[:80] + "…"
    if isinstance(value, str) and len(value) > 80:
        return value[:80] + "…"
    return str(value)


async def field_value_for_form(session: AsyncSession, obj: Any | None, spec: FieldSpec) -> Any:
    """Current value for rendering an add/edit form input."""
    if obj is None:
        if spec.kind == "checkbox":
            return False
        if spec.kind == "datetime" and spec.has_default:
            return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
        if spec.kind == "m2m":
            return set()
        if spec.kind == "fk":
            return {"id": None, "label": ""}
        return ""
    if spec.kind == "fk":
        fk_id = getattr(obj, spec.column_name)
        related = await session.get(spec.related_model, fk_id) if fk_id else None
        return {"id": fk_id, "label": str(related) if related else ""}
    if spec.kind == "m2m":
        rows = await session.execute(select(campaign_users.c.user_id).where(campaign_users.c.campaign_id == obj.id))
        return {r[0] for r in rows}
    value = getattr(obj, spec.name)
    if spec.kind == "datetime" and value is not None:
        return value.strftime("%Y-%m-%dT%H:%M")
    if spec.kind == "json":
        return json.dumps(value, indent=2) if value is not None else ""
    if value is None:
        return ""
    return value


def parse_form_value(form, spec: FieldSpec) -> tuple[Any, str | None]:
    """Returns (value, error). error is a human-readable message, or None."""
    raw = form.get(spec.name)
    if spec.kind == "checkbox":
        return (raw is not None, None)
    if spec.kind == "fk":
        if not raw:
            if spec.nullable:
                return None, None
            return None, f"{spec.label} is required."
        try:
            return int(raw), None
        except ValueError:
            return None, f"{spec.label} must be a numeric id."
    if raw is None or raw == "":
        # Django's blank=True text/textarea fields carry default="" rather than
        # null=True (NOT NULL column, empty string is a valid value) — has_default
        # is the signal that blank submission is acceptable for those. "select"
        # blank-ness is opt-in per field (ModelAdmin.blank_choice_fields) since most
        # choice fields have a default value but must still not accept blank.
        if spec.kind in ("text", "textarea"):
            if spec.nullable or spec.has_default:
                return "", None
            return None, f"{spec.label} is required."
        if spec.nullable:
            return ("" if spec.kind == "select" else None), None
        return None, f"{spec.label} is required."
    if spec.kind == "number":
        try:
            return int(raw), None
        except ValueError:
            return None, f"{spec.label} must be a whole number."
    if spec.kind == "float":
        try:
            return float(raw), None
        except ValueError:
            return None, f"{spec.label} must be a number."
    if spec.kind == "datetime":
        try:
            return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc), None
        except ValueError:
            return None, f"{spec.label} must be a valid date/time."
    if spec.kind == "json":
        try:
            return json.loads(raw), None
        except json.JSONDecodeError as exc:
            return None, f"{spec.label} must be valid JSON ({exc})."
    return raw, None


def build_router(admin: ModelAdmin) -> APIRouter:
    router = APIRouter(prefix=f"/{admin.slug}")
    model = admin.model
    pk_col = inspect(model).primary_key[0]
    all_specs = build_field_specs(admin)
    specs_by_name = {s.name: s for s in all_specs}
    editable_specs = [s for s in form_specs(admin, all_specs) if s.kind != "binary"]

    async def _apply_csrf_check(request: Request, form) -> None:
        from webadmin.csrf import validate_csrf
        validate_csrf(request, form.get("csrf_token"))

    def _pop_flash(request: Request) -> str | None:
        return request.session.pop("flash", None)

    @router.get("", response_class=HTMLResponse)
    async def list_view(request: Request, session: AsyncSession = Depends(get_session), user=Depends(require_login)):
        stmt = select(model)
        qp = request.query_params

        q = qp.get("q", "").strip()
        if q and admin.search_fields:
            conditions = []
            for field_path in admin.search_fields:
                if "__" in field_path:
                    rel_name, col_name = field_path.split("__", 1)
                    rel = inspect(model).relationships[rel_name]
                    related_model = rel.mapper.class_
                    stmt = stmt.join(related_model)
                    conditions.append(getattr(related_model, col_name).ilike(f"%{q}%"))
                else:
                    conditions.append(getattr(model, field_path).ilike(f"%{q}%"))
            stmt = stmt.where(or_(*conditions))

        active_filters: dict[str, str] = {}
        for filter_name in admin.list_filter:
            value = qp.get(filter_name)
            if not value:
                continue
            active_filters[filter_name] = value
            spec = specs_by_name.get(filter_name)
            if spec and spec.kind == "fk":
                stmt = stmt.where(getattr(model, spec.column_name) == int(value))
            elif spec and spec.kind == "checkbox":
                stmt = stmt.where(getattr(model, filter_name) == (value == "true"))
            else:
                stmt = stmt.where(getattr(model, filter_name) == value)

        date_from = qp.get("date_from")
        date_to = qp.get("date_to")
        if admin.date_hierarchy:
            column = getattr(model, admin.date_hierarchy)
            if date_from:
                stmt = stmt.where(column >= datetime.fromisoformat(date_from).replace(tzinfo=timezone.utc))
            if date_to:
                stmt = stmt.where(column <= datetime.fromisoformat(date_to + "T23:59:59").replace(tzinfo=timezone.utc))

        total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()

        page = max(int(qp.get("page", "1") or "1"), 1)
        stmt = stmt.order_by(pk_col.desc()).limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)
        rows = (await session.execute(stmt)).scalars().all()

        row_data = []
        for obj in rows:
            cells = [await display_value(session, obj, f, specs_by_name) for f in admin.list_display]
            row_data.append((getattr(obj, pk_col.name), cells))

        filter_options: dict[str, list[tuple[str, str]]] = {}
        for filter_name in admin.list_filter:
            spec = specs_by_name.get(filter_name)
            if spec and spec.kind == "fk":
                related_rows = (await session.execute(select(spec.related_model))).scalars().all()
                filter_options[filter_name] = [(str(r.id), str(r)) for r in related_rows]
            elif spec and spec.kind == "checkbox":
                filter_options[filter_name] = [("true", "Yes"), ("false", "No")]
            elif spec and spec.kind == "select":
                filter_options[filter_name] = [(c.value, c.name.replace("_", " ").title()) for c in spec.choices]
            else:
                filter_options[filter_name] = []

        qs_without_page = "&".join(f"{k}={v}" for k, v in qp.multi_items() if k != "page")

        return templates.TemplateResponse(request, "list.html", {
            "admin": admin,
            "columns": admin.list_display,
            "rows": row_data,
            "total": total,
            "page": page,
            "page_size": PAGE_SIZE,
            "has_next": page * PAGE_SIZE < total,
            "qs_without_page": qs_without_page,
            "q": q,
            "active_filters": active_filters,
            "filter_options": filter_options,
            "date_from": date_from or "",
            "date_to": date_to or "",
            "flash": _pop_flash(request),
            "user": user,
        })

    async def _render_form(request, session, obj, errors, user, status_code=200):
        field_rows = []
        for spec in editable_specs:
            value = await field_value_for_form(session, obj, spec)
            readonly = spec.name in admin.readonly_fields or spec.kind == "binary"
            options = None
            if spec.kind == "select":
                options = [(c.value, c.name.replace("_", " ").title()) for c in spec.choices]
            elif spec.kind == "m2m" or (spec.kind == "fk" and not spec.is_raw_id):
                all_related = (await session.execute(select(spec.related_model))).scalars().all()
                options = [(r.id, str(r)) for r in all_related]
            field_rows.append({"spec": spec, "value": value, "readonly": readonly, "options": options})

        return templates.TemplateResponse(request, "form.html", {
            "admin": admin,
            "obj": obj,
            "field_rows": field_rows,
            "errors": errors,
            "is_add": obj is None,
            "user": user,
        }, status_code=status_code)

    if admin.can_add:
        @router.get("/add", response_class=HTMLResponse)
        async def add_form(request: Request, session: AsyncSession = Depends(get_session), user=Depends(require_login)):
            return await _render_form(request, session, None, [], user)

        @router.post("/add", response_class=HTMLResponse)
        async def add_submit(request: Request, session: AsyncSession = Depends(get_session), user=Depends(require_login)):
            form = await request.form()
            await _apply_csrf_check(request, form)

            values: dict[str, Any] = {}
            errors: list[str] = []
            m2m_values: dict[str, set[int]] = {}
            for spec in editable_specs:
                if spec.name in admin.readonly_fields:
                    continue
                if spec.kind == "m2m":
                    m2m_values[spec.name] = {int(v) for v in form.getlist(spec.name) if v}
                    continue
                value, error = parse_form_value(form, spec)
                if error:
                    errors.append(error)
                else:
                    values[spec.column_name] = value

            if errors:
                return await _render_form(request, session, None, errors, user, status_code=400)

            obj = model(**values)
            session.add(obj)
            await session.flush()
            for field_name, user_ids in m2m_values.items():
                if user_ids:
                    await session.execute(campaign_users.insert().values(
                        [{"campaign_id": obj.id, "user_id": uid} for uid in user_ids],
                    ))
            await session.commit()
            request.session["flash"] = f"{admin.label} created."
            return RedirectResponse(f"/admin/{admin.slug}", status_code=303)

    @router.get("/{obj_id}/edit", response_class=HTMLResponse)
    async def edit_form(obj_id: int, request: Request, session: AsyncSession = Depends(get_session), user=Depends(require_login)):
        obj = await session.get(model, obj_id)
        if obj is None:
            raise HTTPException(status_code=404)
        return await _render_form(request, session, obj, [], user)

    @router.post("/{obj_id}/edit", response_class=HTMLResponse)
    async def edit_submit(obj_id: int, request: Request, session: AsyncSession = Depends(get_session), user=Depends(require_login)):
        obj = await session.get(model, obj_id)
        if obj is None:
            raise HTTPException(status_code=404)
        form = await request.form()
        await _apply_csrf_check(request, form)

        errors: list[str] = []
        m2m_values: dict[str, set[int]] = {}
        for spec in editable_specs:
            if spec.name in admin.readonly_fields:
                continue
            if spec.kind == "m2m":
                m2m_values[spec.name] = {int(v) for v in form.getlist(spec.name) if v}
                continue
            value, error = parse_form_value(form, spec)
            if error:
                errors.append(error)
            else:
                setattr(obj, spec.column_name, value)

        if errors:
            await session.rollback()
            # rollback() expires every object tracked by the session (including
            # `user`, loaded by the require_login dependency earlier in this same
            # request) — refresh before the template touches user.username, since
            # Jinja's synchronous render can't await a lazy reload.
            await session.refresh(user)
            obj = await session.get(model, obj_id)
            return await _render_form(request, session, obj, errors, user, status_code=400)

        for field_name, user_ids in m2m_values.items():
            await session.execute(campaign_users.delete().where(campaign_users.c.campaign_id == obj.id))
            if user_ids:
                await session.execute(campaign_users.insert().values(
                    [{"campaign_id": obj.id, "user_id": uid} for uid in user_ids],
                ))
        await session.commit()
        request.session["flash"] = f"{admin.label} saved."
        return RedirectResponse(f"/admin/{admin.slug}", status_code=303)

    if admin.can_delete:
        @router.get("/{obj_id}/delete", response_class=HTMLResponse)
        async def delete_confirm(obj_id: int, request: Request, session: AsyncSession = Depends(get_session), user=Depends(require_login)):
            obj = await session.get(model, obj_id)
            if obj is None:
                raise HTTPException(status_code=404)
            return templates.TemplateResponse(request, "confirm_delete.html", {
                "admin": admin, "obj": obj, "obj_id": obj_id,
            })

        @router.post("/{obj_id}/delete")
        async def delete_submit(obj_id: int, request: Request, session: AsyncSession = Depends(get_session), user=Depends(require_login)):
            form = await request.form()
            await _apply_csrf_check(request, form)
            await session.execute(delete(model).where(pk_col == obj_id))
            await session.commit()
            request.session["flash"] = f"{admin.label} deleted."
            return RedirectResponse(f"/admin/{admin.slug}", status_code=303)

    @router.post("/action")
    async def run_action(request: Request, session: AsyncSession = Depends(get_session), user=Depends(require_login)):
        form = await request.form()
        await _apply_csrf_check(request, form)
        action_name = form.get("action")
        ids = [int(v) for v in form.getlist("ids") if v]

        if not ids:
            request.session["flash"] = "No rows selected."
        elif action_name == "delete_selected":
            if not admin.can_delete:
                raise HTTPException(status_code=403)
            result = await session.execute(delete(model).where(pk_col.in_(ids)))
            await session.commit()
            request.session["flash"] = f"Deleted {result.rowcount} {admin.label.lower()}(s)."
        elif action_name in admin.actions_by_name:
            message = await admin.actions_by_name[action_name].handler(session, ids)
            request.session["flash"] = message
        else:
            raise HTTPException(status_code=400, detail="Unknown action.")

        return RedirectResponse(f"/admin/{admin.slug}", status_code=303)

    return router
