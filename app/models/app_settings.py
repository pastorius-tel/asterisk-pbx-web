"""アプリ全体設定 (singleton row)。

gunicorn は複数ワーカープロセス (-w 2 等) で動くため、Pydantic Settings の
インスタンス変数を実行時に書き換える方式 (旧 system_language /
reserve_leading_01_for_outbound の実装) では、書き換えたワーカーと
表示を返すワーカーが別プロセスになった場合に反映されない/表示が
チラつく問題があった (複数ワーカー間でメモリが共有されないため)。

これを避けるため、実行時に切り替え可能な設定は DB の 1 行に保持し、
全ワーカーが同じ SQLite ファイルを見ることで一貫性を保つ。
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AppSettings(Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    system_language: Mapped[str] = mapped_column(String(8), default="ja")
    """全エンドポイントに付与する language= の値。'' なら付与しない。"""

    reserve_leading_01_for_outbound: Mapped[bool] = mapped_column(Boolean, default=True)
    """True: 内線ダイヤルパターンの先頭桁を 2〜9 限定にし、0/1 始まりの
    番号 (携帯 090 等・特番 110/119・186 等) は内線側に一切マッチさせず
    外線発信ルート側の判定に回す。"""

    sip_port: Mapped[int] = mapped_column(Integer, default=5060)
    """内線 (PJSIP UDP transport) が待受する SIP ポート。
    既定は 5060。他システムと衝突する場合などにここで変更する。"""

    internal_ring_seconds: Mapped[int] = mapped_column(Integer, default=30)
    """内線同士 (from-internal でのダイヤル) の呼び出し (鳴音) 時間 (秒)。
    この秒数だけ相手の電話機を鳴らし、応答が無ければ (ボイスメールが
    設定されていれば) ボイスメールへ、無ければそのまま切断する。
    既定は 30 秒。"""

    voicemail_filename_pattern: Mapped[str] = mapped_column(
        String(255), default="{extension}_{datetime}"
    )
    """留守番電話メッセージを Web 画面から再生/ダウンロードする際の
       ファイル名パターン (拡張子 .wav は自動付与)。Asterisk 自体の
       保存名 (msgNNNN.wav) は変更できないため、これは「見た目の
       ファイル名」のみに適用される。
       使えるプレースホルダー: {extension} (内線番号) {datetime} (録音日時)"""

    voicemail_datetime_format: Mapped[str] = mapped_column(
        String(64), default="%Y%m%d_%H%M%S"
    )
    """voicemail_filename_pattern 中の {datetime} に使う日時書式
       (Python strftime 形式)。既定 %Y%m%d_%H%M%S → 20260817_103005。"""

    voicemail_mail_subject_template: Mapped[str] = mapped_column(
        String(255), default="[留守番電話] {extension} 様へ新着メッセージ"
    )
    """留守番電話の通知メール件名テンプレート。
       使えるプレースホルダー: {extension} {caller} {datetime} {duration}"""

    voicemail_mail_body_template: Mapped[str] = mapped_column(
        Text,
        default=(
            "{extension} 様\n\n"
            "新しいボイスメッセージが届きました。\n\n"
            "  発信者 : {caller}\n"
            "  受信日時 : {datetime}\n"
            "  長さ : {duration}\n"
            "  メールボックス : {extension}\n\n"
            "添付の音声ファイルをご確認ください。\n"
        ),
    )
    """留守番電話の通知メール本文テンプレート。
       使えるプレースホルダー: {extension} {caller} {datetime} {duration}

       {datetime} は「2026年8月18日 午後6時25分35秒」のような、OS の
       ロケール設定に依存しない日本語形式で固定生成される (Asterisk
       自身の emaildateformat は %A/%B/%p 等が英語になりがちなため、
       本ツールが Python 側でメールを組み立てて送信することで確実に
       日本語表記にしている。詳細は README 参照)。"""

    international_call_action: Mapped[str] = mapped_column(String(16), default="none")
    """海外からの着信と判定した場合の一律の処理:
       "none" (何もしない) / "voicemail" (指定内線の留守番電話へ) /
       "hangup" (即切断)。

       判定方法: 発信者番号が日本の標準的な国内番号パターン (0 で
       始まる携帯電話・固定電話・IP電話・フリーダイヤル等) に一致
       しない場合、海外または形式不明の番号とみなす (個々の国の国番号
       や "+"/"010" プレフィックスの正確な形式はトランク/キャリアに
       よって異なり確実に特定できないため、消去法で判定している)。"""

    international_voicemail_target: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )
    """international_call_action が voicemail の場合の転送先内線番号。"""

    voicemail_announce_datetime: Mapped[bool] = mapped_column(Boolean, default=False)
    """留守番電話の再生時に録音日時を読み上げるか (voicemail.conf の
       envelope=yes/no に対応)。

       有効にするには、日時読み上げ用の音声ファイル (digits/ji, digits/fun,
       digits/nichi, digits/mon-0〜11 等) が生成済みである必要がある。
       一般的な日本語音声パックにはこれらが含まれていないため、
       「音源」→「文章から音声を作る」画面から生成できるようにしている。
       未生成のまま有効にすると、再生時に "File does not exist" エラーで
       通話が切断されてしまうため、既定は False。"""

    # --- Asterisk → 本ツールの内部フック用トークン ---
    #
    # ダイヤルプラン (extensions.conf) に埋め込まれ、Asterisk が curl で
    # 本ツールを叩くときに付ける合言葉。これが一致しないリクエストは
    # 拒否する。以前は config.py に既定値 ("change-me-...") を持たせて
    # いたが、.env を書き換えずに使う人がいると「既定値を知っていれば
    # 誰でも偽の着信履歴を作れる/FAX 受信処理を起動できる」状態になる。
    #
    # そこで DB 側に保持し、未設定なら初回に自動生成する方式へ変更した。
    # (gunicorn の複数ワーカーが同じ値を見る必要があるため、プロセス内
    #  メモリではなく DB に置いている。)
    # .env に明示的に設定されていればそちらを優先する。
    fax_hook_token: Mapped[str] = mapped_column(String(64), default="")
    voicemail_hook_token: Mapped[str] = mapped_column(String(64), default="")
    calllog_hook_token: Mapped[str] = mapped_column(String(64), default="")
