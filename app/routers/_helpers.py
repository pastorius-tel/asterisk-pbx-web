"""ルーター共通のヘルパー。

主にテンプレートに「選択肢」を一括で渡すためのユーティリティ。
ここを通すことで、UI が常に enum と同期する。
"""

from __future__ import annotations

from app.models.base import (
    AudioCategory,
    Codec,
    DirectMedia,
    DtmfMode,
    IvrAction,
    MohSortMode,
    NatMode,
    QueueStrategy,
    RingStrategy,
    RouteAction,
    Transport,
    TrunkType,
)


def enum_choices(enum_cls) -> list[dict[str, str]]:  # type: ignore[no-untyped-def]
    """Enum を <option> 用の [{value, label}, ...] に変換。"""
    return [{"value": e.value, "label": _label(enum_cls, e)} for e in enum_cls]


# 日本語ラベル
_LABELS: dict[type, dict[str, str]] = {
    Codec: {
        "ulaw": "G.711 μ-law (国内標準)",
        "alaw": "G.711 A-law (欧州)",
        "g722": "G.722 (HD voice)",
        "g729": "G.729 (低帯域)",
        "opus": "Opus",
        "gsm": "GSM",
    },
    Transport: {
        "udp": "UDP",
        "tcp": "TCP",
        "tls": "TLS (SIPS)",
        "ws": "WebSocket",
        "wss": "WebSocket Secure",
    },
    DtmfMode: {
        "rfc4733": "RFC4733 (推奨)",
        "inband": "インバンド",
        "info": "SIP INFO",
        "auto": "自動判定",
    },
    NatMode: {
        "no": "NAT なし",
        "force_rport": "force_rport のみ",
        "comedia": "comedia のみ",
        "force_rport,comedia": "force_rport + comedia (推奨)",
        "auto_force_rport": "auto_force_rport",
        "auto_comedia": "auto_comedia",
    },
    DirectMedia: {
        "yes": "有効",
        "no": "無効 (推奨)",
        "nonat": "NAT 越えがない時のみ",
        "update": "UPDATE で",
    },
    TrunkType: {
        "register": "REGISTER 認証",
        "peer": "IP-PEER",
        "iax2": "IAX2",
        "hikari_office_a_hgw": "ひかり電話オフィスA / HGW 経由 (未検証)",
        "hikari_office_a_og": "ひかり電話オフィスA / OG 経由 (未検証)",
    },
    RouteAction: {
        "extension": "内線",
        "ring_group": "リンググループ",
        "queue": "キュー",
        "ivr": "IVR",
        "voicemail": "ボイスメール",
        "fax": "FAX 受信",
        "hangup": "切断",
        "time_condition": "時間条件",
    },
    RingStrategy: {
        "ringall": "全員同時",
        "hunt": "順番に",
        "memoryhunt": "続きから順番に",
        "random": "ランダム",
        "leastrecent": "直近応答が古い順",
        "fewestcalls": "応対数が少ない順",
    },
    QueueStrategy: {
        "ringall": "全員同時",
        "leastrecent": "直近応答が古い順",
        "fewestcalls": "応対数が少ない順",
        "random": "ランダム",
        "rrmemory": "順番に (続きから記憶)",
        "linear": "常に先頭から順番に",
        "wrandom": "重み付きランダム",
    },
    AudioCategory: {
        "moh": "保留音 (MoH)",
        "park": "パーク呼出音",
        "ivr": "IVR ガイダンス",
        "voicemail": "ボイスメール挨拶",
        "custom": "汎用 (Playback)",
    },
    MohSortMode: {
        "alpha": "アルファベット順",
        "random": "ランダム",
        "randstart": "ランダム開始 → 順番に",
        "listfiles": "指定順",
    },
    IvrAction: {
        "extension": "内線",
        "ring_group": "リンググループ",
        "queue": "キュー",
        "ivr": "別の IVR へ",
        "voicemail": "ボイスメール",
        "hangup": "切断",
        "playback_hangup": "案内のみ再生して切断",
    },
}


def _label(enum_cls, member) -> str:  # type: ignore[no-untyped-def]
    return _LABELS.get(enum_cls, {}).get(member.value, member.value)


def label_for(enum_cls, value: str | None) -> str:  # type: ignore[no-untyped-def]
    """値だけ渡してラベルを引く (テンプレート用)。未登録/None は値そのまま返す。"""
    if value is None:
        return ""
    return _LABELS.get(enum_cls, {}).get(value, value)


def all_enum_choices() -> dict[str, list[dict[str, str]]]:
    """テンプレートに渡す共通 context。"""
    return {
        "codecs": enum_choices(Codec),
        "transports": enum_choices(Transport),
        "dtmf_modes": enum_choices(DtmfMode),
        "nat_modes": enum_choices(NatMode),
        "direct_media": enum_choices(DirectMedia),
        "trunk_types": enum_choices(TrunkType),
        "route_actions": enum_choices(RouteAction),
        "ring_strategies": enum_choices(RingStrategy),
        "queue_strategies": enum_choices(QueueStrategy),
        "audio_categories": enum_choices(AudioCategory),
        "moh_sort_modes": enum_choices(MohSortMode),
        "ivr_actions": enum_choices(IvrAction),
    }


# ---------------------------------------------------------------------------
# フォーム値の変換ユーティリティ
# ---------------------------------------------------------------------------

# SQLite の INTEGER は 64bit まで。これを超える値を渡すと
# OverflowError で 500 になるため、変換の時点で弾く。
_SQLITE_INT_MAX = 2**63 - 1


def form_int(value, default: int | None = None) -> int | None:  # type: ignore[no-untyped-def]
    """フォームの文字列を int に変換する。変換できなければ default。

    ブラウザのフォームからは必ず数字が来る想定でも、URL を直接叩けば
    任意の文字列が届く。int() をそのまま呼ぶと ValueError で
    InternalServerError になるため、ここを必ず通す。
    64bit を超える巨大な数も OverflowError になるので弾く。
    """
    if value is None:
        return default
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    if abs(n) > _SQLITE_INT_MAX:
        return default
    return n
