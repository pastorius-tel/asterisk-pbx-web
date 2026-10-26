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
    ただし旧版の .env.example に載っていた見本の値 (公開されている文字列)
    がそのまま残っている環境があるため、それは「未設定」として扱い、
    自動生成した値に差し替える。見本のままだと、文字列を知っている人は
    誰でもフックを叩けてしまう。
  - 比較は hmac.compare_digest で行う (文字列 != では、先頭から何文字
    一致したかが応答時間の差として漏れるため)。
  - 送信元 IP も既定で 127.0.0.1 / ::1 に限定する。Asterisk と本ツールを
    別ホストで動かす場合は .env の TRUSTED_HOOK_HOSTS で許可する。
"""

from __future__ import annotations

import base64
import binascii
import hmac
import logging
import re
import secrets

from fastapi import HTTPException, Request
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import AppSettings

log = logging.getLogger(__name__)

# ダイヤルプランから渡される自由文字列フィールドの接頭辞。
#
# 経緯: 着信通知のフックは Asterisk の System(curl ...) で叩いている。
# curl のコマンド文字列には発信者名 (${CALLERID(name)}) や相手 FAX の
# 局番号 (${REMOTESTATIONID}) など「外部から届く値」がそのまま入る。
# これらは Asterisk がコマンド文字列へ展開したあと /bin/sh に渡される
# ため、値にシェルの特殊文字が入っていると、意図しないコマンドとして
# 解釈されてしまう余地があった (着信 1 本でサーバー上の任意コマンドを
# 実行させられる、という深刻な経路)。
#
# 対策: これらの値はダイヤルプラン側で ${BASE64_ENCODE(...)} により
# base64 へ変換してから渡す。base64 の文字種は [A-Za-z0-9+/=] だけで
# シェルの特殊文字を一切含まないため、/bin/sh に渡っても安全。
# サーバー側 (ここ) で "b64:" 接頭辞を見て復号する。古いダイヤルプラン
# (接頭辞なしの素の値) が残っていても壊れないよう、接頭辞が無ければ
# そのまま使う。
_B64_PREFIX = "b64:"

# .env で指定されたフック用トークンとして受け付ける形式。
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{16,128}")

# トークンを渡す HTTP ヘッダ名。URL (クエリ) に載せると Web サーバーの
# アクセスログにそのまま記録され、ログを読める人に知られてしまうため、
# 現行のダイヤルプランはヘッダで渡す。旧版のダイヤルプランとの互換の
# ため、クエリの token= も引き続き受け付ける。
HOOK_TOKEN_HEADER = "X-Hook-Token"


def decode_hook_field(value: str) -> str:
    """フック経由で届いた値を復号する ("b64:" 接頭辞があれば base64 復号)。

    復号できない値 (壊れた base64 等) は安全側に倒して空文字にする。
    接頭辞が無い値は後方互換のためそのまま返す。
    """
    if not value:
        return value
    if value.startswith(_B64_PREFIX):
        raw = value[len(_B64_PREFIX):]
        if not raw:
            return ""
        try:
            return base64.b64decode(raw, validate=True).decode("utf-8", "replace")
        except (binascii.Error, ValueError):
            log.warning("フックの base64 フィールドを復号できませんでした。")
            return ""
    return value

# 名前 → (.env 側の設定値, AppSettings 側のカラム名)
_HOOKS: dict[str, tuple[str, str]] = {
    "fax": ("fax_hook_token", "fax_hook_token"),
    "voicemail": ("voicemail_hook_token", "voicemail_hook_token"),
    "calllog": ("calllog_hook_token", "calllog_hook_token"),
}

# 旧版の .env.example に見本として載せていた値。これをそのままコピーして
# 運用している環境があるため、設定されていても「未設定」と同じ扱いにして
# 自動生成したトークンへ差し替える (公開済みの文字列なので意味がない)。
# 比較は小文字化して行う。
_PLACEHOLDER_TOKENS: frozenset[str] = frozenset(
    {
        "please-change-this-fax-hook-token",
        "please-change-this-voicemail-hook-token",
        "please-change-this-calllog-hook-token",
        "please-change-this",
        "please-change-me",
        "change-me",
        "changeme",
        "your-token-here",
        "token",
        "secret",
    }
)


async def get_hook_token(db: AsyncSession, kind: str) -> str:
    """フック用トークンを取得する。無ければ生成して保存する。"""
    env_attr, col = _HOOKS[kind]
    env_value = (getattr(settings, env_attr, "") or "").strip()
    if env_value and env_value.lower() in _PLACEHOLDER_TOKENS:
        log.warning(
            ".env の %s が見本の値のままなので無視し、自動生成した"
            "トークンを使います。.env の該当行を削除してください。",
            env_attr.upper(),
        )
        env_value = ""
    if env_value and not _TOKEN_RE.fullmatch(env_value):
        # トークンはダイヤルプラン (シェルに渡るコマンド行) に埋め込まれる。
        # 英数字・_・- 以外が入っていると引用が崩れて通知が壊れるうえ、
        # 短すぎる値は推測されやすい。使わずに自動生成の値へ切り替える。
        log.warning(
            ".env の %s は使える文字 (英数字・_・-) 以外を含むか短すぎる"
            "(16 文字未満) ため無視し、自動生成したトークンを使います。",
            env_attr.upper(),
        )
        env_value = ""
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

    # 「空なら書き込む」を 1 つの UPDATE で行い、その後に読み直す。
    # 本番は gunicorn のワーカーが 2 つあるため、単純に setattr → commit
    # すると、2 つのワーカーが同時に別々のトークンを作って後勝ちで
    # 上書きし、片方が extensions.conf に書いたトークンが DB と食い違って
    # フックが 403 になる恐れがある。条件付き UPDATE なら最初の 1 つだけが
    # 書き込みに成功し、もう片方は読み直しで同じ値を得る。
    token = secrets.token_urlsafe(24)
    column = getattr(AppSettings, col)
    result = await db.execute(
        update(AppSettings)
        .where(AppSettings.id == row.id)
        .where(or_(column.is_(None), column == ""))
        .values({col: token})
    )
    await db.commit()
    if result.rowcount:
        log.info("%s フック用トークンを自動生成しました。", kind)
    await db.refresh(row, attribute_names=[col])
    return (getattr(row, col, "") or "").strip() or token


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
    """送信元とトークンの両方を検証する。不一致なら 403。

    トークンはヘッダ (X-Hook-Token) を優先し、無ければクエリの token=
    (旧版のダイヤルプラン) を見る。"""
    check_hook_source(request)
    token = request.headers.get(HOOK_TOKEN_HEADER) or token
    expected = await get_hook_token(db, kind)
    if not expected or not hmac.compare_digest(
        (token or "").encode("utf-8"), expected.encode("utf-8")
    ):
        log.warning("内部フックのトークンが一致しません (%s)。", kind)
        raise HTTPException(status_code=403, detail="invalid token")
