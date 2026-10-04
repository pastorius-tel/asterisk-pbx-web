"""ルートのフォームスキーマ。"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.models.base import RouteAction
from app.validators import (
    normalize_pattern_input,
    normalize_phone_number,
    validate_exten_pattern,
)


class OutboundRouteForm(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    priority: int = Field(default=100, ge=1, le=9999)
    pattern: str = Field(..., min_length=1, max_length=64)
    prefix_strip: int = Field(default=0, ge=0, le=10)
    prefix_prepend: str | None = Field(default=None, max_length=16)
    trunk_id: int = Field(..., ge=1)
    cid_override: str | None = Field(default=None, max_length=80)
    require_password: bool = False
    password: str | None = Field(default=None, max_length=16)
    enabled: bool = True
    note: str | None = Field(default=None, max_length=255)

    @field_validator("pattern")
    @classmethod
    def _validate_pattern(cls, v: str) -> str:
        # このパターンは extensions.conf の `exten => <ここ>,1,...` に
        # そのまま入る。"];[" のような値を保存できてしまうと、発信ルートの
        # コンテキスト全体が読み込めなくなり **外線発信が全滅する**。
        v = normalize_pattern_input(v)
        err = validate_exten_pattern(v)
        if err:
            raise ValueError(err)
        return v


class InboundRouteForm(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    did_number: str | None = Field(default=None, max_length=32)
    caller_id_pattern: str | None = Field(default=None, max_length=64)
    destination_type: RouteAction
    destination_value: str = Field(..., min_length=1, max_length=64)
    ring_seconds: int = Field(default=30, ge=5, le=300)
    no_answer_type: str | None = Field(default=None, max_length=16)
    no_answer_value: str | None = Field(default=None, max_length=64)
    cid_prefix: str | None = Field(default=None, max_length=16)
    record_call: bool = False
    priority: int = Field(default=100, ge=1, le=9999)
    enabled: bool = True
    note: str | None = Field(default=None, max_length=255)

    @field_validator("did_number")
    @classmethod
    def _normalize_did(cls, v: str | None) -> str | None:
        """着信番号 (DID) を半角の数字だけに正規化する。

        `exten => <DID>,1,...` として使うため、全角やハイフンが
        混ざっていると「着信ルートを登録したのに鳴らない」ことになる。
        """
        if v is None:
            return None
        cleaned = normalize_phone_number(v)
        if v.strip() and not cleaned:
            raise ValueError("着信番号には半角数字を入力してください。")
        return cleaned or None

    @field_validator("caller_id_pattern")
    @classmethod
    def _validate_cid_pattern(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        cleaned = normalize_pattern_input(v)
        err = validate_exten_pattern(cleaned)
        if err:
            raise ValueError(f"発信者番号パターン: {err}")
        return cleaned
