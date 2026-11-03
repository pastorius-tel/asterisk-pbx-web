"""通知メールの SMTP 送信 (受信 FAX・留守番電話・テストメールで共通)。

接続先は FAX 設定の「メール設定」(FaxConfig の smtp_* / mail_*)。
本ツールが smtplib で直接送るため、サーバーの Postfix は不要
(Postfix を設定済みなら、ホスト 127.0.0.1 / ポート 25 / セキュリティ
「なし」/ ユーザー名・パスワード空欄 で、そちら経由でも送れる)。

送信処理を 1 か所にまとめ、次をそろえて行う:
  - Date / Message-ID ヘッダーを付ける (smtplib は付けない。無いと受信側で
    迷惑メール扱いや拒否の原因になる)
  - SMTP サーバーが DATA に返した応答 (「250 2.0.0 OK ... queued as ...」)
    と Message-ID を結果に入れる (届かないときに送信先で追跡できるように)
  - 一部の宛先だけ拒否された場合も、それを結果に残す
  - よくある失敗を日本語の対処つきで返す

同期関数 (smtplib)。呼び出し側で run_in_executor 推奨。
"""

from __future__ import annotations

import smtplib
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

SMTP_TIMEOUT = 30  # 秒


@dataclass
class MailResult:
    ok: bool
    detail: str
    message_id: str = ""
    server_reply: str = ""


class _RecordReplyMixin:
    """DATA に対するサーバーの最終応答を残す (smtplib は捨ててしまう)。"""

    last_data_reply: str = ""

    def data(self, msg):  # type: ignore[override]
        code, resp = super().data(msg)  # type: ignore[misc]
        text = resp.decode("utf-8", "replace") if isinstance(resp, bytes) else str(resp)
        self.last_data_reply = f"{code} {text}".strip()
        return code, resp


class _SMTP(_RecordReplyMixin, smtplib.SMTP):
    pass


class _SMTPSSL(_RecordReplyMixin, smtplib.SMTP_SSL):
    pass


def split_addresses(text: str | None) -> list[str]:
    """「a@example.com, b@example.com」→ ["a@example.com", "b@example.com"]"""
    return [a.strip() for a in (text or "").replace(";", ",").split(",") if a.strip()]


def _from_domain(mail_from: str) -> str:
    addr = parseaddr(mail_from or "")[1]
    domain = addr.rpartition("@")[2].strip().strip(".")
    if domain and all(c.isalnum() or c in "-." for c in domain):
        return domain
    return socket.getfqdn() or "localhost"


def prepare(msg: EmailMessage, mail_from: str) -> str:
    """Date / Message-ID を付ける (既にあれば残す)。Message-ID を返す。"""
    if "Date" not in msg:
        msg["Date"] = formatdate(localtime=True)
    if "Message-ID" not in msg:
        msg["Message-ID"] = make_msgid(domain=_from_domain(mail_from))
    return str(msg["Message-ID"])


def _is_loopback(host: str) -> bool:
    return host.strip().lower() in ("localhost", "127.0.0.1", "::1")


def _reply_text(e: BaseException) -> str:
    """例外を「535 5.7.8 Username and Password not accepted」のような読める形にする。"""
    def dec(v) -> str:
        return v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        return ", ".join(f"{a}: {c} {dec(r)}" for a, (c, r) in e.recipients.items())
    if isinstance(e, smtplib.SMTPResponseException):
        return f"{e.smtp_code} {dec(e.smtp_error)}".strip()
    return str(e) or e.__class__.__name__


def explain_error(cfg, e: BaseException) -> str:
    """送信失敗の理由を、対処のヒントつきの日本語にする。"""
    raw = _reply_text(e)
    host = (cfg.smtp_host or "").strip()
    port = cfg.smtp_port
    gmail = host.lower().endswith("gmail.com")
    if isinstance(e, smtplib.SMTPAuthenticationError):
        hint = "SMTP ユーザー名かパスワードが違います。"
        if gmail:
            hint += (" Gmail は通常のパスワードでは送れません。2 段階認証を有効にして"
                     "「アプリパスワード」(16 文字) を発行し、パスワード欄に入れてください。")
        return f"{hint} (サーバーの応答: {raw})"
    if isinstance(e, smtplib.SMTPNotSupportedError) or "not supported by server" in raw:
        if "STARTTLS" in raw:
            if _is_loopback(host):
                return ("このサーバー (Postfix など) は STARTTLS を使いません。"
                        f"セキュリティを「なし」にしてください。({raw})")
            return f"SMTP サーバーが STARTTLS に対応していません。セキュリティとポートを確かめてください。({raw})"
        if _is_loopback(host):
            return ("このサーバー (Postfix など) は認証を使いません。SMTP ユーザー名と"
                    f"パスワードを空欄にしてください。({raw})")
        return f"SMTP サーバーが認証 (ログイン) に対応していません。ユーザー名・パスワードを空欄にするか、セキュリティを確かめてください。({raw})"
    if isinstance(e, smtplib.SMTPSenderRefused):
        hint = "送信元アドレス (From) を受け付けませんでした。"
        if gmail:
            hint += " Gmail では、From を SMTP ユーザー名と同じアドレスにしてください。"
        return f"{hint} (サーバーの応答: {raw})"
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        return f"通知先アドレス (To) をすべて受け付けませんでした。アドレスを確かめてください。({raw})"
    if isinstance(e, smtplib.SMTPDataError):
        return f"サーバーがメールの受け取りを拒否しました。({raw})"
    if isinstance(e, ssl.SSLCertVerificationError):
        if _is_loopback(host):
            return ("このサーバーの証明書を確認できません。同じサーバーの Postfix を使う場合は、"
                    f"セキュリティを「なし」にしてください。({raw})")
        return f"SMTP サーバーの証明書を確認できません。ホスト名が正しいか確かめてください。({raw})"
    security = cfg.smtp_security or "starttls"
    mismatch = (security != "ssl" and port == 465) or (security == "ssl" and port in (25, 587))
    if isinstance(e, (ssl.SSLError, smtplib.SMTPServerDisconnected, TimeoutError, socket.timeout)) and (
        mismatch or isinstance(e, (ssl.SSLError, smtplib.SMTPServerDisconnected))
    ):
        return ("暗号化の方式がポートと合っていない可能性があります。ふつうは、ポート 587 なら"
                "「STARTTLS」、465 なら「SSL/TLS」、このサーバーの Postfix (25) なら「なし」です。"
                f"({raw})")
    if isinstance(e, socket.gaierror):
        return f"SMTP ホスト名「{host}」を引けません。綴りと、サーバーの DNS 設定を確かめてください。({raw})"
    if isinstance(e, (TimeoutError, socket.timeout)):
        return (f"{host}:{port} から応答がありません。ポート番号と、ファイアウォール・"
                f"プロバイダーの送信制限 (25 番ポートの遮断など) を確かめてください。({raw})")
    if isinstance(e, ConnectionRefusedError):
        return f"{host}:{port} に接続を断られました。ポート番号と、相手のサーバーが動いているか確かめてください。({raw})"
    if isinstance(e, OSError):
        return f"{host}:{port} に接続できません。({raw})"
    return f"SMTP 送信エラー: {raw}"


