"""会社指定休日 — 年末年始・お盆・ゴールデンウィーク等、会社が独自に
定める休業日。

国民の祝日 (NationalHoliday) とは別項目として管理する。祝日は内閣府の
データから自動取得できるが、こちらは会社ごとに異なるためカレンダーから
手動で登録する。

期間 (開始日〜終了日) で登録でき、「毎年繰り返す」を選べば年末年始・
お盆のように毎年ほぼ同じ日付で発生する休みを一度の登録で済ませられる。
"""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.day_pattern import DayPattern


class CompanyHoliday(Base):
    __tablename__ = "company_holidays"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    name: Mapped[str] = mapped_column(String(80))
    """表示名 (例: 「年末年始」「夏季休業」「創立記念日」)。"""

    start_month: Mapped[int] = mapped_column(Integer)
    start_day: Mapped[int] = mapped_column(Integer)
    end_month: Mapped[int] = mapped_column(Integer)
    end_day: Mapped[int] = mapped_column(Integer)
    """開始/終了の月日。開始 > 終了なら年をまたぐ期間 (例: 12/29〜1/3)。"""

    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """None なら毎年繰り返し。整数ならその年のみ。"""

    pattern_id: Mapped[int | None] = mapped_column(
        ForeignKey("day_patterns.id"), nullable=True
    )
    """この期間に適用する日次パターン。未設定なら「終日休業」として扱う
       (時間条件側で「指定休日の扱い」に指定したパターンを使う)。"""

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    pattern: Mapped[DayPattern | None] = relationship(lazy="selectin")
