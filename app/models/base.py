"""Base クラスと共通の列挙型。

すべての「選択肢」をここに集約する。
画面の <select> は必ずこれらの enum を参照する → 入力ミスを排除。
"""

from __future__ import annotations

import enum
import logging
import re
from datetime import datetime

from sqlalchemy import DateTime, String, Text, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

log = logging.getLogger(__name__)


def _now_local() -> datetime:
    """レコードの作成/更新日時に使うローカル時刻 (サーバーのタイムゾーン)。

    以前は server_default=func.now() を使っていたが、SQLite では
    func.now() が CURRENT_TIMESTAMP に変換され、**SQLite の仕様上これは
    常に UTC を返す**ため、サーバーを Asia/Tokyo にしていても FAX 受信
    日時などが 9 時間ずれて表示されていた。
    DB 側ではなく Python 側で時刻を作ることで、サーバーのタイムゾーン
    (setup_dependencies.sh で Asia/Tokyo に設定) をそのまま反映する。
    """
    return datetime.now()


class Base(DeclarativeBase):
    """全モデル共通の基底クラス。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_local, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_now_local,
        onupdate=_now_local,
        nullable=False,
    )


# ===========================================================================
# 設定ファイルインジェクション対策 (重要)
# ===========================================================================
#
# 本ツールは DB の値を extensions.conf / pjsip.conf 等へそのまま埋め込む。
# もし値に改行が含まれていると、Asterisk から見て「次の行」が生まれてしまい、
# 内線の表示名のような無害な欄から任意の設定行を注入できてしまう。
#
#   表示名 = "営業部\n same => n,System(id > /tmp/pwned)\n;"
#     ↓ 生成された extensions.conf
#   ;=== 内線 201 : 営業部
#    same => n,System(id > /tmp/pwned)      ← asterisk ユーザーで任意コマンド実行
#
# 埋め込み箇所は 2600 行超のジェネレータ全体に散在しており (実測で
# 「カラム → 生成ファイル」の組み合わせが 60 件)、1 箇所ずつエスケープを
# 書くと必ず書き漏れが出る。そこで **DB へ書き込む直前に一括で除去する**。
# ここを通らずに DB へ文字列が入る経路は無いため、これが最終防衛線になる。
#
# 除去対象: 改行 (CR/LF) と制御文字 (NUL 等)。タブは半角スペースにする。
# 文字数や日本語には一切手を触れないので、通常の入力は影響を受けない。

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# 改行を保持してよい (= Asterisk の設定ファイルに出力されない) カラム。
# メール本文テンプレートなど、複数行で入力することに意味がある欄だけを
# 明示的に許可する。(テーブル名, カラム名) で指定する。
_MULTILINE_ALLOWED: frozenset[tuple[str, str]] = frozenset(
    {
        ("app_settings", "voicemail_mail_body_template"),
        ("fax_config", "mail_body_template"),
    }
)


def sanitize_config_text(value: str, *, allow_newlines: bool = False) -> str:
    """設定ファイルに出力しても安全な文字列へ正規化する。

    - CR/LF → 半角スペース (allow_newlines=True のときは LF のみ残す)
    - タブ → 半角スペース
    - その他の制御文字 → 削除
    """
    if allow_newlines:
        text = value.replace("\r\n", "\n").replace("\r", "\n")
        text = text.replace("\t", " ")
        return _CONTROL_CHARS_RE.sub("", text)
    text = value.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return _CONTROL_CHARS_RE.sub("", text)


def _sanitize_instance(target: object) -> None:
    """1 レコード分の文字列カラムをすべて正規化する。"""
    mapper = getattr(type(target), "__mapper__", None)
    if mapper is None:
        return
    table_name = mapper.local_table.name if mapper.local_table is not None else ""
    for attr_name, attr in mapper.column_attrs.items():
        if not attr.columns:
            continue
        col = attr.columns[0]
        if not isinstance(col.type, (String, Text)):
            continue
        value = getattr(target, attr_name, None)
        if not isinstance(value, str):
            continue
        allow_nl = (table_name, col.name) in _MULTILINE_ALLOWED
        cleaned = sanitize_config_text(value, allow_newlines=allow_nl)
        if cleaned != value:
            log.warning(
                "設定に使えない文字を除去しました: %s.%s", table_name, col.name
            )
            setattr(target, attr_name, cleaned)


@event.listens_for(Base, "before_insert", propagate=True)
def _sanitize_before_insert(mapper, connection, target) -> None:  # type: ignore[no-untyped-def]
    _sanitize_instance(target)


@event.listens_for(Base, "before_update", propagate=True)
def _sanitize_before_update(mapper, connection, target) -> None:  # type: ignore[no-untyped-def]
    _sanitize_instance(target)


# ===========================================================================
# 列挙型 (画面 <select> 用) — Asterisk 側の値とそのまま一致させる
# ===========================================================================


class Codec(str, enum.Enum):
    """音声コーデック。"""

    ULAW = "ulaw"          # G.711 μ-law (北米/日本)
    ALAW = "alaw"          # G.711 A-law (欧州)
    G722 = "g722"          # HD voice
    G729 = "g729"          # 低帯域
    OPUS = "opus"
    GSM = "gsm"


class Transport(str, enum.Enum):
    """SIP トランスポート。"""

    UDP = "udp"
    TCP = "tcp"
    TLS = "tls"
    WS = "ws"
    WSS = "wss"


class DtmfMode(str, enum.Enum):
    """DTMF 通知方式。"""

    RFC4733 = "rfc4733"    # 既定 (旧 RFC2833)
    INBAND = "inband"
    INFO = "info"
    AUTO = "auto"


class NatMode(str, enum.Enum):
    """NAT/RTP 透過モード。"""

    NO = "no"
    FORCE_RPORT = "force_rport"
    COMEDIA = "comedia"
    FORCE_RPORT_COMEDIA = "force_rport,comedia"
    AUTO_FORCE_RPORT = "auto_force_rport"
    AUTO_COMEDIA = "auto_comedia"


class DirectMedia(str, enum.Enum):
    YES = "yes"
    NO = "no"
    NONAT = "nonat"
    UPDATE = "update"


class TrunkType(str, enum.Enum):
    """SIP トランクの認証/接続方式。"""

    REGISTER = "register"              # ITSP に REGISTER して使う一般的な構成
    PEER = "peer"                      # IP-PEER (固定 IP / 認証なし)
    IAX2 = "iax2"                      # 旧 IAX2
    HIKARI_OFFICE_A_HGW = "hikari_office_a_hgw"
    """NTT ひかり電話オフィスA を HGW (PR-500/600 系) 経由で収容。
       - HGW LAN 側 IP に対して REGISTER
       - 認証: HGW 上で設定したユーザID (4桁) + パスワード
       - From ヘッダのユーザ部は HGW 内線番号 (1〜2桁、契約番号ではない)
       - 着信は契約電話番号宛 INVITE が来るため、To ヘッダで DID 振り分け
       - 同一 LAN 想定で NAT 越え不要
    """
    HIKARI_OFFICE_A_OG = "hikari_office_a_og"
    """NTT ひかり電話オフィスA を OG (OG410Xa/420Xa/820Xa 系) 経由で収容。
       - 基本的な SIP の流れは HGW と同じ
       - 内線番号は通常 2 桁 (10〜99)
       - MAC アドレス認証 (パスワード空) の運用もある
       - OG 配下では DHCP で経路情報が降ってこない構成があるため
         disable_rport の指定が必要なケースもある
    """


class RouteAction(str, enum.Enum):
    """着信ルートの転送先種別。"""

    EXTENSION = "extension"
    RING_GROUP = "ring_group"
    QUEUE = "queue"
    IVR = "ivr"
    VOICEMAIL = "voicemail"
    FAX = "fax"
    HANGUP = "hangup"
    TIME_CONDITION = "time_condition"


class RingStrategy(str, enum.Enum):
    """リンググループの呼出戦略。"""

    RINGALL = "ringall"          # 全員同時
    HUNT = "hunt"                # 順番に
    MEMORYHUNT = "memoryhunt"    # 直前の続きから
    RANDOM = "random"
    LEASTRECENT = "leastrecent"
    FEWESTCALLS = "fewestcalls"


class QueueStrategy(str, enum.Enum):
    """キュー (app_queue) の呼出戦略。Asterisk 公式の queues.conf
    strategy= に指定できる値そのもの (roundrobin は非推奨のため含めない)。"""

    RINGALL = "ringall"          # 全員同時に鳴らす
    LEASTRECENT = "leastrecent"  # 直近の応答が一番古いメンバーから
    FEWESTCALLS = "fewestcalls"  # 応対数が少ないメンバーから
    RANDOM = "random"
    RRMEMORY = "rrmemory"        # 順番に (前回の続きから記憶)
    LINEAR = "linear"            # 常に登録順の先頭から順番に
    WRANDOM = "wrandom"          # 重み付きランダム (penalty を考慮)


class AudioCategory(str, enum.Enum):
    """音源の用途。出力先サブディレクトリと用途別フィルタに使う。"""

    MOH = "moh"                  # 保留音 (Music on Hold)
    PARK = "park"                # パーク呼出音
    IVR = "ivr"                  # IVR ガイダンス音声
    VOICEMAIL = "voicemail"      # ボイスメール挨拶
    CUSTOM = "custom"            # その他 (Playback で任意に使う)


class MohSortMode(str, enum.Enum):
    """保留音クラスの再生順。"""

    ALPHA = "alpha"              # ファイル名アルファベット順
    RANDOM = "random"
    RANDSTART = "randstart"      # ランダムに開始してアルファベット順
    LISTFILES = "listfiles"      # 並び順を厳密に指定 (DB 順序)


class IvrAction(str, enum.Enum):
    """IVR メニューでのキー入力に対する動作。"""

    EXTENSION = "extension"
    RING_GROUP = "ring_group"
    QUEUE = "queue"
    IVR = "ivr"                  # 別の IVR へ
    VOICEMAIL = "voicemail"
    HANGUP = "hangup"
    PLAYBACK_HANGUP = "playback_hangup"  # 案内のみ再生して切断
