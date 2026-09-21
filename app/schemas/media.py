"""音源・保留音・パーク・IVR フォーム検証用スキーマ。"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.models.base import AudioCategory, IvrAction, MohSortMode


class AudioUploadForm(BaseModel):
    """音源アップロードフォーム (ファイル本体は別途 UploadFile で受ける)。"""

    name: str = Field(..., min_length=1, max_length=80)
    category: AudioCategory = AudioCategory.MOH
    note: str | None = Field(default=None, max_length=255)


class MohClassForm(BaseModel):
    class_name: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_]+$")
    display_name: str = Field(..., min_length=1, max_length=80)
    sort_mode: MohSortMode = MohSortMode.RANDOM
    is_default: bool = False
    enabled: bool = True
    note: str | None = Field(default=None, max_length=255)
    audio_file_ids: list[int] = Field(default_factory=list)

    @field_validator("class_name")
    @classmethod
    def _no_uppercase_only_keywords(cls, v: str) -> str:
        # Asterisk 予約語回避
        if v.lower() in ("general", "globals"):
            raise ValueError(f"クラス名 '{v}' は予約語のため使用できません。")
        return v


class ParkingLotForm(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_]+$")
    park_ext: str = Field(default="700", pattern=r"^\d{3,5}$")
    park_pos_start: int = Field(default=701, ge=10, le=99999)
    park_pos_end: int = Field(default=720, ge=10, le=99999)
    park_time_seconds: int = Field(default=120, ge=10, le=3600)
    moh_class: str | None = Field(default=None, max_length=64)
    comeback_context: str = Field(default="from-internal", max_length=64)
    findslot: str = Field(default="first", pattern=r"^(first|next)$")
    is_default: bool = False
    enabled: bool = True
    note: str | None = Field(default=None, max_length=255)

    @field_validator("park_pos_end")
    @classmethod
    def _range_check(cls, v: int, info) -> int:  # type: ignore[no-untyped-def]
        start = info.data.get("park_pos_start", 0)
        if v < start:
            raise ValueError(
                f"レンジ終端 ({v}) は開始 ({start}) より大きくしてください。"
            )
        if v - start > 99:
            raise ValueError("パークレンジは 100 スロット以内にしてください。")
        return v


class IvrForm(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_]+$")
    display_name: str = Field(..., min_length=1, max_length=80)
    greeting_audio_id: int | None = None
    invalid_audio_id: int | None = None
    timeout_audio_id: int | None = None
    timeout_seconds: int = Field(default=5, ge=1, le=60)
    max_retries: int = Field(default=3, ge=1, le=10)
    fallback_action: IvrAction = IvrAction.HANGUP
    fallback_value: str | None = Field(default=None, max_length=64)
    enabled: bool = True
    note: str | None = Field(default=None, max_length=255)


class IvrEntryForm(BaseModel):
    key_input: str = Field(..., pattern=r"^([0-9]|\*|#|_X\.)$")
    action: IvrAction
    action_value: str | None = Field(default=None, max_length=64)
    label: str | None = Field(default=None, max_length=80)
