"""発信ルート CRUD。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import OutboundRoute, Trunk
from app.routers._helpers import all_enum_choices
from app.schemas.route import OutboundRouteForm

router = APIRouter(prefix="/outbound-routes", tags=["outbound_routes"])


# 共通テンプレートで使う「番号パターンのプリセット」
PATTERN_PRESETS: list[dict[str, str]] = [
    {"label": "国内 0 始まり (固定/携帯)", "value": "_0Z."},
    {"label": "携帯 (070/080/090)", "value": "_0[789]0XXXXXXXX"},
    {"label": "固定 03 (東京)", "value": "_03XXXXXXXX"},
    {"label": "フリーダイヤル (0120)", "value": "_0120XXXXXX"},
    {"label": "国際電話 (010 prefix)", "value": "_010X."},
    {"label": "緊急 (110/118/119)", "value": "_11[089]"},
]


async def _trunk_choices(db: AsyncSession) -> list[dict[str, str]]:
    rows = (await db.scalars(select(Trunk).order_by(Trunk.name))).all()
    return [{"value": str(t.id), "label": f"{t.name} ({t.host})"} for t in rows]


@router.get("/", response_class=HTMLResponse)
async def list_routes(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    rows = (
        await db.scalars(select(OutboundRoute).order_by(OutboundRoute.priority))
    ).all()
    trunk_map = {t.id: t.name for t in (await db.scalars(select(Trunk))).all()}
    return request.app.state.templates.TemplateResponse(
        request,
        "outbound_routes/list.html",
        {"items": rows, "trunk_map": trunk_map, "title": "発信ルート一覧"},
    )


@router.get("/new", response_class=HTMLResponse)
async def new_form(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request,
        "outbound_routes/form.html",
        {
            "item": None,
            "form_action": "/outbound-routes/",
            "title": "発信ルートの追加",
            "trunks": await _trunk_choices(db),
            "pattern_presets": PATTERN_PRESETS,
            **all_enum_choices(),
        },
    )


@router.post("/", response_class=HTMLResponse)
async def create(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    raw = dict(await request.form())
    return await _save(request, db, raw, instance=None)


@router.get("/{rid}/edit", response_class=HTMLResponse)
async def edit_form(rid: int, request: Request, db: AsyncSession = Depends(get_db)):
    obj = await db.get(OutboundRoute, rid)
    if obj is None:
        raise HTTPException(404)
    return request.app.state.templates.TemplateResponse(
        request,
        "outbound_routes/form.html",
        {
            "item": obj,
            "form_action": f"/outbound-routes/{rid}",
            "title": f"発信ルートの編集 — {obj.name}",
            "trunks": await _trunk_choices(db),
            "pattern_presets": PATTERN_PRESETS,
            **all_enum_choices(),
        },
    )


@router.post("/{rid}", response_class=HTMLResponse)
async def update(rid: int, request: Request, db: AsyncSession = Depends(get_db)):
    obj = await db.get(OutboundRoute, rid)
    if obj is None:
        raise HTTPException(404)
    raw = dict(await request.form())
    return await _save(request, db, raw, instance=obj)


@router.post("/{rid}/delete")
async def delete(rid: int, db: AsyncSession = Depends(get_db)):
    obj = await db.get(OutboundRoute, rid)
    if obj is None:
        raise HTTPException(404)
    await db.delete(obj)
    await db.commit()
    return RedirectResponse("/outbound-routes/", status_code=status.HTTP_303_SEE_OTHER)


async def _save(request, db: AsyncSession, data: dict, instance: OutboundRoute | None):
    for k in ("require_password", "enabled"):
        data[k] = k in data
    # 空文字を None に正規化 (Optional フィールド向け)
    for k in ("prefix_prepend", "cid_override", "password", "note"):
        if data.get(k) == "":
            data[k] = None
    if "trunk_id" in data:
        try:
            data["trunk_id"] = int(data["trunk_id"])
        except (TypeError, ValueError):
            pass

    try:
        validated = OutboundRouteForm.model_validate(data)
    except ValidationError as ve:
        return request.app.state.templates.TemplateResponse(
            request,
            "outbound_routes/form.html",
            {
                "item": instance,
                "form_action": (
                    f"/outbound-routes/{instance.id}" if instance else "/outbound-routes/"
                ),
                "title": "入力エラー",
                "errors": ve.errors(),
                "submitted": data,
                "trunks": await _trunk_choices(db),
                "pattern_presets": PATTERN_PRESETS,
                **all_enum_choices(),
            },
            status_code=400,
        )

    payload = validated.model_dump()
    if instance is None:
        instance = OutboundRoute(**payload)
        db.add(instance)
    else:
        for k, v in payload.items():
            setattr(instance, k, v)
    await db.commit()
    return RedirectResponse("/outbound-routes/", status_code=status.HTTP_303_SEE_OTHER)
