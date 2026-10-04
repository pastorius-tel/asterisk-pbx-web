"""時間条件 — 営業時間/時間外で着信先を分岐させる。

v0.3.42 で、単純な time_range/days_of_week 指定から、曜日ごとに
「パターン (DayPattern)」を割り当てる方式に刷新。加えて、全時間条件が
共通で参照する「カレンダー例外 (CalendarException)」— 祝日・盆休み・
年末年始など — が、曜日ごとの既定パターンより優先して適用される。
"""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.day_pattern import DayPattern


class TimeCondition(Base):
    __tablename__ = "time_conditions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    name: Mapped[str] = mapped_column(String(80), unique=True)

    # 曜日ごとの既定パターン (カレンダー例外に該当しない日はこちらを使う)。
    # 未設定 (None) の曜日は「終日 OFF」相当として扱う。
    mon_pattern_id: Mapped[int | None] = mapped_column(ForeignKey("day_patterns.id"), nullable=True)
    tue_pattern_id: Mapped[int | None] = mapped_column(ForeignKey("day_patterns.id"), nullable=True)
    wed_pattern_id: Mapped[int | None] = mapped_column(ForeignKey("day_patterns.id"), nullable=True)
    thu_pattern_id: Mapped[int | None] = mapped_column(ForeignKey("day_patterns.id"), nullable=True)
    fri_pattern_id: Mapped[int | None] = mapped_column(ForeignKey("day_patterns.id"), nullable=True)
    sat_pattern_id: Mapped[int | None] = mapped_column(ForeignKey("day_patterns.id"), nullable=True)
    sun_pattern_id: Mapped[int | None] = mapped_column(ForeignKey("day_patterns.id"), nullable=True)

    use_calendar_exceptions: Mapped[bool] = mapped_column(Boolean, default=True)
    """True なら、共通のカレンダー例外 (CalendarException) を考慮する。
    v0.3.64 で「祝日」「指定休日」を独立した項目に分けたため、こちらは
    それ以前から使っている設定を壊さないために残している。"""

    # --- 祝日 (内閣府データ) の扱い ---
    use_national_holidays: Mapped[bool] = mapped_column(Boolean, default=False)
    """True なら、国民の祝日 (NationalHoliday) を曜日設定より優先する。"""

    national_holiday_pattern_id: Mapped[int | None] = mapped_column(
        ForeignKey("day_patterns.id"), nullable=True
    )
    """祝日に適用する日次パターン。未設定なら「終日休業」扱い。
    午前だけ営業する等の場合はここにパターンを指定する。"""

    # --- 会社指定休日 (年末年始・お盆等) の扱い ---
    use_company_holidays: Mapped[bool] = mapped_column(Boolean, default=False)
    """True なら、会社指定休日 (CompanyHoliday) を曜日設定より優先する。"""

    company_holiday_pattern_id: Mapped[int | None] = mapped_column(
        ForeignKey("day_patterns.id"), nullable=True
    )
    """指定休日に適用する日次パターン (指定休日側で個別に指定されて
    いればそちらが優先)。未設定なら「終日休業」扱い。"""

    # 営業時間内/外の転送先
    inside_type: Mapped[str] = mapped_column(String(16))
    inside_value: Mapped[str] = mapped_column(String(64))
    outside_type: Mapped[str] = mapped_column(String(16))
    outside_value: Mapped[str] = mapped_column(String(64))

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    mon_pattern: Mapped[DayPattern | None] = relationship(foreign_keys=[mon_pattern_id], lazy="selectin")
    tue_pattern: Mapped[DayPattern | None] = relationship(foreign_keys=[tue_pattern_id], lazy="selectin")
    wed_pattern: Mapped[DayPattern | None] = relationship(foreign_keys=[wed_pattern_id], lazy="selectin")
    thu_pattern: Mapped[DayPattern | None] = relationship(foreign_keys=[thu_pattern_id], lazy="selectin")
    fri_pattern: Mapped[DayPattern | None] = relationship(foreign_keys=[fri_pattern_id], lazy="selectin")
    sat_pattern: Mapped[DayPattern | None] = relationship(foreign_keys=[sat_pattern_id], lazy="selectin")
    sun_pattern: Mapped[DayPattern | None] = relationship(foreign_keys=[sun_pattern_id], lazy="selectin")
    national_holiday_pattern: Mapped[DayPattern | None] = relationship(
        foreign_keys=[national_holiday_pattern_id], lazy="selectin"
    )
    company_holiday_pattern: Mapped[DayPattern | None] = relationship(
        foreign_keys=[company_holiday_pattern_id], lazy="selectin"
    )

    WEEKDAY_FIELDS = (
        ("mon", "月"), ("tue", "火"), ("wed", "水"), ("thu", "木"),
        ("fri", "金"), ("sat", "土"), ("sun", "日"),
    )
