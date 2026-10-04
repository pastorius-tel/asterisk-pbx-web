"""キュー CRUD。

複数の内線をチェックボックスで選択し、着信をオペレーターに順次
割り振る「待ち行列」を作成する。着信ルート・リンググループのフォール
バック・IVR 等の転送先として「キュー」を選べるようにするための実体
(ダイヤルプラン生成・queues.conf 生成は既に実装済み)。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Extension, Queue, QueueMember
from app.routers._helpers import all_enum_choices
from app.routers.system import get_app_settings
from app.schemas.queue import QueueForm

router = APIRouter(prefix="/queues", tags=["queues"])


async def _extension_choices(db: AsyncSession) -> list[dict[str, str]]:
    rows = (
        await db.scalars(
            select(Extension).where(Extension.enabled.is_(True)).order_by(Extension.extension)
        )
    ).all()
    return [
        {"value": e.extension, "label": f"{e.extension} {e.display_name}"}
        for e in rows
    ]


async def _voicemail_choices(db: AsyncSession) -> list[dict[str, str]]:
    rows = (
        await db.scalars(
            select(Extension).where(Extension.voicemail_enabled.is_(True))
            .order_by(Extension.extension)
        )
    ).all()
    return [
        {"value": e.extension, "label": f"{e.extension} {e.display_name}"}
        for e in rows
    ]


@router.get("/", response_class=HTMLResponse)
async def list_queues(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    rows = (await db.scalars(select(Queue).order_by(Queue.queue_number))).all()
    return request.app.state.templates.TemplateResponse(
        request, "queues/list.html",
        {"items": rows, "title": "キュー"},
    )


@router.get("/new", response_class=HTMLResponse)
async def new_queue_form(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, "queues/form.html",
        {
            "item": None,
            "form_action": "/queues/",
            "title": "キューの追加",
            "ext_choices": await _extension_choices(db),
            "selected_members": [],
            "vm_choices": await _voicemail_choices(db),
            **all_enum_choices(),
        },
    )


@router.post("/", response_class=HTMLResponse)
async def create_queue(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    raw = await request.form()
    return await _save(request, db, raw, None)


@router.get("/{q_id}/edit", response_class=HTMLResponse)
async def edit_queue_form(
    q_id: int, request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    obj = await db.get(Queue, q_id)
    if obj is None:
        raise HTTPException(status_code=404)
    return request.app.state.templates.TemplateResponse(
        request, "queues/form.html",
        {
            "item": obj,
            "form_action": f"/queues/{q_id}",
            "title": f"キューの編集 — {obj.name}",
            "ext_choices": await _extension_choices(db),
            "selected_members": [m.extension for m in obj.members],
            "vm_choices": await _voicemail_choices(db),
            **all_enum_choices(),
        },
    )


@router.post("/{q_id}", response_class=HTMLResponse)
async def update_queue(
    q_id: int, request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    obj = await db.get(Queue, q_id)
    if obj is None:
        raise HTTPException(status_code=404)
    raw = await request.form()
    return await _save(request, db, raw, obj)


@router.post("/{q_id}/delete")
async def delete_queue(q_id: int, db: AsyncSession = Depends(get_db)) -> RedirectResponse:
    obj = await db.get(Queue, q_id)
    if obj is None:
        raise HTTPException(status_code=404)
    await db.delete(obj)
    await db.commit()
    return RedirectResponse("/queues/", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------


async def _save(request, db: AsyncSession, raw, instance: Queue | None):  # type: ignore[no-untyped-def]
    member_extensions = [v for v in raw.getlist("member_extensions") if v]

    fallback_type = raw.get("fallback_type") or None
    fallback_value = raw.get("fallback_value") or None
    if fallback_type == "hangup":
        fallback_value = None

    data = {
        "queue_number": raw.get("queue_number", ""),
        "name": raw.get("name", ""),
        "strategy": raw.get("strategy", "ringall"),
        "timeout": raw.get("timeout") or 15,
        "retry": raw.get("retry") or 5,
        "wrapuptime": raw.get("wrapuptime") or 0,
        "maxlen": raw.get("maxlen") or 0,
        "music_class": raw.get("music_class") or "default",
        "fallback_type": fallback_type,
        "fallback_value": fallback_value,
        "enabled": "enabled" in raw,
        "note": raw.get("note") or None,
        "member_extensions": member_extensions,
    }

    try:
        validated = QueueForm.model_validate(data)
        errors: list[dict] = []
    except ValidationError as ve:
        validated = None
        errors = ve.errors()

    # 0/1 始まりのキュー番号チェック (内線番号ポリシーと同じ理由。
    # リンググループと同様、キュー番号も [from-internal] 相当の
    # 名前空間と衝突しないよう同じ運用ポリシーを適用する)。
    app_settings = await get_app_settings(db)
    qn = data.get("queue_number", "")
    if app_settings.reserve_leading_01_for_outbound and qn and qn[0] in ("0", "1"):
        errors.append({
            "loc": ("queue_number",),
            "msg": (
                "キュー番号は 2 以降の数字で始めてください "
                "(0/1 始まりは外線発信用に予約されています。"
                "「システム」画面の設定で変更できます)。"
            ),
            "type": "value_error",
        })

    if errors:
        return request.app.state.templates.TemplateResponse(
            request, "queues/form.html",
            {
                "item": instance,
                "form_action": (
                    f"/queues/{instance.id}" if instance else "/queues/"
                ),
                "title": "入力エラー",
                "errors": errors,
                "submitted": data,
                "ext_choices": await _extension_choices(db),
                "selected_members": member_extensions,
                "vm_choices": await _voicemail_choices(db),
                **all_enum_choices(),
            },
            status_code=400,
        )

    assert validated is not None
    payload = validated.model_dump()
    members = payload.pop("member_extensions")

    if instance is None:
        instance = Queue(**payload)
        db.add(instance)
        await db.flush()
    else:
        for k, v in payload.items():
            setattr(instance, k, v)
        for m in list(instance.members):
            await db.delete(m)
        await db.flush()

    for ext in members:
        db.add(QueueMember(queue_id=instance.id, extension=ext))

    await db.commit()
    return RedirectResponse("/queues/", status_code=status.HTTP_303_SEE_OTHER)
