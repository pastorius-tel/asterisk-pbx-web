"""SIP トランク — ITSP / 上位 PBX との接続。"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Trunk(Base):
    __tablename__ = "trunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    """トランク名 (英数 / アンダースコア)。pjsip.conf の [section] になる。"""

    trunk_type: Mapped[str] = mapped_column(String(16), default="register")
    """register / peer / iax2"""

    # --- 接続先 ---
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer, default=5060)
    transport: Mapped[str] = mapped_column(String(8), default="udp")

    # --- 認証 (register 用) ---
    username: Mapped[str | None] = mapped_column(String(80), nullable=True)
    secret: Mapped[str | None] = mapped_column(String(80), nullable=True)
    auth_username: Mapped[str | None] = mapped_column(String(80), nullable=True)

    # --- 発信者番号 ---
    from_user: Mapped[str | None] = mapped_column(String(80), nullable=True)
    from_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    outbound_caller_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    """発信時に通知する番号 (例: 0312345678)。"""

    # --- メディア (全部 select) ---
    codec_primary: Mapped[str] = mapped_column(String(8), default="ulaw")
    codec_secondary: Mapped[str | None] = mapped_column(String(8), nullable=True)
    dtmf_mode: Mapped[str] = mapped_column(String(16), default="rfc4733")
    nat_mode: Mapped[str] = mapped_column(String(32), default="force_rport,comedia")

    # --- 同時接続/表示 ---
    max_channels: Mapped[int] = mapped_column(Integer, default=10)
    """0 で無制限。"""

    qualify: Mapped[bool] = mapped_column(Boolean, default=True)
    """OPTIONS で死活監視を行う。"""

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # --- ひかり電話オフィスA / NTT 系で使う拡張 ---
    hgw_extension_number: Mapped[str | None] = mapped_column(String(8), nullable=True)
    """HGW/OG 上で設定した内線番号 (1〜2桁、例: "3" や "10")。
       From ヘッダのユーザ部および client_uri のユーザ部に使用。
       認証ユーザID (`username` フィールド, 例: "0003") とは別物。"""

    additional_did_numbers: Mapped[str | None] = mapped_column(String(512), nullable=True)
    """追加 DID 番号 (カンマ区切り)。
       例: '0312345679,0312345680'
       着信時に To ヘッダから取り出して振り分けに使う。"""

    use_mac_auth: Mapped[bool] = mapped_column(Boolean, default=False)
    """OG 系で MAC アドレス認証 (パスワードなし) を使う場合に True。
       True のとき auth セクションは生成しない。"""

    disable_rport: Mapped[bool] = mapped_column(Boolean, default=False)
    """[system] disable_rport=yes を有効化するか。
       OG 配下や直収構成で Via ヘッダの rport を抑止する必要がある場合に True。"""

    trust_id_inbound: Mapped[bool] = mapped_column(Boolean, default=False)
    """着信時に P-Asserted-Identity 等を信用するか。
       HGW 経由では基本不要 (HGW 側が処理する)。直収時のみ True。"""

    send_pai: Mapped[bool] = mapped_column(Boolean, default=False)
    """発信時に P-Asserted-Identity を送るか。
       HGW 経由では基本不要。直収時のみ True。"""

    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