def deliver(cfg, msg: EmailMessage, *, timeout: int = SMTP_TIMEOUT) -> MailResult:
    """cfg (FaxConfig) の SMTP 設定で msg を送る。"""
    host = (cfg.smtp_host or "").strip()
    if not host:
        return MailResult(False, "SMTP ホストが未設定です。")
    if not msg.get("From") or not msg.get("To"):
        return MailResult(False, "送信元 (From) か宛先 (To) が空です。")
    message_id = prepare(msg, str(msg["From"]))
    security = cfg.smtp_security or "starttls"
    try:
        if security == "ssl":
            s: _SMTP | _SMTPSSL = _SMTPSSL(
                host, cfg.smtp_port, timeout=timeout, context=ssl.create_default_context()
            )
        else:
            s = _SMTP(host, cfg.smtp_port, timeout=timeout)
        with s:
            s.ehlo()
            if security == "starttls":
                s.starttls(context=ssl.create_default_context())
                s.ehlo()
            if cfg.smtp_username:
                if security == "none" and not s.has_extn("auth") and s.has_extn("starttls"):
                    # Gmail などは暗号化 (STARTTLS) の後でしかログインを受け付けない
                    return MailResult(
                        False,
                        "この SMTP サーバーは、暗号化してからでないとログインできません。"
                        "セキュリティを「STARTTLS」にしてください (ポート 587 の場合)。",
                        message_id=message_id,
                    )
                s.login(cfg.smtp_username, cfg.smtp_password or "")
            refused = s.send_message(msg)
            reply = s.last_data_reply
    except Exception as e:  # noqa: BLE001
        return MailResult(False, explain_error(cfg, e), message_id=message_id)

    to_all = split_addresses(str(msg.get("To", "")))
    accepted = [a for a in to_all if a not in refused]
    detail = f"{', '.join(accepted) or '宛先'} へ送信しました。"
    if refused:
        bad = ", ".join(f"{a} ({c} {r.decode('utf-8', 'replace') if isinstance(r, bytes) else r})"
                        for a, (c, r) in refused.items())
        detail += f" ただし次の宛先は受け付けられませんでした: {bad}"
    return MailResult(True, detail, message_id=message_id, server_reply=reply)


def build_test_mail(cfg, *, now: datetime | None = None) -> EmailMessage:
    """メール設定のテスト用メール (通知と同じ From / To)。"""
    now = now or datetime.now()
    stamp = now.strftime("%Y年%m月%d日 %H:%M:%S")
    msg = EmailMessage()
    msg["Subject"] = f"[テスト] PBX 通知メールの確認 ({stamp})"
    msg["From"] = cfg.mail_from
    msg["To"] = ", ".join(split_addresses(cfg.mail_to))
    host = (cfg.smtp_host or "").strip()
    msg.set_content(
        "Asterisk PBX Web Management のメール設定のテストです。\n\n"
        "このメールが届いていれば、受信 FAX と留守番電話の通知メールも\n"
        "同じ設定で届きます。\n\n"
        f"  送信日時     : {stamp}\n"
        f"  SMTP サーバー: {host}:{cfg.smtp_port} ({cfg.smtp_security})\n"
        f"  送信元 (From): {cfg.mail_from}\n"
        f"  宛先 (To)    : {msg['To']}\n\n"
        "このメールに返信する必要はありません。\n"
    )
    return msg


def send_test_mail(cfg) -> MailResult:
    """通知先 (To) へ、実際にテストメールを送る。"""
    if not (cfg.smtp_host or "").strip():
        return MailResult(False, "SMTP ホストを入力してください。")
    if not (cfg.mail_from or "").strip():
        return MailResult(False, "送信元アドレス (From) を入力してください。")
    if not split_addresses(cfg.mail_to):
        return MailResult(False, "通知先アドレス (To) を入力してください (テストメールはここへ送ります)。")
    return deliver(cfg, build_test_mail(cfg))
