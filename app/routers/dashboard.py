"""ダッシュボード — トップページ。"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import CallLog, Extension, FaxLog
from app.services import voicemail_service
from app.services.ami import reload_asterisk_safely
from app.services.asterisk_config import write_all_configs

router = APIRouter()

DASHBOARD_ITEM_LIMIT = 5
"""ダッシュボードに表示する FAX 履歴・留守番電話メッセージの件数。
   詳細な一覧は各専用ページ (/fax/ 、/voicemail/) に任せ、ここでは
   概要だけを見せる想定。"""


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    # 発着信履歴 (着信・発信をそれぞれ直近分だけ。
    #  内線数などの統計カードは実運用で見る機会が無かったため v0.4.2 で廃止し、
    #  一番よく見る発着信履歴を先頭に置いている)
    recent_in = (
        await db.scalars(
            select(CallLog).where(CallLog.direction == "in")
            .order_by(desc(CallLog.started_at)).limit(DASHBOARD_ITEM_LIMIT)
        )
    ).all()
    recent_out = (
        await db.scalars(
            select(CallLog).where(CallLog.direction == "out")
            .order_by(desc(CallLog.started_at)).limit(DASHBOARD_ITEM_LIMIT)
        )
    ).all()

    # FAX 送受信履歴 (直近分だけ。詳細な一覧は /fax/ で見る想定)
    recent_faxes = (
        await db.scalars(
            select(FaxLog)
            .order_by(desc(FaxLog.created_at))
            .limit(DASHBOARD_ITEM_LIMIT)
        )
    ).all()

    # 留守番電話メッセージ一覧 (ボイスメールが有効な内線ぶんを集約し、
    # 新しい順に上位だけ表示。詳細な一覧は /voicemail/ で見る想定)
    vm_exts = (
        await db.scalars(
            select(Extension).where(Extension.voicemail_enabled.is_(True))
        )
    ).all()
    recent_voicemails = []
    vm_ext_names = {e.extension: e.display_name for e in vm_exts}
    for e in vm_exts:
        recent_voicemails.extend(voicemail_service.list_messages(e.extension))
    recent_voicemails.sort(key=lambda m: m.when or datetime.min, reverse=True)
    recent_voicemails = recent_voicemails[:DASHBOARD_ITEM_LIMIT]

    return request.app.state.templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "recent_in": recent_in,
            "recent_out": recent_out,
            "recent_faxes": recent_faxes,
            "recent_voicemails": recent_voicemails,
            "vm_ext_names": vm_ext_names,
            "title": "ダッシュボード",
        },
    )


@router.post("/apply", response_class=HTMLResponse)
async def apply_config(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    """設定ファイルを書き出して Asterisk をリロード。"""
    written = await write_all_configs(db)
    reload_results = await reload_asterisk_safely()

    return request.app.state.templates.TemplateResponse(
        request,
        "partials/apply_result.html",
        {
            "written": [{"name": k, "path": str(v)} for k, v in written.items()],
            "reload_results": reload_results,
        },
    )
