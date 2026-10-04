"""サンプルデータを投入するスクリプト。

使い方:
    python -m scripts.seed
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.database import AsyncSessionLocal, init_db
from app.models import Extension, OutboundRoute, Trunk

SAMPLE_EXTENSIONS = [
    {
        "extension": "1001",
        "display_name": "営業 1",
        "secret": "DemoPass1234abcd",
        "voicemail_enabled": True,
        "voicemail_pin": "1001",
    },
    {
        "extension": "1002",
        "display_name": "営業 2",
        "secret": "DemoPass5678efgh",
    },
    {
        "extension": "1003",
        "display_name": "受付",
        "secret": "DemoPass9012ijkl",
        "voicemail_enabled": True,
        "voicemail_pin": "1003",
    },
]

SAMPLE_TRUNK = {
    "name": "hikari_main",
    "trunk_type": "hikari_office_a_hgw",
    "host": "192.168.1.1",                  # HGW (PR-500MI 等) の LAN IP
    "port": 5060,
    "transport": "udp",
    "hgw_extension_number": "3",            # HGW 内線番号 (1〜2 桁)
    "username": "0003",                     # HGW のユーザID (4 桁)
    "secret": "ChangeMeOnHGW!",             # HGW のパスワード
    "outbound_caller_id": "0312345678",     # 主番号 (契約電話番号)
    "additional_did_numbers": "0312345679,0312345680",
    "codec_primary": "ulaw",
    "dtmf_mode": "rfc4733",
    "nat_mode": "no",                       # HGW と同 LAN
    "use_mac_auth": False,
    "disable_rport": False,
    "trust_id_inbound": False,              # HGW 経由では不要
    "send_pai": False,                      # HGW 経由では不要
    "max_channels": 8,
}

SAMPLE_OUTBOUND_ROUTES = [
    {
        "name": "国内発信 (0始まり)",
        "priority": 100,
        "pattern": "_0Z.",
    },
    {
        "name": "特番・緊急通報 (1始まり: 110/119/104/186 等)",
        "priority": 10,
        "pattern": "_1Z.",
    },
]


async def seed() -> None:
    await init_db()
    async with AsyncSessionLocal() as db:
        # Trunk
        t = (await db.scalars(select(Trunk).where(Trunk.name == SAMPLE_TRUNK["name"]))).first()
        if t is None:
            t = Trunk(**SAMPLE_TRUNK)
            db.add(t)
            await db.flush()
            print(f"[+] Trunk: {t.name}")

        # Extensions
        for ext_data in SAMPLE_EXTENSIONS:
            existing = (
                await db.scalars(
                    select(Extension).where(Extension.extension == ext_data["extension"])
                )
            ).first()
            if existing is None:
                db.add(Extension(**ext_data))
                print(f"[+] Extension: {ext_data['extension']}")

        # Outbound routes
        for r in SAMPLE_OUTBOUND_ROUTES:
            existing = (
                await db.scalars(select(OutboundRoute).where(OutboundRoute.name == r["name"]))
            ).first()
            if existing is None:
                db.add(OutboundRoute(trunk_id=t.id, **r))
                print(f"[+] Route: {r['name']}")

        await db.commit()
        print("done.")


if __name__ == "__main__":
    asyncio.run(seed())
