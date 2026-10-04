"""国民の祝日 — 内閣府が公開している祝日データ。

内閣府「国民の祝日について」のページで公開されている CSV
(https://www8.cao.go.jp/chosei/shukujitsu/syukujitsu.csv) を取り込む。
このデータはデジタル庁のデータカタログサイトで CC-BY として公開されて
いるオープンデータ。

会社独自の休業日 (年末年始・お盆・GW 等) は CompanyHoliday で別途
管理する。祝日は毎年日付が変わるもの (成人の日・春分の日など) がある
ため、年ごとの実日付をそのまま保持する。
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import Date, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class NationalHoliday(Base):
    __tablename__ = "national_holidays"
    __table_args__ = (
        UniqueConstraint("holiday_date", name="uq_national_holiday_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    holiday_date: Mapped[date] = mapped_column(Date, index=True)
    """祝日の日付 (実日付)。"""

    name: Mapped[str] = mapped_column(String(64))
    """祝日の名称 (例: 「敬老の日」「元日」)。振替休日は「休日」となる。"""

    year: Mapped[int] = mapped_column(Integer, index=True)
    """holiday_date の年。年ごとの一覧表示・削除に使う。"""
