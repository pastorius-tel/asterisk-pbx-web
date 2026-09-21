"""システム設定 (日本語音声インストール / 言語切替 / ダイヤルポリシー 等)。

実行時に切り替え可能な設定は AppSettings テーブル (singleton row) に
保持する。gunicorn の複数ワーカー間でも一貫した値を返すため。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
    StreamingResponse,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import AppSettings, Extension
from app.services import backup_service, tts_service

log = logging.getLogger(__name__)

router = APIRouter(prefix="/system", tags=["system"])


async def _get_asterisk_version() -> str | None:
    """`asterisk -rx "core show version"` を安全に (読み取り専用) 実行。

    失敗しても例外を投げず None を返す (Asterisk 未起動時等)。
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "asterisk", "-rx", "core show version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        text = stdout.decode("utf-8", "replace").strip()
        return text or None
    except (TimeoutError, OSError):
        return None

# 日本語音声のインストール状況確認用パス
JA_SOUNDS_DIR = Path("/var/lib/asterisk/sounds/ja")
# 「正しくインストールされている」と判定する代表的ファイル名 (拡張子任意)
JA_SOUNDS_SAMPLES = ("vm-intro", "auth-thankyou", "hello-world")


async def get_app_settings(db: AsyncSession) -> AppSettings:
    """AppSettings の singleton row を取得 (無ければ既定値で作成)。"""
    row = (await db.scalars(select(AppSettings).limit(1))).first()
    if row is None:
        row = AppSettings()
        db.add(row)
        await db.commit()
        await db.refresh(row)
    # 既存 DB への自動マイグレーションで追加されたカラムは、SQLite の
    # ALTER TABLE ADD COLUMN が Python 側の default= を反映しないため
    # NULL のままになりうる (設定画面をまだ一度も保存していない場合)。
    # 表示・利用側で None のまま扱うと画面表示が崩れるため、ここで
    # メモリ上だけ補完する (DB へは書き込まない)。
    if not row.internal_ring_seconds:
        row.internal_ring_seconds = 30
    if not row.voicemail_filename_pattern:
        row.voicemail_filename_pattern = "{extension}_{datetime}"
    if not row.voicemail_datetime_format:
        row.voicemail_datetime_format = "%Y%m%d_%H%M%S"
    if not row.voicemail_mail_subject_template:
        row.voicemail_mail_subject_template = "[留守番電話] {extension} 様へ新着メッセージ"
    if not row.voicemail_mail_body_template:
        row.voicemail_mail_body_template = (
            "{extension} 様\n\n"
            "新しいボイスメッセージが届きました。\n\n"
            "  発信者 : {caller}\n"
            "  受信日時 : {datetime}\n"
            "  長さ : {duration}\n"
            "  メールボックス : {extension}\n\n"
            "添付の音声ファイルをご確認ください。\n"
        )
    if not row.international_call_action:
        row.international_call_action = "none"
    return row


def _check_ja_sounds() -> dict[str, object]:
    """日本語音声のインストール状態を返す。"""
    if not JA_SOUNDS_DIR.exists():
        return {"installed": False, "file_count": 0, "samples_found": []}
    try:
        samples_found = []
        for base in JA_SOUNDS_SAMPLES:
            for f in JA_SOUNDS_DIR.glob(f"{base}.*"):
                samples_found.append(f.name)
                break
        file_count = sum(1 for _ in JA_SOUNDS_DIR.rglob("*") if _.is_file())
        return {
            "installed": len(samples_found) >= 2,
            "file_count": file_count,
            "samples_found": samples_found,
        }
    except OSError:
        return {"installed": False, "file_count": 0, "samples_found": []}


