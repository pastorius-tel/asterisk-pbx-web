"""発着信履歴 (通話ログ)。

Asterisk の CDR (Master.csv) を読む方式ではなく、ダイヤルプランから
本ツールへ通知させて記録する方式にしている。理由:

  - CSV の解析より確実で、重複取り込みの管理が不要
  - 「どこへ着信したか」(内線 / 留守番電話 / FAX / IVR 等) を、本ツールが
    持っている設定に基づいて正確に記録できる (CDR の dst だけでは
    リンググループ経由か留守電行きかを区別しづらい)
  - FAX・留守番電話の通知で既に使っている仕組みをそのまま使える

この履歴は、迷惑電話ブロックリストへの登録や、電話帳 (短縮ダイヤル)
機能の入力元としても使う。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CallLog(Base):
    __tablename__ = "call_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    direction: Mapped[str] = mapped_column(String(8), index=True)
    """"in" (着信) / "out" (発信)。"""

    started_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    """通話が始まった日時 (サーバーのローカル時刻)。"""

    peer_number: Mapped[str] = mapped_column(String(64), index=True)
    """相手の電話番号。着信なら発信者番号、発信ならダイヤルした番号。
    非通知の場合は "anonymous" が入る。"""

    peer_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    """相手の名前 (発信者名が通知された場合)。"""

    did_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """着信した自局番号 (どの番号にかかってきたか)。発信時は未設定。"""

    dest_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    """着信先の種別: extension / ring_group / queue / ivr / voicemail /
    fax / timecond / hangup など。一覧の「着信先」列に使う。"""

    dest_value: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """着信先の番号や名前 (内線番号、グループ番号、IVR 名など)。"""

    dest_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    """画面表示用の着信先名 (例: 「内線 201 田中」「留守番電話 202」)。
    設定が後から変わっても履歴の見え方が変わらないよう、記録時の
    表示名をそのまま保存する。"""

    disposition: Mapped[str | None] = mapped_column(String(16), nullable=True)
    """通話結果: ANSWERED (応答) / NO ANSWER (不応答) /
    BUSY / FAILED / VOICEMAIL (留守電に録音) / FAX など。"""

    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """通話時間 (秒)。呼び出しから終了までの長さ。"""

    billable_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """応答してから終了までの秒数。不応答なら 0。"""

    uniqueid: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    """Asterisk のチャネル一意 ID。重複登録を避けるために使う。"""

    trunk_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """経由したトランク名。"""

    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    """迷惑電話ブロックにより切断/留守電へ回された着信かどうか。"""

    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
