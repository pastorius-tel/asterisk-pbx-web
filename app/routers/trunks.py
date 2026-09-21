"""SIP トランク (Trunk) CRUD ルーター。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Trunk
from app.routers._helpers import all_enum_choices
from app.schemas.trunk import TrunkForm
from app.services.presets import PRESET_CHOICES, PRESETS

router = APIRouter(prefix="/trunks", tags=["trunks"])


def _form_context() -> dict:
    """フォーム用に共通で渡す context (列挙型 + プリセット)。"""
    return {
        **all_enum_choices(),
        "preset_choices": PRESET_CHOICES,
        "presets": PRESETS,
    }


@router.get("/", response_class=HTMLResponse)
async def list_trunks(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    rows = (await db.scalars(select(Trunk).order_by(Trunk.name))).all()
    return request.app.state.templates.TemplateResponse(
        request, "trunks/list.html", {"items": rows, "title": "トランク一覧"}
    )


@router.get("/new", response_class=HTMLResponse)
async def new_trunk_form(request: Request) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request,
        "trunks/form.html",
        {
            "item": None,
            "form_action": "/trunks/",
            "title": "トランクの追加",
            **_form_context(),
        },
    )


@router.post("/", response_class=HTMLResponse)
async def create_trunk(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    raw = dict(await request.form())
    return await _save(request, db, raw, instance=None)


@router.get("/{trunk_id}/edit", response_class=HTMLResponse)
async def edit_trunk_form(
    trunk_id: int, request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    obj = await db.get(Trunk, trunk_id)
    if obj is None:
        raise HTTPException(status_code=404)
    return request.app.state.templates.TemplateResponse(
        request,
        "trunks/form.html",
        {
            "item": obj,
            "form_action": f"/trunks/{trunk_id}",
            "title": f"トランクの編集 — {obj.name}",
            **_form_context(),
        },
    )


@router.post("/{trunk_id}", response_class=HTMLResponse)
async def update_trunk(
    trunk_id: int, request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    obj = await db.get(Trunk, trunk_id)
    if obj is None:
        raise HTTPException(status_code=404)
    raw = dict(await request.form())
    return await _save(request, db, raw, instance=obj)


@router.post("/{trunk_id}/delete")
async def delete_trunk(
    trunk_id: int, db: AsyncSession = Depends(get_db)
) -> RedirectResponse:
    obj = await db.get(Trunk, trunk_id)
    if obj is None:
        raise HTTPException(status_code=404)
    await db.delete(obj)
    await db.commit()
    return RedirectResponse("/trunks/", status_code=status.HTTP_303_SEE_OTHER)


async def _save(request: Request, db: AsyncSession, data: dict, instance: Trunk | None):
    for k in (
        "qualify", "enabled",
        "use_mac_auth", "disable_rport",
        "trust_id_inbound", "send_pai",
    ):
        data[k] = k in data
    # 空文字列を None に正規化 (Optional フィールド向け)
    for k in (
        "username", "secret", "auth_username", "from_user", "from_domain",
        "outbound_caller_id", "codec_secondary",
        "hgw_extension_number", "additional_did_numbers", "note",
    ):
        if data.get(k) == "":
            data[k] = None

    try:
        validated = TrunkForm.model_validate(data)
    except ValidationError as ve:
        return request.app.state.templates.TemplateResponse(
            request,
            "trunks/form.html",
            {
                "item": instance,
                "form_action": f"/trunks/{instance.id}" if instance else "/trunks/",
                "title": "入力エラー",
                "errors": ve.errors(),
                "submitted": data,
                **_form_context(),
            },
            status_code=400,
        )

    payload = validated.model_dump()
    if instance is None:
        instance = Trunk(**payload)
        db.add(instance)
    else:
        for k, v in payload.items():
            setattr(instance, k, v)
    await db.commit()
    return RedirectResponse("/trunks/", status_code=status.HTTP_303_SEE_OTHER)
