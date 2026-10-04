"""トランクプリセット定義。

UI のフォーム上で「プリセット選択」→ 各フィールドが既定値で埋まる。
HGW (PR-500/600 系) と OG (OG410Xa/420Xa/820Xa 系) は別物として扱う。
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Hikari Denwa Office A — HGW 経由 (PR-500MI / PR-600 系)
# ---------------------------------------------------------------------------
# 想定構成:
#   [Asterisk] ── LAN ── [HGW: PR-500/600 系] ── 光回線 ── [NTT 網]
#
# HGW 側の設定:
#   - 「電話設定」→「内線設定」で IP 端末を追加
#   - ダイジェスト認証 = 行う、ユーザID (例: 0003) とパスワードを設定
#   - その内線に契約電話番号 (主番号 + 追加番号) を割り当て
#
# このフォームに転記する値:
#   - host                 = HGW の LAN 側 IP (例: 192.168.1.1)
#   - hgw_extension_number = HGW で設定した内線番号 (1〜2桁、例: "3")
#   - username             = HGW で表示されるユーザID (4桁、例: "0003")
#   - secret               = HGW で設定したパスワード
#   - outbound_caller_id   = 契約電話番号 (発信表示用、例: "0312345678")
#   - additional_did_numbers = 追加番号をカンマ区切り

HIKARI_OFFICE_A_HGW: dict[str, Any] = {
    "trunk_type": "hikari_office_a_hgw",
    "host": "192.168.1.1",          # HGW の LAN 側 IP (要確認)
    "port": 5060,
    "transport": "udp",
    "codec_primary": "ulaw",        # G.711 μ-law 必須
    "codec_secondary": None,
    "dtmf_mode": "rfc4733",
    "nat_mode": "no",               # 同一 LAN 想定 (HGW 経由は NAT 越えなし)
    "qualify": True,
    "use_mac_auth": False,
    "disable_rport": False,         # HGW 経由なら通常不要
    "trust_id_inbound": False,      # HGW 経由なら不要 (HGW が CID を処理)
    "send_pai": False,              # HGW 経由なら不要 (HGW が発番制御)
    "max_channels": 8,              # オフィスA 既定 (契約により可変)
}

# ---------------------------------------------------------------------------
# Hikari Denwa Office A — OG 経由 (OG410Xa / OG420Xa / OG820Xa)
# ---------------------------------------------------------------------------
# 想定構成:
#   [Asterisk] ── LAN ── [OG410Xa/420Xa/820Xa] ── 光回線 ── [NTT 網]
#                                  └─ ビジネスホン (アナログ/ISDN)
#
# OG 側の設定:
#   - 「電話設定」→「IP 端末/GW 収容設定」で Asterisk サーバの MAC を登録
#   - 内線番号は通常 2 桁 (10〜99)
#   - 「ダイジェスト認証」を「行う」にしてユーザID/パスワード認証 (推奨)
#     ※ MAC 認証だけで動作する OG もある (use_mac_auth=True)
#
# OG 経由は HGW 経由と比べ:
#   - 内線番号が 2 桁 (例: "10")
#   - DHCP で経路情報が降ってこないケースあり → デフォルトゲートウェイを OG に
#   - disable_rport が必要なケースもある (発信時 400 Bad Request 対策)

HIKARI_OFFICE_A_OG: dict[str, Any] = {
    "trunk_type": "hikari_office_a_og",
    "host": "192.168.1.1",          # OG の LAN 側 IP (要確認)
    "port": 5060,
    "transport": "udp",
    "codec_primary": "ulaw",
    "codec_secondary": None,
    "dtmf_mode": "rfc4733",
    "nat_mode": "no",
    "qualify": True,
    "use_mac_auth": False,          # 通常はパスワード認証推奨
    "disable_rport": False,         # 発信できないトラブル時に True へ
    "trust_id_inbound": False,
    "send_pai": False,
    "max_channels": 8,
}


REGISTER_GENERIC: dict[str, Any] = {
    "trunk_type": "register",
    "port": 5060,
    "transport": "udp",
    "codec_primary": "ulaw",
    "dtmf_mode": "rfc4733",
    "nat_mode": "force_rport,comedia",
    "qualify": True,
    "max_channels": 10,
}


PEER_GENERIC: dict[str, Any] = {
    "trunk_type": "peer",
    "port": 5060,
    "transport": "udp",
    "codec_primary": "ulaw",
    "dtmf_mode": "rfc4733",
    "nat_mode": "no",
    "qualify": True,
    "max_channels": 10,
}


PRESETS: dict[str, dict[str, Any]] = {
    "hikari_office_a_hgw": HIKARI_OFFICE_A_HGW,
    "hikari_office_a_og": HIKARI_OFFICE_A_OG,
    "register_generic": REGISTER_GENERIC,
    "peer_generic": PEER_GENERIC,
}


PRESET_CHOICES: list[dict[str, str]] = [
    {"value": "", "label": "— プリセットなし —"},
    {"value": "hikari_office_a_hgw", "label": "ひかり電話オフィスA / HGW 経由 (PR-500/600) ※未検証"},
    {"value": "hikari_office_a_og", "label": "ひかり電話オフィスA / OG 経由 (OG410/420/820Xa) ※未検証"},
    {"value": "register_generic", "label": "汎用 ITSP (REGISTER)"},
    {"value": "peer_generic", "label": "汎用 IP-PEER"},
]
