"""日次パターン フォーム検証用スキーマ。"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.validators import normalize_time_ranges, validate_time_ranges


class DayPatternForm(BaseModel):
    """日次パターン 追加/編集フォーム。"""

    name: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    display_name: str = Field(..., min_length=1, max_length=80)
    on_ranges: str = Field(default="", max_length=255)
    """HH:MM-HH:MM をカンマ区切りで複数指定。空文字列なら終日 OFF。"""
    enabled: bool = True
    note: str | None = Field(default=None, max_length=255)

    @field_validator("on_ranges")
    @classmethod
    def _validate_on_ranges(cls, v: str) -> str:
        # スケジュール設定画面 (routers/schedules.py) と同じ検証を使う。
        # 以前はこちらだけ書式チェックのみで、25:99-99:99 のような
        # 存在しない時刻が保存できてしまっていた。
        err = validate_time_ranges(v)
        if err:
            raise ValueError(err)
        return normalize_time_ranges(v)
