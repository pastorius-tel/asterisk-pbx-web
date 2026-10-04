"""リンググループ — 複数の内線を同時/順番に呼び出すグループ。"""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class RingGroup(Base):
    __tablename__ = "ring_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    group_number: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    """グループの仮想内線番号 (例: 600)。"""

    name: Mapped[str] = mapped_column(String(80))
    strategy: Mapped[str] = mapped_column(String(16), default="ringall")
    ring_seconds: Mapped[int] = mapped_column(Integer, default=20)

    # 鳴らない場合の転送先 (Inbound と同じ仕組み)
    fallback_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    fallback_value: Mapped[str | None] = mapped_column(String(64), nullable=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    members: Mapped[list[RingGroupMember]] = relationship(
        back_populates="group",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class RingGroupMember(Base):
    __tablename__ = "ring_group_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("ring_groups.id"))
    extension: Mapped[str] = mapped_column(String(16))
    """所属内線番号 (Extension テーブルの extension と一致)。"""
    order_index: Mapped[int] = mapped_column(Integer, default=0)

    group: Mapped[RingGroup] = relationship(back_populates="members")
