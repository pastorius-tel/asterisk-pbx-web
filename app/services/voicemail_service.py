"""ボイスメールサービス。

Asterisk (app_voicemail) はメッセージを DB ではなくファイルシステムに
直接保存する (デフォルトの mailbox storage)。本ツールは独自の DB
テーブルを持たず、常にファイルシステムを直接走査して一覧を作る
(Asterisk 自身が実際に読み書きする実体そのものを見るため、表示と
実体がズレる心配が無い)。

保存パス: <voicemail_spool_dir>/<context>/<内線番号>/<フォルダ>/msgNNNN.<拡張子>
  例: /var/spool/asterisk/voicemail/default/201/INBOX/msg0000.wav
      /var/spool/asterisk/voicemail/default/201/INBOX/msg0000.txt (メタデータ)

メタデータ (.txt) の書式 ([message] セクションの key=value):
  origmailbox / context / macrocontext / exten / priority / callerchan
  callerid (例: "0312345678" <0312345678>) / origdate / origtime (unix time)
  category / duration (秒)
"""

from __future__ import annotations

import logging
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from app.config import settings

log = logging.getLogger(__name__)

VM_CONTEXT = "default"
"""voicemail.conf 側で全内線を [default] コンテキストで運用しているため固定。"""

# 一覧表示の対象フォルダ。Urgent/Work/Family/Friends/Cust1-5 等もあり得るが、
# 実運用で使うのはほぼ INBOX (新着) と Old (聴取済) のみのため、ひとまず
# この 2 つを対象にする。
FOLDERS = ("INBOX", "Old")

_CALLERID_RE = re.compile(r'"?([^"<]*)"?\s*<([^>]*)>')
# アンカー必須: 未アンカーだと match()/search() で使ったときに
# "../201" のような値の一部に一致してしまう (現在は fullmatch だけ
# だが、将来の書き換えで穴にならないよう明示しておく)。
_EXTENSION_RE = re.compile(r"^[0-9]{1,10}$")
_SAFE_FILENAME = re.compile(r"[^0-9A-Za-z_\-\u3040-\u30ff\u4e00-\u9fff]")


@dataclass
class VoicemailMessage:
    extension: str
    folder: str
    msg_num: str
    """ファイル名の数値部分 (例 'msg0000' の '0000')。削除・再生時の識別子。"""
    caller_number: str | None
    caller_name: str | None
    when: datetime | None
    duration_sec: int | None
    audio_path: Path | None
    """再生・ダウンロードに使う実体ファイル (.wav を優先)。無ければ None。"""


def _mailbox_dir(extension: str) -> Path:
    return settings.voicemail_spool_dir / VM_CONTEXT / extension


def _parse_meta(txt_path: Path) -> dict[str, str]:
    meta: dict[str, str] = {}
    try:
        for line in txt_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith(";") or line.startswith("[") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            meta[k.strip()] = v.strip()
    except OSError as e:
        log.warning("ボイスメールメタデータ読み込み失敗: %s (%s)", txt_path, e)
    return meta


def _pick_audio(dir_: Path, msg_num: str) -> Path | None:
    """再生用の実体ファイルを選ぶ。ブラウザ再生できる .wav を優先。"""
    for ext in ("wav", "WAV"):
        p = dir_ / f"msg{msg_num}.{ext}"
        if p.exists():
            return p
    # .wav が無い場合 (旧設定で録音された等) は他形式でも返す
    # (ダウンロードだけは可能にする。ブラウザ再生はできない可能性がある)
    for p in dir_.glob(f"msg{msg_num}.*"):
        if p.suffix.lower() != ".txt":
            return p
    return None


def _load_one(extension: str, folder: str, msg_num: str) -> VoicemailMessage | None:
    """1件分のメッセージを .txt メタデータ + 音声実体から組み立てる。

    list_messages / get_message の共通処理 (重複排除のため一本化)。
    """
    dir_ = _mailbox_dir(extension) / folder
    txt_path = dir_ / f"msg{msg_num}.txt"
    if not txt_path.exists():
        return None
    meta = _parse_meta(txt_path)

    caller_number = None
    caller_name = None
    cid = meta.get("callerid", "")
    m = _CALLERID_RE.match(cid)
    if m:
        caller_name = m.group(1).strip() or None
        caller_number = m.group(2).strip() or None
    elif cid and cid.lower() != "unknown":
        caller_number = cid

    when = None
    origtime = meta.get("origtime")
    if origtime and origtime.isdigit():
        try:
            when = datetime.fromtimestamp(int(origtime))
        except (ValueError, OSError):
            when = None

    duration = None
    dur_raw = meta.get("duration")
    if dur_raw and dur_raw.isdigit():
        duration = int(dur_raw)

    return VoicemailMessage(
        extension=extension,
        folder=folder,
        msg_num=msg_num,
        caller_number=caller_number,
        caller_name=caller_name,
        when=when,
        duration_sec=duration,
        audio_path=_pick_audio(dir_, msg_num),
    )


