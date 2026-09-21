"""カレンダー例外 フォーム検証用スキーマ。"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class CalendarExceptionForm(BaseModel):
    """カレンダー例外 追加/編集フォーム。

    Web フォーム側では開始日/終了日を HTML5 の <input type="date">
    (実際のカレンダー UI) で入力してもらい、"YYYY-MM-DD" 文字列として
    受け取る。「毎年繰り返す」が有効なら年を切り捨てて月日だけを保存する。
    """

    name: str = Field(..., min_length=1, max_length=80)
    start_date: str = Field(..., description="YYYY-MM-DD")
    end_date: str = Field(..., description="YYYY-MM-DD")
    recurring: bool = True
    """True: 毎年繰り返す (年を無視)。False: 指定した年のみ。"""
    pattern_id: int
    priority: int = Field(default=100, ge=0, le=1000)
    enabled: bool = True
    note: str | None = Field(default=None, max_length=255)

    start_month: int = Field(default=0, ge=0, le=12)
    start_day: int = Field(default=0, ge=0, le=31)
    end_month: int = Field(default=0, ge=0, le=12)
    end_day: int = Field(default=0, ge=0, le=31)
    year: int | None = None

    @model_validator(mode="after")
    def _derive_month_day_year(self) -> CalendarExceptionForm:
        try:
            sy, sm, sd = (int(x) for x in self.start_date.split("-"))
            ey, em, ed = (int(x) for x in self.end_date.split("-"))
        except (ValueError, AttributeError) as e:
            raise ValueError("日付の形式が正しくありません。") from e
        self.start_month, self.start_day = sm, sd
        self.end_month, self.end_day = em, ed
        self.year = None if self.recurring else sy
        return self
