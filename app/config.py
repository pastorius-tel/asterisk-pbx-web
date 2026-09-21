"""アプリケーション設定 (環境変数から読込)。"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """環境変数 / .env から設定を読み込む。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Database ---
    database_url: str = "sqlite+aiosqlite:///./pbx.db"

    # --- Web ---
    secret_key: str = ""
    """セッション Cookie の署名鍵。空の場合は起動のたびにランダム生成する
    (= 再起動でログアウトされる)。固定したい場合は .env に十分長い
    ランダム文字列を設定すること。既知の固定値を使うと、Cookie を
    偽造されてログインを迂回されうるため、既定値は意図的に空にしている。"""

    host: str = "127.0.0.1"
    """待ち受けアドレス。既定はループバックのみ。
    LAN の他の PC から使う場合に 0.0.0.0 にする
    (その場合は ADMIN_PASSWORD を必ず設定すること)。"""

    port: int = 8080
    debug: bool = False
    """True にすると例外時にスタックトレースをブラウザへ返す。
    設定内容やパスの手がかりが漏れるため、運用時は必ず False。"""

    # --- 管理画面のログイン ---
    admin_user: str = "admin"
    admin_password: str = ""
    """管理画面のパスワード (平文)。空なら認証なしで動作する。"""

    admin_password_hash: str = ""
    """pbkdf2_sha256$... 形式のハッシュ。平文を置きたくない場合に使う。
    `python -m app.auth hash` で生成できる。両方設定時はこちらを優先。"""

    session_max_age: int = 60 * 60 * 12
    """ログインの有効時間 (秒)。既定 12 時間。"""

    trusted_hook_hosts: str = "127.0.0.1,::1"
    """内部フック (Asterisk からの curl) を受け付ける送信元 IP。
    カンマ区切り。空文字にすると送信元を制限しない
    (Asterisk が別ホストにある構成向け)。"""

    # --- Asterisk ---
    asterisk_config_dir: Path = Field(default=Path("/etc/asterisk"))
    """Asterisk の実運用 conf 置き場。通常は /etc/asterisk 直下でよい。
    サブディレクトリ (managed 等) を指定すると Asterisk が実際に読む
    ファイルと本ツールが書き込むファイルの場所がズレて「変更を反映」が
    サイレントに空振りする事故の元になるため、既定値は直下にしている。"""
    asterisk_sounds_dir: Path = Field(default=Path("/var/lib/asterisk/sounds/ja/managed"))
    """生成 WAV 音源の配置先。Asterisk が読めるパス。
       実機の標準は /var/lib/asterisk/sounds/<lang>/。"""

    rtp_start: int = 10000
    rtp_end: int = 20000

    ami_host: str = "127.0.0.1"
    ami_port: int = 5038
    ami_user: str = "admin"
    ami_secret: str = ""
    ami_permit: str = "127.0.0.1/255.255.255.255"
    """AMI 接続を許可する IP/マスク (manager.conf の permit 行)。"""

    default_context: str = "from-internal"
    system_language: str = "ja"
    """全エンドポイントに付与する language= の値。日本語音声を使うなら 'ja'、
    英語に戻すなら 'en'。'' (空) なら language 行自体を出力しない。"""

    reserve_leading_01_for_outbound: bool = True
    """True の場合、内線同士のダイヤルプラン (3〜5桁) の先頭 1 桁は
    2〜9 のみにマッチさせる (Asterisk のパターン文字 'N')。
    これにより 0/1 で始まる番号 (携帯 090/080/070、固定 03/06、
    フリーダイヤル 0120、警察 110・消防 119 等の特番) は内線パターンに
    一切マッチしなくなり、確実に発信ルート (トランク) 側へ流れる。
    False にすると従来通り 0-9 すべてにマッチする ('X') 動作に戻る。"""

    # --- 音源変換 ---
    ffmpeg_path: str = "ffmpeg"
    """ffmpeg のパス。$PATH 上にあれば 'ffmpeg' で OK。"""

    uploads_dir: Path = Field(default=Path("./uploads"))
    """アップロード元ファイル (mp3 等) の保存先。変換後は asterisk_sounds_dir へ。"""

    # --- FAX ---
    fax_spool_dir: Path = Field(default=Path("/var/spool/asterisk/fax"))
    """Asterisk が ReceiveFAX/SendFAX で読み書きする TIFF の置き場。
       Asterisk (asterisk ユーザー) と本ツールの両方が読み書きできる必要がある。"""

    fax_store_dir: Path = Field(default=Path("/var/lib/asterisk-pbx-web/fax"))
    """送受信済み FAX を PDF で永続保存するディレクトリ。
       受信: 受信FAX_YYYYMMDD_HHMM_<着信番号>.pdf
       送信: 送信FAX_YYYYMMDD_HHMM_<宛先番号>.pdf"""

    tiff2pdf_path: str = "tiff2pdf"
    """libtiff-tools の tiff2pdf。受信 TIFF → PDF 変換に使用。"""

    gs_path: str = "gs"
    """Ghostscript。送信時に PDF → FAX 用 TIFF (G3, 204x196dpi) 変換に使用。"""

    # --- ボイスメール ---
    voicemail_spool_dir: Path = Field(
        default=Path("/var/spool/asterisk/voicemail")
    )
    """Asterisk (app_voicemail) がメッセージを保存するディレクトリ。
       実体は <ここ>/<context>/<内線番号>/<フォルダ(INBOX等)>/msgNNNN.wav。
       本ツールが読み書き (再生用配信・削除) するため asterisk ユーザーとの
       共有アクセス権が必要。"""

    fax_hook_token: str = ""
    """Asterisk のダイヤルプランから本ツールの受信完了フックを叩く際の簡易認証トークン。
       extensions.conf に埋め込まれ、POST /fax/hook/received のクエリで照合する。"""

    voicemail_hook_token: str = ""
    """Asterisk のダイヤルプランから本ツールの留守番電話録音完了フックを
       叩く際の簡易認証トークン。extensions.conf に埋め込まれ、
       GET /voicemail/hook/received のクエリで照合する。"""

    calllog_hook_token: str = ""
    """Asterisk のダイヤルプランから発着信履歴を記録するフックを叩く際の
       簡易認証トークン。extensions.conf に埋め込まれ、
       GET /call-logs/hook/record のクエリで照合する。"""

    backup_dir: Path = Field(default=Path("./backups"))
    """DB (pbx.db) + .env のバックアップ zip を保存するディレクトリ。
       「システム」画面から作成・ダウンロード・削除できる。"""

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith(("postgresql", "postgres"))

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


settings = Settings()
