"""内線 (Extension) — SIP 端末を表す。"""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Extension(Base):
    __tablename__ = "extensions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # --- 基本 ---
    extension: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    """内線番号 (例: 1001)。3〜10 桁の数字を想定。"""

    display_name: Mapped[str] = mapped_column(String(80))
    """表示名 (Caller ID の Name に流用)。"""

    secret: Mapped[str] = mapped_column(String(80))
    """SIP パスワード。フォームで自動生成ボタンを提供。"""

    # --- SIP/メディア (UI は全部 select) ---
    transport: Mapped[str] = mapped_column(String(8), default="udp")
    codec_primary: Mapped[str] = mapped_column(String(8), default="ulaw")
    codec_secondary: Mapped[str | None] = mapped_column(String(8), nullable=True)
    dtmf_mode: Mapped[str] = mapped_column(String(16), default="rfc4733")
    nat_mode: Mapped[str] = mapped_column(String(32), default="force_rport,comedia")
    direct_media: Mapped[str] = mapped_column(String(8), default="no")

    # --- 機能 ---
    context: Mapped[str] = mapped_column(String(64), default="from-internal")

    # --- ボイスメール ---
    voicemail_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    """内線のボイスメール (留守番電話) を有効にするか。"""

    voicemail_pin: Mapped[str | None] = mapped_column(String(16), nullable=True)
    """*97 で留守電を聞くときの暗証番号 (未設定なら 1234)。"""

    voicemail_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    """録音を通知するメールアドレス。設定すると留守電録音時に送信。
       カンマ区切りで複数指定可。"""

    voicemail_attach: Mapped[bool] = mapped_column(Boolean, default=True)
    """メール通知時に音声ファイル (wav) を添付するか。"""

    voicemail_delete_after_email: Mapped[bool] = mapped_column(
        Boolean, default=False
    )
    """メール送信後にサーバー上の録音を削除するか
       (True=メールのみ運用 / False=*97 でも聞ける)。"""

    # --- 留守番電話専用内線 (電話機を持たない、応答専用の内線) ---
    voicemail_only: Mapped[bool] = mapped_column(Boolean, default=False)
    """True にすると、この内線は電話機を接続しない「留守番電話専用」に
       なる。着信すると呼び出しを行わず、すぐに応答メッセージを再生する。
       営業時間外アナウンス等の転送先として使う。"""

    voicemail_only_mode: Mapped[str] = mapped_column(String(16), default="record")
    """留守番電話専用内線の動作:
         "record"   : 応答メッセージ再生後、相手のメッセージを録音する
         "announce" : 応答メッセージを再生するだけで録音せず切断する
                      (営業時間外・移転案内など、用件を受け付けない場合)

       Asterisk の VoiceMail() には「録音せず案内だけ」という選択肢が
       無い (s オプションは案内文の読み上げを省くだけで録音は行われる)
       ため、announce では VoiceMail() を使わず Playback() + Hangup() で
       実現している。"""

    # --- 応答しなかったときの動作 (通常の内線向け) ---
    no_answer_audio_id: Mapped[int | None] = mapped_column(
        ForeignKey("audio_files.id"), nullable=True
    )
    """呼び出しても応答しなかった場合に再生する音源。
       未設定なら再生せず、下の no_answer_action にそのまま進む。"""

    no_answer_action: Mapped[str] = mapped_column(String(16), default="voicemail")
    """音源を再生した後 (または応答が無かった時) の動作:
         "voicemail" : この内線の留守番電話へ (録音する)
         "hangup"    : 切断する (録音しない。案内だけ流したい場合)

       着信ルート側にも「応答なし時」の設定があるが、あちらは着信ルート
       ごとの設定。こちらは内線ごとの設定で、内線同士の通話でも効く。"""

    voicemail_greeting_audio_id: Mapped[int | None] = mapped_column(
        ForeignKey("audio_files.id"), nullable=True
    )
    """応答メッセージに使う音源。未設定なら Asterisk 既定の
       「ただいま電話に出ることができません」が再生される
       (announce モードでは音源の設定が必須)。"""

    call_waiting: Mapped[bool] = mapped_column(Boolean, default=True)
    pickup_group: Mapped[str | None] = mapped_column(String(16), nullable=True)
    callgroup: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # --- 制限 ---
    max_contacts: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # --- メモ ---
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
