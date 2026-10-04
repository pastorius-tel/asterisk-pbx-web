"""ログイン / ログアウト画面。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import auth
from app.config import settings

log = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])


def _safe_next(raw: str | None) -> str:
    """遷移先 URL を自サイト内に限定する (オープンリダイレクト対策)。

    '//evil.example.com' や 'https://evil.example.com' が来ても、
    ログイン後に外部サイトへ飛ばされないようにする。
    """
    value = (raw or "").strip()
    if not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/") -> HTMLResponse:  # noqa: A002
    if not auth.auth_enabled():
        return RedirectResponse("/", status_code=303)  # type: ignore[return-value]
    if request.session.get(auth.SESSION_KEY):
        return RedirectResponse(_safe_next(next), status_code=303)  # type: ignore[return-value]
    templates = request.app.state.templates
    resp = templates.TemplateResponse(
        request,
        "login.html",
        {"title": "ログイン", "next": _safe_next(next), "error": None},
    )
    return auth.apply_no_store(resp)  # type: ignore[return-value]


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),  # noqa: A002
) -> HTMLResponse:
    templates = request.app.state.templates
    target = _safe_next(next)
    ip = auth.client_ip(request)

    wait = auth.throttle.blocked_for(ip)
    if wait:
        resp = templates.TemplateResponse(
            request,
            "login.html",
            {
                "title": "ログイン",
                "next": target,
                "error": f"ログインの試行が続いたため一時的に受け付けません。約 {wait // 60 + 1} 分後にもう一度お試しください。",
            },
            status_code=429,
        )
        return auth.apply_no_store(resp)  # type: ignore[return-value]

    if auth.check_credentials(username, password):
        auth.throttle.record_success(ip)
        auth.login_user(request)
        log.info("ログイン成功: %s (%s)", settings.admin_user, ip)
        return RedirectResponse(target, status_code=303)  # type: ignore[return-value]

    auth.throttle.record_failure(ip)
    log.warning("ログイン失敗: user=%r from=%s", username, ip)
    resp = templates.TemplateResponse(
        request,
        "login.html",
        {
            "title": "ログイン",
            "next": target,
            "error": "ユーザー名またはパスワードが違います。",
        },
        status_code=401,
    )
    return auth.apply_no_store(resp)  # type: ignore[return-value]


@router.get("/logout")
@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    auth.logout_user(request)
    return RedirectResponse("/login", status_code=303)
