"""着信ルート — 外線への着信をどこに流すか (DID ルーティング)。"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class InboundRoute(Base):
    __tablename__ = "inbound_routes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    name: Mapped[str] = mapped_column(String(64), unique=True)

    # --- マッチ条件 ---
    did_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """着信 DID。空欄/`_X.` で「すべての DID」。"""

    caller_id_pattern: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """発信者番号でのフィルタ (例: 03Z. )。"""

    # --- 転送先 ---
    destination_type: Mapped[str] = mapped_column(String(16))
    """extension / ring_group / queue / ivr / voicemail / hangup / time_condition"""

    destination_value: Mapped[str] = mapped_column(String(64))
    """転送先の ID または番号。"""

    # --- 応答なし時のフォールバック (destination_type=extension のとき有効) ---
    ring_seconds: Mapped[int] = mapped_column(Integer, default=30)
    """内線を呼び出す秒数。これを過ぎると no_answer_* へ。"""

    no_answer_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    """応答なし時の転送先種別。
       voicemail / extension / hangup / None(切断)。
       ウ「応答なし→ボイスメール」はここを voicemail にする。"""

    no_answer_value: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """応答なし時の転送先 (voicemail なら内線番号=メールボックス)。"""

    # --- オプション ---
    cid_prefix: Mapped[str | None] = mapped_column(String(16), nullable=True)
    """発信者表示名にプレフィクスを付ける (例: '[外線]')。"""

    record_call: Mapped[bool] = mapped_column(Boolean, default=False)

    priority: Mapped[int] = mapped_column(Integer, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
