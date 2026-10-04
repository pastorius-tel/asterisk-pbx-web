"""日次パターン — 1日の中で「営業時間内 (ON)」となる時間帯のテンプレート。

時間条件 (TimeCondition) の曜日ごとの割り当てや、カレンダー例外
(CalendarException) の適用パターンとして再利用される。
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class DayPattern(Base):
    __tablename__ = "day_patterns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    name: Mapped[str] = mapped_column(String(64), unique=True)
    """内部識別名 (英数字)。"""

    display_name: Mapped[str] = mapped_column(String(80))

    on_ranges: Mapped[str] = mapped_column(String(255), default="09:00-18:00")
    """ON (営業時間) となる時間帯。HH:MM-HH:MM をカンマ区切りで複数指定可
    (例: "09:00-12:00,13:00-18:00")。空文字列なら終日 OFF 扱い
    (祝日・休業日などの「全休」パターンに使う)。"""

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
