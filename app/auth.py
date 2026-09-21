"""管理画面のログイン認証。

本ツールは PBX の全設定を書き換えられるうえ、留守番電話の録音を再生・
ダウンロードでき、発信ルートも変更できる。つまり **画面に到達できる人 =
その会社の電話を自由にできる人** である。社内 LAN だから安全、という
前提は (Wi-Fi ゲスト、持ち込み PC、踏み台にされた複合機などを考えると)
成り立たないため、パスワードを設定できるようにしている。

方針 (既定値の考え方):
  - `.env` に ADMIN_PASSWORD が設定されていれば認証が有効になる。
  - 未設定なら認証なしで動く (既存の導入を壊さないため)。ただし起動時に
    警告ログを出し、画面上部にも赤い帯で警告を出し続ける。
  - 新規インストール (scripts/install_all.sh) ではパスワードを自動生成
    するので、新しく入れた人は最初から認証ありになる。

パスワードの保存形式は 2 通りに対応する:
  - ADMIN_PASSWORD      … 平文 (LAN 内の小規模運用向け。手軽さ優先)
  - ADMIN_PASSWORD_HASH … pbkdf2_sha256$<反復回数>$<salt>$<hash>
                          平文を置きたくない場合はこちら
                          (`python -m app.auth hash` で生成できる)
両方が設定されている場合はハッシュを優先する。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass, field

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app.config import settings

log = logging.getLogger(__name__)

SESSION_KEY = "pbx_user"

# 認証なしでアクセスできるパス。
#   /login    : ログイン画面そのもの
#   /static   : CSS / JS (ログイン画面の見た目に必要)
#   /healthz  : 監視用
#   */hook/*  : Asterisk のダイヤルプランから curl で叩かれる内部フック。
#               ブラウザのセッションを持てないため、専用トークンで認証する
#               (app/routers/_hooks.py の verify_hook_token)。
_PUBLIC_PREFIXES = ("/login", "/logout", "/static/", "/healthz", "/favicon.ico")
_PUBLIC_HOOKS = ("/fax/hook/", "/voicemail/hook/", "/call-logs/hook/")

_PBKDF2_ROUNDS = 240_000


# ---------------------------------------------------------------------------
# パスワードハッシュ
# ---------------------------------------------------------------------------


def hash_password(password: str, *, rounds: int = _PBKDF2_ROUNDS) -> str:
    """pbkdf2_sha256$<rounds>$<salt>$<hash> 形式の文字列を作る。

    外部ライブラリ (passlib 等) を足さずに済むよう標準ライブラリだけで
    実装している。ラウンド数は OWASP の推奨値に合わせてある。
    """
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return (
        f"pbkdf2_sha256${rounds}$"
        f"{base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"
    )


def verify_password(password: str, stored: str) -> bool:
    """平文またはハッシュ文字列と照合する (タイミング差を作らない比較)。"""
    stored = (stored or "").strip()
    if not stored:
        return False
    if stored.startswith("pbkdf2_sha256$"):
        try:
            _, rounds_s, salt_b64, hash_b64 = stored.split("$", 3)
            rounds = int(rounds_s)
            salt = base64.b64decode(salt_b64)
            expected = base64.b64decode(hash_b64)
        except (ValueError, TypeError):
            log.error("ADMIN_PASSWORD_HASH の形式が不正です。")
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
        return hmac.compare_digest(dk, expected)
    return hmac.compare_digest(password.encode("utf-8"), stored.encode("utf-8"))


# ---------------------------------------------------------------------------
# 有効/無効の判定
# ---------------------------------------------------------------------------


def auth_enabled() -> bool:
    """パスワードが設定されていれば認証あり。"""
    return bool(
        (settings.admin_password_hash or "").strip()
        or (settings.admin_password or "").strip()
    )


def _stored_secret() -> str:
    return (settings.admin_password_hash or "").strip() or (
        settings.admin_password or ""
    ).strip()


def check_credentials(username: str, password: str) -> bool:
    if not hmac.compare_digest(
        (username or "").encode("utf-8"), settings.admin_user.encode("utf-8")
    ):
        # ユーザー名が違う場合もパスワード検証と同じ時間をかける
        # (存在するユーザー名かどうかを応答時間から推測させない)
        verify_password(password or "", _stored_secret())
        return False
    return verify_password(password or "", _stored_secret())


# ---------------------------------------------------------------------------
# ログイン試行の制限 (総当たり対策)
# ---------------------------------------------------------------------------


@dataclass
class _Attempts:
    count: int = 0
    blocked_until: float = 0.0


@dataclass
class LoginThrottle:
    """IP ごとの失敗回数を数え、一定回数を超えたら一時的に拒否する。

    プロセス内のメモリのみで管理する。gunicorn を複数ワーカーで動かすと
    ワーカーごとの数え方になるが、それでも総当たりの速度は桁で落ちる。
    (本格的な保護が要る場合はリバースプロキシ側で制限する前提。)
    """

    max_attempts: int = 8
    block_seconds: float = 300.0
    _by_ip: dict[str, _Attempts] = field(default_factory=dict)

    def blocked_for(self, ip: str) -> int:
        st = self._by_ip.get(ip)
        if st is None:
            return 0
        remain = st.blocked_until - time.monotonic()
        return int(remain) if remain > 0 else 0

    def record_failure(self, ip: str) -> None:
        st = self._by_ip.setdefault(ip, _Attempts())
        st.count += 1
        if st.count >= self.max_attempts:
            st.blocked_until = time.monotonic() + self.block_seconds
            st.count = 0
            log.warning("ログイン失敗が続いたため %s を一時的に拒否します。", ip)

    def record_success(self, ip: str) -> None:
        self._by_ip.pop(ip, None)


throttle = LoginThrottle()


# ---------------------------------------------------------------------------
# ミドルウェア
# ---------------------------------------------------------------------------


def _is_public(path: str) -> bool:
    if path in ("/login", "/logout", "/healthz"):
        return True
    if path.startswith(_PUBLIC_PREFIXES):
        return True
    return any(h in path for h in _PUBLIC_HOOKS)


async def auth_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    """未ログインならログイン画面へ飛ばす。"""
    if not auth_enabled() or _is_public(request.url.path):
        return await call_next(request)

    if request.session.get(SESSION_KEY):
        return await call_next(request)

    # htmx / fetch からの呼び出しにリダイレクトを返すと画面内に
    # ログインフォームが埋め込まれてしまうので、その場合は 401 を返す。
    if request.headers.get("hx-request") or request.headers.get(
        "accept", ""
    ).startswith("application/json"):
        return JSONResponse(
            {"ok": False, "reason": "ログインが必要です"}, status_code=401
        )

    nxt = request.url.path
    if request.url.query:
        nxt = f"{nxt}?{request.url.query}"
    return RedirectResponse(f"/login?next={_quote(nxt)}", status_code=303)


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


def login_user(request: Request) -> None:
    request.session[SESSION_KEY] = settings.admin_user
    # ログイン成功のたびにセッション ID を作り直す
    # (セッション固定化攻撃の対策)
    request.session["sid"] = secrets.token_urlsafe(16)


def logout_user(request: Request) -> None:
    request.session.clear()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def apply_no_store(response: Response) -> Response:
    """認証が要る画面をブラウザにキャッシュさせない。"""
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------------------
# CLI: パスワードハッシュの生成
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    import getpass
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "hash":
        pw = getpass.getpass("新しい管理パスワード: ")
        pw2 = getpass.getpass("もう一度入力: ")
        if pw != pw2:
            print("一致しません。")
            sys.exit(1)
        if len(pw) < 8:
            print("8 文字以上にしてください。")
            sys.exit(1)
        print("\n.env に以下の行を追加してください:\n")
        print(f"ADMIN_PASSWORD_HASH={hash_password(pw)}")
    else:
        print("使い方: python -m app.auth hash")