@router.get("/", response_class=HTMLResponse)
async def system_home(
    request: Request, dt_error: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    state = _check_ja_sounds()
    app_settings = await get_app_settings(db)
    asterisk_version = await _get_asterisk_version()
    _dt_done, _dt_total, _ = tts_service.datetime_sounds_status()
    ext_choices = [
        {"value": e.extension, "label": f"{e.extension} {e.display_name}"}
        for e in (
            await db.scalars(
                select(Extension).where(Extension.enabled.is_(True)).order_by(Extension.extension)
            )
        ).all()
    ]
    return request.app.state.templates.TemplateResponse(
        request, "system/index.html",
        {
            "title": "システム",
            "ja_state": state,
            "ja_sounds_dir": str(JA_SOUNDS_DIR),
            "current_language": app_settings.system_language,
            "reserve_leading_01_for_outbound": app_settings.reserve_leading_01_for_outbound,
            "sip_port": app_settings.sip_port,
            "internal_ring_seconds": app_settings.internal_ring_seconds,
            "international_call_action": app_settings.international_call_action,
            "international_voicemail_target": app_settings.international_voicemail_target,
            "voicemail_announce_datetime": app_settings.voicemail_announce_datetime,
            "dt_sounds_done": _dt_done,
            "dt_sounds_total": _dt_total,
            "dt_error": dt_error,
            "ext_choices": ext_choices,
            "backups": backup_service.list_backups(),
            "asterisk_version": asterisk_version,
        },
    )


@router.post("/install-ja-sounds", response_class=StreamingResponse)
async def install_ja_sounds(request: Request) -> StreamingResponse:
    """日本語音声インストールスクリプトを実行し、出力をストリーミング表示。"""
    script_path = (
        Path(__file__).resolve().parent.parent.parent
        / "scripts" / "install_japanese_sounds.sh"
    )
    if not script_path.exists():
        async def err_gen():
            yield f"ERROR: スクリプトが見つかりません: {script_path}\n".encode()
        return StreamingResponse(err_gen(), media_type="text/plain; charset=utf-8")

    async def run_and_stream():
        proc = await asyncio.create_subprocess_exec(
            "bash", str(script_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        yield "=== 日本語音声インストール開始 ===\n".encode()
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            yield line
        rc = await proc.wait()
        yield f"\n=== 終了コード: {rc} ===\n".encode()
        if rc == 0:
            yield (
                "\n完了しました。"
                "ダッシュボードの「変更を Asterisk へ反映」を押すと、"
                "全内線エンドポイントに language=ja が反映されます。\n"
            ).encode()

    return StreamingResponse(
        run_and_stream(), media_type="text/plain; charset=utf-8"
    )


@router.post("/language", response_class=HTMLResponse)
async def change_language(
    request: Request, db: AsyncSession = Depends(get_db), lang: str = Form(""),
) -> RedirectResponse:
    """システム言語設定を変更 (DB に保存。全 gunicorn ワーカーに反映される)。"""
    if lang not in ("ja", "en", ""):
        lang = "ja"
    app_settings = await get_app_settings(db)
    app_settings.system_language = lang
    await db.commit()
    return RedirectResponse("/system/", status_code=303)


@router.post("/dial-policy", response_class=HTMLResponse)
async def change_dial_policy(
    request: Request,
    db: AsyncSession = Depends(get_db),
    reserve_leading_01: str = Form(""),
) -> RedirectResponse:
    """0/1 始まりを外線発信専用に予約するポリシーを切替 (DB に保存)。"""
    app_settings = await get_app_settings(db)
    app_settings.reserve_leading_01_for_outbound = reserve_leading_01 == "on"
    await db.commit()
    return RedirectResponse("/system/", status_code=303)


@router.post("/voicemail-announce", response_class=HTMLResponse)
async def change_voicemail_announce(
    request: Request,
    db: AsyncSession = Depends(get_db),
    announce_datetime: str = Form(""),
) -> RedirectResponse:
    """留守番電話の再生時に録音日時を読み上げるかを切替 (DB に保存)。

    有効にするには日時読み上げ用の音声ファイルが生成済みである必要が
    あるため、未生成の場合は有効化を拒否する (未生成のまま有効にすると
    再生時にエラーで通話が切断されてしまうため)。
    """
    want = announce_datetime == "on"
    if want:
        done, total, _ = tts_service.datetime_sounds_status()
        if done < total:
            # 音声が揃っていないので有効化しない
            return RedirectResponse("/system/?dt_error=1", status_code=303)
    app_settings = await get_app_settings(db)
    app_settings.voicemail_announce_datetime = want
    await db.commit()
    return RedirectResponse("/system/", status_code=303)


@router.post("/nuisance-settings", response_class=HTMLResponse)
async def change_nuisance_settings(
    request: Request,
    db: AsyncSession = Depends(get_db),
    international_call_action: str = Form("none"),
    international_voicemail_target: str = Form(""),
) -> RedirectResponse:
    """海外からの着信 (と判定した場合) の一律の処理を切替 (DB に保存)。"""
    if international_call_action not in ("none", "voicemail", "hangup"):
        international_call_action = "none"
    app_settings = await get_app_settings(db)
    app_settings.international_call_action = international_call_action
    app_settings.international_voicemail_target = (
        international_voicemail_target or None
        if international_call_action == "voicemail"
        else None
    )
    await db.commit()
    return RedirectResponse("/system/", status_code=303)


@router.post("/sip-port", response_class=HTMLResponse)
async def change_sip_port(
    request: Request,
    db: AsyncSession = Depends(get_db),
    sip_port: int = Form(5060),
) -> RedirectResponse:
    """内線 (PJSIP UDP transport) の待受ポートを変更 (DB に保存)。"""
    if not (1 <= sip_port <= 65535):
        sip_port = 5060
    app_settings = await get_app_settings(db)
    app_settings.sip_port = sip_port
    await db.commit()
    return RedirectResponse("/system/", status_code=303)


@router.post("/internal-ring-seconds", response_class=HTMLResponse)
async def change_internal_ring_seconds(
    request: Request,
    db: AsyncSession = Depends(get_db),
    internal_ring_seconds: int = Form(30),
) -> RedirectResponse:
    """内線同士の呼び出し (鳴音) 時間を変更 (DB に保存)。"""
    if not (5 <= internal_ring_seconds <= 120):
        internal_ring_seconds = 30
    app_settings = await get_app_settings(db)
    app_settings.internal_ring_seconds = internal_ring_seconds
    await db.commit()
    return RedirectResponse("/system/", status_code=303)


# ---------------------------------------------------------------------------
# バックアップ / 復元
# ---------------------------------------------------------------------------


@router.post("/backups/create", response_class=HTMLResponse)
async def create_backup_endpoint(request: Request) -> RedirectResponse:
    try:
        await backup_service.create_backup()
    except RuntimeError as exc:
        log.warning("backup creation failed: %s", exc)
    return RedirectResponse("/system/#backups", status_code=303)


@router.get("/backups/{name}/download")
async def download_backup(name: str) -> FileResponse:
    try:
        p = backup_service.backup_path(name)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=404) from e
    return FileResponse(p, filename=name, media_type="application/zip")


@router.post("/backups/{name}/delete", response_class=HTMLResponse)
async def delete_backup_endpoint(name: str) -> RedirectResponse:
    try:
        backup_service.delete_backup(name)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=404) from e
    return RedirectResponse("/system/#backups", status_code=303)


@router.post("/backups/restore-stage", response_class=HTMLResponse)
async def restore_stage(
    request: Request, file: UploadFile, db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """アップロードされたバックアップを検証してステージングし、
    確定に必要なコマンドを表示する (稼働中の DB は直接上書きしない)。"""
    content = await file.read()
    state = _check_ja_sounds()
    app_settings = await get_app_settings(db)

    try:
        info = await backup_service.stage_restore(content, file.filename or "")
        stage_error = None
    except ValueError as exc:
        info = None
        stage_error = str(exc)

    return request.app.state.templates.TemplateResponse(
        request, "system/index.html",
        {
            "title": "システム",
            "ja_state": state,
            "ja_sounds_dir": str(JA_SOUNDS_DIR),
            "current_language": app_settings.system_language,
            "reserve_leading_01_for_outbound": app_settings.reserve_leading_01_for_outbound,
            "sip_port": app_settings.sip_port,
            "internal_ring_seconds": app_settings.internal_ring_seconds,
            "backups": backup_service.list_backups(),
            "restore_info": info,
            "restore_error": stage_error,
        },
    )
