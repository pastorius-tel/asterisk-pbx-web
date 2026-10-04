"""時間条件 フォーム検証用スキーマ。"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

# TimeCondition の内側/外側の転送先として選べる種別。
# FAX 受信は時間条件の文脈にそぐわないため対象外。
_ALLOWED_DEST_TYPES = {"extension", "ring_group", "queue", "ivr", "voicemail", "hangup"}


class TimeConditionForm(BaseModel):
    """時間条件 追加/編集フォーム。"""

    name: str = Field(..., min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    """ダイヤルプランのコンテキスト名 (timecond-<name>) にそのまま使うため、
    英数字・アンダースコア・ハイフンのみ。"""

    mon_pattern_id: int | None = None
    tue_pattern_id: int | None = None
    wed_pattern_id: int | None = None
    thu_pattern_id: int | None = None
    fri_pattern_id: int | None = None
    sat_pattern_id: int | None = None
    sun_pattern_id: int | None = None

    use_calendar_exceptions: bool = True

    inside_type: str = Field(..., max_length=16)
    inside_value: str = Field(..., max_length=64)
    outside_type: str = Field(..., max_length=16)
    outside_value: str = Field(..., max_length=64)

    enabled: bool = True
    note: str | None = Field(default=None, max_length=255)

    @field_validator("inside_type", "outside_type")
    @classmethod
    def _validate_dest_type(cls, v: str) -> str:
        if v not in _ALLOWED_DEST_TYPES:
            raise ValueError(f"不正な転送先種別です: {v}")
        return v
