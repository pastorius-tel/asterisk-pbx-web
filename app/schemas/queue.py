"""キュー フォーム検証用スキーマ。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.base import QueueStrategy


class QueueForm(BaseModel):
    """キュー追加/編集フォーム。"""

    queue_number: str = Field(..., min_length=2, max_length=16, pattern=r"^\d+$")
    name: str = Field(..., min_length=1, max_length=80)
    strategy: QueueStrategy = QueueStrategy.RINGALL
    timeout: int = Field(default=15, ge=5, le=300)
    """1 メンバー当たりの呼出秒数。"""
    retry: int = Field(default=5, ge=0, le=300)
    wrapuptime: int = Field(default=0, ge=0, le=600)
    maxlen: int = Field(default=0, ge=0, le=100)
    """0 で無制限。"""
    music_class: str = Field(default="default", max_length=64)

    fallback_type: str | None = Field(default=None, max_length=16)
    """メンバー不在時・応答なし時の転送先種別: extension / voicemail /
    hangup / None。"""
    fallback_value: str | None = Field(default=None, max_length=64)

    enabled: bool = True
    note: str | None = Field(default=None, max_length=255)

    member_extensions: list[str] = Field(default_factory=list)
    """キューに所属させる内線番号のリスト (空でも可 — 後から動的に
    ログインさせる運用も考慮)。"""