def list_messages(extension: str) -> list[VoicemailMessage]:
    """指定内線のメッセージ一覧 (INBOX + Old) を新しい順で返す。"""
    results: list[VoicemailMessage] = []
    base = _mailbox_dir(extension)
    for folder in FOLDERS:
        dir_ = base / folder
        if not dir_.is_dir():
            continue
        for txt_path in sorted(dir_.glob("msg*.txt")):
            msg_num = txt_path.stem.removeprefix("msg")
            msg = _load_one(extension, folder, msg_num)
            if msg is not None:
                results.append(msg)
    results.sort(key=lambda m: m.when or datetime.min, reverse=True)
    return results


def get_message(extension: str, folder: str, msg_num: str) -> VoicemailMessage | None:
    """1件分の詳細 (発信者・日時・音声パス等) を取得。不正な入力なら None。"""
    if (
        not _EXTENSION_RE.fullmatch(extension)
        or folder not in FOLDERS
        or not re.fullmatch(r"[0-9]{1,6}", msg_num)
    ):
        return None
    return _load_one(extension, folder, msg_num)


def build_voicemail_filename(
    pattern: str, dt_format: str, extension: str, when: datetime | None
) -> str:
    """Web 画面での再生・ダウンロード時に使うファイル名 (.wav 付き) を組み立てる。

    Asterisk 自体の保存ファイル名 (msgNNNN.wav) は仕様上変更できない
    (app_voicemail の内部実装に固定されており、勝手に変えると
    VoicemailMain 側の再生・既読管理と整合しなくなる) ため、実体は
    そのまま、Web からダウンロード/再生させる際の「見た目のファイル名」
    だけをここで自由な形式に変換する (FAX の仕組みと同じ考え方)。

    プレースホルダーが未知のものを含んでいたり書式が壊れていても、
    ダウンロード自体が失敗しないよう例外は握りつぶして既定形式に
    フォールバックする。
    """
    when = when or datetime.now()
    ctx = {
        "extension": extension,
        "datetime": when.strftime(dt_format),
    }
    try:
        name = pattern.format(**ctx)
    except (KeyError, IndexError, ValueError):
        name = f"{extension}_{when.strftime(dt_format)}"
    name = _SAFE_FILENAME.sub("_", name).strip("_") or "voicemail"
    return f"{name}.wav"


def get_audio_path(extension: str, folder: str, msg_num: str) -> Path | None:
    """再生・ダウンロード用に実体ファイルパスを取得 (存在確認込み)。"""
    msg = get_message(extension, folder, msg_num)
    return msg.audio_path if msg is not None else None


def delete_message(extension: str, folder: str, msg_num: str) -> bool:
    """メッセージ一式 (.wav/.gsm/.wav49/.txt) を削除する。"""
    if (
        not _EXTENSION_RE.fullmatch(extension)
        or folder not in FOLDERS
        or not re.fullmatch(r"[0-9]{1,6}", msg_num)
    ):
        return False
    dir_ = _mailbox_dir(extension) / folder
    if not dir_.is_dir():
        return False
    deleted = False
    for p in dir_.glob(f"msg{msg_num}.*"):
        try:
            p.unlink(missing_ok=True)
            deleted = True
        except OSError as e:
            log.warning("ボイスメール削除失敗: %s (%s)", p, e)
    return deleted


def storage_available() -> bool:
    """ボイスメール保存先ディレクトリの有無を確認 (画面上の注意表示用)。"""
    return settings.voicemail_spool_dir.is_dir()


def format_japanese_datetime(dt: datetime) -> str:
    """「2026年8月18日 午後6時25分35秒」のような日本語日時文字列を作る。

    strftime の %A(曜日名) / %B(月名) / %p(午前午後) は Asterisk が使う
    C ライブラリのロケール設定に依存し、日本語ロケールが入っていない
    環境では英語表記になってしまう (この問題は emaildateformat=%A, %B
    ... で実際に発生した)。ここでは年月日時分秒を数値として自前で
    組み立てることで、サーバーのロケール設定に一切左右されず確実に
    日本語表記になるようにしている。月日は 0 埋めしない (8月18日)。
    """
    period = "午前" if dt.hour < 12 else "午後"
    hour12 = dt.hour % 12
    if hour12 == 0:
        hour12 = 12
    return (
        f"{dt.year}年{dt.month}月{dt.day}日 "
        f"{period}{hour12}時{dt.minute}分{dt.second}秒"
    )


