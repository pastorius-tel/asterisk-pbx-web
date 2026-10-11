"""ブラウザ経由の攻撃 (DNS リバインディング / クロスサイト) を防ぐガード。

本ツールは社内 LAN での利用を前提にしているが、「LAN 内だから安全」とは
言い切れない。利用者の PC は同時に外部のサイトも開いており、そのサイトの
JavaScript が、利用者のブラウザを"踏み台"にして LAN 内の本ツールを
操作しようとする攻撃が実在する。ここではその 2 種類を塞ぐ。

1. DNS リバインディング (Host ヘッダ検査)
   攻撃者のドメインの名前解決を途中から本ツールの LAN IP に付け替えると、
   ブラウザからは "同一オリジン" に見えてしまい、外部サイトの JS が
   本ツールの応答を読めるようになる。これを防ぐには、本ツールが受け取る
   Host ヘッダを、正規のアクセス方法 (IP 直打ち / localhost / 明示的に
   許可したホスト名) に限定すればよい。攻撃はドメイン名でアクセスさせる
   必要があるため、素の IP とループバックだけ通せば成立しなくなる。

2. クロスサイトからの書き込み (CSRF: Origin / Sec-Fetch-Site 検査)
   セッション Cookie は SameSite=Lax にしてあり、他サイトからの POST
   では送られない (一次的な CSRF 対策)。それに加えて、状態を変える
   リクエスト (POST 等) では Origin / Sec-Fetch-Site ヘッダを確認し、
   別オリジン由来のものを拒否する (多層防御)。認証を無効にして運用して
   いる場合 (Cookie が無い場合) はこちらが主たる防御になる。

いずれも Asterisk からの内部フック (GET + トークン認証) には影響しない。
"""

from __future__ import annotations

import ipaddress
import logging

from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.config import settings

log = logging.getLogger(__name__)

# 状態を変えうる HTTP メソッド (これらだけ Origin を厳しく見る)
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _host_only(value: str) -> str:
    """"host:port" から host 部分だけを取り出す (IPv6 の [..]:port も考慮)。"""
    value = (value or "").strip()
    if not value:
        return ""
    if value.startswith("["):  # [::1]:8080 のような IPv6
        return value[1:].split("]", 1)[0]
    # ホスト名/ IPv4 は最後の ":" でポートを切る (IPv6 素のままは ":" 多数)
    if value.count(":") == 1:
        return value.split(":", 1)[0]
    return value


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _allowed_host_names() -> set[str]:
    """設定で明示的に許可されたホスト名の集合 (小文字)。"""
    raw = (settings.allowed_hosts or "").strip()
    names = {h.strip().lower() for h in raw.split(",") if h.strip()}
    # 待ち受けアドレスがホスト名なら、それも許可する
    h = (settings.host or "").strip().lower()
    if h and h not in ("0.0.0.0", "::"):  # noqa: S104
        names.add(h)
    return names


def _host_allowed(host: str) -> bool:
    if not host:
        # Host ヘッダ無し (HTTP/1.0 等)。ローカル監視などで来る場合がある。
        # 書き込みは別途 Origin で見るので、ここでは通す。
        return True
    host = host.lower()
    if host in ("localhost",) or host.endswith(".localhost"):
        return True
    if _is_ip_literal(host):
        return True
    return host in _allowed_host_names()


def _host_port(netloc: str, scheme: str = "") -> tuple[str, str]:
    """"host:port" を (host, port) に分解する。ポート省略時はスキーム既定。"""
    netloc = (netloc or "").strip()
    host = _host_only(netloc).lower()
    port = ""
    if netloc.startswith("["):  # [IPv6]:port
        rest = netloc.split("]", 1)[1] if "]" in netloc else ""
        if rest.startswith(":"):
            port = rest[1:]
    elif netloc.count(":") == 1:
        port = netloc.split(":", 1)[1]
    if not port:
        port = {"https": "443", "http": "80"}.get(scheme.lower(), "")
    return host, port


def _origin_matches(request: Request, origin: str) -> bool:
    """Origin がリクエストのホスト **かつポート** と一致するか。

    ホストだけで判定すると、同じ IP の別ポートで動く別サービス
    (例: Asterisk 自身の HTTP/ARI :8088) からの操作を通してしまう。
    ブラウザの Cookie は "サイト" 単位 (ポートを区別しない) で送られるため、
    同一 IP の別ポートからの書き込みに Cookie が付いてしまう。これを防ぐ
    ため、ポートまで含めて厳密に突き合わせる。スキームはリバースプロキシ
    構成でずれ得るので比較に含めない。"""
    # origin 例: "http://192.168.1.50:8080"
    if "://" not in origin:
        return False
    scheme, after = origin.split("://", 1)
    o_host, o_port = _host_port(after, scheme)
    r_host, r_port = _host_port(
        request.headers.get("host", ""), request.url.scheme
    )
    return bool(o_host) and o_host == r_host and o_port == r_port


async def web_guard_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    host = _host_only(request.headers.get("host", ""))

    # --- (1) DNS リバインディング: Host ヘッダ検査 ---
    if not _host_allowed(host):
        log.warning("許可されていない Host ヘッダを拒否しました: %r", host)
        return PlainTextResponse(
            "このホスト名ではアクセスできません。サーバーの IP アドレスで"
            "開くか、.env の ALLOWED_HOSTS にホスト名を追加してください。",
            status_code=400,
        )

    # --- (2) CSRF: 状態変更リクエストの Origin / Sec-Fetch-Site 検査 ---
    if request.method in _UNSAFE_METHODS:
        origin = request.headers.get("origin", "")
        sec_site = request.headers.get("sec-fetch-site", "")
        # Sec-Fetch-Site: same-origin / none のみ許可。
        # same-site は「同じサイトの別オリジン」(別ポート等) を含み、
        # 同一 IP の別ポートで動く別サービスからの書き込みを通して
        # しまうため許可しない。none = アドレスバー直打ち等のユーザー起点。
        if sec_site and sec_site not in ("same-origin", "none"):
            log.warning(
                "クロスサイトからの書き込みを拒否: %s %s (Sec-Fetch-Site=%s)",
                request.method, request.url.path, sec_site,
            )
            return _deny(request)
        # Origin があるなら Host と一致していること。
        if origin and origin.lower() != "null" and not _origin_matches(request, origin):
            log.warning(
                "別オリジンからの書き込みを拒否: %s %s (Origin=%s)",
                request.method, request.url.path, origin,
            )
            return _deny(request)

    return await call_next(request)


def _deny(request: Request):  # type: ignore[no-untyped-def]
    if request.headers.get("hx-request") or request.headers.get(
        "accept", ""
    ).startswith("application/json"):
        return JSONResponse(
            {"ok": False, "reason": "クロスサイトからの操作は許可されていません"},
            status_code=403,
        )
    return PlainTextResponse(
        "クロスサイトからの操作は許可されていません。", status_code=403
    )
