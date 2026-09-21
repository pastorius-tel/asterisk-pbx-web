"""パーキングロット (ParkingLot) CRUD ルーター。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import MohClass, ParkingLot
from app.routers._helpers import all_enum_choices
from app.schemas.media import ParkingLotForm

router = APIRouter(prefix="/parking-lots", tags=["parking_lots"])


async def _moh_choices(db: AsyncSession) -> list[dict[str, str]]:
    rows = (await db.scalars(select(MohClass).order_by(MohClass.class_name))).all()
    return [{"value": "", "label": "— default 保留音 —"}] + [
        {"value": (r.class_name if not r.is_default else "default"),
         "label": r.display_name}
        for r in rows
    ]


@router.get("/", response_class=HTMLResponse)
async def list_lots(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    rows = (await db.scalars(select(ParkingLot).order_by(ParkingLot.park_pos_start))).all()
    return request.app.state.templates.TemplateResponse(
        request, "parking_lots/list.html",
        {"items": rows, "title": "コールパーク"},
    )


@router.get("/new", response_class=HTMLResponse)
async def new_form(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, "parking_lots/form.html",
        {
            "item": None,
            "form_action": "/parking-lots/",
            "title": "コールパークの追加",
            "moh_classes": await _moh_choices(db),
            **all_enum_choices(),
        },
    )


@router.post("/", response_class=HTMLResponse)
async def create(request: Request, db: AsyncSession = Depends(get_db)):
    raw = dict(await request.form())
    return await _save(request, db, raw, None)


@router.get("/{pid}/edit", response_class=HTMLResponse)
async def edit_form(pid: int, request: Request, db: AsyncSession = Depends(get_db)):
    obj = await db.get(ParkingLot, pid)
    if obj is None:
        raise HTTPException(404)
    return request.app.state.templates.TemplateResponse(
        request, "parking_lots/form.html",
        {
            "item": obj,
            "form_action": f"/parking-lots/{pid}",
            "title": f"コールパークの編集 — {obj.name}",
            "moh_classes": await _moh_choices(db),
            **all_enum_choices(),
        },
    )


@router.post("/{pid}", response_class=HTMLResponse)
async def update(pid: int, request: Request, db: AsyncSession = Depends(get_db)):
    obj = await db.get(ParkingLot, pid)
    if obj is None:
        raise HTTPException(404)
    raw = dict(await request.form())
    return await _save(request, db, raw, obj)


@router.post("/{pid}/delete")
async def delete(pid: int, db: AsyncSession = Depends(get_db)):
    obj = await db.get(ParkingLot, pid)
    if obj is None:
        raise HTTPException(404)
    await db.delete(obj)
    await db.commit()
    return RedirectResponse("/parking-lots/", status_code=status.HTTP_303_SEE_OTHER)


async def _save(request, db: AsyncSession, data: dict, instance: ParkingLot | None):
    for k in ("is_default", "enabled"):
        data[k] = k in data
    for k in ("moh_class", "note"):
        if data.get(k) == "":
            data[k] = None
    for k in ("park_pos_start", "park_pos_end", "park_time_seconds"):
        if data.get(k):
            try:
                data[k] = int(data[k])
            except (TypeError, ValueError):
                pass

    try:
        validated = ParkingLotForm.model_validate(data)
    except ValidationError as ve:
        return request.app.state.templates.TemplateResponse(
            request, "parking_lots/form.html",
            {
                "item": instance,
                "form_action": (
                    f"/parking-lots/{instance.id}" if instance else "/parking-lots/"
                ),
                "title": "入力エラー",
                "errors": ve.errors(),
                "submitted": data,
                "moh_classes": await _moh_choices(db),
                **all_enum_choices(),
            },
            status_code=400,
        )

    payload = validated.model_dump()
    if instance is None:
        instance = ParkingLot(**payload)
        db.add(instance)
    else:
        for k, v in payload.items():
            setattr(instance, k, v)
    await db.commit()
    return RedirectResponse("/parking-lots/", status_code=status.HTTP_303_SEE_OTHER)
