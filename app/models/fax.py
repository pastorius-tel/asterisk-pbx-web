"""FAX 設定・送受信ログのモデル。

- FaxConfig : SMTP 設定 + 受信 FAX を受ける内線/DID + 通知先メール (シングルトン的に 1 行運用)
- FaxLog    : 送受信 1 件ごとの履歴 (PDF パス、相手番号、日時、状態)
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class FaxConfig(Base):
    """FAX のグローバル設定。基本的に id=1 の 1 行だけ使う。"""

    __tablename__ = "fax_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    """FAX 機能全体の有効/無効。"""

    # --- 受信 FAX の Asterisk 側設定 ---
    fax_did_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """FAX 受信に割り当てる契約番号 (DID)。
       ひかり電話オフィスA の追加番号などをここに指定。
       着信ルートでこの番号宛を ReceiveFAX へ振り分ける。"""

    fax_extension: Mapped[str] = mapped_column(String(16), default="9000")
    """内線側から FAX 受信をテストする時の内線番号 (例: 9000)。
       from-internal からこの番号にかけると ReceiveFAX が起動する。"""

    station_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """自局 FAX 番号 (送信時に相手に通知される CSID / TSI)。
       通常は契約電話番号を入れる。"""

    header_text: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """送信 FAX のヘッダーに載せる組織名など (任意)。"""

    minrate: Mapped[int] = mapped_column(Integer, default=4800)
    """FAX 伝送の最小ボーレート (bps)。Asterisk 既定値は 4800。
       選択可能値: 2400 / 4800 / 7200 / 9600 / 12000 / 14400。
       回線品質が悪くネゴシエーションが不安定な場合、9600 等に下げる
       (かつ maxrate も同程度以下に) と改善することがある。"""

    cng_detect_wait_seconds: Mapped[int] = mapped_column(Integer, default=2)
    """音声通話とFAXを共用する番号 (リンググループ) で、応答後すぐに
       内線を鳴らし始めず、この秒数だけ待ってから鳴らし始めるようにする。
       fax_detect (CNGトーン検知) は着信からある程度の時間を要するため、
       0 のままだと検知が完了する前に内線が一瞬鳴ってしまうことがある。
       この待機時間を設けることで、その一瞬の鳴音を防げる。
       ただし通常の音声着信でも同じだけ呼び出し開始が遅れるトレードオフ
       がある (既定 2 秒)。FAX 機能が無効なら適用されない。"""

    maxrate: Mapped[int] = mapped_column(Integer, default=14400)
    """FAX 伝送の最大ボーレート (bps)。Asterisk 既定値は 14400。
       T.38 が使えず音声 (G.711) パススルーのみの回線では、
       9600 や 7200 に下げた方が安定するケースが多い。"""

    ecm_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    """ECM (エラー訂正モード) を使うか。

       ECM はデータを誤りなく送るための仕組みだが、T.38 が使えず音声
       (G.711) モードで受信している回線では、パケットの揺らぎで ECM の
       再送が失敗し、複数ページの FAX で 2 ページ目以降が欠落したり
       受信自体が失敗したりすることがある
       (ログに "T.30 ECM carrier not found" が多発する状態)。
       その場合はこれを無効にすると改善することがある
       (画質はわずかに落ちるが、受信の確実性が上がる)。"""

    # --- 受信 FAX の処理選択 (チェックボックス) ---
    deliver_mail: Mapped[bool] = mapped_column(Boolean, default=True)
    """受信 FAX をメールに添付して通知するか。"""

    deliver_smb: Mapped[bool] = mapped_column(Boolean, default=False)
    """受信 FAX を外部 SMB 共有に PDF 保存するか。"""

    deliver_local: Mapped[bool] = mapped_column(Boolean, default=True)
    """受信 FAX をローカル (fax_store_dir) にも保存するか。
       メール/SMB を使う場合でも履歴ダウンロード用に既定で True 推奨。"""

    # --- SMB/CIFS (受信 FAX の外部保存先) ---
    smb_server: Mapped[str | None] = mapped_column(String(255), nullable=True)
    """SMB サーバーのホスト名 or IP (例: 192.168.1.10)。"""

    smb_share: Mapped[str | None] = mapped_column(String(255), nullable=True)
    """共有名 (例: share)。\\\\192.168.1.10\\share の 'share' 部分。"""

    smb_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    """共有内のサブディレクトリ (例: FAX)。空ならルート直下。"""

    smb_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    smb_password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    smb_domain: Mapped[str | None] = mapped_column(String(128), nullable=True)
    """Windows ドメイン/ワークグループ (任意。通常は空 or WORKGROUP)。"""

    smb_port: Mapped[int] = mapped_column(Integer, default=445)
    """SMB ポート (通常 445)。"""

    # --- SMTP (受信 FAX のメール通知用) ---
    smtp_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    smtp_port: Mapped[int] = mapped_column(Integer, default=587)
    smtp_security: Mapped[str] = mapped_column(String(8), default="starttls")
    """none / starttls / ssl のいずれか。Gmail は starttls(587) か ssl(465)。"""

    smtp_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    smtp_password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    """Gmail の場合はアプリパスワード (2 段階認証必須)。"""

    mail_from: Mapped[str | None] = mapped_column(String(255), nullable=True)
    """送信元アドレス (例: fax@example.com)。"""

    mail_to: Mapped[str | None] = mapped_column(String(512), nullable=True)
    """受信 FAX 通知の宛先。カンマ区切りで複数可。"""

    mail_subject_prefix: Mapped[str] = mapped_column(
        String(64), default="[FAX受信]"
    )
    """通知メールの件名プレフィックス。"""

    mail_body_template: Mapped[str] = mapped_column(
        Text,
        default=(
            "FAX を受信しました。\n\n"
            "  着信電話番号 : {peer}\n"
            "  受信日時     : {datetime}\n"
            "  ページ数     : {pages}\n"
            "  ファイル     : {filename}\n\n"
            "本メールに受信 FAX (PDF) を添付しています。\n"
        ),
    )
    """通知メール本文のテンプレート。
       使えるプレースホルダー: {peer} {datetime} {pages} {filename}"""

    # --- 保存ファイル名パターン (送受信共通の書式) ---
    filename_pattern_recv: Mapped[str] = mapped_column(
        String(255), default="受信FAX_{datetime}_{peer}"
    )
    """受信 FAX の保存ファイル名パターン (拡張子 .pdf は自動付与)。
       使えるプレースホルダー: {datetime} {peer}"""

    filename_pattern_send: Mapped[str] = mapped_column(
        String(255), default="送信FAX_{datetime}_{peer}"
    )
    """送信 FAX の保存ファイル名パターン (拡張子 .pdf は自動付与)。
       使えるプレースホルダー: {datetime} {peer}"""

    filename_datetime_format: Mapped[str] = mapped_column(
        String(64), default="%Y%m%d_%H%M"
    )
    """ファイル名中の {datetime} に使う日時書式 (Python strftime 形式)。
       例: %Y%m%d_%H%M → 20260817_1030 / %Y-%m-%d_%H-%M-%S → 2026-08-17_10-30-05"""

    note: Mapped[str | None] = mapped_column(String(255), nullable=True)


class FaxLog(Base):
    """送受信 1 件の履歴。"""

    __tablename__ = "fax_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    direction: Mapped[str] = mapped_column(String(8))
    """'recv' (受信) または 'send' (送信)。"""

    status: Mapped[str] = mapped_column(String(16), default="pending")
    """pending / processing / success / failed"""

    peer_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """受信: 着信元電話番号 / 送信: 宛先電話番号。"""

    pages: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """ページ数 (Asterisk の FAXOPT(pages) から)。"""

    pdf_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    """保存済み PDF の絶対パス。"""

    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    """送信時にアップロードされた元 PDF のファイル名。"""

    mail_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    """受信 FAX のメール通知を送ったか。"""

    smb_saved: Mapped[bool] = mapped_column(Boolean, default=False)
    """受信 FAX を外部 SMB 共有に保存できたか。"""

    smb_path_saved: Mapped[str | None] = mapped_column(String(512), nullable=True)
    """SMB 上の保存先パス (例: \\\\192.168.1.10\\share\\FAX\\受信FAX_...pdf)。"""

    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    """失敗時の詳細 (Asterisk の FAXSTATUS / 変換エラー / SMTP / SMB エラー等)。"""
