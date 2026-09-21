"""FAX ルーター。

  GET  /fax/                  履歴一覧 + ステータス
  GET  /fax/config            設定画面 (SMTP / 受信DID / 自局番号)
  POST /fax/config            設定保存
  POST /fax/config/test-smtp  SMTP 疎通テスト
  GET  /fax/send              送信フォーム
  POST /fax/send              PDF アップロード → SendFAX 実行
  GET  /fax/{id}/download     保存済み PDF ダウンロード
  POST /fax/{id}/delete       履歴削除 (単体)
  POST /fax/delete-bulk       履歴削除 (複数選択・一括)
  GET  /fax/hook/received     Asterisk から受信完了通知 (内部用)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.hooks import verify_hook
from app.models import FaxConfig, FaxLog, Trunk
from app.services import fax_service
from app.services.ami import AmiClient

log = logging.getLogger(__name__)

router = APIRouter(prefix="/fax", tags=["fax"])


async def _get_or_create_config(db: AsyncSession) -> FaxConfig:
    cfg = (await db.scalars(select(FaxConfig).limit(1))).first()
    if cfg is None:
        cfg = FaxConfig()
        db.add(cfg)
        await db.commit()
        await db.refresh(cfg)
    # 既存 DB に自動マイグレーションで追加されたカラムは、SQLite の
    # ALTER TABLE ADD COLUMN が Python 側の default= を反映しないため
    # NULL のままになりうる (設定画面をまだ一度も保存していない場合)。
    # 表示・利用側で None のまま扱うと空欄表示やエラーの原因になるため、
    # ここで欠けている値だけメモリ上で補完する (DB へは書き込まない)。
    if not cfg.filename_pattern_recv:
        cfg.filename_pattern_recv = "受信FAX_{datetime}_{peer}"
    if not cfg.filename_pattern_send:
        cfg.filename_pattern_send = "送信FAX_{datetime}_{peer}"
    if not cfg.filename_datetime_format:
        cfg.filename_datetime_format = "%Y%m%d_%H%M"
    if not cfg.mail_body_template:
        cfg.mail_body_template = (
            "FAX を受信しました。\n\n"
            "  着信電話番号 : {peer}\n"
            "  受信日時     : {datetime}\n"
            "  ページ数     : {pages}\n"
            "  ファイル     : {filename}\n\n"
            "本メールに受信 FAX (PDF) を添付しています。\n"
        )
    # cng_detect_wait_seconds は 0 も正当な値 (待機なし = 無効化) の
    # ため、他のフィールドと違い not ではなく is None で判定する。
    if cfg.cng_detect_wait_seconds is None:
        cfg.cng_detect_wait_seconds = 2
    return cfg


@router.get("/", response_class=HTMLResponse)
async def fax_home(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    cfg = await _get_or_create_config(db)
    logs = (
        await db.scalars(select(FaxLog).order_by(desc(FaxLog.created_at)).limit(100))
    ).all()
    return request.app.state.templates.TemplateResponse(
        request, "fax/list.html",
        {
            "title": "FAX 送受信",
            "cfg": cfg,
            "logs": logs,
            "tools": fax_service.tools_available(),
        },
    )


@router.get("/config", response_class=HTMLResponse)
async def fax_config_form(
    request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    """メイン設定画面: 基本設定 + 受信処理チェックボックス + サブ画面へのボタン。"""
    cfg = await _get_or_create_config(db)
    return request.app.state.templates.TemplateResponse(
        request, "fax/config.html",
        {"title": "FAX 設定", "cfg": cfg},
    )


@router.post("/config", response_class=HTMLResponse, response_model=None)
async def fax_config_save(
    request: Request,
    db: AsyncSession = Depends(get_db),
    enabled: str | None = Form(None),
    fax_did_number: str | None = Form(None),
    fax_extension: str = Form("9000"),
    station_id: str | None = Form(None),
    header_text: str | None = Form(None),
    minrate: int = Form(4800),
    maxrate: int = Form(14400),
    ecm_enabled: str = Form(""),
    cng_detect_wait_seconds: int = Form(2),
    deliver_mail: str | None = Form(None),
    deliver_smb: str | None = Form(None),
    deliver_local: str | None = Form(None),
    filename_pattern_recv: str | None = Form(None),
    filename_pattern_send: str | None = Form(None),
    filename_datetime_format: str | None = Form(None),
    note: str | None = Form(None),
) -> RedirectResponse:
    """メイン設定 (基本 + 受信処理の ON/OFF) を保存。"""
    cfg = await _get_or_create_config(db)
    cfg.enabled = enabled is not None
    cfg.fax_did_number = (fax_did_number or "").strip() or None
    cfg.fax_extension = (fax_extension or "9000").strip()
    cfg.station_id = (station_id or "").strip() or None
    cfg.header_text = (header_text or "").strip() or None
    # 有効なボーレート値のみ受け付ける (不正値は既定値にフォールバック)
    _valid_rates = {2400, 4800, 7200, 9600, 12000, 14400}
    cfg.minrate = minrate if minrate in _valid_rates else 4800
    cfg.maxrate = maxrate if maxrate in _valid_rates else 14400
    cfg.ecm_enabled = ecm_enabled == "on"
    # 0〜10秒の範囲に丸める (極端な値による事故防止)
    cfg.cng_detect_wait_seconds = max(0, min(10, cng_detect_wait_seconds))
    cfg.deliver_mail = deliver_mail is not None
    cfg.deliver_smb = deliver_smb is not None
    cfg.deliver_local = deliver_local is not None
    if filename_pattern_recv and filename_pattern_recv.strip():
        cfg.filename_pattern_recv = filename_pattern_recv.strip()
    if filename_pattern_send and filename_pattern_send.strip():
        cfg.filename_pattern_send = filename_pattern_send.strip()
    if filename_datetime_format and filename_datetime_format.strip():
        cfg.filename_datetime_format = filename_datetime_format.strip()
    cfg.note = (note or "").strip() or None
    await db.commit()
    return RedirectResponse("/fax/config", status_code=status.HTTP_303_SEE_OTHER)


# --- メール設定サブ画面 ---


@router.get("/config/mail", response_class=HTMLResponse)
async def fax_config_mail_form(
    request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    cfg = await _get_or_create_config(db)
    return request.app.state.templates.TemplateResponse(
        request, "fax/config_mail.html",
        {"title": "FAX メール設定", "cfg": cfg},
    )


@router.post("/config/mail", response_class=HTMLResponse, response_model=None)
async def fax_config_mail_save(
    request: Request,
    db: AsyncSession = Depends(get_db),
    smtp_host: str | None = Form(None),
    smtp_port: int = Form(587),
    smtp_security: str = Form("starttls"),
    smtp_username: str | None = Form(None),
    smtp_password: str | None = Form(None),
    mail_from: str | None = Form(None),
    mail_to: str | None = Form(None),
    mail_subject_prefix: str = Form("[FAX受信]"),
    mail_body_template: str | None = Form(None),
) -> RedirectResponse:
    cfg = await _get_or_create_config(db)
    cfg.smtp_host = (smtp_host or "").strip() or None
    cfg.smtp_port = smtp_port
    cfg.smtp_security = smtp_security
    cfg.smtp_username = (smtp_username or "").strip() or None
    if smtp_password:  # 空送信時は既存を保持
        cfg.smtp_password = smtp_password
    cfg.mail_from = (mail_from or "").strip() or None
    cfg.mail_to = (mail_to or "").strip() or None
    cfg.mail_subject_prefix = mail_subject_prefix or "[FAX受信]"
    if mail_body_template and mail_body_template.strip():
        cfg.mail_body_template = mail_body_template
    await db.commit()
    return RedirectResponse(
        "/fax/config/mail", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/config/mail/test", response_class=HTMLResponse)
async def fax_test_smtp(
    request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    cfg = await _get_or_create_config(db)
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, fax_service.smtp_test, cfg)
    return request.app.state.templates.TemplateResponse(
        request, "fax/config_mail.html",
        {
            "title": "FAX メール設定",
            "cfg": cfg,
            "test_ok": result.ok,
            "test_msg": result.detail,
        },
    )


# --- SMB 外部保存設定サブ画面 ---


@router.get("/config/smb", response_class=HTMLResponse)
async def fax_config_smb_form(
    request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    cfg = await _get_or_create_config(db)
    return request.app.state.templates.TemplateResponse(
        request, "fax/config_smb.html",
        {"title": "FAX 外部保存 (SMB) 設定", "cfg": cfg},
    )


@router.post("/config/smb", response_class=HTMLResponse, response_model=None)
async def fax_config_smb_save(
    request: Request,
    db: AsyncSession = Depends(get_db),
    smb_server: str | None = Form(None),
    smb_share: str | None = Form(None),
    smb_path: str | None = Form(None),
    smb_username: str | None = Form(None),
    smb_password: str | None = Form(None),
    smb_domain: str | None = Form(None),
    smb_port: int = Form(445),
) -> RedirectResponse:
    cfg = await _get_or_create_config(db)
    cfg.smb_server = (smb_server or "").strip() or None
    cfg.smb_share = (smb_share or "").strip().strip("/\\") or None
    cfg.smb_path = (smb_path or "").strip().strip("/\\") or None
    cfg.smb_username = (smb_username or "").strip() or None
    if smb_password:  # 空送信時は既存を保持
        cfg.smb_password = smb_password
    cfg.smb_domain = (smb_domain or "").strip() or None
    cfg.smb_port = smb_port or 445
    await db.commit()
    return RedirectResponse(
        "/fax/config/smb", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/config/smb/test", response_class=HTMLResponse)
async def fax_test_smb(
    request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    cfg = await _get_or_create_config(db)
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, fax_service.smb_test, cfg)
    return request.app.state.templates.TemplateResponse(
        request, "fax/config_smb.html",
        {
            "title": "FAX 外部保存 (SMB) 設定",
            "cfg": cfg,
            "test_ok": result.ok,
            "test_msg": result.detail,
        },
    )


@router.get("/send", response_class=HTMLResponse)
async def fax_send_form(
    request: Request, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    cfg = await _get_or_create_config(db)
    trunks = (
        await db.scalars(
            select(Trunk).where(Trunk.enabled.is_(True)).order_by(Trunk.name)
        )
    ).all()
    return request.app.state.templates.TemplateResponse(
        request, "fax/send.html",
        {
            "title": "FAX 送信",
            "cfg": cfg,
            "tools": fax_service.tools_available(),
            "trunks": trunks,
        },
    )


@router.post("/send", response_class=HTMLResponse, response_model=None)
async def fax_send(
    request: Request,
    db: AsyncSession = Depends(get_db),
    dest_number: str = Form(...),
    trunk_name: str = Form(...),
    file: UploadFile = File(...),
) -> HTMLResponse | RedirectResponse:
    cfg = await _get_or_create_config(db)

    if not (file.filename or "").lower().endswith(".pdf"):
        return await _send_error(request, db, cfg, "PDF ファイルを選択してください。")

    dest = "".join(c for c in dest_number if c.isdigit())
    if not dest:
        return await _send_error(request, db, cfg, "宛先 FAX 番号が不正です。")

    # アップロード PDF を一時保存
    work = settings.fax_spool_dir / "send_work"
    try:
        work.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return await _send_error(request, db, cfg, f"作業ディレクトリ作成失敗: {e}")

    ts = datetime.now()
    src_pdf = work / f"src_{ts.strftime('%Y%m%d_%H%M%S')}.pdf"
    src_pdf.write_bytes(await file.read())

    fxlog = FaxLog(
        direction="send",
        status="processing",
        peer_number=dest,
        original_filename=file.filename,
    )
    db.add(fxlog)
    await db.flush()

    # PDF → TIFF
    conv = await fax_service.pdf_to_fax_tiff(src_pdf, work)
    if not conv.ok or conv.output_path is None:
        fxlog.status = "failed"
        fxlog.error_detail = f"PDF→TIFF 変換失敗: {conv.log[:1000]}"
        await db.commit()
        return await _send_error(request, db, cfg, "PDF→TIFF 変換に失敗しました。" + conv.log[:500])

    tiff_path = conv.output_path

    # Asterisk へ SendFAX を Originate で投入
    # Local チャネルから dialplan の fax-send-out へ入り、SendFAX を実行
    ok, detail = await _originate_sendfax(
        dest=dest, trunk_name=trunk_name, tiff_path=tiff_path,
        station_id=cfg.station_id or "", header=cfg.header_text or "",
    )

    if not ok:
        fxlog.status = "failed"
        fxlog.error_detail = f"SendFAX 起動失敗: {detail}"
        await db.commit()
        return await _send_error(request, db, cfg, f"FAX 送信の起動に失敗しました: {detail}")

    # 送信した PDF を年月日付きで保存 (送信結果の確定は非同期だが、投入時点で保存)
    saved = fax_service.store_sent_pdf(src_pdf, dest, ts, cfg)
    fxlog.pdf_path = str(saved) if saved else None
    fxlog.status = "success"  # Originate 受理 = 投入成功 (実際の送達は FAXSTATUS 依存)
    await db.commit()

    return RedirectResponse("/fax/", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/{fax_id}/view")
async def fax_view(
    fax_id: int, db: AsyncSession = Depends(get_db)
) -> FileResponse:
    """PDF をブラウザ内で直接閲覧 (ダウンロードさせずインライン表示)。

    /download はダウンロード保存用 (Content-Disposition: attachment)、
    こちらは閲覧用 (Content-Disposition: inline) として役割を分ける。
    """
    obj = await db.get(FaxLog, fax_id)
    if obj is None or not obj.pdf_path:
        raise HTTPException(404, detail="PDF が見つかりません。")
    p = Path(obj.pdf_path)
    if not p.exists():
        raise HTTPException(404, detail="PDF ファイルが存在しません。")
    return FileResponse(
        p,
        media_type="application/pdf",
        filename=p.name,
        content_disposition_type="inline",
    )


@router.get("/{fax_id}/download")
async def fax_download(
    fax_id: int, db: AsyncSession = Depends(get_db)
) -> FileResponse:
    obj = await db.get(FaxLog, fax_id)
    if obj is None or not obj.pdf_path:
        raise HTTPException(404, detail="PDF が見つかりません。")
    p = Path(obj.pdf_path)
    if not p.exists():
        raise HTTPException(404, detail="PDF ファイルが存在しません。")
    return FileResponse(p, media_type="application/pdf", filename=p.name)


@router.post("/{fax_id}/delete")
async def fax_delete(
    fax_id: int, db: AsyncSession = Depends(get_db)
) -> RedirectResponse:
    obj = await db.get(FaxLog, fax_id)
    if obj is not None:
        # PDF 実体も削除
        if obj.pdf_path:
            try:
                Path(obj.pdf_path).unlink(missing_ok=True)
            except OSError:
                pass
        await db.delete(obj)
        await db.commit()
    return RedirectResponse("/fax/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/delete-bulk")
async def fax_delete_bulk(
    db: AsyncSession = Depends(get_db),
    fax_ids: list[int] = Form(default_factory=list),
) -> RedirectResponse:
    """履歴一覧でチェックした複数件をまとめて削除 (PDF 実体も削除)。"""
    if fax_ids:
        objs = (
            await db.scalars(select(FaxLog).where(FaxLog.id.in_(fax_ids)))
        ).all()
        for obj in objs:
            if obj.pdf_path:
                try:
                    Path(obj.pdf_path).unlink(missing_ok=True)
                except OSError:
                    pass
            await db.delete(obj)
        await db.commit()
    return RedirectResponse("/fax/", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/hook/received")
async def fax_hook_received(
    request: Request,
    db: AsyncSession = Depends(get_db),
    token: str = "",
    file: str = "",
    src: str = "",
    status: str = "",  # noqa: A002
    pages: str = "",
    uniqueid: str = "",
) -> dict:
    """Asterisk の ReceiveFAX 完了後に curl で叩かれる内部フック。

    - TIFF を PDF に変換し年月日付きで保存
    - FaxLog に記録
    - 設定に応じて: メール添付通知 / SMB 外部保存 / ローカル保存
    """
    await verify_hook(request, db, "fax", token)

    cfg = await _get_or_create_config(db)
    now = datetime.now()
    try:
        pages_i = int(pages) if pages.isdigit() else None
    except (ValueError, AttributeError):
        pages_i = None

    fxlog = FaxLog(
        direction="recv",
        status="processing",
        peer_number=src or None,
        pages=pages_i,
    )
    db.add(fxlog)
    await db.flush()

    # FAXSTATUS が SUCCESS でなければ失敗記録だけ
    if status.upper() != "SUCCESS":
        fxlog.status = "failed"
        fxlog.error_detail = f"Asterisk FAXSTATUS={status}"
        await db.commit()
        return {"ok": False, "reason": f"FAXSTATUS={status}"}

    # 受信 TIFF のパスは Asterisk から渡されるが、フックはネットワーク
    # 越しに叩かれる以上「Asterisk 以外がここへ任意のパスを渡してくる」
    # 可能性を前提に扱う。このパスのファイルは PDF に変換されメールで
    # 送られるため、無検査だとサーバー上の任意ファイルを持ち出せて
    # しまう。FAX スプールディレクトリの中だけを受け付ける。
    try:
        tiff_path = Path(file).resolve(strict=True)
        spool_dir = settings.fax_spool_dir.resolve()
        tiff_path.relative_to(spool_dir)
    except (OSError, ValueError):
        log.warning("FAX フックに想定外のパスが渡されました: %r", file)
        fxlog.status = "failed"
        fxlog.error_detail = "受信ファイルのパスが不正です"
        await db.commit()
        return {"ok": False, "reason": "invalid file path"}

    conv = await fax_service.received_tiff_to_pdf(tiff_path, src, now, cfg)
    if not conv.ok or conv.output_path is None:
        fxlog.status = "failed"
        fxlog.error_detail = f"TIFF→PDF 失敗: {conv.log[:1000]}"
        await db.commit()
        return {"ok": False, "reason": "tiff2pdf failed"}

    pdf_path = conv.output_path
    fxlog.pdf_path = str(pdf_path)
    fxlog.status = "success"

    # 重要: メール送信 / SMB 保存は数十秒かかる可能性がある。
    # その間 SQLite トランザクションを保持すると後続の受信フックが
    # "database is locked" で失敗するため、ここで一旦コミットして
    # DB ロックを解放する。必要な値はローカル変数に退避しておき、
    # 配信処理の後で別トランザクションとして結果を書き戻す。
    fxlog_id = fxlog.id
    cfg_snapshot = _ConfigSnapshot(cfg)
    await db.commit()

    errors: list[str] = []
    mail_ok = False
    smb_ok = False
    smb_remote: str | None = None
    loop = asyncio.get_event_loop()

    # --- (1) メール添付通知 ---
    if cfg_snapshot.deliver_mail:
        if cfg_snapshot.smtp_host and cfg_snapshot.mail_to:
            mail = await loop.run_in_executor(
                None,
                fax_service.send_received_fax_mail,
                cfg_snapshot, pdf_path, src, now, pages_i,
            )
            mail_ok = mail.ok
            if not mail.ok:
                errors.append(f"メール: {mail.detail}")
        else:
            errors.append("メール: SMTP 設定 (host/to) が未入力")

    # --- (2) SMB 外部保存 ---
    if cfg_snapshot.deliver_smb:
        if cfg_snapshot.smb_server and cfg_snapshot.smb_share:
            smb = await loop.run_in_executor(
                None,
                fax_service.save_received_fax_smb,
                cfg_snapshot, pdf_path,
            )
            smb_ok = smb.ok
            smb_remote = smb.remote_path
            if not smb.ok:
                errors.append(f"SMB: {smb.detail}")
        else:
            errors.append("SMB: サーバー/共有名が未入力")

    # --- (3) ローカル保存しない設定なら PDF を削除 ---
    #   (メール/SMB のいずれかが成功している場合のみ削除。
    #    全部失敗なら復旧用にローカルへ残す)
    final_pdf_path: str | None = str(pdf_path)
    if not cfg_snapshot.deliver_local:
        if mail_ok or smb_ok:
            try:
                pdf_path.unlink(missing_ok=True)
                final_pdf_path = None
            except OSError:
                pass

    # --- 配信結果を別トランザクションで書き戻す ---
    obj = await db.get(FaxLog, fxlog_id)
    if obj is not None:
        obj.mail_sent = mail_ok
        obj.smb_saved = smb_ok
        obj.smb_path_saved = smb_remote
        obj.pdf_path = final_pdf_path
        if errors:
            obj.error_detail = " / ".join(errors)
        await db.commit()

    # 受信元の spool TIFF は不要なので削除 (保存は PDF)
    try:
        tiff_path.unlink(missing_ok=True)
    except OSError:
        pass

    return {
        "ok": True,
        "pdf": final_pdf_path,
        "mail_sent": mail_ok,
        "smb_saved": smb_ok,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# 内部ヘルパ
# ---------------------------------------------------------------------------


class _ConfigSnapshot:
    """FaxConfig の値を DB セッションから切り離してコピーするスナップショット。

    受信フックで重い配信処理 (メール/SMB) の前に DB をコミットすると、
    ORM オブジェクト (cfg) は expire されアクセス時に再クエリが走る。
    それを避けるため、必要な属性だけプレーンな値としてここに退避する。
    fax_service の関数は属性アクセスしかしないのでこれで差し替え可能。
    """

    _FIELDS = (
        "deliver_mail", "deliver_smb", "deliver_local",
        "smtp_host", "smtp_port", "smtp_security",
        "smtp_username", "smtp_password",
        "mail_from", "mail_to", "mail_subject_prefix", "mail_body_template",
        "smb_server", "smb_share", "smb_path",
        "smb_username", "smb_password", "smb_domain", "smb_port",
        "station_id", "header_text",
    )

    def __init__(self, cfg) -> None:  # FaxConfig
        for f in self._FIELDS:
            setattr(self, f, getattr(cfg, f, None))


async def _send_error(
    request: Request, db: AsyncSession, cfg: FaxConfig, msg: str
) -> HTMLResponse:
    trunks = (
        await db.scalars(
            select(Trunk).where(Trunk.enabled.is_(True)).order_by(Trunk.name)
        )
    ).all()
    return request.app.state.templates.TemplateResponse(
        request, "fax/send.html",
        {
            "title": "FAX 送信",
            "cfg": cfg,
            "tools": fax_service.tools_available(),
            "trunks": trunks,
            "error_message": msg,
        },
        status_code=400,
    )


async def _originate_sendfax(
    dest: str,
    trunk_name: str,
    tiff_path: Path,
    station_id: str,
    header: str,
) -> tuple[bool, str]:
    """AMI Originate で SendFAX を実行する。

    手順:
      Originate Channel=Local/sendfax@fax-send-out
        → fax-send-out コンテキストで Dial(PJSIP/<dest>@trunk) し
          A() オプションは使わず、外線がつながったら SendFAX。
      簡易化のため Originate の Application=Dial で外線に発信し、
      Variable で TIFF パス等を渡し、専用 context で SendFAX 実行。

    実際の dialplan 連携を単純化するため、ここでは Originate で
    Local チャネルを起こし、変数経由で fax-send-out に渡す。
    """
    # 変数名には __ (ダブルアンダースコア) プレフィックスを付ける。
    # これが無いと、Dial() の b() オプションで新たに発信される PJSIP
    # チャネル (fax-send-exec を実行する側) には変数が継承されず、
    # ${FAX_FILE} が空文字になって SendFAX が
    # "SendFAX requires an argument" で失敗する
    # (Asterisk の変数継承は _VAR で 1階層、__VAR で無期限に継承される。
    #  Local チャネル → Dial() が生成する新チャネル、と1階層挟むため
    #  __ が必要)。
    variables = {
        "__FAX_FILE": str(tiff_path),
        "__FAX_DEST": dest,
        "__FAX_TRUNK": trunk_name,
        "__FAX_SID": station_id,
        "__FAX_HDR": header,
    }
    try:
        async with AmiClient() as ami:
            # Local チャネルを fax-send-out コンテキストの s,1 に向けて起こす
            resp = await ami.action({
                "Action": "Originate",
                "Channel": "Local/s@fax-send-out",
                "Context": "fax-send-out",
                "Exten": "s",
                "Priority": "1",
                "CallerID": station_id or dest,
                "Async": "true",
                # Variable は複数行必要なので action() に渡せるよう個別キー化
                "Variable": ",".join(f"{k}={v}" for k, v in variables.items()),
            })
            return resp.success, resp.message
    except Exception as e:  # noqa: BLE001
        return False, str(e)
