"""コールキュー — オペレーター/エージェントによる順次応答。"""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Queue(Base):
    __tablename__ = "queues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    queue_number: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(80))

    strategy: Mapped[str] = mapped_column(String(16), default="ringall")
    timeout: Mapped[int] = mapped_column(Integer, default=15)
    """1 メンバー当たりの呼出秒数。"""

    retry: Mapped[int] = mapped_column(Integer, default=5)
    wrapuptime: Mapped[int] = mapped_column(Integer, default=0)
    maxlen: Mapped[int] = mapped_column(Integer, default=0)
    """0 で無制限。"""

    music_class: Mapped[str] = mapped_column(String(64), default="default")

    fallback_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    fallback_value: Mapped[str | None] = mapped_column(String(64), nullable=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    members: Mapped[list[QueueMember]] = relationship(
        back_populates="queue",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class QueueMember(Base):
    __tablename__ = "queue_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    queue_id: Mapped[int] = mapped_column(ForeignKey("queues.id"))
    extension: Mapped[str] = mapped_column(String(16))
    penalty: Mapped[int] = mapped_column(Integer, default=0)
    """値が小さいほど優先的に呼び出される。"""
    paused: Mapped[bool] = mapped_column(Boolean, default=False)

    queue: Mapped[Queue] = relationship(back_populates="members")
