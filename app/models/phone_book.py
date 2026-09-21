"""電話帳 (短縮ダイヤル)。

発着信履歴から「電話帳に登録」して増やしていくことを想定した、
相手先の名前と番号の一覧。

将来的に次の用途へ広げられるようにしている:
  - 着信時に相手の名前を電話機へ表示する (CALLERID(name) の差し替え)
  - 短縮番号 (例: 71) をダイヤルして発信する
  - 発着信履歴の一覧に相手の名前を出す
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class PhoneBookEntry(Base):
    __tablename__ = "phone_book_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    name: Mapped[str] = mapped_column(String(128))
    """表示名 (会社名・担当者名など)。着信時の名前表示に使う。"""

    number: Mapped[str] = mapped_column(String(64), index=True)
    """電話番号。ハイフンは保存時に取り除く。"""

    kana: Mapped[str | None] = mapped_column(String(128), nullable=True)
    """よみがな (並べ替え・検索用)。"""

    speed_dial: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    """短縮ダイヤル番号 (2〜3桁の数字。例: "01")。

       電話機からは「アスタリスク 7 + この番号」でダイヤルする
       (短縮番号 01 なら *701)。既存の特番 (*8 パーク取得 / *43 エコー /
       *97・*98 留守番電話) と衝突しないよう *7 を短縮ダイヤル用に
       割り当てている。未設定なら短縮発信はできない (登録だけ)。"""

    company: Mapped[str | None] = mapped_column(String(128), nullable=True)
    """会社名・部署名など (名前とは別に持ちたい場合)。"""

    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
