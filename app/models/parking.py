"""パーク (コールパーク) 設定。

Asterisk の res_parking (旧 features.conf [parkinglot_*] / parking.conf) を生成。
- 番号レンジ (例: 701-720)
- 取り出し時間
- 保留中の MoH クラス
- タイムアウト時の戻し先
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ParkingLot(Base):
    __tablename__ = "parking_lots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    name: Mapped[str] = mapped_column(String(64), unique=True)
    """parking.conf のセクション名 (英数アンダースコア)。例: 'default'。"""

    park_ext: Mapped[str] = mapped_column(String(16), default="700")
    """パーク用の代表内線 (押すとパーク開始)。"""

    park_pos_start: Mapped[int] = mapped_column(Integer, default=701)
    park_pos_end: Mapped[int] = mapped_column(Integer, default=720)
    """取り出し時にダイヤルする番号レンジ (701-720)。"""

    park_time_seconds: Mapped[int] = mapped_column(Integer, default=120)
    """この秒数で取り出されなかったらタイムアウト動作。"""

    moh_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """保留中に流す MoH クラス名。None で 'default'。"""

    comeback_context: Mapped[str] = mapped_column(String(64), default="from-internal")
    """タイムアウト時の戻し先コンテキスト。"""

    findslot: Mapped[str] = mapped_column(String(16), default="first")
    """空きスロット検索方式: first / next。"""

    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    """アプリで 1 つだけ default にする。"""

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