@dataclass
class MailResult:
    ok: bool
    detail: str


def send_voicemail_notification(
    smtp_cfg,  # FaxConfig (SMTP 接続情報を流用)
    app_settings,  # AppSettings (件名・本文テンプレート)
    msg: VoicemailMessage,
    extension_display_name: str,
    to_address: str,
    attach: bool,
) -> MailResult:
    """留守番電話の録音完了をメールで通知 (音声ファイル添付可)。

    FAX 側は Asterisk 自身が sendmail 経由で直接送信するが、留守番電話は
    本ツール (Python) が Asterisk からの完了フック経由で組み立てて SMTP
    送信する。これにより日時表記を確実に日本語化でき、件名・本文・
    (既存の) ファイル名もすべて自由にカスタマイズできる。

    同期関数 (smtplib)。呼び出し側で run_in_executor 推奨。
    """
    if not smtp_cfg.smtp_host or not smtp_cfg.mail_from:
        return MailResult(False, "SMTP 設定 (host/from) が未設定です。FAX 設定画面のメール設定を確認してください。")
    if not to_address:
        return MailResult(False, "宛先メールアドレスが空です。")

    when = msg.when or datetime.now()
    ctx = {
        "extension": extension_display_name or msg.extension,
        "caller": msg.caller_number or "非通知/不明",
        "datetime": format_japanese_datetime(when),
        "duration": f"{msg.duration_sec}秒" if msg.duration_sec is not None else "不明",
    }

    subject_tpl = app_settings.voicemail_mail_subject_template or "[留守番電話] {extension} 様へ新着メッセージ"
    body_tpl = app_settings.voicemail_mail_body_template or (
        "{extension} 様\n\n新しいボイスメッセージが届きました。\n\n"
        "  発信者 : {caller}\n  受信日時 : {datetime}\n  長さ : {duration}\n\n"
        "添付の音声ファイルをご確認ください。\n"
    )
    try:
        subject = subject_tpl.format(**ctx)
    except (KeyError, IndexError, ValueError):
        subject = f"[留守番電話] {ctx['extension']} 様へ新着メッセージ"
    try:
        body = body_tpl.format(**ctx)
    except (KeyError, IndexError, ValueError) as e:
        body = (
            f"(本文テンプレートの書式にエラーがあります: {e})\n\n"
            f"発信者: {ctx['caller']} / 受信日時: {ctx['datetime']} / 長さ: {ctx['duration']}"
        )

    mail = EmailMessage()
    mail["Subject"] = subject
    mail["From"] = smtp_cfg.mail_from
    mail["To"] = to_address
    mail.set_content(body)

    if attach and msg.audio_path is not None:
        try:
            fname = build_voicemail_filename(
                app_settings.voicemail_filename_pattern,
                app_settings.voicemail_datetime_format,
                msg.extension, when,
            )
            data = msg.audio_path.read_bytes()
            mail.add_attachment(data, maintype="audio", subtype="wav", filename=fname)
        except OSError as e:
            log.warning("留守番電話の音声添付に失敗: %s", e)

    try:
        if smtp_cfg.smtp_security == "ssl":
            ctx2 = ssl.create_default_context()
            with smtplib.SMTP_SSL(smtp_cfg.smtp_host, smtp_cfg.smtp_port, timeout=30, context=ctx2) as s:
                if smtp_cfg.smtp_username:
                    s.login(smtp_cfg.smtp_username, smtp_cfg.smtp_password or "")
                s.send_message(mail)
        else:
            with smtplib.SMTP(smtp_cfg.smtp_host, smtp_cfg.smtp_port, timeout=30) as s:
                s.ehlo()
                if smtp_cfg.smtp_security == "starttls":
                    s.starttls(context=ssl.create_default_context())
                    s.ehlo()
                if smtp_cfg.smtp_username:
                    s.login(smtp_cfg.smtp_username, smtp_cfg.smtp_password or "")
                s.send_message(mail)
        return MailResult(True, "送信しました。")
    except Exception as e:  # noqa: BLE001
        return MailResult(False, f"SMTP 送信エラー: {e}")
