"""リンググループ フォーム検証用スキーマ。"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.models.base import RingStrategy


class RingGroupForm(BaseModel):
    """リンググループ追加/編集フォーム。"""

    group_number: str = Field(..., min_length=2, max_length=16, pattern=r"^\d+$")
    name: str = Field(..., min_length=1, max_length=80)
    strategy: RingStrategy = RingStrategy.RINGALL
    ring_seconds: int = Field(default=20, ge=5, le=120)

    fallback_type: str | None = Field(default=None, max_length=16)
    """応答なし時の転送先種別: voicemail / extension / hangup / None。"""
    fallback_value: str | None = Field(default=None, max_length=64)

    enabled: bool = True
    note: str | None = Field(default=None, max_length=255)

    member_extensions: list[str] = Field(default_factory=list)
    """同時 (または順番) に呼び出す内線番号のリスト。"""

    @field_validator("member_extensions")
    @classmethod
    def _at_least_one_member(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("少なくとも 1 つ以上の内線を選択してください。")
        return v
