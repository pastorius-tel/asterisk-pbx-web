"""FastAPI アプリ本体。"""

from __future__ import annotations

import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager

# tomllib は Python 3.11 から標準。Raspberry Pi 版は Ubuntu 22.04 の
# OS 標準 Python (3.10) でも動かすため、無い場合は簡易読み取りに切り替える
# (pyproject.toml から version を 1 つ読むだけなので正規表現で十分)。
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    tomllib = None  # type: ignore[assignment]
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app import auth
from app.config import settings
from app.database import engine, init_db
from app.routers import (
    audio,
    blocked_numbers,
    calendar_exceptions,
    call_logs,
    dashboard,
    day_patterns,
    extensions,
    fax,
    holidays,
    inbound_routes,
    ivrs,
    login,
    moh_classes,
    outbound_routes,
    parking,
    phone_book,
    queues,
    ring_group,
    schedules,
    system,
    time_conditions,
    trunks,
    voicemail,
)

logging.basicConfig(level=logging.INFO if not settings.debug else logging.DEBUG)
log = logging.getLogger(__name__)


def _read_app_version() -> str:
    """pyproject.toml からアプリのバージョン文字列を読み取る。

    静的ファイル (CSS/JS) のキャッシュバスティング (?v=<version>) に
    使う。読み取れない場合でもアプリ自体は起動できるよう、失敗しても
    例外を投げずに固定値へフォールバックする。
    """
    path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        if tomllib is not None:
            with open(path, "rb") as f:
                data = tomllib.load(f)
            return str(data["project"]["version"])
        # Python 3.10: [project] セクションの version = "x.y.z" を探す
        text = path.read_text(encoding="utf-8")
        section = text.split("[project]", 1)[1].split("\n[", 1)[0]
        m = re.search(r'^version\s*=\s*"([^"]+)"', section, re.MULTILINE)
        if m is None:
            raise KeyError("version")
        return m.group(1)
    except (OSError, KeyError, IndexError, ValueError) as e:
        # tomllib.TOMLDecodeError は ValueError のサブクラス
        log.warning("pyproject.toml からバージョンを読み取れませんでした: %s", e)
        return "0"


APP_VERSION = _read_app_version()


def _resolve_secret_key() -> str:
    """セッション Cookie の署名鍵を決める。

    .env に十分な長さの SECRET_KEY があればそれを使う。無い / 短い /
    サンプル値のままの場合は、**既知の鍵で Cookie を偽造されるのを
    防ぐため** 起動のたびにランダム生成する (再起動でログアウトされる
    代わりに、鍵が漏れている状態にはならない)。
    """
    key = (settings.secret_key or "").strip()
    weak = {"change-me", "changeme", "secret", "please-change-me-to-a-long-random-string"}
    if len(key) >= 32 and key.lower() not in weak:
        return key
    if key:
        log.warning(
            "SECRET_KEY が短いか既定値のままです。自動生成した鍵を使います。"
        )
    return _shared_fallback_secret()


def _shared_fallback_secret() -> str:
    """SECRET_KEY が無いときの鍵を、全ワーカーで共有できる形で用意する。

    以前はプロセスごとに secrets.token_urlsafe() で作っていたが、本番は
    gunicorn でワーカーを 2 つ起動するため、ワーカーごとに鍵が違って
    しまい、ログイン後に別のワーカーへ振り分けられた瞬間に Cookie の
    署名が合わずログアウトされる (画面を開くたびにログイン画面へ戻る
    ように見える) という不具合になっていた。

    そこで DB と同じディレクトリに 0600 で 1 度だけ書き出し、全ワーカー・
    再起動後もそれを使う。複数のワーカーが同時に作ろうとしても、
    O_EXCL で最初の 1 つだけが作成に成功し、残りはそれを読む。
    書けない場合だけ、従来どおりプロセス内の一時鍵に戻る。
    """
    from app.database import _init_lock_dir  # 循環 import 回避

    base = _init_lock_dir()
    if base is None:
        return secrets.token_urlsafe(48)
    path = base / ".session_secret"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        pass  # 他のワーカーが先に作った (または前回の起動で作った)
    except OSError as e:
        log.warning("セッション鍵を保存できません (%s): %s", path, e)
        return secrets.token_urlsafe(48)
    else:
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(48))
        log.info("SECRET_KEY が未設定のため、セッション鍵を自動生成しました: %s", path)
    # 作成直後に別ワーカーが読むと空のことがあるので、中身が入るまで待つ
    for _ in range(50):
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError as e:
            log.warning("セッション鍵を読めません (%s): %s", path, e)
            return secrets.token_urlsafe(48)
        if len(value) >= 32:
            return value
        time.sleep(0.05)
    log.warning("セッション鍵が空のままです。一時鍵を使います: %s", path)
    return secrets.token_urlsafe(48)


