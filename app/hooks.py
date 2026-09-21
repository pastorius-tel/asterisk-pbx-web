"""Asterisk のダイヤルプランから叩かれる内部フックの認証。

FAX 受信完了 / 留守番電話の録音完了 / 発着信履歴の記録 は、Asterisk が
``System(curl ...)`` で本ツールの URL を叩くことで通知される。これらは
ブラウザのログインセッションを持てないため、URL に付けた合言葉
(トークン) と送信元 IP で認証する。

設計上の注意:
  - トークンが未設定のまま「照合したら通す」実装にすると、事実上
    誰でも叩ける状態になる。そこで **未設定なら初回に自動生成** し、
    DB (app_settings) に保存する。gunicorn の複数ワーカーが同じ値を
    見る必要があるためプロセス内メモリには置かない。
  - .env に明示的な値があればそちらを優先する (既存導入との互換)。
  - 比較は hmac.compare_digest で行う (文字列 != では、先頭から何文字
    一致したかが応答時間の差として漏れるため)。
  - 送信元 IP も既定で 127.0.0.1 / ::1 に限定する。Asterisk と本ツールを
    別ホストで動かす場合は .env の TRUSTED_HOOK_HOSTS で許可する。
"""

from __future__ import annotations

import hmac
import logging
import secrets

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import AppSettings

log = logging.getLogger(__name__)

# 名前 → (.env 側の設定値, AppSettings 側のカラム名)
_HOOKS: dict[str, tuple[str, str]] = {
    "fax": ("fax_hook_token", "fax_hook_token"),
    "voicemail": ("voicemail_hook_token", "voicemail_hook_token"),
    "calllog": ("calllog_hook_token", "calllog_hook_token"),
}


async def get_hook_token(db: AsyncSession, kind: str) -> str:
    """フック用トークンを取得する。無ければ生成して保存する。"""
    env_attr, col = _HOOKS[kind]
    env_value = (getattr(settings, env_attr, "") or "").strip()
    if env_value:
        return env_value

    row = (await db.scalars(select(AppSettings).limit(1))).first()
    if row is None:
        row = AppSettings()
        db.add(row)
        await db.flush()

    current = (getattr(row, col, "") or "").strip()
    if current:
        return current

    token = secrets.token_urlsafe(24)
    setattr(row, col, token)
    await db.commit()
    log.info("%s フック用トークンを自動生成しました。", kind)
    return token


def _trusted_hosts() -> set[str]:
    raw = (settings.trusted_hook_hosts or "").strip()
    if not raw:
        return set()
    return {h.strip() for h in raw.split(",") if h.strip()}


def check_hook_source(request: Request) -> None:
    """送信元 IP が許可されているか確認する。"""
    allowed = _trusted_hosts()
    if not allowed:  # 空 = 制限しない (別ホストの Asterisk 向け)
        return
    host = request.client.host if request.client else ""
    if host not in allowed:
        log.warning("許可されていない送信元からの内部フック: %s", host)
        raise HTTPException(status_code=403, detail="forbidden")


async def verify_hook(request: Request, db: AsyncSession, kind: str, token: str) -> None:
    """送信元とトークンの両方を検証する。不一致なら 403。"""
    check_hook_source(request)
    expected = await get_hook_token(db, kind)
    if not expected or not hmac.compare_digest(
        (token or "").encode("utf-8"), expected.encode("utf-8")
    ):
        log.warning("内部フックのトークンが一致しません (%s)。", kind)
        raise HTTPException(status_code=403, detail="invalid token")
