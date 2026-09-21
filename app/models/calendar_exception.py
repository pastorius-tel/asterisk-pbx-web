"""カレンダー例外 — 特定の日付 (範囲) に、通常の曜日設定を上書きして
特定のパターンを適用する。祝日・盆休み・年末年始など、全ての時間条件で
共通して考慮される「会社の休日カレンダー」を表現する。
"""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.day_pattern import DayPattern


class CalendarException(Base):
    __tablename__ = "calendar_exceptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    name: Mapped[str] = mapped_column(String(80))
    """表示名 (例: 「元日」「盆休み」「年末年始」)。"""

    start_month: Mapped[int] = mapped_column(Integer)
    start_day: Mapped[int] = mapped_column(Integer)
    end_month: Mapped[int] = mapped_column(Integer)
    end_day: Mapped[int] = mapped_column(Integer)
    """開始/終了の月日 (年を含まない)。開始 > 終了の場合、年をまたぐ
    範囲として扱う (例: 12/29 〜 1/3)。"""

    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """None なら毎年繰り返し (盆休み・年末年始など、ほぼ同じ日付で
    毎年発生するもの向け)。整数を指定すると、その年のみに限定される
    (成人の日など年によって日付が動く祝日向け)。"""

    pattern_id: Mapped[int] = mapped_column(ForeignKey("day_patterns.id"))
    """この期間に適用するパターン。通常は「終日 OFF」パターンを使う想定
    だが、半日休業日などにも対応できるよう任意のパターンを指定可能。"""

    priority: Mapped[int] = mapped_column(Integer, default=100)
    """複数の例外期間が重なった場合、値が小さいほうを優先する。"""

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    pattern: Mapped[DayPattern] = relationship(lazy="selectin")
