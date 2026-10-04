"""発信ルート — 内線がダイヤルした番号をどのトランクに乗せるか。"""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class OutboundRoute(Base):
    __tablename__ = "outbound_routes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    name: Mapped[str] = mapped_column(String(64), unique=True)

    priority: Mapped[int] = mapped_column(Integer, default=100)
    """小さいほど優先。"""

    # --- マッチ条件 (ビルダー UI でパターンを生成) ---
    pattern: Mapped[str] = mapped_column(String(64))
    """Asterisk 拡張パターン (例: _0Z. / _0[1-9]XXXXXXXX)。"""

    prefix_strip: Mapped[int] = mapped_column(Integer, default=0)
    """先頭から削るプレフィクス桁数。"""

    prefix_prepend: Mapped[str | None] = mapped_column(String(16), nullable=True)
    """ダイヤル前に付ける番号 (例: 0)。"""

    # --- 出力 ---
    trunk_id: Mapped[int] = mapped_column(ForeignKey("trunks.id"))

    cid_override: Mapped[str | None] = mapped_column(String(80), nullable=True)
    """このルートで上書きする発信者番号。空ならトランクの値。"""

    # --- 制限 ---
    require_password: Mapped[bool] = mapped_column(Boolean, default=False)
    password: Mapped[str | None] = mapped_column(String(16), nullable=True)
    """国際電話など制限したいとき。"""

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
