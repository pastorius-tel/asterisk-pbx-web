"""迷惑電話ブロックリスト CRUD。

利用者自身が発信者番号 (またはパターン) を登録し、該当する着信を
即切断または指定内線の留守番電話へ転送する。インターネットから自動で
迷惑電話リストを取得する機能ではない (信頼できる無料の一括取得手段が
存在しないため。README 参照)。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import AudioFile, BlockedNumber, Extension
from app.routers._helpers import form_int
from app.schemas.blocked_number import BlockedNumberForm

router = APIRouter(prefix="/blocked-numbers", tags=["blocked_numbers"])


async def _extension_choices(db: AsyncSession) -> list[dict[str, str]]:
    rows = (
        await db.scalars(
            select(Extension).where(Extension.enabled.is_(True)).order_by(Extension.extension)
        )
    ).all()
    return [{"value": e.extension, "label": f"{e.extension} {e.display_name}"} for e in rows]


async def _audio_choices(db: AsyncSession) -> list[dict[str, str]]:
    """「メッセージを流して切断」で選べる音源の一覧。

    変換が完了しているものだけを出す (変換前/失敗の音源を選べてしまうと、
    着信時に存在しないファイルを再生しようとして通話が異常終了する)。
    """
    rows = (
        await db.scalars(
            select(AudioFile)
            .where(AudioFile.conversion_status == "ok")
            .order_by(AudioFile.name)
        )
    ).all()
    return [{"value": str(a.id), "label": a.name} for a in rows]


@router.get("/", response_class=HTMLResponse)
async def list_blocked_numbers(
    request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    rows = (await db.scalars(select(BlockedNumber).order_by(BlockedNumber.id.desc()))).all()
    # 一覧で音源名を出すための対応表 (削除済みの音源は名前が引けない)
    audio_names = {
        a.id: a.name for a in (await db.scalars(select(AudioFile))).all()
    }
    return request.app.state.templates.TemplateResponse(
        request, "blocked_numbers/list.html",
        {"items": rows, "title": "迷惑電話ブロックリスト", "audio_names": audio_names},
    )


@router.get("/new", response_class=HTMLResponse)
async def new_blocked_number_form(
    request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, "blocked_numbers/form.html",
        {
            "item": None, "form_action": "/blocked-numbers/", "title": "ブロック番号の追加",
            "ext_choices": await _extension_choices(db),
            "audio_choices": await _audio_choices(db),
        },
    )


@router.post("/", response_class=HTMLResponse)
async def create_blocked_number(
    request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    raw = await request.form()
    return await _save(request, db, raw, None)


@router.get("/{b_id}/edit", response_class=HTMLResponse)
async def edit_blocked_number_form(
    b_id: int, request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    obj = await db.get(BlockedNumber, b_id)
    if obj is None:
        raise HTTPException(status_code=404)
    return request.app.state.templates.TemplateResponse(
        request, "blocked_numbers/form.html",
        {
            "item": obj, "form_action": f"/blocked-numbers/{b_id}",
            "title": f"ブロック番号の編集 — {obj.pattern}",
            "ext_choices": await _extension_choices(db),
            "audio_choices": await _audio_choices(db),
        },
    )


@router.post("/{b_id}", response_class=HTMLResponse)
async def update_blocked_number(
    b_id: int, request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    obj = await db.get(BlockedNumber, b_id)
    if obj is None:
        raise HTTPException(status_code=404)
    raw = await request.form()
    return await _save(request, db, raw, obj)


@router.post("/{b_id}/delete")
async def delete_blocked_number(
    b_id: int, db: AsyncSession = Depends(get_db)
) -> RedirectResponse:
    obj = await db.get(BlockedNumber, b_id)
    if obj is None:
        raise HTTPException(status_code=404)
    await db.delete(obj)
    await db.commit()
    return RedirectResponse("/blocked-numbers/", status_code=status.HTTP_303_SEE_OTHER)


async def _save(request, db: AsyncSession, raw, instance: BlockedNumber | None):  # type: ignore[no-untyped-def]
    data = {
        "pattern": raw.get("pattern", ""),
        "action": raw.get("action", "hangup"),
        "voicemail_target": raw.get("voicemail_target") or None,
        "audio_id": form_int(raw.get("audio_id")),
        "note": raw.get("note") or None,
        "enabled": "enabled" in raw,
    }

    try:
        validated = BlockedNumberForm.model_validate(data)
        errors: list[dict] = []
    except ValidationError as ve:
        validated = None
        errors = ve.errors()

    if errors:
        return request.app.state.templates.TemplateResponse(
            request, "blocked_numbers/form.html",
            {
                "item": instance,
                "form_action": f"/blocked-numbers/{instance.id}" if instance else "/blocked-numbers/",
                "title": "入力エラー",
                "errors": errors,
                "submitted": data,
                "ext_choices": await _extension_choices(db),
                "audio_choices": await _audio_choices(db),
            },
            status_code=400,
        )

    assert validated is not None
    payload = validated.model_dump()

    if instance is None:
        instance = BlockedNumber(**payload)
        db.add(instance)
    else:
        for k, v in payload.items():
            setattr(instance, k, v)

    await db.commit()
    return RedirectResponse("/blocked-numbers/", status_code=status.HTTP_303_SEE_OTHER)
