"""トランクフォーム検証用スキーマ。"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.base import Codec, DtmfMode, NatMode, Transport, TrunkType

_HIKARI_TYPES = (TrunkType.HIKARI_OFFICE_A_HGW, TrunkType.HIKARI_OFFICE_A_OG)


class TrunkForm(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")
    trunk_type: TrunkType = TrunkType.REGISTER

    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(default=5060, ge=1, le=65535)
    transport: Transport = Transport.UDP

    username: str | None = Field(default=None, max_length=80)
    """REGISTER 時の認証ユーザID。
       ひかり電話オフィスA では HGW/OG で表示される 4 桁ユーザID (例: '0003')。"""

    secret: str | None = Field(default=None, max_length=80)
    auth_username: str | None = Field(default=None, max_length=80)

    from_user: str | None = Field(default=None, max_length=80)
    from_domain: str | None = Field(default=None, max_length=255)
    outbound_caller_id: str | None = Field(default=None, max_length=80)
    """契約電話番号 (発信時に通知させたい番号、表示用)。
       ひかり電話オフィスA では From ヘッダではなく HGW 側の発番制御で扱われる。"""

    codec_primary: Codec = Codec.ULAW
    codec_secondary: Codec | None = None
    dtmf_mode: DtmfMode = DtmfMode.RFC4733
    nat_mode: NatMode = NatMode.FORCE_RPORT_COMEDIA

    max_channels: int = Field(default=10, ge=0, le=10000)
    qualify: bool = True
    enabled: bool = True

    # NTT 系拡張
    hgw_extension_number: str | None = Field(default=None, max_length=8)
    additional_did_numbers: str | None = Field(default=None, max_length=512)
    use_mac_auth: bool = False
    disable_rport: bool = False
    trust_id_inbound: bool = False
    send_pai: bool = False

    note: str | None = Field(default=None, max_length=255)

    @field_validator("additional_did_numbers")
    @classmethod
    def _validate_dids(cls, v: str | None) -> str | None:
        if not v:
            return None
        nums = [n.strip() for n in v.split(",") if n.strip()]
        for n in nums:
            if not n.isdigit() or not (8 <= len(n) <= 15):
                raise ValueError(f"DID 番号 '{n}' は 8〜15 桁の数字で指定してください。")
        return ",".join(nums)

    @model_validator(mode="after")
    def _validate_combinations(self) -> TrunkForm:
        """フィールド間の依存をチェック。"""
        # ひかり電話オフィスA: HGW/OG 内線番号が必須
        if self.trunk_type in _HIKARI_TYPES:
            if not self.hgw_extension_number:
                raise ValueError(
                    "ひかり電話オフィスA では HGW/OG 内線番号 (例: '3' や '10') が必須です。"
                )
            if not self.hgw_extension_number.isdigit() or not (
                1 <= len(self.hgw_extension_number) <= 4
            ):
                raise ValueError(
                    "HGW/OG 内線番号は 1〜4 桁の数字で指定してください。"
                )
            # MAC 認証 OFF 時は secret 必須
            if not self.use_mac_auth and not self.secret:
                raise ValueError(
                    "ひかり電話オフィスA でユーザID/パスワード認証の場合は secret が必須です。"
                    " (MAC アドレス認証で運用するなら『MAC アドレス認証を使う』を ON にしてください)"
                )
            # MAC 認証 OFF 時は username (ユーザID) 必須
            if not self.use_mac_auth and not self.username:
                raise ValueError(
                    "ひかり電話オフィスA でユーザID/パスワード認証の場合は"
                    " 認証ユーザID (4桁、例: '0003') が必須です。"
                )

        # REGISTER 方式は secret 必須
        if self.trunk_type == TrunkType.REGISTER and not self.secret:
            raise ValueError("REGISTER 方式ではパスワード (secret) が必須です。")

        return self
