"""ログイン失敗の記録 (総当たり対策)。

以前はワーカープロセスのメモリ上で数えていたため、gunicorn の 2 ワーカー
それぞれで別々に数えられ (実質 2 倍の回数を試せる)、再起動で 0 に戻って
いた。DB に置くことで、全ワーカー・再起動後も同じ回数で制限する。
"""

from __future__ import annotations

from sqlalchemy import Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class LoginAttempt(Base):
    __tablename__ = "login_attempts"

    ip: Mapped[str] = mapped_column(String(64), primary_key=True)
    """送信元 IP アドレス。"""

    failures: Mapped[int] = mapped_column(Integer, default=0)
    """連続した失敗回数 (ブロックした時点で 0 に戻す)。"""

    blocked_until: Mapped[float] = mapped_column(Float, default=0.0)
    """この UNIX 時刻 (秒) まではログインを受け付けない。"""

    last_failure: Mapped[float] = mapped_column(Float, default=0.0)
    """最後に失敗した UNIX 時刻 (秒)。古い記録を数えないために使う。"""
