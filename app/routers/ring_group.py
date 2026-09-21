"""リンググループ CRUD。

複数の内線をチェックボックスで選択し、着信時にまとめて呼び出す
(全員同時 / 順番に 等の戦略) グループを作成する。
着信ルートの転送先として「リンググループ」を選べるようにするための
土台 (ダイヤルプラン生成・着信ルートの選択肢は既に実装済み)。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Extension, RingGroup, RingGroupMember
from app.routers._helpers import all_enum_choices
from app.routers.system import get_app_settings
from app.schemas.ring_group import RingGroupForm

router = APIRouter(prefix="/ring-groups", tags=["ring_groups"])


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


@router.get("/", response_class=HTMLResponse)
async def list_ring_groups(
    request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    rows = (
        await db.scalars(select(RingGroup).order_by(RingGroup.group_number))
    ).all()
    return request.app.state.templates.TemplateResponse(
        request, "ring_groups/list.html",
        {"items": rows, "title": "リンググループ"},
    )


@router.get("/new", response_class=HTMLResponse)
async def new_ring_group_form(
    request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, "ring_groups/form.html",
        {
            "item": None,
            "form_action": "/ring-groups/",
            "title": "リンググループの追加",
            "ext_choices": await _extension_choices(db),
            "selected_members": [],
            "vm_choices": await _voicemail_choices(db),
            **all_enum_choices(),
        },
    )


@router.post("/", response_class=HTMLResponse)
async def create_ring_group(
    request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    raw = await request.form()
    return await _save(request, db, raw, None)


@router.get("/{rg_id}/edit", response_class=HTMLResponse)
async def edit_ring_group_form(
    rg_id: int, request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    obj = await db.get(RingGroup, rg_id)
    if obj is None:
        raise HTTPException(status_code=404)
    return request.app.state.templates.TemplateResponse(
        request, "ring_groups/form.html",
        {
            "item": obj,
            "form_action": f"/ring-groups/{rg_id}",
            "title": f"リンググループの編集 — {obj.name}",
            "ext_choices": await _extension_choices(db),
            "selected_members": [m.extension for m in obj.members],
            "vm_choices": await _voicemail_choices(db),
            **all_enum_choices(),
        },
    )


@router.post("/{rg_id}", response_class=HTMLResponse)
async def update_ring_group(
    rg_id: int, request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    obj = await db.get(RingGroup, rg_id)
    if obj is None:
        raise HTTPException(status_code=404)
    raw = await request.form()
    return await _save(request, db, raw, obj)


@router.post("/{rg_id}/delete")
async def delete_ring_group(
    rg_id: int, db: AsyncSession = Depends(get_db)
) -> RedirectResponse:
    obj = await db.get(RingGroup, rg_id)
    if obj is None:
        raise HTTPException(status_code=404)
    await db.delete(obj)
    await db.commit()
    return RedirectResponse("/ring-groups/", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------


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


async def _save(request, db: AsyncSession, raw, instance: RingGroup | None):  # type: ignore[no-untyped-def]
    # multipart form では同名キーが複数値になる → getlist
    member_extensions = [v for v in raw.getlist("member_extensions") if v]

    fallback_type = raw.get("fallback_type") or None
    fallback_value = raw.get("fallback_value") or None
    if fallback_type == "hangup":
        fallback_value = None

    data = {
        "group_number": raw.get("group_number", ""),
        "name": raw.get("name", ""),
        "strategy": raw.get("strategy", "ringall"),
        "ring_seconds": raw.get("ring_seconds") or 20,
        "fallback_type": fallback_type,
        "fallback_value": fallback_value,
        "enabled": "enabled" in raw,
        "note": raw.get("note") or None,
        "member_extensions": member_extensions,
    }

    try:
        validated = RingGroupForm.model_validate(data)
        errors: list[dict] = []
    except ValidationError as ve:
        validated = None
        errors = ve.errors()

    # 0/1 始まりのグループ番号チェック (内線番号ポリシーと同じ理由)。
    # グループ番号は [from-internal] に明示 exten として登録されるため、
    # 内線番号ポリシーの N/X ワイルドカードの制約を直接は受けないが、
    # 「0/1 始まりは外線発信用に予約」という運用ポリシーと矛盾しないよう
    # ここでも同様にチェックする。
    app_settings = await get_app_settings(db)
    gn = data.get("group_number", "")
    if app_settings.reserve_leading_01_for_outbound and gn and gn[0] in ("0", "1"):
        errors.append({
            "loc": ("group_number",),
            "msg": (
                "グループ番号は 2 以降の数字で始めてください "
                "(0/1 始まりは外線発信用に予約されています。"
                "「システム」画面の設定で変更できます)。"
            ),
            "type": "value_error",
        })

    if errors:
        return request.app.state.templates.TemplateResponse(
            request, "ring_groups/form.html",
            {
                "item": instance,
                "form_action": (
                    f"/ring-groups/{instance.id}" if instance else "/ring-groups/"
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
        instance = RingGroup(**payload)
        db.add(instance)
        await db.flush()
    else:
        for k, v in payload.items():
            setattr(instance, k, v)
        # 既存メンバーをクリア (更新時のみ)
        for m in list(instance.members):
            await db.delete(m)
        await db.flush()

    for idx, ext in enumerate(members):
        db.add(RingGroupMember(group_id=instance.id, extension=ext, order_index=idx))

    await db.commit()
    return RedirectResponse("/ring-groups/", status_code=status.HTTP_303_SEE_OTHER)
