"""FAX サービス。

受信フロー:
  Asterisk(ReceiveFAX) → TIFF を fax_spool_dir に保存
  → 本ツールが TIFF→PDF 変換 (tiff2pdf)
  → 受信FAX_YYYYMMDD_HHMM_<着信番号>.pdf として fax_store_dir に保存
  → SMTP で通知メール (着信番号・受信時刻を本文、PDF 添付)

送信フロー:
  Web から PDF アップロード
  → PDF→TIFF 変換 (Ghostscript, FAX G3 204x196dpi mono)
  → Asterisk(SendFAX) で送信
  → 送信済み PDF を 送信FAX_YYYYMMDD_HHMM_<宛先番号>.pdf として保存

必要な外部コマンド:
  - tiff2pdf  (libtiff-tools)        受信 TIFF → PDF
  - gs        (ghostscript)          送信 PDF → TIFF
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from app.config import settings

log = logging.getLogger(__name__)

_SAFE = re.compile(r"[^0-9A-Za-z_-]")


def _safe_number(num: str | None) -> str:
    """電話番号をファイル名に使える形に。空なら 'unknown'。"""
    if not num:
        return "unknown"
    cleaned = _SAFE.sub("", num)
    return cleaned or "unknown"


def _stamp(dt: datetime | None = None, fmt: str = "%Y%m%d_%H%M") -> str:
    return (dt or datetime.now()).strftime(fmt)


def build_fax_filename(
    pattern: str,
    dt_format: str,
    peer_number: str | None,
    when: datetime | None = None,
) -> str:
    """設定されたパターンから保存ファイル名 (.pdf 付き) を組み立てる。

    プレースホルダーが未知のものを含んでいたり書式が壊れていても、
    ファイル保存自体が失敗しないよう例外は握りつぶして既定形式に
    フォールバックする。
    """
    when = when or datetime.now()
    ctx = {
        "datetime": _stamp(when, dt_format),
        "peer": _safe_number(peer_number),
    }
    try:
        name = pattern.format(**ctx)
    except (KeyError, IndexError, ValueError):
        name = f"FAX_{ctx['datetime']}_{ctx['peer']}"
    name = _SAFE_FILENAME.sub("_", name).strip("_") or "FAX"
    return f"{name}.pdf"


_SAFE_FILENAME = re.compile(r"[^0-9A-Za-z_\-\u3040-\u30ff\u4e00-\u9fff]")


def tools_available() -> dict[str, bool]:
    """必要な外部コマンドの有無を返す。"""
    return {
        "tiff2pdf": shutil.which(settings.tiff2pdf_path) is not None,
        "gs": shutil.which(settings.gs_path) is not None,
    }


@dataclass
class ConvertResult:
    ok: bool
    output_path: Path | None
    log: str


async def _run(cmd: list[str], timeout: float = 120.0) -> tuple[int, str]:
    """サブプロセス実行。(returncode, stderr+stdout) を返す。"""
    log.info("exec: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        return 124, f"timeout after {timeout}s"
    return proc.returncode or 0, out.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 受信: TIFF → PDF
# ---------------------------------------------------------------------------


async def received_tiff_to_pdf(
    tiff_path: Path,
    peer_number: str | None,
    received_at: datetime | None = None,
    cfg=None,  # FaxConfig | None
) -> ConvertResult:
    """受信 TIFF を PDF に変換し、設定されたファイル名パターンで保存。

    既定の出力: <fax_store_dir>/受信FAX_YYYYMMDD_HHMM_<着信番号>.pdf
    (cfg.filename_pattern_recv / cfg.filename_datetime_format で変更可能)
    """
    if not tiff_path.exists():
        return ConvertResult(False, None, f"TIFF が見つかりません: {tiff_path}")

    store = settings.fax_store_dir
    try:
        store.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return ConvertResult(False, None, f"保存先を作成できません: {store} ({e})")

    received_at = received_at or datetime.now()
    # cfg 自体が None の場合だけでなく、既存 DB に新カラムを追加した
    # 直後 (ALTER TABLE ADD COLUMN 直後で、設定画面をまだ一度も保存
    # していない状態) は cfg.filename_pattern_recv 自体が NULL に
    # なりうるため、両方のケースでデフォルト値にフォールバックする。
    pattern = (cfg.filename_pattern_recv if cfg is not None else None) or "受信FAX_{datetime}_{peer}"
    dt_fmt = (cfg.filename_datetime_format if cfg is not None else None) or "%Y%m%d_%H%M"
    fname = build_fax_filename(pattern, dt_fmt, peer_number, received_at)
    out_path = _unique_path(store / fname)

    rc, msg = await _run([
        settings.tiff2pdf_path,
        "-o", str(out_path),
        str(tiff_path),
    ])
    if rc != 0 or not out_path.exists():
        return ConvertResult(False, None, f"tiff2pdf 失敗 (rc={rc})\n{msg}")

    return ConvertResult(True, out_path, msg.strip())


# ---------------------------------------------------------------------------
# 送信: PDF → TIFF (FAX G3)
# ---------------------------------------------------------------------------


async def pdf_to_fax_tiff(pdf_path: Path, work_dir: Path) -> ConvertResult:
    """送信用に PDF を FAX 互換 TIFF (CCITT G3, 204x196dpi, mono) に変換。

    Asterisk の SendFAX が読める形式にする。
    出力: <work_dir>/<pdf stem>.tif
    """
    if not pdf_path.exists():
        return ConvertResult(False, None, f"PDF が見つかりません: {pdf_path}")
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return ConvertResult(False, None, f"作業ディレクトリ作成失敗: {e}")

    out_path = work_dir / (pdf_path.stem + ".tif")

    # Ghostscript の tiffg3 デバイスで FAX 標準解像度に。
    #
    # 注意 (Ghostscript 9.50+、特に 10.x で顕著): -dSAFER サンドボックスは
    # Ghostscript 9.50 以降デフォルトで有効になっており、明示的に指定
    # しなくても内部の PostScript/PDF インタプリタが「許可されていない
    # パス」へのファイルアクセスを拒否する。これは実際の OS のファイル
    # 権限 (chmod/chown) とは無関係に、Ghostscript 自身のポリシー層で
    # 拒否されるため、エラーメッセージに "Last OS error: Permission
    # denied" と出ても、ファイルの所有者やパーミッションを直しても解決
    # しない (既知の Ghostscript の挙動: /undefinedfilename +
    # "Last OS error: Permission denied" という組み合わせが典型的な
    # SAFER サンドボックス拒否のシグネチャ)。
    # --permit-file-read= / --permit-file-write= で明示的に許可する
    # ことで、SAFER を維持したまま (=dNOSAFER で無効化せず安全に) 解決する。
    rc, msg = await _run([
        settings.gs_path,
        "-q",
        "-dNOPAUSE",
        "-dBATCH",
        "-dSAFER",
        f"--permit-file-read={pdf_path}",
        f"--permit-file-write={out_path}",
        "-sDEVICE=tiffg3",
        "-r204x196",
        "-g1728x2156",          # A4 FAX 標準 (横 1728px)
        "-dPDFFitPage",
        f"-sOutputFile={out_path}",
        str(pdf_path),
    ])
    if rc != 0 or not out_path.exists():
        return ConvertResult(False, None, f"gs 変換失敗 (rc={rc})\n{msg}")
    return ConvertResult(True, out_path, msg.strip())


def _unique_path(path: Path) -> Path:
    """同名ファイルが既に存在する場合、末尾に連番を付けて重複を避ける。

    ファイル名パターンから日時を外す設定にした場合など、同じ名前に
    なりうるケースの上書き事故を防ぐための保険。
    """
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    i = 2
    while True:
        cand = path.with_name(f"{stem}_{i}{suffix}")
        if not cand.exists():
            return cand
        i += 1


def store_sent_pdf(
    src_pdf: Path,
    peer_number: str | None,
    sent_at: datetime | None = None,
    cfg=None,  # FaxConfig | None
) -> Path | None:
    """送信した PDF を設定されたファイル名パターンで保存。

    既定の出力: <fax_store_dir>/送信FAX_YYYYMMDD_HHMM_<宛先番号>.pdf
    (cfg.filename_pattern_send / cfg.filename_datetime_format で変更可能)
    """
    store = settings.fax_store_dir
    try:
        store.mkdir(parents=True, exist_ok=True)
        sent_at = sent_at or datetime.now()
        # (received_tiff_to_pdf と同じ理由: cfg はあっても属性が
        #  NULL のケースを考慮してフォールバックする)
        pattern = (cfg.filename_pattern_send if cfg is not None else None) or "送信FAX_{datetime}_{peer}"
        dt_fmt = (cfg.filename_datetime_format if cfg is not None else None) or "%Y%m%d_%H%M"
        fname = build_fax_filename(pattern, dt_fmt, peer_number, sent_at)
        dst = _unique_path(store / fname)
        shutil.copy2(src_pdf, dst)
        return dst
    except OSError as e:
        log.warning("送信 PDF 保存失敗: %s", e)
        return None


# ---------------------------------------------------------------------------
# SMTP 通知メール
# ---------------------------------------------------------------------------


@dataclass
class MailResult:
    ok: bool
    detail: str


def send_received_fax_mail(
    cfg,  # FaxConfig
    pdf_path: Path,
    peer_number: str | None,
    received_at: datetime,
    pages: int | None = None,
) -> MailResult:
    """受信 FAX を SMTP で通知 (PDF 添付)。

    同期関数 (smtplib)。呼び出し側で run_in_executor 推奨。
    """
    if not cfg.smtp_host or not cfg.mail_from or not cfg.mail_to:
        return MailResult(False, "SMTP 設定 (host/from/to) が未設定です。")

    recipients = [a.strip() for a in cfg.mail_to.split(",") if a.strip()]
    if not recipients:
        return MailResult(False, "宛先メールアドレスが空です。")

    num_disp = peer_number or "不明"
    ts_disp = received_at.strftime("%Y年%m月%d日 %H:%M")
    pages_disp = str(pages) if pages is not None else "不明"

    msg = EmailMessage()
    msg["Subject"] = f"{cfg.mail_subject_prefix} {num_disp} ({ts_disp})"
    msg["From"] = cfg.mail_from
    msg["To"] = ", ".join(recipients)

    template = cfg.mail_body_template or (
        "FAX を受信しました。\n\n"
        "  着信電話番号 : {peer}\n"
        "  受信日時     : {datetime}\n"
        "  ページ数     : {pages}\n"
        "  ファイル     : {filename}\n\n"
        "本メールに受信 FAX (PDF) を添付しています。\n"
    )
    try:
        body = template.format(
            peer=num_disp,
            datetime=ts_disp,
            pages=pages_disp,
            filename=pdf_path.name,
        )
    except (KeyError, IndexError, ValueError) as e:
        # テンプレートの書式が壊れていても通知自体は送れるようにする
        body = (
            f"(本文テンプレートの書式にエラーがあります: {e})\n\n"
            f"着信電話番号: {num_disp} / 受信日時: {ts_disp} / "
            f"ページ数: {pages_disp} / ファイル: {pdf_path.name}"
        )
    msg.set_content(body)

    try:
        data = pdf_path.read_bytes()
        msg.add_attachment(
            data, maintype="application", subtype="pdf", filename=pdf_path.name
        )
    except OSError as e:
        return MailResult(False, f"添付 PDF を読めません: {e}")

    try:
        if cfg.smtp_security == "ssl":
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=30, context=ctx) as s:
                if cfg.smtp_username:
                    s.login(cfg.smtp_username, cfg.smtp_password or "")
                s.send_message(msg)
        else:
            with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as s:
                s.ehlo()
                if cfg.smtp_security == "starttls":
                    s.starttls(context=ssl.create_default_context())
                    s.ehlo()
                if cfg.smtp_username:
                    s.login(cfg.smtp_username, cfg.smtp_password or "")
                s.send_message(msg)
        return MailResult(True, f"{len(recipients)} 件へ送信しました。")
    except Exception as e:  # noqa: BLE001
        return MailResult(False, f"SMTP 送信エラー: {e}")


def smtp_test(cfg) -> MailResult:  # FaxConfig
    """SMTP 設定の疎通テスト (ログインまで)。"""
    if not cfg.smtp_host:
        return MailResult(False, "SMTP ホスト未設定")
    try:
        if cfg.smtp_security == "ssl":
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=20, context=ctx) as s:
                if cfg.smtp_username:
                    s.login(cfg.smtp_username, cfg.smtp_password or "")
        else:
            with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=20) as s:
                s.ehlo()
                if cfg.smtp_security == "starttls":
                    s.starttls(context=ssl.create_default_context())
                    s.ehlo()
                if cfg.smtp_username:
                    s.login(cfg.smtp_username, cfg.smtp_password or "")
        return MailResult(True, "SMTP 接続・認証に成功しました。")
    except Exception as e:  # noqa: BLE001
        return MailResult(False, f"SMTP テスト失敗: {e}")


# ---------------------------------------------------------------------------
# SMB/CIFS 外部保存
# ---------------------------------------------------------------------------


@dataclass
class SmbResult:
    ok: bool
    detail: str
    remote_path: str | None = None


def _smb_unc(cfg, filename: str) -> str:
    """表示用 UNC パス文字列を組み立てる。"""
    sub = (cfg.smb_path or "").strip("/\\")
    parts = [p for p in [cfg.smb_share, sub, filename] if p]
    return "\\\\" + (cfg.smb_server or "") + "\\" + "\\".join(parts)


def save_received_fax_smb(cfg, pdf_path: Path) -> SmbResult:  # FaxConfig
    """受信 FAX PDF を SMB/CIFS 共有に保存する。

    smbprotocol を使用 (mount.cifs 不要・純 Python・SMB2/3)。
    同期関数なので呼び出し側で run_in_executor 推奨。
    """
    if not cfg.smb_server or not cfg.smb_share:
        return SmbResult(False, "SMB サーバー/共有名が未設定です。")
    if not pdf_path.exists():
        return SmbResult(False, f"保存元 PDF が見つかりません: {pdf_path}")

    try:
        import smbclient
    except ImportError:
        return SmbResult(
            False,
            "smbprotocol が未インストールです。pip install smbprotocol",
        )

    server = cfg.smb_server.strip()
    share = cfg.smb_share.strip().strip("/\\")
    subdir = (cfg.smb_path or "").strip().strip("/\\").replace("/", "\\")
    fname = pdf_path.name

    # smbclient は \\server\share\dir\file 形式
    base = f"\\\\{server}\\{share}"
    remote_dir = base if not subdir else f"{base}\\{subdir}"
    remote_file = f"{remote_dir}\\{fname}"

    try:
        # 認証情報を登録
        smbclient.register_session(
            server,
            username=cfg.smb_username or "",
            password=cfg.smb_password or "",
            port=cfg.smb_port or 445,
        )
        # サブディレクトリが無ければ作成 (多段対応)
        if subdir:
            cur = base
            for seg in subdir.split("\\"):
                cur = f"{cur}\\{seg}"
                try:
                    smbclient.makedirs(cur, exist_ok=True)
                except OSError:
                    pass
        # ファイル転送
        with open(pdf_path, "rb") as src, smbclient.open_file(
            remote_file, mode="wb"
        ) as dst:
            shutil.copyfileobj(src, dst)
        return SmbResult(
            True, "SMB に保存しました。", remote_path=_smb_unc(cfg, fname)
        )
    except Exception as e:  # noqa: BLE001
        return SmbResult(False, f"SMB 保存エラー: {e}")
    finally:
        try:
            import smbclient

            smbclient.delete_session(server, port=cfg.smb_port or 445)
        except Exception:  # noqa: BLE001
            pass


def smb_test(cfg) -> SmbResult:  # FaxConfig
    """SMB 設定の疎通テスト (接続 + 共有/ディレクトリ一覧取得まで)。"""
    if not cfg.smb_server or not cfg.smb_share:
        return SmbResult(False, "SMB サーバー/共有名が未設定です。")
    try:
        import smbclient
    except ImportError:
        return SmbResult(False, "smbprotocol が未インストールです。")

    server = cfg.smb_server.strip()
    share = cfg.smb_share.strip().strip("/\\")
    subdir = (cfg.smb_path or "").strip().strip("/\\").replace("/", "\\")
    target = f"\\\\{server}\\{share}"
    if subdir:
        target = f"{target}\\{subdir}"

    try:
        smbclient.register_session(
            server,
            username=cfg.smb_username or "",
            password=cfg.smb_password or "",
            port=cfg.smb_port or 445,
        )
        # ディレクトリ一覧を試行 (権限・パス確認)
        try:
            list(smbclient.scandir(target))
            msg = f"接続成功。保存先 {target} にアクセスできました。"
        except OSError:
            # サブディレクトリが未作成でも共有自体に繋がれば OK 扱い
            list(smbclient.scandir(f"\\\\{server}\\{share}"))
            msg = (
                f"接続成功。共有 \\\\{server}\\{share} にアクセスできました "
                f"(サブフォルダ {subdir or '(なし)'} は受信時に自動作成されます)。"
            )
        return SmbResult(True, msg)
    except Exception as e:  # noqa: BLE001
        return SmbResult(False, f"SMB テスト失敗: {e}")
    finally:
        try:
            import smbclient

            smbclient.delete_session(server, port=cfg.smb_port or 445)
        except Exception:  # noqa: BLE001
            pass