def _log_security_state() -> None:
    """起動時に、危険な設定のまま動いていないか警告する。"""
    if not auth.auth_enabled():
        log.warning(
            "=" * 68
            + "\n  管理画面のログインが無効です (ADMIN_PASSWORD 未設定)。\n"
            "  この画面に到達できる人は、内線・トランクの設定変更、\n"
            "  留守番電話の再生、発信ルートの書き換えがすべて行えます。\n"
            "  .env に ADMIN_PASSWORD を設定して再起動してください。\n"
            + "=" * 68
        )
    if settings.debug:
        log.warning(
            "DEBUG=true で動作しています。例外時にスタックトレースが"
            "ブラウザへ返るため、運用時は false にしてください。"
        )
    if settings.host == "0.0.0.0" and not auth.auth_enabled():  # noqa: S104
        log.warning(
            "認証なしで全インターフェース (0.0.0.0) を待ち受けています。"
            "ネットワーク上の誰でも PBX を操作できる状態です。"
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("DB を初期化中…")
    await init_db()
    _log_security_state()
    log.info("起動完了 (DB=%s)", settings.database_url.split("@")[-1])
    yield
    # 終了時に DB 接続プールを明示的に閉じる。
    # これをしないと "Connection was deleted before being closed" の
    # 警告が出るほか、systemd で再起動を繰り返す運用で接続が残りやすい。
    log.info("シャットダウン中 (DB 接続を解放)")
    await engine.dispose()
    log.info("シャットダウン完了")


app = FastAPI(
    title="Asterisk PBX Web Management",
    version=APP_VERSION,
    lifespan=lifespan,
    debug=settings.debug,
)

# ---- ミドルウェア ----
# 注意: add_middleware は「後に追加したものが外側」になる。
# 認証チェックは request.session を読むため、SessionMiddleware より
# 内側 (= 先に追加) でなければならない。
app.middleware("http")(auth.auth_middleware)

app.add_middleware(
    SessionMiddleware,
    secret_key=_resolve_secret_key(),
    max_age=settings.session_max_age,
    same_site="lax",
    # same_site="lax" により、他サイトのページから本ツールへ POST された
    # 場合に Cookie が送られない = CSRF 対策になる。
    # https_only は既定 False。社内 LAN の http:// 運用を想定しているため。
    # TLS を前提にできる環境では True を推奨。
    https_only=False,
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """基本的なセキュリティヘッダーを付与する。"""

    async def dispatch(self, request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response


app.add_middleware(SecurityHeadersMiddleware)

# ---- Templates / Static ----
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["auth_enabled"] = auth.auth_enabled
templates.env.globals["admin_user"] = lambda: settings.admin_user


# Asterisk の CDR disposition を日本語ラベルに直す。
# ダッシュボードと発着信履歴の両方で同じ表記にするため、
# テンプレートごとに if を書かずここへ集約する。
_DISPOSITION_LABELS = {
    "ANSWERED": ("応答", "badge--ok"),
    "NO ANSWER": ("不応答", "badge"),
    "NOANSWER": ("不応答", "badge"),
    "BUSY": ("話中", "badge"),
    "FAILED": ("失敗", "badge--off"),
    "CONGESTION": ("混雑", "badge--off"),
}


def _disposition(value: str | None) -> dict[str, str]:
    if not value:
        return {"label": "-", "css": ""}
    label, css = _DISPOSITION_LABELS.get(
        value.upper(), (value, "badge--off")
    )
    return {"label": label, "css": css}


templates.env.globals["disposition"] = _disposition
# 静的ファイル (CSS/JS) のリンクに ?v=<version> を付けるためのグローバル
# 変数。app.css を更新してバージョンを上げるたびに URL 自体が変わるため、
# ブラウザ (特にモバイル Chrome/Safari は積極的にキャッシュしがち) が
# 古い CSS を握ったまま更新に気付かない、という問題を防げる。
templates.env.globals["app_version"] = APP_VERSION
app.state.templates = templates
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# ---- Routers ----
app.include_router(login.router)
app.include_router(dashboard.router)
app.include_router(extensions.router)
app.include_router(trunks.router)
app.include_router(outbound_routes.router)
app.include_router(inbound_routes.router)
app.include_router(audio.router)
app.include_router(moh_classes.router)
app.include_router(parking.router)
app.include_router(ivrs.router)
app.include_router(fax.router)
app.include_router(voicemail.router)
app.include_router(system.router)
app.include_router(ring_group.router)
app.include_router(queues.router)
app.include_router(time_conditions.router)
app.include_router(day_patterns.router)
app.include_router(calendar_exceptions.router)
app.include_router(blocked_numbers.router)
app.include_router(schedules.router)
app.include_router(holidays.router)
app.include_router(call_logs.router)
app.include_router(phone_book.router)


@app.exception_handler(PermissionError)
async def permission_error_handler(
    request: Request, exc: PermissionError
) -> HTMLResponse:
    """権限エラーを素の 500 ではなく、原因と対処が分かる画面で返す。

    本ツールは Asterisk の conf・音源・スプールを直接書き換えるため、
    実行ユーザーの権限不足はもっとも起こりやすい障害でありながら、
    画面には「Internal Server Error」しか出ず原因が分からなかった。
    個別の機能側でも捕まえているが、取りこぼしをここで受け止める。
    """
    from app.services.permissions import describe_permission_error

    target = getattr(exc, "filename", None) or "(パス不明)"
    detail = describe_permission_error(target, exc)
    log.warning("権限エラー: %s %s — %s", request.method, request.url.path, exc)
    return HTMLResponse(
        app.state.templates.get_template("error_permission.html").render(
            request=request, title="権限エラー", detail=detail,
        ),
        status_code=500,
    )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


def main() -> None:  # uvicorn 経由でなく `python -m app.main` でも起動できるように
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )


if __name__ == "__main__":
    main()
