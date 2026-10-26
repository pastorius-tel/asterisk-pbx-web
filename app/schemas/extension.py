"""内線フォーム検証用スキーマ。"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.models.base import Codec, DirectMedia, DtmfMode, NatMode, Transport


class ExtensionForm(BaseModel):
    """内線追加/編集フォーム。"""

    extension: str = Field(..., min_length=2, max_length=10, pattern=r"^\d+$")
    display_name: str = Field(..., min_length=1, max_length=80)
    # 構造上の下限は 8 (旧版で作成済みの内線を編集で保存できなくしないため)。
    # 新規・変更時の「10 文字以上 + 強度」は _validate_secret で課す。
    secret: str = Field(..., min_length=8, max_length=80)

    transport: Transport = Transport.UDP
    codec_primary: Codec = Codec.ULAW
    codec_secondary: Codec | None = None
    dtmf_mode: DtmfMode = DtmfMode.RFC4733
    nat_mode: NatMode = NatMode.FORCE_RPORT_COMEDIA
    direct_media: DirectMedia = DirectMedia.NO

    context: str = Field(default="from-internal", max_length=64)
    voicemail_enabled: bool = False
    voicemail_pin: str | None = Field(default=None, max_length=16)
    voicemail_email: str | None = Field(default=None, max_length=255)
    voicemail_attach: bool = True
    voicemail_delete_after_email: bool = False

    # 応答しなかったときの動作
    no_answer_audio_id: int | None = None
    no_answer_action: str = "voicemail"

    # 留守番電話専用内線
    voicemail_only: bool = False
    voicemail_only_mode: str = "record"
    voicemail_greeting_audio_id: int | None = None
    call_waiting: bool = True
    pickup_group: str | None = Field(default=None, max_length=16)
    callgroup: str | None = Field(default=None, max_length=16)

    max_contacts: int = Field(default=1, ge=1, le=20)
    enabled: bool = True
    note: str | None = Field(default=None, max_length=255)

    # 内線番号の先頭桁 (0/1 始まり禁止) チェックは、AppSettings が DB 管理
    # (gunicorn 複数ワーカー間で一貫させるため) になったことに伴い、
    # 非同期 DB アクセスができないこのスキーマ内ではなく
    # app/routers/extensions.py の _save_extension() 側で行う。

    @field_validator("secret")
    @classmethod
    def _validate_secret(cls, v: str, info) -> str:  # type: ignore[no-untyped-def]
        """SIP パスワードの弱すぎる値を弾く。

        内線 (SIP エンドポイント) のパスワードは、ネットワーク上から
        登録 (REGISTER) を試せるため、PBX でもっとも狙われる総当たりの
        対象になる。画面の自動生成は 16 文字の英数字なので通常は問題ない
        が、手入力で弱い値が設定されるのを防ぐ。

        ただし、既存の内線を編集するとき、パスワードを変更していない
        (保存済みの値のまま) 場合はこの強度チェックを行わない。旧版で
        作成した弱めのパスワードが原因で、名前やコーデックなど別の項目を
        直すだけの保存までできなくなる (電話機の再設定を強いる) のを防ぐ。
        変更していないかどうかは、呼び出し側が context で渡す。
        """
        s = (v or "").strip()
        existing = (info.context or {}).get("existing_secret") if info.context else None
        if existing is not None and s == existing:
            return s
        if len(s) < 10:
            raise ValueError("SIP パスワードは 10 文字以上にしてください。")
        lowered = s.lower()
        weak = {
            "password", "passw0rd", "changeme", "secret", "1234567890",
            "0123456789", "voippassword", "asterisk", "default",
        }
        if lowered in weak:
            raise ValueError("推測されやすいパスワードです。別の値にしてください。")
        if len(set(s)) < 4:
            raise ValueError("同じ文字が多すぎます。英数字を混ぜた 10 文字以上にしてください。")
        if s.isdigit():
            raise ValueError("数字だけのパスワードは避け、英字を混ぜてください。")
        if s.isalpha():
            raise ValueError("英字だけのパスワードは避け、数字を混ぜてください。")
        return s

    @field_validator("voicemail_pin")
    @classmethod
    def _validate_vm_pin(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not v.isdigit() or len(v) < 4:
            raise ValueError("ボイスメール PIN は 4 桁以上の数字で入力してください。")
        return v

    @field_validator("codec_secondary")
    @classmethod
    def _no_dup_codec(cls, v: Codec | None, info) -> Codec | None:  # type: ignore[no-untyped-def]
        if v is not None and v == info.data.get("codec_primary"):
            raise ValueError("第 1 / 第 2 コーデックは別の値を指定してください。")
        return v
