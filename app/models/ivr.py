"""IVR (音声自動応答) メニュー。

着信ルート → IVR を選ぶと、ガイダンス音声を流して DTMF キー入力を受ける。
各キー (0〜9, *, #) ごとに転送先を指定。
"""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Ivr(Base):
    __tablename__ = "ivrs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    name: Mapped[str] = mapped_column(String(64), unique=True)
    """ダイヤルプランのコンテキスト名 'ivr-<name>' になる。"""

    display_name: Mapped[str] = mapped_column(String(80))

    greeting_audio_id: Mapped[int | None] = mapped_column(
        ForeignKey("audio_files.id"), nullable=True
    )
    """挨拶ガイダンス音源 (IVR カテゴリ推奨)。"""

    invalid_audio_id: Mapped[int | None] = mapped_column(
        ForeignKey("audio_files.id"), nullable=True
    )
    """無効キー時の案内 (任意)。"""

    timeout_audio_id: Mapped[int | None] = mapped_column(
        ForeignKey("audio_files.id"), nullable=True
    )
    """無入力タイムアウト時の案内 (任意)。"""

    timeout_seconds: Mapped[int] = mapped_column(Integer, default=5)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)

    # 無効キー/タイムアウト時の最終動作 (max_retries 超過後)
    fallback_action: Mapped[str] = mapped_column(String(16), default="hangup")
    fallback_value: Mapped[str | None] = mapped_column(String(64), nullable=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    entries: Mapped[list[IvrEntry]] = relationship(
        back_populates="ivr",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="IvrEntry.key_input",
    )


class IvrEntry(Base):
    __tablename__ = "ivr_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    ivr_id: Mapped[int] = mapped_column(ForeignKey("ivrs.id"))

    key_input: Mapped[str] = mapped_column(String(4))
    """0-9, *, #, または '_X.' (任意の番号入力)。"""

    action: Mapped[str] = mapped_column(String(16))
    """extension / ring_group / queue / ivr / voicemail / hangup / playback_hangup"""

    action_value: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """転送先の番号や ID。"""

    label: Mapped[str | None] = mapped_column(String(80), nullable=True)
    """表示用ラベル (例: '1: 営業時間案内へ')。"""

    ivr: Mapped[Ivr] = relationship(back_populates="entries")
