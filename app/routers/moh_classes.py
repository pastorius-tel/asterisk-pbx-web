"""保留音クラス (MohClass) CRUD。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import AudioFile, MohClass, MohClassItem
from app.routers._helpers import all_enum_choices
from app.schemas.media import MohClassForm

router = APIRouter(prefix="/moh-classes", tags=["moh_classes"])


async def _audio_choices(db: AsyncSession) -> list[dict[str, str]]:
    rows = (
        await db.scalars(
            select(AudioFile)
            .where(AudioFile.category == "moh", AudioFile.conversion_status == "ok")
            .order_by(AudioFile.name)
        )
    ).all()
    return [{"value": str(r.id), "label": r.name} for r in rows]


@router.get("/", response_class=HTMLResponse)
async def list_moh(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    rows = (await db.scalars(select(MohClass).order_by(MohClass.class_name))).all()
    return request.app.state.templates.TemplateResponse(
        request, "moh_classes/list.html",
        {"items": rows, "title": "保留音クラス"},
    )


@router.get("/new", response_class=HTMLResponse)
async def new_form(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, "moh_classes/form.html",
        {
            "item": None,
            "form_action": "/moh-classes/",
            "title": "保留音クラスの追加",
            "audio_choices": await _audio_choices(db),
            "selected_audio_ids": [],
            **all_enum_choices(),
        },
    )


@router.post("/", response_class=HTMLResponse)
async def create(request: Request, db: AsyncSession = Depends(get_db)):
    raw = await request.form()
    return await _save(request, db, raw, None)


@router.get("/{mid}/edit", response_class=HTMLResponse)
async def edit_form(mid: int, request: Request, db: AsyncSession = Depends(get_db)):
    obj = await db.get(MohClass, mid)
    if obj is None:
        raise HTTPException(404)
    return request.app.state.templates.TemplateResponse(
        request, "moh_classes/form.html",
        {
            "item": obj,
            "form_action": f"/moh-classes/{mid}",
            "title": f"保留音クラスの編集 — {obj.class_name}",
            "audio_choices": await _audio_choices(db),
            "selected_audio_ids": [i.audio_file_id for i in obj.items],
            **all_enum_choices(),
        },
    )


@router.post("/{mid}", response_class=HTMLResponse)
async def update(mid: int, request: Request, db: AsyncSession = Depends(get_db)):
    obj = await db.get(MohClass, mid)
    if obj is None:
        raise HTTPException(404)
    raw = await request.form()
    return await _save(request, db, raw, obj)


@router.post("/{mid}/delete")
async def delete(mid: int, db: AsyncSession = Depends(get_db)):
    obj = await db.get(MohClass, mid)
    if obj is None:
        raise HTTPException(404)
    await db.delete(obj)
    await db.commit()
    return RedirectResponse("/moh-classes/", status_code=status.HTTP_303_SEE_OTHER)


async def _save(request, db: AsyncSession, raw, instance: MohClass | None):
    # multipart form では同名キーが複数値になる → getlist
    audio_file_ids: list[int] = []
    for v in raw.getlist("audio_file_ids"):
        try:
            audio_file_ids.append(int(v))
        except (TypeError, ValueError):
            pass

    data = {
        "class_name": raw.get("class_name", ""),
        "display_name": raw.get("display_name", ""),
        "sort_mode": raw.get("sort_mode", "random"),
        "is_default": "is_default" in raw,
        "enabled": "enabled" in raw,
        "note": raw.get("note") or None,
        "audio_file_ids": audio_file_ids,
    }

    try:
        validated = MohClassForm.model_validate(data)
    except ValidationError as ve:
        return request.app.state.templates.TemplateResponse(
            request, "moh_classes/form.html",
            {
                "item": instance,
                "form_action": (
                    f"/moh-classes/{instance.id}" if instance else "/moh-classes/"
                ),
                "title": "入力エラー",
                "errors": ve.errors(),
                "submitted": data,
                "audio_choices": await _audio_choices(db),
                "selected_audio_ids": audio_file_ids,
                **all_enum_choices(),
            },
            status_code=400,
        )

    payload = validated.model_dump()
    ids = payload.pop("audio_file_ids")

    if instance is None:
        instance = MohClass(**payload)
        db.add(instance)
        await db.flush()
    else:
        for k, v in payload.items():
            setattr(instance, k, v)
        # 既存アイテムをクリア (更新時のみ)
        for item in list(instance.items):
            await db.delete(item)
        await db.flush()

    # 新規アイテム追加
    for idx, aid in enumerate(ids):
        db.add(MohClassItem(
            moh_class_id=instance.id, audio_file_id=aid, order_index=idx
        ))

    await db.commit()
    return RedirectResponse("/moh-classes/", status_code=status.HTTP_303_SEE_OTHER)
