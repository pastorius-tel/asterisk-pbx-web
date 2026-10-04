"""IVR (音声自動応答) CRUD ルーター。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import AudioFile, Ivr, IvrEntry
from app.routers._helpers import all_enum_choices, form_int
from app.schemas.media import IvrForm

router = APIRouter(prefix="/ivrs", tags=["ivrs"])


async def _audio_choices(db: AsyncSession, category: str | None = None) -> list[dict[str, str]]:
    q = select(AudioFile).where(AudioFile.conversion_status == "ok")
    if category:
        q = q.where(AudioFile.category == category)
    q = q.order_by(AudioFile.name)
    rows = (await db.scalars(q)).all()
    return [{"value": "", "label": "— 指定なし —"}] + [
        {"value": str(r.id), "label": f"[{r.category}] {r.name}"} for r in rows
    ]


@router.get("/", response_class=HTMLResponse)
async def list_ivrs(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    rows = (await db.scalars(select(Ivr).order_by(Ivr.name))).all()
    return request.app.state.templates.TemplateResponse(
        request, "ivrs/list.html",
        {"items": rows, "title": "IVR (音声自動応答)"},
    )


@router.get("/new", response_class=HTMLResponse)
async def new_form(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, "ivrs/form.html",
        {
            "item": None,
            "form_action": "/ivrs/",
            "title": "IVR の追加",
            "audio_choices": await _audio_choices(db),
            **all_enum_choices(),
        },
    )


@router.post("/", response_class=HTMLResponse)
async def create(request: Request, db: AsyncSession = Depends(get_db)):
    raw = await request.form()
    return await _save(request, db, raw, None)


@router.get("/{ivr_id}/edit", response_class=HTMLResponse)
async def edit_form(ivr_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    obj = await db.get(Ivr, ivr_id)
    if obj is None:
        raise HTTPException(404)
    return request.app.state.templates.TemplateResponse(
        request, "ivrs/form.html",
        {
            "item": obj,
            "form_action": f"/ivrs/{ivr_id}",
            "title": f"IVR の編集 — {obj.display_name}",
            "audio_choices": await _audio_choices(db),
            **all_enum_choices(),
        },
    )


@router.post("/{ivr_id}", response_class=HTMLResponse)
async def update(ivr_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    obj = await db.get(Ivr, ivr_id)
    if obj is None:
        raise HTTPException(404)
    raw = await request.form()
    return await _save(request, db, raw, obj)


@router.post("/{ivr_id}/delete")
async def delete(ivr_id: int, db: AsyncSession = Depends(get_db)):
    obj = await db.get(Ivr, ivr_id)
    if obj is None:
        raise HTTPException(404)
    await db.delete(obj)
    await db.commit()
    return RedirectResponse("/ivrs/", status_code=status.HTTP_303_SEE_OTHER)


async def _save(request, db: AsyncSession, raw, instance: Ivr | None):
    # まず IVR 本体
    data = {
        "name": raw.get("name", ""),
        "display_name": raw.get("display_name", ""),
        "greeting_audio_id": _to_int_or_none(raw.get("greeting_audio_id")),
        "invalid_audio_id": _to_int_or_none(raw.get("invalid_audio_id")),
        "timeout_audio_id": _to_int_or_none(raw.get("timeout_audio_id")),
        "timeout_seconds": form_int(raw.get("timeout_seconds"), 5),
        "max_retries": form_int(raw.get("max_retries"), 3),
        "fallback_action": raw.get("fallback_action", "hangup"),
        "fallback_value": raw.get("fallback_value") or None,
        "enabled": "enabled" in raw,
        "note": raw.get("note") or None,
    }

    try:
        validated = IvrForm.model_validate(data)
    except ValidationError as ve:
        return request.app.state.templates.TemplateResponse(
            request, "ivrs/form.html",
            {
                "item": instance,
                "form_action": f"/ivrs/{instance.id}" if instance else "/ivrs/",
                "title": "入力エラー",
                "errors": ve.errors(),
                "submitted": data,
                "audio_choices": await _audio_choices(db),
                **all_enum_choices(),
            },
            status_code=400,
        )

    payload = validated.model_dump()
    if instance is None:
        instance = Ivr(**payload)
        db.add(instance)
        await db.flush()
    else:
        for k, v in payload.items():
            setattr(instance, k, v)
        # 既存エントリをクリア (更新時のみ)
        for item in list(instance.entries):
            await db.delete(item)
        await db.flush()

    # エントリ追加

    keys = raw.getlist("entry_key")
    actions = raw.getlist("entry_action")
    values = raw.getlist("entry_value")
    labels = raw.getlist("entry_label")
    for i, key in enumerate(keys):
        if not key:
            continue
        db.add(IvrEntry(
            ivr_id=instance.id,
            key_input=key,
            action=actions[i] if i < len(actions) else "hangup",
            action_value=(values[i] if i < len(values) and values[i] else None),
            label=(labels[i] if i < len(labels) and labels[i] else None),
        ))

    await db.commit()
    return RedirectResponse("/ivrs/", status_code=status.HTTP_303_SEE_OTHER)


def _to_int_or_none(v) -> int | None:  # type: ignore[no-untyped-def]
    if v in (None, "", "None"):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
