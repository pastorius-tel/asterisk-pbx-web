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

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app.config import settings

log = logging.getLogger(__name__)

SESSION_KEY = "pbx_user"
# ログイン時点の資格情報 (ユーザー名 + パスワード設定) の指紋。
# .env のパスワードを変えたら、それ以前のセッションを無効にするために使う。
_FP_KEY = "pbx_cred"

# セッション Cookie の署名鍵 (main.py が起動時に設定する)。
# 指紋の計算に使い、Cookie を読まれてもパスワードを推測できないようにする。
_session_key: bytes = b""


def set_session_key(key: str) -> None:
    global _session_key
    _session_key = key.encode("utf-8")


def _credential_fingerprint() -> str:
    """現在の資格情報の指紋 (署名鍵つき HMAC。Cookie から逆算できない)。"""
    msg = f"{settings.admin_user}\0{_stored_secret()}".encode()
    return hmac.new(_session_key or b"-", msg, hashlib.sha256).hexdigest()[:32]

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


class LoginThrottle:
    """IP ごとの失敗回数を数え、一定回数を超えたら一時的に拒否する。

    回数は DB (login_attempts) に置く。gunicorn の全ワーカーで共有され、
    再起動しても消えない (以前はワーカーごとのメモリだったため、
    2 ワーカーなら実質 2 倍の回数を試せ、再起動で 0 に戻っていた)。
    """

    max_attempts: int = 8
    block_seconds: float = 300.0
    window_seconds: float = 900.0
    """最後の失敗からこの秒数が過ぎたら、それまでの失敗は数えない。"""

    async def blocked_for(self, ip: str) -> int:
        from app.database import AsyncSessionLocal
        from app.models import LoginAttempt

        async with AsyncSessionLocal() as db:
            row = await db.get(LoginAttempt, ip)
            if row is None:
                return 0
            remain = (row.blocked_until or 0.0) - time.time()
            return int(remain) + 1 if remain > 0 else 0

    async def record_failure(self, ip: str) -> None:
        from sqlalchemy.exc import IntegrityError

        from app.database import AsyncSessionLocal
        from app.models import LoginAttempt

        now = time.time()
        # 同じ新規 IP から同時に失敗が届くと、両方が「行が無い」と判断して
        # INSERT しようとし、主キー衝突 (IntegrityError) で片方が 500 に
        # なりうる (しかもその失敗は数えられない)。衝突したらロールバック
        # して「行がある」前提で数回やり直す。
        for _ in range(4):
            try:
                async with AsyncSessionLocal() as db:
                    row = await db.get(LoginAttempt, ip)
                    if row is None:
                        row = LoginAttempt(
                            ip=ip, failures=0, blocked_until=0.0, last_failure=0.0
                        )
                        db.add(row)
                    if now - (row.last_failure or 0.0) > self.window_seconds:
                        row.failures = 0
                    row.failures = (row.failures or 0) + 1
                    row.last_failure = now
                    if row.failures >= self.max_attempts:
                        row.blocked_until = now + self.block_seconds
                        row.failures = 0
                        log.warning(
                            "ログイン失敗が続いたため %s を一時的に拒否します。", ip
                        )
                    await db.commit()
                return
            except IntegrityError:
                # 別のワーカーが先に同じ行を作った。やり直すと db.get が
                # その行を拾い、更新側の経路を通る。
                continue
        log.warning("ログイン失敗の記録に繰り返し失敗しました (%s)。", ip)

    async def record_success(self, ip: str) -> None:
        from app.database import AsyncSessionLocal
        from app.models import LoginAttempt

        async with AsyncSessionLocal() as db:
            row = await db.get(LoginAttempt, ip)
            if row is not None:
                await db.delete(row)
                await db.commit()


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
        # パスワード (またはユーザー名) を変えた後は、それ以前のセッションを
        # 通さない。Cookie は署名付きで改ざんはできないが、サーバー側に
        # 失効の仕組みが無いため、資格情報の指紋で古いものを弾く。
        if hmac.compare_digest(
            str(request.session.get(_FP_KEY, "")), _credential_fingerprint()
        ):
            return await call_next(request)
        request.session.clear()

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
    request.session.clear()
    request.session[SESSION_KEY] = settings.admin_user
    request.session[_FP_KEY] = _credential_fingerprint()
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
