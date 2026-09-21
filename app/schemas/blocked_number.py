"""迷惑電話ブロックリスト フォーム検証用スキーマ。"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

from app.validators import normalize_pattern_input, validate_exten_pattern


class BlockedNumberForm(BaseModel):
    """迷惑電話ブロックリスト 追加/編集フォーム。"""

    pattern: str = Field(..., min_length=1, max_length=64)
    """発信者番号 (完全一致)、または先頭に "_" を付けた Asterisk 拡張
    パターン (前方一致等)。"""

    action: str = Field(default="hangup")
    """"hangup" (即切断) / "voicemail" (指定内線の留守番電話へ)。"""

    voicemail_target: str | None = Field(default=None, max_length=16)
    note: str | None = Field(default=None, max_length=255)
    enabled: bool = True

    @field_validator("pattern")
    @classmethod
    def _validate_pattern(cls, v: str) -> str:
        # 全角数字や全角ハイフンで貼り付けられることが多いので先に直す。
        # ハイフンは番号側の区切りとして入力されがちなので除去してから
        # 検証する (Asterisk 拡張パターンの "-" (範囲指定) は [] の中で
        # 使うため、そちらは to_halfwidth_number では消えない)。
        v = normalize_pattern_input(v)
        err = validate_exten_pattern(v)
        if err:
            raise ValueError(err)
        return v

    @field_validator("action")
    @classmethod
    def _validate_action(cls, v: str) -> str:
        if v not in ("hangup", "voicemail"):
            raise ValueError(f"不正な action です: {v}")
        return v

    @model_validator(mode="after")
    def _require_target_for_voicemail(self) -> BlockedNumberForm:
        if self.action == "voicemail" and not self.voicemail_target:
            raise ValueError("留守番電話への転送を選ぶ場合、転送先の内線を選択してください。")
        if self.action == "hangup":
            self.voicemail_target = None
        return self
