"""音源ファイル (mp3 アップロード → wav 変換後の管理)。

実体は 2 ファイル: 元の mp3 (uploads_dir) と 変換後 wav (asterisk_sounds_dir)。
DB にはメタ情報 (用途、表示名、ファイル名、duration 等) を保存。
"""

from __future__ import annotations

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class AudioFile(Base):
    """変換済みの音源ファイル 1 つを表す。"""

    __tablename__ = "audio_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    name: Mapped[str] = mapped_column(String(80))
    """表示名 (例: '昼間用 保留音')。"""

    category: Mapped[str] = mapped_column(String(16))
    """用途: moh / park / ivr / voicemail / custom。"""

    # --- ファイル ---
    original_filename: Mapped[str] = mapped_column(String(255))
    """ユーザーがアップロードした元のファイル名 (例: 'office_bgm.mp3')。"""

    storage_name: Mapped[str] = mapped_column(String(80), unique=True)
    """サニタイズ済み内部ファイル名 (拡張子なし、英数+ハイフン+アンダースコア)。
       実ファイルは:
         <uploads_dir>/<storage_name>.<元の拡張子>    (元 MP3 等)
         <asterisk_sounds_dir>/<category>/<storage_name>.wav (変換後)
    """

    source_format: Mapped[str] = mapped_column(String(8))
    """元ファイルの拡張子 (mp3 / wav / m4a / ogg)。"""

    duration_seconds: Mapped[float | None] = mapped_column(nullable=True)
    sample_rate: Mapped[int] = mapped_column(Integer, default=8000)
    """変換後のサンプリングレート (Asterisk 既定 8000Hz)。"""

    bytes_size: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- 状態 ---
    conversion_status: Mapped[str] = mapped_column(String(16), default="pending")
    """pending / ok / failed"""

    conversion_log: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    """ffmpeg の stderr 等 (失敗時の診断用)。"""

    # --- 音声合成 (TTS) で作成した音源のみ使用 ---
    tts_text: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    """読み上げた文章。これがあると「文章を編集して作り直す」ことが
    できる (アップロード音源と違い元ファイルが無いため、再変換では
    なく再合成する)。"""

    tts_speed: Mapped[float | None] = mapped_column(Float, nullable=True)
    """合成時の話速 (1.0 が標準)。"""

    tts_voice: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """使用した音響モデル (声) の識別子。"""

    tts_pitch: Mapped[float | None] = mapped_column(Float, nullable=True)
    """ピッチ (半音単位。+ で高く)。"""

    tts_tone: Mapped[float | None] = mapped_column(Float, nullable=True)
    """声質 (オールパス係数 0〜1)。None なら音響モデル既定。"""

    tts_gain: Mapped[float | None] = mapped_column(Float, nullable=True)
    """音量 (dB)。"""

    tts_clarity: Mapped[float | None] = mapped_column(Float, nullable=True)
    """明瞭さ (ポストフィルタ係数)。"""

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)


class MohClass(Base):
    """保留音クラス (musiconhold.conf の 1 セクション)。

    複数のオーディオファイルをまとめて、Asterisk からは class 名で参照する。
    例: クラス '昼間営業中' = [bgm_morning.wav, bgm_afternoon.wav]
    """

    __tablename__ = "moh_classes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    class_name: Mapped[str] = mapped_column(String(64), unique=True)
    """musiconhold.conf の [section]。英数アンダースコアのみ。"""

    display_name: Mapped[str] = mapped_column(String(80))
    sort_mode: Mapped[str] = mapped_column(String(16), default="random")

    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    """default クラスとして扱う (アプリで 1 つだけ)。"""

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    items: Mapped[list[MohClassItem]] = relationship(
        back_populates="moh_class",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="MohClassItem.order_index",
    )


class MohClassItem(Base):
    """保留音クラスに含まれる音源ファイル。"""

    __tablename__ = "moh_class_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    moh_class_id: Mapped[int] = mapped_column(ForeignKey("moh_classes.id"))
    audio_file_id: Mapped[int] = mapped_column(ForeignKey("audio_files.id"))
    order_index: Mapped[int] = mapped_column(Integer, default=0)

    moh_class: Mapped[MohClass] = relationship(back_populates="items")
