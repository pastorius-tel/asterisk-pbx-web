"""内線 (Extension) CRUD ルーター。

URL 設計:
  GET  /extensions/              一覧
  GET  /extensions/new           追加フォーム
  POST /extensions/              作成
  GET  /extensions/{id}/edit     編集フォーム
  POST /extensions/{id}          更新
  POST /extensions/{id}/delete   削除
"""

from __future__ import annotations

import secrets
import string

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import AudioFile, Extension
from app.routers._helpers import all_enum_choices
from app.routers.system import get_app_settings
from app.schemas.extension import ExtensionForm


async def _voicemail_audio_choices(db: AsyncSession) -> list[dict[str, str]]:
    """留守番電話専用内線の応答メッセージに使える音源の選択肢。

    カテゴリを限定せず、変換済み (再生可能) の音源をすべて出す
    (voicemail カテゴリで作るのが自然だが、ivr や custom で作った音源を
     流用したい場合もあるため)。
    """
    rows = (
        await db.scalars(
            select(AudioFile)
            .where(AudioFile.conversion_status == "ok")
            .order_by(AudioFile.category, AudioFile.name)
        )
    ).all()
    return [
        {"value": str(a.id), "label": f"[{a.category}] {a.name}"} for a in rows
    ]


router = APIRouter(prefix="/extensions", tags=["extensions"])


def _gen_secret(length: int = 16) -> str:
    """SIP パスワードを安全に自動生成。"""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


@router.get("/", response_class=HTMLResponse)
async def list_extensions(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    rows = (await db.scalars(select(Extension).order_by(Extension.extension))).all()
    return request.app.state.templates.TemplateResponse(
        request,
        "extensions/list.html",
        {"items": rows, "title": "内線一覧"},
    )


@router.get("/new", response_class=HTMLResponse)
async def new_extension_form(
    request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    app_settings = await get_app_settings(db)
    return request.app.state.templates.TemplateResponse(
        request,
        "extensions/form.html",
        {
            "item": None,
            "form_action": "/extensions/",
            "title": "内線の追加",
            "auto_secret": _gen_secret(),
            "reserve_leading_01_for_outbound": app_settings.reserve_leading_01_for_outbound,
            "voicemail_audio_choices": await _voicemail_audio_choices(db),
            **all_enum_choices(),
        },
    )


@router.post("/", response_class=HTMLResponse)
async def create_extension(
    request: Request,
    db: AsyncSession = Depends(get_db),
    form: dict = Depends(lambda: None),  # noqa: B008 placeholder
) -> HTMLResponse:
    raw = await request.form()
    return await _save_extension(request, db, dict(raw), instance=None)


@router.get("/{ext_id}/edit", response_class=HTMLResponse)
async def edit_extension_form(
    ext_id: int, request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    obj = await db.get(Extension, ext_id)
    if obj is None:
        raise HTTPException(status_code=404)
    app_settings = await get_app_settings(db)
    return request.app.state.templates.TemplateResponse(
        request,
        "extensions/form.html",
        {
            "item": obj,
            "form_action": f"/extensions/{ext_id}",
            "title": f"内線の編集 — {obj.extension}",
            "auto_secret": _gen_secret(),
            "reserve_leading_01_for_outbound": app_settings.reserve_leading_01_for_outbound,
            "voicemail_audio_choices": await _voicemail_audio_choices(db),
            **all_enum_choices(),
        },
    )


@router.post("/{ext_id}", response_class=HTMLResponse)
async def update_extension(
    ext_id: int, request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    obj = await db.get(Extension, ext_id)
    if obj is None:
        raise HTTPException(status_code=404)
    raw = await request.form()
    return await _save_extension(request, db, dict(raw), instance=obj)


@router.post("/{ext_id}/delete")
async def delete_extension(
    ext_id: int, db: AsyncSession = Depends(get_db)
) -> RedirectResponse:
    obj = await db.get(Extension, ext_id)
    if obj is None:
        raise HTTPException(status_code=404)
    await db.delete(obj)
    await db.commit()
    return RedirectResponse("/extensions/", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------


async def _save_extension(
    request: Request,
    db: AsyncSession,
    data: dict,
    instance: Extension | None,
) -> HTMLResponse | RedirectResponse:
    # チェックボックスは未チェックのとき POST に含まれない → 補正
    for bool_key in (
        "voicemail_enabled", "voicemail_attach",
        "voicemail_delete_after_email", "call_waiting", "enabled",
        "voicemail_only",
    ):
        data[bool_key] = bool_key in data
    # 空文字を None に正規化 (Optional フィールド向け)
    for k in (
        "codec_secondary", "voicemail_pin", "voicemail_email",
        "pickup_group", "callgroup", "note",
        "voicemail_greeting_audio_id", "no_answer_audio_id",
    ):
        if data.get(k) == "":
            data[k] = None

    app_settings = await get_app_settings(db)

    errors: list[dict] = []
    try:
        validated = ExtensionForm.model_validate(data)
    except ValidationError as ve:
        errors = ve.errors()
        validated = None

    # 0/1 始まりの内線番号チェック (DB 管理の AppSettings を参照するため
    # Pydantic スキーマ側ではなくここで行う)。
    ext_num = data.get("extension", "")
    if (
        app_settings.reserve_leading_01_for_outbound
        and ext_num
        and ext_num[0] in ("0", "1")
    ):
        errors.append({
            "loc": ("extension",),
            "msg": (
                "内線番号は 2 以降の数字で始めてください "
                "(0/1 始まりは外線発信用に予約されています。"
                "「システム」画面の設定で変更できます)。"
            ),
            "type": "value_error",
        })

    if errors:
        return request.app.state.templates.TemplateResponse(
            request,
            "extensions/form.html",
            {
                "item": instance,
                "form_action": (
                    f"/extensions/{instance.id}" if instance else "/extensions/"
                ),
                "title": "入力エラー",
                "errors": errors,
                "auto_secret": _gen_secret(),
                "submitted": data,
                "reserve_leading_01_for_outbound": app_settings.reserve_leading_01_for_outbound,
                "voicemail_audio_choices": await _voicemail_audio_choices(db),
            **all_enum_choices(),
            },
            status_code=400,
        )

    assert validated is not None
    payload = validated.model_dump()

    if instance is None:
        instance = Extension(**payload)
        db.add(instance)
    else:
        for k, v in payload.items():
            setattr(instance, k, v)

    await db.commit()
    return RedirectResponse("/extensions/", status_code=status.HTTP_303_SEE_OTHER)
