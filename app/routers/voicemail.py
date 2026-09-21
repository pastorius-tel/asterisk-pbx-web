"""ボイスメール (留守番電話) ルーター。

  GET  /voicemail/                                   一覧 (全内線横断・任意で内線フィルタ)
  GET  /voicemail/config                              設定画面 (ファイル名・メールテンプレート)
  POST /voicemail/config                              設定保存
  GET  /voicemail/{ext}/{folder}/{msg}/play           ブラウザ内再生 (inline)
  GET  /voicemail/{ext}/{folder}/{msg}/download       ダウンロード (attachment)
  POST /voicemail/{ext}/{folder}/{msg}/delete         削除 (単体)
  POST /voicemail/delete-bulk                         削除 (複数選択・一括)
  GET  /voicemail/hook/received                       Asterisk から録音完了通知 (内部用)

メッセージの実体は Asterisk (app_voicemail) がファイルシステムに直接
保存するもの (voicemail_service 参照) を都度スキャンして表示する。
本ツール側で DB テーブルは持たない (実体とのズレが起きないため)。

メール通知は Asterisk 自身の sendmail 経由ではなく、本ツールが
録音完了フック経由で受け取って SMTP 送信する (FAX と同じ SMTP 設定
(FaxConfig) を共有)。日時表記を確実に日本語化でき、件名・本文・
添付ファイル名もすべて自由にカスタマイズできるようにするため。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.hooks import verify_hook
from app.models import Extension, FaxConfig
from app.routers.system import get_app_settings
from app.services import voicemail_service

log = logging.getLogger(__name__)

router = APIRouter(prefix="/voicemail", tags=["voicemail"])


async def _get_fax_config(db: AsyncSession) -> FaxConfig | None:
    """FAX 設定 (SMTP 接続情報を留守番電話の通知メールと共有するため) を取得。"""
    return (await db.scalars(select(FaxConfig).limit(1))).first()


@router.get("/", response_class=HTMLResponse)
async def voicemail_home(
    request: Request,
    db: AsyncSession = Depends(get_db),
    extension: str | None = None,
) -> HTMLResponse:
    exts = (
        await db.scalars(
            select(Extension)
            .where(Extension.voicemail_enabled.is_(True))
            .order_by(Extension.extension)
        )
    ).all()

    target_exts = [e.extension for e in exts]
    if extension and extension in target_exts:
        target_exts = [extension]

    messages = []
    for ext in target_exts:
        messages.extend(voicemail_service.list_messages(ext))
    messages.sort(key=lambda m: m.when or datetime.min, reverse=True)

    ext_names = {e.extension: e.display_name for e in exts}

    return request.app.state.templates.TemplateResponse(
        request, "voicemail/list.html",
        {
            "title": "留守番電話",
            "messages": messages,
            "extensions": exts,
            "ext_names": ext_names,
            "selected_extension": extension or "",
            "storage_available": voicemail_service.storage_available(),
        },
    )


@router.get("/config", response_class=HTMLResponse)
async def voicemail_config_form(
    request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    app_settings = await get_app_settings(db)
    return request.app.state.templates.TemplateResponse(
        request, "voicemail/config.html",
        {"title": "留守番電話 設定", "app_settings": app_settings},
    )


@router.post("/config", response_class=HTMLResponse)
async def voicemail_config_save(
    db: AsyncSession = Depends(get_db),
    voicemail_filename_pattern: str | None = Form(None),
    voicemail_datetime_format: str | None = Form(None),
    voicemail_mail_subject_template: str | None = Form(None),
    voicemail_mail_body_template: str | None = Form(None),
) -> RedirectResponse:
    app_settings = await get_app_settings(db)
    if voicemail_filename_pattern and voicemail_filename_pattern.strip():
        app_settings.voicemail_filename_pattern = voicemail_filename_pattern.strip()
    if voicemail_datetime_format and voicemail_datetime_format.strip():
        app_settings.voicemail_datetime_format = voicemail_datetime_format.strip()
    if voicemail_mail_subject_template and voicemail_mail_subject_template.strip():
        app_settings.voicemail_mail_subject_template = voicemail_mail_subject_template.strip()
    if voicemail_mail_body_template and voicemail_mail_body_template.strip():
        app_settings.voicemail_mail_body_template = voicemail_mail_body_template.strip()
    await db.commit()
    return RedirectResponse("/voicemail/config", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/{extension}/{folder}/{msg_num}/play")
async def voicemail_play(
    extension: str, folder: str, msg_num: str, db: AsyncSession = Depends(get_db)
) -> FileResponse:
    msg = voicemail_service.get_message(extension, folder, msg_num)
    if msg is None or msg.audio_path is None or not msg.audio_path.exists():
        raise HTTPException(404, detail="音声ファイルが見つかりません。")
    app_settings = await get_app_settings(db)
    fname = voicemail_service.build_voicemail_filename(
        app_settings.voicemail_filename_pattern,
        app_settings.voicemail_datetime_format,
        extension, msg.when,
    )
    return FileResponse(
        msg.audio_path, media_type="audio/wav", filename=fname,
        content_disposition_type="inline",
    )


@router.get("/{extension}/{folder}/{msg_num}/download")
async def voicemail_download(
    extension: str, folder: str, msg_num: str, db: AsyncSession = Depends(get_db)
) -> FileResponse:
    msg = voicemail_service.get_message(extension, folder, msg_num)
    if msg is None or msg.audio_path is None or not msg.audio_path.exists():
        raise HTTPException(404, detail="音声ファイルが見つかりません。")
    app_settings = await get_app_settings(db)
    fname = voicemail_service.build_voicemail_filename(
        app_settings.voicemail_filename_pattern,
        app_settings.voicemail_datetime_format,
        extension, msg.when,
    )
    return FileResponse(msg.audio_path, media_type="audio/wav", filename=fname)


@router.post("/{extension}/{folder}/{msg_num}/delete")
async def voicemail_delete(
    extension: str, folder: str, msg_num: str,
) -> RedirectResponse:
    voicemail_service.delete_message(extension, folder, msg_num)
    return RedirectResponse("/voicemail/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/delete-bulk")
async def voicemail_delete_bulk(
    vm_keys: list[str] = Form(default_factory=list),
) -> RedirectResponse:
    """一覧でチェックした複数件をまとめて削除。

    各チェックボックスの value は "内線:フォルダ:メッセージ番号" の
    複合キー文字列 (例 '201:INBOX:0000')。
    """
    for key in vm_keys:
        parts = key.split(":")
        if len(parts) != 3:
            continue
        ext, folder, msg_num = parts
        voicemail_service.delete_message(ext, folder, msg_num)
    return RedirectResponse("/voicemail/", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/hook/received")
async def voicemail_hook_received(
    request: Request,
    db: AsyncSession = Depends(get_db),
    token: str = "",
    mailbox: str = "",
    vmstatus: str = "",
) -> dict:
    """Asterisk の VoiceMail() 実行後に curl で叩かれる内部フック。

    VMSTATUS が SUCCESS の場合のみ、その内線 (mailbox) の最新メッセージ
    (INBOX 内で最も新しいもの) を取得し、通知メールアドレスが設定されて
    いれば SMTP で送信する。件名・本文・日時表記・添付ファイル名は
    すべて本ツール側 (Python) で組み立てるため、Asterisk のロケール
    設定に左右されず確実に日本語表記になる。
    """
    await verify_hook(request, db, "voicemail", token)

    if vmstatus.upper() != "SUCCESS":
        return {"ok": False, "reason": f"VMSTATUS={vmstatus}"}

    ext = (
        await db.scalars(select(Extension).where(Extension.extension == mailbox))
    ).first()
    if ext is None:
        return {"ok": False, "reason": f"内線 {mailbox} が見つかりません"}
    if not (ext.voicemail_email or "").strip():
        # 通知メール未設定の内線もあり得る (録音自体は正常) ので、
        # エラーではなく単に「通知の必要が無い」旨を返す。
        return {"ok": True, "reason": "通知メール未設定のため送信スキップ"}

    messages = voicemail_service.list_messages(mailbox)
    if not messages:
        return {"ok": False, "reason": "メッセージが見つかりません"}
    latest = messages[0]  # list_messages は新しい順にソート済み

    fax_cfg = await _get_fax_config(db)
    if fax_cfg is None:
        return {"ok": False, "reason": "SMTP 設定 (FAX設定画面のメール設定) が未構成です"}
    app_settings = await get_app_settings(db)

    # smtplib は同期 I/O のため、イベントループをブロックしないよう
    # 別スレッドで実行する (FAX のメール送信と同じ方針)。
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        voicemail_service.send_voicemail_notification,
        fax_cfg, app_settings, latest, ext.display_name,
        ext.voicemail_email, ext.voicemail_attach,
    )

    if not result.ok:
        log.warning("留守番電話メール送信失敗 (内線 %s): %s", mailbox, result.detail)
    elif ext.voicemail_delete_after_email:
        voicemail_service.delete_message(latest.extension, latest.folder, latest.msg_num)

    return {"ok": result.ok, "detail": result.detail}
