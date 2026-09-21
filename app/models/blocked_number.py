"""迷惑電話ブロックリスト — 登録した発信者番号 (パターン) からの着信を
自動的に処理する。

自動的にインターネットから迷惑電話リストを取得する機能ではなく (信頼
できる無料の一括取得手段が存在しないため。README 参照)、利用者自身が
番号を登録して管理する方式。着信ルート等とは独立して、全トランクの
着信に対して優先的にチェックされる。
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class BlockedNumber(Base):
    __tablename__ = "blocked_numbers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    pattern: Mapped[str] = mapped_column(String(64))
    """発信者番号、または Asterisk 拡張パターン (例: 固定の
    "0312345678" や、前方一致の "_03AAAABBBB" 等)。先頭が "_" なら
    パターンマッチ、そうでなければ完全一致として扱う。"""

    action: Mapped[str] = mapped_column(String(16), default="hangup")
    """一致した場合の処理: "hangup" (即切断) / "voicemail" (指定内線の
    留守番電話へ)。"""

    voicemail_target: Mapped[str | None] = mapped_column(String(16), nullable=True)
    """action が voicemail の場合の転送先内線番号。"""

    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    """登録理由等のメモ (例: 「しつこい勧誘」)。"""

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
