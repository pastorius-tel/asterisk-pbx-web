"""DB の内容から Asterisk の設定ファイルを生成する。

出力先: settings.asterisk_config_dir (例: /etc/asterisk/managed/)

生成ファイル一覧:
  ├── asterisk.conf           (Asterisk のディレクトリ設定とランタイム)
  ├── modules.conf            (ロードするモジュール)
  ├── logger.conf             (ログ出力設定)
  ├── rtp.conf                (RTP ポート範囲)
  ├── manager.conf            (AMI 設定)
  ├── pjsip.conf              (トランスポート + トランク + 内線)
  ├── extensions.conf         (ダイヤルプラン: 発信/着信/IVR/パーク等)
  ├── musiconhold.conf        (保留音クラス)
  ├── parking.conf            (コールパーク)
  ├── queues.conf             (キュー)
  └── voicemail.conf          (ボイスメール)

Asterisk を素インストール後、以下を実行すれば全 conf がこの管理下に置かれる:

    sudo systemctl stop asterisk
    sudo mv /etc/asterisk /etc/asterisk.orig.bak
    sudo ln -s <asterisk_config_dir> /etc/asterisk
    sudo systemctl start asterisk

または、既存の /etc/asterisk を残したまま managed/ にだけ置いて、
本体側から #include <managed/*.conf> する運用でも可。

すべて UTF-8。Asterisk の include には絶対パスも使えるが、
ここでは Asterisk のデフォルトディレクトリ配置を踏襲する。
"""

from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.hooks import get_hook_token
from app.models import (
    AppSettings,
    AudioFile,
    BlockedNumber,
    CalendarException,
    CompanyHoliday,
    DayPattern,
    Extension,
    FaxConfig,
    InboundRoute,
    Ivr,
    MohClass,
    NationalHoliday,
    OutboundRoute,
    ParkingLot,
    PhoneBookEntry,
    Queue,
    RingGroup,
    TimeCondition,
    Trunk,
)
from app.services.audio_convert import asterisk_sound_reference

# ===========================================================================
# 設定ファイルの「構造」を壊さないための正規化 (多層防御の 2 段目)
# ===========================================================================
#
# 1 段目は app/models/base.py の DB 書き込み時フィルタで、改行と制御文字を
# 除去している (これで「任意の行を注入される」問題は塞がれる)。
# ただし改行以外にも、Asterisk の設定ファイルでは意味を持つ文字がある:
#
#   ;   → その行の以降がコメント扱いになり、後続の引数が消える
#   ,   → Goto(ctx,exten,pri) の引数区切りがズレる
#   ] [ → コンテキスト見出し [name] が壊れる
#   空白 → コンテキスト名に入ると別名のコンテキストになってしまう
#
# これらは「乗っ取り」ではなく「設定が静かに壊れる」種類の事故だが、
# 電話が繋がらなくなるのでむしろ実害は大きい。コンテキスト名やダイヤル
# 先として使う値は、ここで安全な文字だけに絞る。

_IDENT_SAFE = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)
_DIAL_SAFE = frozenset("0123456789*#+")
# Asterisk のエクステンションパターンで使える文字 (_ X Z N . ! [0-9-])
_PATTERN_SAFE = frozenset("0123456789XZNxzn.!_[]-*#+")


def _ident(value: str | None, fallback: str = "unnamed") -> str:
    """コンテキスト名・セクション名として安全な文字列にする。

    使えない文字は '_' に置換する (削除すると別々の名前が衝突しうるため)。
    """
    text = (value or "").strip()
    if not text:
        return fallback
    out = "".join(c if c in _IDENT_SAFE else "_" for c in text)
    return out or fallback


def _dial(value: str | None) -> str:
    """ダイヤル先の番号として安全な文字だけを残す。

    数字と電話で実際に送出できる記号 (* # +) のみ。ハイフンや括弧、
    全角文字が混ざっていても黙って落とす (電話帳の一括入力対策)。
    """
    return "".join(c for c in (value or "") if c in _DIAL_SAFE)


def _pattern(value: str | None) -> str:
    """exten => のパターンとして安全な文字だけを残す。"""
    return "".join(c for c in (value or "") if c in _PATTERN_SAFE)


def _is_valid_exten_pattern(pat: str) -> bool:
    """Asterisk のエクステンションパターンとして成立しているか検査する。

    文字種だけでなく [ ] の対応と範囲指定の向きまで見る。
    '_[9-1]X' のような逆順の範囲は Asterisk がコンテキストごと読み込み
    に失敗することがあり、そうなると「その conf 全体が無効」という
    最悪の壊れ方をするため、生成前に弾く。
    """
    depth = 0
    i = 0
    while i < len(pat):
        ch = pat[i]
        if ch == "[":
            if depth:  # ネストは不可
                return False
            close = pat.find("]", i + 1)
            if close == -1:
                return False
            inner = pat[i + 1 : close]
            if not inner:
                return False
            # 範囲指定 (例 2-9) の向きを検査
            j = 0
            while j < len(inner):
                if j + 2 < len(inner) and inner[j + 1] == "-":
                    lo, hi = inner[j], inner[j + 2]
                    if not (lo.isdigit() and hi.isdigit()) or lo > hi:
                        return False
                    j += 3
                else:
                    if not inner[j].isdigit():
                        return False
                    j += 1
            i = close + 1
            continue
        if ch == "]":
            return False
        i += 1
    return depth == 0


def _text(value: str | None, limit: int = 120) -> str:
    """NoOp() やコメントに出す説明文を安全にする。

    ';' はコメント開始、')' は関数の閉じ括弧として解釈されるため除去する。
    """
    text = (value or "").strip()
    for ch in (";", "(", ")", ","):
        text = text.replace(ch, " ")
    text = " ".join(text.split())
    return text[:limit]


async def _get_system_language(db: AsyncSession) -> str:
    """AppSettings (DB, 全ワーカー共通) から現在の system_language を取得。

    行がまだ無ければ config.py の既定値 (初回シード用) にフォールバック。
    """
    row = (await db.scalars(select(AppSettings).limit(1))).first()
    if row is None:
        return settings.system_language
    return row.system_language


async def _get_reserve_leading_01(db: AsyncSession) -> bool:
    """AppSettings (DB, 全ワーカー共通) から現在のダイヤルポリシーを取得。"""
    row = (await db.scalars(select(AppSettings).limit(1))).first()
    if row is None:
        return settings.reserve_leading_01_for_outbound
    return row.reserve_leading_01_for_outbound


async def _get_sip_port(db: AsyncSession) -> int:
    """AppSettings (DB, 全ワーカー共通) から現在の SIP 待受ポートを取得。"""
    row = (await db.scalars(select(AppSettings).limit(1))).first()
    if row is None:
        return 5060
    return row.sip_port


async def _get_internal_ring_seconds(db: AsyncSession) -> int:
    """AppSettings (DB, 全ワーカー共通) から内線同士の鳴音時間 (秒) を取得。

    行が無い場合、または (SQLite の ALTER TABLE ADD COLUMN では
    既存行に Python 側の default= が反映されないため) 値が NULL の
    場合は、既定値 30 秒にフォールバックする。
    """
    row = (await db.scalars(select(AppSettings).limit(1))).first()
    if row is None or not row.internal_ring_seconds:
        return 30
    return row.internal_ring_seconds


def _voicemail_hangup_notify_context(hook_token: str) -> str:
    """留守番電話の録音完了通知用の共通ハングアップハンドラーコンテキスト。

    経緯 (重要): 当初は VoiceMail() の「次の行」で curl 通知を実行して
    いたが、これは Asterisk の一般的な仕様上、発信側 (メッセージを
    残した相手) が録音後にそのまま電話を切ると、次のダイヤルプラン
    行に進む前にチャンネル自体が失われ、通知が一切実行されない
    (Asterisk コミュニティでも "MOST PEOPLE IN THE WORLD HANG UP"
    と指摘される、まさに最も一般的な留守電の終わり方で毎回失敗する
    重大な欠陥だった)。

    Asterisk の「ハングアップハンドラー」 (CHANNEL(hangup_handler_push))
    は、通常のダイヤルプラン実行とは独立して「チャンネルが実際に
    切断される瞬間」に確実に実行される仕組みで、相手がどのように
    電話を切っても (# を押す・タイムアウト・そのまま切る) 確実に
    発火する。VoiceMail() が設定する ${VMSTATUS} も、切断直前まで
    同一チャンネル上の変数として参照できるため、ここで判定できる。
    """
    hook_url = (
        f"http://127.0.0.1:{settings.port}/voicemail/hook/received"
        f"?token={hook_token}"
    )
    return (
        "\n[voicemail-hangup-notify]\n"
        "exten => s,1,NoOp(Voicemail hangup handler: "
        "VMSTATUS=${VMSTATUS} mailbox=${VM_HOOK_MAILBOX})\n"
        ' same => n,GotoIf($["${VMSTATUS}" = "SUCCESS"]?notify:skip)\n'
        " same => n(notify),System(curl -s -m 20 -G "
        f"'{hook_url}' "
        "--data-urlencode 'mailbox=${VM_HOOK_MAILBOX}' "
        "--data-urlencode 'vmstatus=${VMSTATUS}')\n"
        " same => n,Return()\n"
        " same => n(skip),Return()\n\n"
    )


def _calllog_hangup_handler_context(hook_token: str) -> str:
    """発着信履歴を記録するハングアップハンドラー [calllog-notify]。

    通話が切れる瞬間に本ツールへ通知して、発着信履歴を 1 件記録する。
    ハングアップハンドラーなので、相手がどう電話を切っても確実に
    発火する (途中で Hangup() した場合も含む)。

    ${CDR(...)} から通話時間や応答状況を取れるため、CSV (Master.csv) を
    解析しなくても必要な情報が揃う。着信先 (内線 / 留守電 / FAX 等) は
    ダイヤルプラン側で __CL_DEST_* にセットしておく。
    """
    hook_url = (
        f"http://127.0.0.1:{settings.port}/call-logs/hook/record"
        f"?token={hook_token}"
    )
    return (
        "\n[calllog-notify]\n"
        "exten => s,1,NoOp(CallLog: ${CL_DIRECTION} peer=${CL_PEER} "
        "dest=${CL_DEST_TYPE}:${CL_DEST_VALUE})\n"
        # 方向が未設定のチャネル (内部処理用など) は記録しない
        ' same => n,GotoIf($["${CL_DIRECTION}" = ""]?skip)\n'
        " same => n,System(curl -s -m 20 -G "
        f"'{hook_url}' "
        "--data-urlencode 'direction=${CL_DIRECTION}' "
        "--data-urlencode 'peer=${CL_PEER}' "
        "--data-urlencode 'peer_name=${CL_PEER_NAME}' "
        "--data-urlencode 'did=${CL_DID}' "
        "--data-urlencode 'dest_type=${CL_DEST_TYPE}' "
        "--data-urlencode 'dest_value=${CL_DEST_VALUE}' "
        "--data-urlencode 'dest_label=${CL_DEST_LABEL}' "
        "--data-urlencode 'trunk=${CL_TRUNK}' "
        "--data-urlencode 'blocked=${CL_BLOCKED}' "
        "--data-urlencode 'disposition=${CDR(disposition)}' "
        "--data-urlencode 'duration=${CDR(duration)}' "
        "--data-urlencode 'billsec=${CDR(billsec)}' "
        "--data-urlencode 'uniqueid=${UNIQUEID}' "
        "--data-urlencode 'start=${CDR(start)}')\n"
        " same => n,Return()\n"
        " same => n(skip),Return()\n\n"
    )


def _calllog_setup_actions(
    direction: str, peer_expr: str, dest_type: str = "", dest_value: str = "",
    dest_label: str = "", did_expr: str = "", trunk: str = "",
) -> list[str]:
    """発着信履歴の記録を仕込む action のリスト。

    通話の入口 (着信なら着信ルート、発信なら発信ルート) で呼び、
    チャネル変数に情報をセットしてハングアップハンドラーを登録する。
    変数は __ 付き (継承する) にして、Goto で別コンテキストへ移っても
    保持されるようにする。
    """
    acts = [
        f"Set(__CL_DIRECTION={direction})",
        f"Set(__CL_PEER={peer_expr})",
        "Set(__CL_PEER_NAME=${CALLERID(name)})",
    ]
    if did_expr:
        acts.append(f"Set(__CL_DID={did_expr})")
    if trunk:
        acts.append(f"Set(__CL_TRUNK={trunk})")
    if dest_type:
        acts.append(f"Set(__CL_DEST_TYPE={dest_type})")
    if dest_value:
        acts.append(f"Set(__CL_DEST_VALUE={dest_value})")
    if dest_label:
        # ラベルにカンマや ) が入るとダイヤルプランが壊れるため除去する
        safe = dest_label.replace(",", " ").replace(")", "").replace("(", "")
        acts.append(f"Set(__CL_DEST_LABEL={safe})")
    acts.append("Set(CHANNEL(hangup_handler_push)=calllog-notify,s,1)")
    return acts


def _calllog_dest_actions(dest_type: str, dest_value: str, dest_label: str = "") -> list[str]:
    """着信先が確定した時点で、その情報だけを上書きする action。

    着信ルート → 時間条件 → 留守番電話、のように転送が続く場合、
    最終的にどこへ着信したかを記録できるようにする。
    """
    acts = [f"Set(__CL_DEST_TYPE={dest_type})"]
    if dest_value:
        acts.append(f"Set(__CL_DEST_VALUE={dest_value})")
    if dest_label:
        safe = dest_label.replace(",", " ").replace(")", "").replace("(", "")
        acts.append(f"Set(__CL_DEST_LABEL={safe})")
    return acts


def _voicemail_hook_setup_actions(mailbox_expr: str) -> list[str]:
    """VoiceMail() 実行「前」に置く、ハングアップハンドラー登録の
    action 文字列 2つ ("same => n," を含まない)。actions.extend() で
    使う (actions リストへの追加は VoiceMail() より前になる位置で)。

    __VM_HOOK_MAILBOX は先頭が二重アンダースコアの変数 — Asterisk の
    変数スコープ規則で「Gosub 先に無限に継承される」ため、ハングアップ
    ハンドラー (内部的に Gosub と同じ仕組みで実行される) の中からも
    確実に参照できる (単なる Set(VAR=...) だとローカル変数扱いになり、
    ハンドラー側から見えない可能性がある)。

    mailbox_expr は内線番号を表す式。固定値 ("201") でも
    ダイヤルプラン変数 ("${EXTEN}") でもよい。
    """
    return [
        f"Set(__VM_HOOK_MAILBOX={mailbox_expr})",
        "Set(CHANNEL(hangup_handler_push)=voicemail-hangup-notify,s,1)",
        # 発着信履歴: 留守番電話に入ったことを着信先として記録する。
        # 内線を呼んで応答が無かった場合も、最終的な着信先は
        # 「留守番電話」なので、ここで上書きする。
        "Set(__CL_DEST_TYPE=voicemail)",
        f"Set(__CL_DEST_VALUE={mailbox_expr})",
        f"Set(__CL_DEST_LABEL=留守番電話 {mailbox_expr})",
    ]


def _voicemail_hook_setup_lines(mailbox_expr: str) -> str:
    """_voicemail_hook_setup_actions() を "same => n,...\\n" 形式の
    完成した行にしたもの (out.write() に直接渡す用途。VoiceMail() の
    行を書く前に呼ぶこと)。"""
    return "".join(
        f" same => n,{a}\n" for a in _voicemail_hook_setup_actions(mailbox_expr)
    )


def _render_nuisance_lookup_contexts(blocked_numbers: list[BlockedNumber]) -> str:
    """迷惑電話ブロックリスト・国際着信判定用の共通 Gosub サブルーチンを
    生成する。[from-trunk-*] の入口から Gosub(...,${CALLERID(num)},1) で
    呼ばれ、判定結果を Return() 経由の GOSUB_RETVAL で返す。

    - [nuisance-blocklist-check]: 一致すれば "hangup" または
      "voicemail:<内線>" を返す。一致しなければ空文字列。
    - [nuisance-intl-check]: 発信者番号が日本の標準的な国内番号
      パターン (0 で始まる携帯電話・固定電話・IP電話・フリーダイヤル等)
      に一致すれば "0" (国内)、一致しなければ "1" (海外/形式不明の
      可能性) を返す。個々の国番号や "+"/"010" プレフィックスの正確な
      形式はトランク/キャリアによって異なり確実に特定できないため、
      消去法で判定している (詳細は README 参照)。
    """
    out = io.StringIO()
    out.write("\n[nuisance-blocklist-check]\n")
    for bn in blocked_numbers:
        if not bn.enabled:
            continue
        # 迷惑電話パターンは exten => の左辺そのものになる。
        # 不正な文字が混ざると context 全体がロードされず、
        # 「迷惑電話ブロックだけでなく着信全体が止まる」事故になるため、
        # 使える文字だけに絞ったうえで、壊れた括弧の組は捨てる。
        pat = _pattern(bn.pattern)
        if not pat or not _is_valid_exten_pattern(pat):
            continue
        target = _dial(bn.voicemail_target)
        if bn.action != "hangup" and not target:
            continue
        ret = "hangup" if bn.action == "hangup" else f"voicemail:{target}"
        out.write(f"exten => {pat},1,Return({ret})\n")
    out.write("exten => _X!,1,Return()\n")
    out.write("exten => s,1,Return()\n")

    out.write("\n[nuisance-intl-check]\n")
    out.write("exten => _0[789]0XXXXXXXX,1,Return(0)\n")  # 携帯電話
    out.write("exten => _0X.,1,Return(0)\n")  # 固定電話/IP電話(050)/フリーダイヤル等、0始まり全般
    out.write("exten => _X!,1,Return(1)\n")  # それ以外 (0始まりでない) = 海外/形式不明
    out.write("exten => s,1,Return(0)\n")  # 非通知は警告対象外 (国内相当として扱う)
    return out.getvalue()


def _nuisance_check_block(
    app_settings: AppSettings, has_blocklist: bool,
    context_name: str, label_suffix: str = "",
) -> str:
    """トランク着信の入口 ([from-trunk-*] の DID 振り分け前、または各
    着信ルートの入口) に挿入する、迷惑電話チェックのダイヤルプラン
    断片。

    ブロックリストに一致、または (設定されていれば) 国際着信と判定
    された場合、その場で action (即切断/留守番電話) を実行して
    Hangup() する。該当しなければ何もせず後続 (通常の DID 振り分け)
    へ進む。

    context_name: この断片が挿入されるコンテキスト名。Goto()/GotoIf()
    のジャンプ先ラベルを必ず "context,extension,priority" の完全な
    3要素形式で指定するために使う。

    【重要な注意】 GotoIf(cond?label) の label を "nuisance_hit" の
    ような単一の単語だけにすると、Asterisk 公式ドキュメントの定義通り
    「[[context,]extension,]priority のうち priority だけを指定した」
    と解釈され、「現在の extension 内の named priority としての
    nuisance_hit」を探しにいってしまう (別の extension 宣言へは
    ジャンプしない)。実機で "No such label 'nuisance_hit' in
    extension '3'" のエラーとなり、迷惑電話判定が機能しない不具合と
    して実際に発生した。そのため、ここでは必ず
    "コンテキスト名,ラベル名,1" という完全な3要素形式で指定する。

    label_suffix: この断片が生成する exten ラベル
    (nuisance_hit/nuisance_hangup/nuisance_skip) を一意にするための
    接尾辞。同一コンテキストにこの断片を複数回挿入する場合
    (例: 着信ルートごとに 1 つずつ) は、route.id 等の一意な値を渡す
    こと。省略時 (トランクの入口に 1 回だけ挿入する場合) は不要。
    """
    intl_action = app_settings.international_call_action
    if not has_blocklist and intl_action == "none":
        return ""

    hit_label = f"nuisance_hit{label_suffix}"
    hangup_label = f"nuisance_hangup{label_suffix}"
    skip_label = f"nuisance_skip{label_suffix}"
    # Goto()/GotoIf() のジャンプ先は必ずこの完全形式 (コンテキスト名を
    # 明示) で組み立てる。単一ラベルの曖昧な解釈を避けるため。
    hit_dest = f"{context_name},{hit_label},1"
    hangup_dest = f"{context_name},{hangup_label},1"
    skip_dest = f"{context_name},{skip_label},1"

    out = io.StringIO()
    out.write(" same => n,Set(__NUISANCE_ACTION=)\n")
    out.write(f' same => n,GotoIf($["${{CALLERID(num)}}" = ""]?{skip_dest})\n')
    if has_blocklist:
        out.write(" same => n,Gosub(nuisance-blocklist-check,${CALLERID(num)},1)\n")
        out.write(" same => n,Set(__NUISANCE_ACTION=${GOSUB_RETVAL})\n")
        out.write(f' same => n,GotoIf($["${{NUISANCE_ACTION}}" != ""]?{hit_dest})\n')
    if intl_action != "none":
        out.write(" same => n,Gosub(nuisance-intl-check,${CALLERID(num)},1)\n")
        out.write(f' same => n,GotoIf($["${{GOSUB_RETVAL}}" != "1"]?{skip_dest})\n')
        if intl_action == "hangup":
            out.write(" same => n,Set(__NUISANCE_ACTION=hangup)\n")
        else:
            out.write(
                f" same => n,Set(__NUISANCE_ACTION=voicemail:{app_settings.international_voicemail_target})\n"
            )
        # 国際判定でヒットした場合はここに来るので、明示的に hit_dest へ
        # 飛ぶ (Asterisk は同一コンテキスト内でも、別の exten 宣言へ暗黙に
        # フォールスルーはしないため、必ず Goto で移動させる必要がある)。
        out.write(f" same => n,Goto({hit_dest})\n")
    # ここに到達するのは「ブロックリストのみ有効でヒットしなかった」場合。
    # 安全側で必ず skip (通常経路) へ進める。
    out.write(f" same => n,Goto({skip_dest})\n")
    out.write(f"exten => {hit_label},1,NoOp(迷惑電話判定によりブロック: ${{CALLERID(num)}} -> ${{NUISANCE_ACTION}})\n")
    out.write(f' same => n,GotoIf($["${{NUISANCE_ACTION}}" = "hangup"]?{hangup_dest})\n')
    out.write(_voicemail_hook_setup_lines("${NUISANCE_ACTION:10}"))
    out.write(" same => n,VoiceMail(${NUISANCE_ACTION:10}@default)\n")
    out.write(" same => n,Hangup()\n")
    out.write(f"exten => {hangup_label},1,Hangup()\n")
    out.write(f"exten => {skip_label},1,NoOp()\n")
    return out.getvalue()


# ===========================================================================
# 共通ヘッダ
# ===========================================================================

_HEADER = (
    "; AUTO-GENERATED -- このファイルは Asterisk PBX Web Management から\n"
    "; 自動生成されています。直接編集しないでください。\n"
    "; 生成元: app/services/asterisk_config.py\n\n"
)


# ===========================================================================
# pjsip.conf: トランスポート → トランク → 内線
# ===========================================================================


def _render_pjsip_transports(sip_port: int) -> str:
    """グローバル PJSIP トランスポート定義。

    本ツールではデフォルトで UDP のみ生成する。
    TCP/TLS を有効化する場合は asterisk_config.py を改変してセクションを追加。

    sip_port: AppSettings (DB) から読んだ現在の待受ポート (既定 5060)。
    「システム」画面から変更可能。

    注: 同一ポートで UDP と TCP を両方バインドすると、一部の環境で
    トランスポート読込が失敗するケースが報告されているため UDP 単独にする。
    """
    return f""";=== グローバルトランスポート定義 ===
[transport-udp]
type=transport
protocol=udp
bind=0.0.0.0:{sip_port}

"""


#--------------------------------------------------------------------------
# pjsip.conf 生成 (内線/トランク用 — 既存)
#--------------------------------------------------------------------------


def _render_pjsip_extension(ext: Extension, system_language: str) -> str:
    """1 内線分の pjsip セクションを生成。

    system_language: AppSettings (DB) から読んだ現在の言語設定。
    複数 gunicorn ワーカー間で一貫させるため、呼び出し元で DB から
    取得した値を渡してもらう (グローバル settings は使わない)。
    """
    codecs = ext.codec_primary
    if ext.codec_secondary:
        codecs = f"{ext.codec_primary},{ext.codec_secondary}"

    nat_lines = []
    if "force_rport" in ext.nat_mode:
        nat_lines.append("rtp_symmetric=yes")
        nat_lines.append("force_rport=yes")
    if "comedia" in ext.nat_mode:
        nat_lines.append("rewrite_contact=yes")

    # 日本語音声プロンプト (system_language が 'ja' 等の場合)
    lang_line = f"language={system_language}\n" if system_language else ""

    return f"""
;=== 内線 {ext.extension} : {_text(ext.display_name)} ===
[{ext.extension}]
type=endpoint
context={ext.context}
disallow=all
allow={codecs}
auth={ext.extension}-auth
aors={ext.extension}
direct_media={ext.direct_media}
dtmf_mode={ext.dtmf_mode}
callerid="{ext.display_name}" <{ext.extension}>
{lang_line}{chr(10).join(nat_lines)}

[{ext.extension}-auth]
type=auth
auth_type=userpass
username={ext.extension}
password={ext.secret}

[{ext.extension}]
type=aor
max_contacts={ext.max_contacts}
qualify_frequency=60
"""


def _render_pjsip_trunk(t: Trunk) -> str:
    """1 トランク分の pjsip セクション。

    ひかり電話オフィスA (HGW/OG 経由) は専用処理に分岐する。
    """
    if t.trunk_type in ("hikari_office_a_hgw", "hikari_office_a_og"):
        return _render_pjsip_hikari(t)

    codecs = t.codec_primary
    if t.codec_secondary:
        codecs = f"{t.codec_primary},{t.codec_secondary}"

    blocks: list[str] = [f";=== トランク {_text(t.name)} ({_text(t.host)}:{t.port})=="]

    extra_lines = ""
    if t.trust_id_inbound:
        extra_lines += "trust_id_inbound=yes\n"
    if t.send_pai:
        extra_lines += "send_pai=yes\nrpid_immediate=yes\n"

    blocks.append(
        f"""[{t.name}]
type=endpoint
context=from-trunk-{_ident(t.name)}
transport=transport-{t.transport}
disallow=all
allow={codecs}
outbound_auth={t.name}-auth
aors={t.name}
from_user={t.from_user or t.username or ''}
from_domain={t.from_domain or t.host}
direct_media=no
dtmf_mode={t.dtmf_mode}
fax_detect=yes
fax_detect_timeout=20
t38_udptl=yes
t38_udptl_ec=redundancy
t38_udptl_nat=no
t38_udptl_maxdatagram=400
{extra_lines}"""
    )

    if t.trunk_type == "register" and t.username and t.secret:
        blocks.append(
            f"""[{t.name}-auth]
type=auth
auth_type=userpass
username={t.auth_username or t.username}
password={t.secret}
"""
        )

    blocks.append(
        f"""[{t.name}]
type=aor
contact=sip:{t.host}:{t.port}
qualify_frequency={'60' if t.qualify else '0'}
"""
    )

    if t.trunk_type == "register" and t.username:
        blocks.append(
            f"""[{t.name}-reg]
type=registration
outbound_auth={t.name}-auth
server_uri=sip:{t.host}:{t.port}
client_uri=sip:{t.username}@{t.host}
retry_interval=60
"""
        )

    return "\n".join(blocks)


def _render_pjsip_hikari(t: Trunk) -> str:
    """ひかり電話オフィスA (HGW / OG 経由) 専用の pjsip セクション。

    生成方針 (voip-info.jp / 妄想エンジン氏 / NTT 取扱説明書を参照):
      - REGISTER: client_uri = sip:<HGW内線番号>@<HGW IP>
                  server_uri = sip:<HGW IP>
      - auth:     username   = <HGW のユーザID 4桁>    (※内線番号とは別)
                  password   = <HGW のパスワード>
      - endpoint: from_user  = <HGW内線番号>            (契約電話番号ではない)
                  from_domain= <HGW IP>
                  direct_media=no, allow=ulaw, dtmf_mode は画面の設定値を使用
                  (外線からの IVR キー入力が効かない場合は inband や auto に
                   変更できるようにするため。README 参照)
      - aor:      contact    = sip:<HGW IP>
      - identify: match      = <HGW IP>
      - 同一 LAN 想定で rtp_symmetric / force_rport / rewrite_contact は明示しない
        (yes にすると Via に rport が入って発信時 400 Bad Request になる)
    """
    hgw_ext = t.hgw_extension_number or ""           # 例: "3" (HGW内線番号)
    auth_user = t.username or t.auth_username or ""  # 例: "0003" (ユーザID)
    auth_pass = t.secret or ""
    primary_did = t.outbound_caller_id or ""

    blocks: list[str] = [
        ";===================================================================",
        f"; ひかり電話オフィスA: {t.name}",
        f";   HGW/OG IP     = {t.host}",
        f";   HGW 内線番号  = {hgw_ext}",
        f";   認証ユーザID  = {auth_user}",
        f";   主番号 (表示) = {primary_did or '(未設定)'}",
        ";===================================================================",
    ]

    #--- registration---
    blocks.append(
        f"""[{t.name}-reg]
type=registration
transport=transport-{t.transport}
outbound_auth={t.name}-auth
server_uri=sip:{t.host}
client_uri=sip:{hgw_ext}@{t.host}
contact_user={hgw_ext}
retry_interval=60
forbidden_retry_interval=600
"""
    )

    #--- auth (MAC 認証時は省略)---
    if not t.use_mac_auth:
        blocks.append(
            f"""[{t.name}-auth]
type=auth
auth_type=userpass
username={auth_user}
password={auth_pass}
"""
        )

    #--- aor---
    blocks.append(
        f"""[{t.name}]
type=aor
contact=sip:{t.host}
qualify_frequency={'30' if t.qualify else '0'}
authenticate_qualify=no
"""
    )

    #--- endpoint---
    auth_line = "" if t.use_mac_auth else f"outbound_auth={t.name}-auth\n"
    # コーデックも画面の設定値を使う (ひかり電話は G.711 μ-law 必須のため
    # 既定は ulaw だが、DTMF の inband 検出等で構成を変えたい場合に
    # 画面から変更できるようにしておく)。
    hikari_codecs = t.codec_primary or "ulaw"
    if t.codec_secondary:
        hikari_codecs = f"{hikari_codecs},{t.codec_secondary}"
    blocks.append(
        f"""[{t.name}]
type=endpoint
transport=transport-{t.transport}
context=from-trunk-{_ident(t.name)}
disallow=all
allow={hikari_codecs}
{auth_line}aors={t.name}
direct_media=no
dtmf_mode={t.dtmf_mode}
from_user={hgw_ext}
from_domain={t.host}
fax_detect=yes
fax_detect_timeout=20
t38_udptl=yes
t38_udptl_ec=redundancy
t38_udptl_nat=no
t38_udptl_maxdatagram=400
"""
    )

    #--- identify (HGW からの着信を当該 endpoint に紐付け)---
    blocks.append(
        f"""[{t.name}]
type=identify
endpoint={t.name}
match={t.host}
"""
    )

    return "\n".join(blocks)


def _has_hikari_trunk_with_disable_rport(trunks: list[Trunk]) -> bool:
    """[system] disable_rport=yes が必要な構成があるか。"""
    return any(
        t.trunk_type in ("hikari_office_a_hgw", "hikari_office_a_og") and t.disable_rport
        for t in trunks
    )


async def render_pjsip_conf(db: AsyncSession) -> str:
    extensions = (await db.scalars(select(Extension).where(Extension.enabled.is_(True)))).all()
    trunks = list((await db.scalars(select(Trunk).where(Trunk.enabled.is_(True)))).all())
    system_language = await _get_system_language(db)
    sip_port = await _get_sip_port(db)

    out = io.StringIO()
    out.write(_HEADER)

    # [global] を明示。IP で識別するトランク (ひかり電話 HGW/OG 等) と
    # ユーザー名/パスワードで識別する内線が混在するため、
    # endpoint_identifier_order を明示しておく (未指定だと Asterisk の
    # ビルド時デフォルトに依存し、バージョンや環境によって挙動が
    # 変わりうるため)。
    out.write(
        ";=== グローバル設定 ===\n"
        "[global]\n"
        "type=global\n"
        "endpoint_identifier_order=ip,username\n"
        "max_forwards=70\n"
        "user_agent=Asterisk PBX Web Management\n\n"
    )

    # [system] disable_rport=yes が必要な構成があれば先頭で宣言
    # (ひかり電話オフィスA + OG 配下 や 直収構成で必要なケースあり)
    if _has_hikari_trunk_with_disable_rport(trunks):
        out.write(
            "; ひかり電話 OG 配下 / 直収構成のため Via ヘッダの rport を無効化\n"
            "[system]\n"
            "type=system\n"
            "disable_rport=yes\n\n"
        )

    # グローバルトランスポート (これがないと transport=transport-udp が解決できない)
    out.write(_render_pjsip_transports(sip_port))

    for t in trunks:
        out.write(_render_pjsip_trunk(t))
        out.write("\n")

    for e in extensions:
        out.write(_render_pjsip_extension(e, system_language))

    return out.getvalue()


#--------------------------------------------------------------------------
# extensions.conf (ダイヤルプラン) 生成
#--------------------------------------------------------------------------


def _outbound_route_to_dialplan(route: OutboundRoute, trunk_name: str) -> str:
    """1 つの発信ルートを extensions.conf の行に変換。"""
    pattern = route.pattern
    if not pattern.startswith("_"):
        pattern = "_" + pattern

    # Goto 先の番号操作
    target = "${EXTEN}"
    if route.prefix_strip:
        target = f"${{EXTEN:{route.prefix_strip}}}"
    if route.prefix_prepend:
        target = f"{route.prefix_prepend}{target}"

    cid_line = ""
    if route.cid_override:
        cid_line = f"\n exten => {pattern},n,Set(CALLERID(num)={route.cid_override})"

    # 発着信履歴: 発信 (ダイヤルした番号を相手先として記録する)
    cl = "".join(
        f"\n same => n,{a}"
        for a in _calllog_setup_actions(
            "out", "${EXTEN}", dest_type="trunk", dest_value=trunk_name,
            dest_label=f"外線発信 {route.name}", trunk=trunk_name,
        )
    )
    return f"""
;=== 発信ルート: {_text(route.name)} (priority={route.priority})==
exten => {pattern},1,NoOp(OUTBOUND {_text(route.name)}){cid_line}{cl}
 same => n,Dial(PJSIP/{target}@{trunk_name},60,TtkK)
 same => n,Hangup()
"""


def _inbound_route_to_dialplan(
    route: InboundRoute,
    system_language: str = "",
    app_settings: AppSettings | None = None,
    has_blocklist: bool = False,
    context_name: str = "",
    vm_only: set[str] | None = None,
    no_answer_exts: dict[str, list[str]] | None = None,
) -> str:
    did = route.did_number or "_X."
    vm_only = vm_only or set()
    no_answer_exts = no_answer_exts or {}

    # 転送先タイプから Dial 文を組み立て
    dest = route.destination_type
    val = route.destination_value
    actions: list[str] = []
    # 発着信履歴: この着信ルートで確定した「かかってきた番号」と
    # 「着信先」を記録する (後段でさらに転送されれば、そちらで上書き)
    if did != "_X.":
        actions.append(f"Set(__CL_DID={did})")
    actions.extend(_calllog_dest_actions(dest, val or "", f"{dest} {val or ''}".strip()))

    if dest == "extension":
        actions.append(
            _dial_or_voicemail_only(val, route.ring_seconds or 30, vm_only)
        )
        na_type = route.no_answer_type
        na_val = route.no_answer_value
        if na_type == "voicemail" and na_val:
            if na_val in vm_only:
                actions.append(f"Goto(from-internal,{na_val},1)")
            else:
                actions.extend(_voicemail_hook_setup_actions(na_val))
                actions.append(f"VoiceMail({na_val}@default,u)")
        elif na_type == "extension" and na_val:
            actions.append(_dial_or_voicemail_only(na_val, 30, vm_only))
        elif na_type == "hangup":
            actions.append("Hangup()")
        elif not na_type and val in no_answer_exts:
            # 着信ルート側に応答なし設定が無く、転送先の内線に個別設定が
            # ある場合は、内線側の設定 (案内音声 + その後の動作) を使う。
            # from-internal の個別 exten は Dial から始まるため、ここでは
            # 応答なし後の処理だけを直接展開する。
            actions.extend(no_answer_exts[val])
    elif dest == "ring_group":
        actions.append(f"Goto(ring-group-{_ident(val)},s,1)")
    elif dest == "queue":
        actions.append(f"Goto(queue-{_ident(val)},s,1)")
    elif dest == "voicemail":
        if val in vm_only:
            # 留守番電話専用内線なら応答メッセージの再生から通す
            actions.append(f"Goto(from-internal,{val},1)")
        else:
            actions.extend(_voicemail_hook_setup_actions(val))
            actions.append(f"VoiceMail({val}@default)")
    elif dest == "ivr":
        actions.append(f"Goto(ivr-{_ident(val)},s,1)")
    elif dest == "time_condition":
        actions.append(f"Goto(timecond-{_ident(val)},s,1)")
    elif dest == "hangup":
        actions.append("Hangup()")

    cid = ""
    # cid_prefix が None ならもちろん、過去のテンプレートのバグで
    # 文字列 "None" がそのまま保存されてしまったケースも防御的に弾く
    # (編集フォームで None が未入力欄にリテラル表示され、気付かず保存
    #  すると起きていた問題。v0.3.7 でフォーム側は修正済みだが、既存の
    #  壊れたデータに対する保険としてここでも弾く)。
    _cid_prefix = (route.cid_prefix or "").strip()
    if _cid_prefix and _cid_prefix != "None":
        cid = f"\n same => n,Set(CALLERID(name)={_cid_prefix}${{CALLERID(name)}})"
    rec = "\n same => n,MixMonitor(${UNIQUEID}.wav)" if route.record_call else ""
    # 外線 (トランク) 経由のチャネルには内線エンドポイントのような
    # language=ja 設定が付かないため、ここで明示的にセットしておく。
    # (未設定だと VoiceMail() 等の音声プロンプトが英語になってしまう)
    lang = f"\n same => n,Set(CHANNEL(language)={system_language})" if system_language else ""

    nuisance = (
        _nuisance_check_block(
            app_settings, has_blocklist, context_name, label_suffix=f"_{route.id}",
        )
        if app_settings is not None
        else ""
    )

    body = "\n".join(f" same => n,{a}" for a in actions)
    return f"""
;=== 着信ルート: {_text(route.name)} ===
exten => {did},1,NoOp(INBOUND {_text(route.name)}){cid}{lang}{rec}
{nuisance}{body}
 same => n,Hangup()
"""


async def render_extensions_conf(db: AsyncSession) -> str:
    out = io.StringIO()
    out.write(_HEADER)

    system_language = await _get_system_language(db)

    # 迷惑電話ブロックリスト・国際着信判定 (トランク着信の入口で共通利用)
    app_settings_row = (await db.scalars(select(AppSettings).limit(1))).first()
    if app_settings_row is None:
        app_settings_row = AppSettings()
    blocked_numbers = (
        await db.scalars(select(BlockedNumber).where(BlockedNumber.enabled.is_(True)))
    ).all()
    has_blocklist = len(blocked_numbers) > 0
    if has_blocklist or app_settings_row.international_call_action != "none":
        out.write(_render_nuisance_lookup_contexts(list(blocked_numbers)))

    # =========================================================
    # [general] / [globals]
    # =========================================================
    out.write(
        "[general]\n"
        "static=yes\n"
        "writeprotect=no\n"
        "autofallthrough=yes\n"
        "clearglobalvars=no\n\n"
    )
    out.write(
        "[globals]\n"
        f"DEFAULT_CONTEXT={settings.default_context}\n"
        "PARKINGLOT=default\n"
        "MOH_DEFAULT=default\n\n"
    )

    # =========================================================
    # [from-internal] 内線同士の通話 + 発信ルート
    # =========================================================
    out.write(f"[{settings.default_context}]\n")

    # コールパーク: 内線通話中に
    #   ##700  でパーク開始 (Park アプリ)
    #   701〜  で取り出し (ParkedCall)
    # ワイルドカード _XXX より明示パターンが優先されるため、
    # parkext と parkpos 範囲を個別 exten として明示登録する。
    lots = list(
        (await db.scalars(select(ParkingLot).where(ParkingLot.enabled.is_(True)))).all()
    )
    if not lots:
        # ParkingLot 未登録時のフォールバック (res_parking.conf の [default] に合わせる)
        _park_ranges = [("700", "default", 701, 720)]
    else:
        _park_ranges = [
            (lot.park_ext, "default" if lot.is_default else lot.name,
             int(lot.park_pos_start), int(lot.park_pos_end))
            for lot in lots
        ]
    out.write(
        "; - コールパーク: パーク開始 (この番号にダイヤルで保留) --\n"
        "; 重要: Park() は必ず「優先度1番目 (最初のステップ)」でなければ\n"
        "; ならない。Asterisk 公式ドキュメント (Park アプリケーション) に\n"
        "; 明記されている通り、ブラインド転送によるパークも *0 等の\n"
        "; DTMF ワンタッチパーク機能も、このパターンを「正規のパーク用\n"
        "; 内線」として認識するために Park() が priority 1 であることを\n"
        "; 要求する。NoOp 等を priority 1 に置くと検出に失敗し、\n"
        "; 転送やDTMF機能コードでのパークが機能しなくなる\n"
        "; (通常のダイヤルでの Park() 自体は実行されるため気付きにくい)。\n"
    )
    for park_ext, lot_name, _p_start, _p_end in _park_ranges:
        out.write(f"exten => {park_ext},1,Park({lot_name})\n")
        out.write(" same => n,Hangup()\n\n")
    out.write("; - コールパーク: 取り出し (701-720 等にダイヤルで取り出し) --\n")
    for _park_ext, lot_name, p_start, p_end in _park_ranges:
        for slot in range(p_start, p_end + 1):
            out.write(
                f"exten => {slot},1,"
                f"NoOp(Retrieve park slot {slot} from {lot_name})\n"
            )
            out.write(f" same => n,ParkedCall({lot_name},{slot})\n")
            out.write(" same => n,Hangup()\n")
        out.write("\n")

    # リンググループの仮想番号を明示登録。
    # 下の内線ワイルドカード (_NXX 等) より先に評価させることで、
    # 「200」のようなリンググループ番号が実在しない内線への Dial に
    # 食われてしまう (パーク番号 700 で過去に起きたのと同じ問題) のを防ぐ。
    # 実体は render_extensions_conf 後半で生成される
    # [ring-group-{group_number}] コンテキストへ Goto するだけ。
    ring_groups_for_dial = (
        await db.scalars(select(RingGroup).where(RingGroup.enabled.is_(True)))
    ).all()
    if ring_groups_for_dial:
        out.write("; - リンググループ (内線から直接ダイヤル可能) --\n")
        for rg in ring_groups_for_dial:
            out.write(
                f"exten => {rg.group_number},1,"
                f"NoOp(Ring group {_text(rg.name)})\n"
            )
            out.write(f" same => n,Goto(ring-group-{_ident(rg.group_number)},s,1)\n\n")

    # 内線通話 (3〜5桁ワイルドカード)
    # 上のパーク番号・リンググループ番号はすべて明示 exten なので、
    # ワイルドカードより優先される。
    #
    # reserve_leading_01_for_outbound が有効な場合、先頭 1 桁は
    # 'N' (2〜9のみ) にする。これにより 0/1 始まりの番号
    # (携帯 090/080/070、固定 03/06、フリーダイヤル 0120、
    #  警察 110・消防 119 等) は内線ワイルドカードに一切マッチせず、
    # 確実に発信ルート (トランク) 側の判定に回る。
    # 内線番号は 2 以降の数字で運用する前提。
    _lead = "N" if await _get_reserve_leading_01(db) else "X"
    _ring_secs = await _get_internal_ring_seconds(db)

    # --- 留守番電話専用内線 ---
    # 電話機を持たず、着信するとすぐ応答メッセージを再生する内線。
    # 下のワイルドカード (_NXX 等) より先に個別の exten を定義することで、
    # 通常の「呼び出してから留守電」ではなくこちらが使われる
    # (Asterisk は具体的なパターンを優先してマッチさせる)。
    vm_only_exts = (
        await db.scalars(
            select(Extension).where(
                Extension.enabled.is_(True), Extension.voicemail_only.is_(True)
            ).order_by(Extension.extension)
        )
    ).all()
    vm_only_numbers = {e.extension for e in vm_only_exts}
    # 音源の一覧 (必要になった時点で読み込む。未定義参照を避けるため
    # ここで空の辞書として初期化しておく)
    audio_files_map: dict[int, AudioFile] = {}
    if vm_only_exts:
        audio_files_map = {
            a.id: a for a in (await db.scalars(select(AudioFile))).all()
        }
        out.write("; - 留守番電話専用内線 (呼び出さずに応答) --\n")
        for e in vm_only_exts:
            out.write(_render_voicemail_only_exten(e, audio_files_map))
        out.write("\n")

    # --- 応答なし時の個別設定を持つ内線 ---
    # ワイルドカードより先に個別 exten を定義して優先させる。
    _na_ext_rows = (
        await db.scalars(
            select(Extension).where(
                Extension.enabled.is_(True),
                Extension.voicemail_only.is_(False),
            ).order_by(Extension.extension)
        )
    ).all()
    # 個別 exten を作る対象:
    #   - 応答なし時の音源を設定している内線
    #   - 応答なし時に「切断する」を選んでいる内線
    # (どちらも既定と異なる動作をするため、専用の exten が必要)
    _na_ext_rows = [
        e for e in _na_ext_rows
        if e.no_answer_audio_id or e.no_answer_action == "hangup"
    ]
    # 着信ルート・IVR 等から転送されたときに使う「応答なし時の処理」。
    # 上の個別 exten の有無にかかわらず、**ボイスメールが有効な内線は
    # すべて**対象にする。そうしないと、音源を設定していない内線へ
    # 転送したときに Dial の次の行が無く、そのまま切断されてしまう
    # (内線同士の通話ではワイルドカード側に VoiceMail があるので
    #  問題にならないが、転送経路には無いため)。
    _na_all_rows = (
        await db.scalars(
            select(Extension).where(
                Extension.enabled.is_(True),
                Extension.voicemail_only.is_(False),
            ).order_by(Extension.extension)
        )
    ).all()
    if _na_ext_rows:
        if not vm_only_exts:
            audio_files_map = {
                a.id: a for a in (await db.scalars(select(AudioFile))).all()
            }
        out.write("; - 応答なし時の個別設定を持つ内線 --\n")
        for e in _na_ext_rows:
            out.write(_render_internal_exten_with_no_answer(e, _ring_secs, audio_files_map))
        out.write("\n")
    # 着信ルート側から参照する用の「内線番号 → 応答なし時の action 列」
    # 音源を持たない内線でも、ボイスメールが有効なら留守番電話へ進む
    # action 列を作る (音源が無ければ Playback は出ず、Asterisk 既定の
    # 「ただいま電話に出られません」案内付きで録音される)。
    if _na_all_rows and not audio_files_map:
        audio_files_map = {
            a.id: a for a in (await db.scalars(select(AudioFile))).all()
        }
    no_answer_map = {
        e.extension: _no_answer_actions(e, audio_files_map)
        for e in _na_all_rows
        if e.no_answer_audio_id or e.voicemail_enabled or e.no_answer_action == "hangup"
    }

    out.write(
        f"; - 内線同士 (3〜5桁、先頭桁は"
        f"{'2-9限定 (0/1始まりは外線発信用に予約)' if _lead == 'N' else '0-9 全て'})--\n"
    )
    out.write(f"exten => _{_lead}XX,1,NoOp(Internal call to ${{EXTEN}})\n")
    out.write(_voicemail_hook_setup_lines("${EXTEN}"))
    out.write(f" same => n,Dial(PJSIP/${{EXTEN}},{_ring_secs},TtkKg)\n")
    out.write(" same => n,VoiceMail(${EXTEN}@default,u)\n")
    out.write(" same => n,Hangup()\n\n")
    out.write(f"exten => _{_lead}XXX,1,NoOp(Internal call to ${{EXTEN}})\n")
    out.write(_voicemail_hook_setup_lines("${EXTEN}"))
    out.write(f" same => n,Dial(PJSIP/${{EXTEN}},{_ring_secs},TtkKg)\n")
    out.write(" same => n,VoiceMail(${EXTEN}@default,u)\n")
    out.write(" same => n,Hangup()\n\n")
    out.write(f"exten => _{_lead}XXXX,1,NoOp(Internal call to ${{EXTEN}})\n")
    out.write(_voicemail_hook_setup_lines("${EXTEN}"))
    out.write(f" same => n,Dial(PJSIP/${{EXTEN}},{_ring_secs},TtkKg)\n")
    out.write(" same => n,VoiceMail(${EXTEN}@default,u)\n")
    out.write(" same => n,Hangup()\n\n")

    # 互換用 include (res_parking 自動生成コンテキスト)
    out.write("include => parkedcalls\n\n")

    # --- 短縮ダイヤル (電話帳) ---
    # *7 + 短縮番号 で電話帳の相手に発信する。
    # 既存の特番 (*8 パーク取得 / *43 エコー / *97・*98 留守番電話) と
    # 衝突しないよう *7 を使っている。
    # 発信そのものは通常の発信ルート判定に任せたいので、番号を
    # そのまま Goto して from-internal の先頭から評価し直す
    # (外線プレフィックスや発信ルートの設定をそのまま活かせる)。
    speed_rows = (
        await db.scalars(
            select(PhoneBookEntry).where(
                PhoneBookEntry.enabled.is_(True),
                PhoneBookEntry.speed_dial.is_not(None),
            ).order_by(PhoneBookEntry.speed_dial)
        )
    ).all()
    if speed_rows:
        out.write("; - 短縮ダイヤル (電話帳) --\n")
        for pb in speed_rows:
            # 短縮番号・発信先はダイヤルプランの構造 (exten => / Goto の
            # 引数) そのものになるため、数字と * # + 以外は落とす。
            # 一括入力でハイフンや括弧付きの番号が入っていても、
            # ここで正規化されるので設定が壊れない。
            speed = _dial(pb.speed_dial)
            number = _dial(pb.number)
            if not speed or not number:
                continue
            label = _text(pb.name, limit=40)
            out.write(
                f"exten => *7{speed},1,"
                f"NoOp(短縮ダイヤル {speed} -> {label} {number})\n"
            )
            out.write(f" same => n,Goto(from-internal,{number},1)\n")
        out.write("\n")

    # ボイスメール再生
    out.write("; - ボイスメールアクセス (*97 自分のVMへ)--\n")
    out.write("exten => *97,1,VoiceMailMain(${CALLERID(num)}@default)\n")
    out.write(" same => n,Hangup()\n\n")
    out.write("exten => *98,1,VoiceMailMain(@default)\n")
    out.write(" same => n,Hangup()\n\n")

    # エコーテスト (回線確認用)
    out.write("; - エコーテスト (*43)--\n")
    out.write("exten => *43,1,Answer()\n")
    out.write(" same => n,Echo()\n")
    out.write(" same => n,Hangup()\n\n")

    # FAX 受信テスト用内線 (from-internal から fax_extension にダイヤル)
    fax_cfg = (await db.scalars(select(FaxConfig))).first()
    if fax_cfg is not None and fax_cfg.enabled:
        out.write("; - FAX 受信テスト内線 --\n")
        out.write(
            f"exten => {fax_cfg.fax_extension},1,"
            f"NoOp(FAX test to {fax_cfg.fax_extension})\n"
        )
        out.write(" same => n,Goto(fax-receive,s,1)\n\n")

    #-- 発信ルート--
    routes = (
        await db.scalars(
            select(OutboundRoute)
            .where(OutboundRoute.enabled.is_(True))
            .order_by(OutboundRoute.priority)
        )
    ).all()
    trunks_list = list((await db.scalars(select(Trunk))).all())
    trunks = {t.id: t.name for t in trunks_list}
    for r in routes:
        tn = trunks.get(r.trunk_id, "unknown")
        out.write(_outbound_route_to_dialplan(r, tn))

    # =========================================================
    # 着信ルート (各トランクごとに context を分離)
    # =========================================================

    # fax_detect=yes による CNG トーン検知時のジャンプ先安全網。
    # Asterisk は検知した瞬間にチャネルが実行中のコンテキスト内で
    # 'fax' エクステンションを探すため、DID 振り分け後に Goto() で
    # 移動する可能性のある全ての着信先コンテキスト
    # (ring-group-N, ivr-N, did-{trunk}, from-trunk-{trunk} 等) から
    # include して、どこに居ても 'fax' が解決できるようにする。
    out.write("\n[fax-safety-net]\n")
    if fax_cfg is not None and fax_cfg.enabled:
        out.write("exten => fax,1,NoOp(CNG detected -> FAX受信へ)\n")
        out.write(" same => n,Goto(fax-receive,s,1)\n\n")
    else:
        # FAX 機能が無効なときは [fax-receive] 自体を生成しないため、
        # ここで Goto すると存在しないコンテキストへ飛んで
        # 「Spawn extension ... exited non-zero」で通話が切れてしまう。
        #
        # トランクの fax_detect が有効なままだと、FAX 機を使っている
        # 相手からの着信で CNG トーンを検知した瞬間にこの 'fax' へ
        # 飛んでくるため、「普通の電話は繋がるのに FAX 機からの着信
        # だけ切れる」という原因の分かりにくい障害になる。
        # FAX を使わない構成では、元の着信処理を続行させる。
        out.write("exten => fax,1,NoOp(CNG detected but FAX receive is disabled)\n")
        out.write(" same => n,Return()\n\n")

    # 留守番電話の録音完了通知フック (ハングアップハンドラー)。
    # VoiceMail() を呼ぶすべての箇所が共通で使う (詳細は
    # _voicemail_hangup_notify_context() のコメント参照)。
    out.write(_voicemail_hangup_notify_context(await get_hook_token(db, "voicemail")))
    # 発着信履歴を記録するハングアップハンドラー (全通話で使う)
    out.write(_calllog_hangup_handler_context(await get_hook_token(db, "calllog")))

    inbounds = (
        await db.scalars(
            select(InboundRoute)
            .where(InboundRoute.enabled.is_(True))
            .order_by(InboundRoute.priority)
        )
    ).all()

    for t in trunks_list:
        out.write(f"\n[from-trunk-{_ident(t.name)}]\n")
        # ひかり電話オフィスA は To ヘッダから DID を取り出して振り分ける
        if t.trunk_type in ("hikari_office_a_hgw", "hikari_office_a_og"):
            out.write(_hikari_did_dispatcher(
                t, inbounds, system_language, app_settings_row, has_blocklist,
                vm_only_numbers, no_answer_map,
            ))
        else:
            for ib in inbounds:
                out.write(_inbound_route_to_dialplan(
                    ib, system_language, app_settings_row, has_blocklist,
                    context_name=f"from-trunk-{_ident(t.name)}",
                    vm_only=vm_only_numbers,
                    no_answer_exts=no_answer_map,
                ))
            # fax_detect=yes がCNGトーンを検知した際のジャンプ先安全網。
            # 共通の [fax-safety-net] を include (理由は hikari 側の
            # コメント参照: 検知時のコンテキストによっては直接定義だと
            # 見つからないことがあるため)。
            out.write("include => fax-safety-net\n\n")

    # =========================================================
    # リンググループ
    # =========================================================
    groups = (
        await db.scalars(select(RingGroup).where(RingGroup.enabled.is_(True)))
    ).all()
    # FAX 機能が有効な場合のみ、CNG 検知の猶予として Answer() 直後に
    # 短い Wait() を挟む (詳細は FaxConfig.cng_detect_wait_seconds の
    # docstring 参照)。FAX 機能を使っていなければ何のメリットも無い
    # 遅延になるだけなので、その場合は従来通り即座に鳴らす。
    _cng_wait = 0
    if fax_cfg is not None and fax_cfg.enabled and fax_cfg.cng_detect_wait_seconds:
        _cng_wait = fax_cfg.cng_detect_wait_seconds
    for g in groups:
        members = "&".join(f"PJSIP/{m.extension}" for m in g.members)
        if not members:
            continue
        out.write(f"\n[ring-group-{_ident(g.group_number)}]\n")
        out.write("include => fax-safety-net\n")
        out.write(f"exten => s,1,NoOp(RingGroup {_text(g.name)})\n")
        for _a in _calllog_dest_actions("ring_group", g.group_number, f"リンググループ {g.name}"):
            out.write(f" same => n,{_a}\n")
        out.write(" same => n,Answer()\n")
        if _cng_wait > 0:
            out.write(
                f" same => n,Wait({_cng_wait})"
                f" ; FAX CNG検知の猶予 (鳴音前の一瞬の呼び出しを防止)\n"
            )
        out.write(f" same => n,Dial({members},{g.ring_seconds},TtkKg)\n")
        if g.fallback_type and g.fallback_value:
            out.write(_fallback_line(g.fallback_type, g.fallback_value, vm_only_numbers, no_answer_map))
        out.write(" same => n,Hangup()\n")

    # =========================================================
    # キュー
    # =========================================================
    queues = (await db.scalars(select(Queue).where(Queue.enabled.is_(True)))).all()
    for q in queues:
        out.write(_render_queue_context(q, vm_only_numbers, no_answer_map))

    # =========================================================
    # 時間条件
    # =========================================================
    time_conditions = (
        await db.scalars(select(TimeCondition).where(TimeCondition.enabled.is_(True)))
    ).all()
    calendar_exceptions = (
        await db.scalars(select(CalendarException).where(CalendarException.enabled.is_(True)))
    ).all()
    # 祝日は今年と来年ぶんだけをダイヤルプランに載せる。全期間 (1955年〜)
    # を出力すると extensions.conf が肥大化し reload も遅くなるため。
    _this_year = datetime.now().year
    national_holidays = (
        await db.scalars(
            select(NationalHoliday)
            .where(NationalHoliday.year.in_([_this_year, _this_year + 1]))
            .order_by(NationalHoliday.year, NationalHoliday.holiday_date)
        )
    ).all()
    company_holidays = (
        await db.scalars(
            select(CompanyHoliday).where(CompanyHoliday.enabled.is_(True))
            .order_by(CompanyHoliday.start_month, CompanyHoliday.start_day)
        )
    ).all()
    for tc in time_conditions:
        out.write(_render_time_condition_context(
            tc, list(calendar_exceptions), vm_only_numbers,
            list(national_holidays), list(company_holidays), no_answer_map,
        ))

    # =========================================================
    # IVR
    # =========================================================
    ivrs = (await db.scalars(select(Ivr).where(Ivr.enabled.is_(True)))).all()
    # 音源 ID → ファイル参照 のマップを事前作成
    audio_files = {a.id: a for a in (await db.scalars(select(AudioFile))).all()}
    for ivr in ivrs:
        out.write(_render_ivr_context(ivr, audio_files, vm_only_numbers, no_answer_map))

    # =========================================================
    # FAX 受信コンテキスト
    # =========================================================
    if fax_cfg is not None and fax_cfg.enabled:
        out.write(_render_fax_context(fax_cfg, await get_hook_token(db, "fax")))

    return out.getvalue()


def _no_answer_actions(
    ext: Extension, audio_files: dict[int, AudioFile],
) -> list[str]:
    """内線が応答しなかった場合の action リストを組み立てる。

    内線ごとに「応答しなかったときに流す音源」と「その後の動作
    (留守番電話に録音する / 切断する)」を設定できる。
    音源が未設定なら再生せず、動作だけを実行する。
    """
    actions: list[str] = []
    audio = audio_files.get(ext.no_answer_audio_id or 0)
    if audio is not None and audio.conversion_status == "ok":
        actions.append(
            f"Playback({asterisk_sound_reference(audio.category, audio.storage_name)})"
        )
    if ext.no_answer_action == "hangup":
        # 案内だけ流して切断 (録音しない)
        actions.append("Hangup()")
    else:
        # 既定: この内線の留守番電話へ録音
        # 音源を自前で流した場合、VoiceMail() 側の案内は不要なので
        # s オプションで省く (二重に案内が流れるのを防ぐ)
        vm_opts = "s" if actions else "u"
        actions.extend(_voicemail_hook_setup_actions(ext.extension))
        actions.append(f"VoiceMail({ext.extension}@default,{vm_opts})")
    return actions


def _render_internal_exten_with_no_answer(
    ext: Extension, ring_seconds: int, audio_files: dict[int, AudioFile],
) -> str:
    """応答なし時の個別設定を持つ内線の exten を生成する。

    ワイルドカード (_NXX 等) より先に定義することで、この内線への通話
    だけは個別設定 (案内音声・その後の動作) が適用される。
    """
    out = io.StringIO()
    out.write(
        f"exten => {ext.extension},1,"
        f"NoOp(Internal call to {ext.extension} / 応答なし時の個別設定あり)\n"
    )
    out.write(f" same => n,Dial(PJSIP/{ext.extension},{ring_seconds},TtkKg)\n")
    for a in _no_answer_actions(ext, audio_files):
        out.write(f" same => n,{a}\n")
    out.write(" same => n,Hangup()\n")
    return out.getvalue()


# 内線を呼び出すときの Dial オプション。
#   T/t : 通話中の転送を許可
#   k/K : 通話中のパーク (保留) を許可
#   g   : 【重要】呼び出し先が応答できなかった場合でも、ダイヤルプランの
#         次の行へ進む。
#         これが無いと、電話機が起動していない (未登録の) 内線を呼んだ
#         とき Dial() が "exited non-zero" でダイヤルプランを打ち切って
#         しまい、応答なし時の案内再生や留守番電話に進まず、そのまま
#         回線が切断されてしまう (実機で発生)。
_DIAL_OPTS = "TtkKg"


def _dial_or_voicemail_only(
    number: str, ring_seconds: int, vm_only_numbers: set[str],
) -> str:
    """内線への転送 action を組み立てる。

    留守番電話専用内線 (電話機を持たない内線) の場合、Dial(PJSIP/...) で
    呼び出しても応答する端末が無いため、呼び出し音が鳴り続けてしまう。
    そこで [from-internal] に用意した留守電専用の exten へ Goto して、
    応答メッセージ再生・録音の処理に入るようにする。
    """
    if number in vm_only_numbers:
        return f"Goto(from-internal,{number},1)"
    return f"Dial(PJSIP/{number},{ring_seconds},TtkKg)"


def _render_voicemail_only_exten(
    ext: Extension, audio_files: dict[int, AudioFile]
) -> str:
    """留守番電話専用内線の exten を生成する。

    通常の内線は「電話機を呼び出して、応答が無ければ留守電」だが、
    この内線は電話機を持たないため、着信したらすぐ応答する。

    mode="record"   : 応答メッセージを流して相手のメッセージを録音
    mode="announce" : 応答メッセージを流すだけで録音せず切断

    【announce の実装について】
    Asterisk の VoiceMail() には「録音せずに案内だけ流す」オプションが
    無い (公式ドキュメント上、s は案内文の読み上げを省くだけで録音は
    行われる)。そのため announce では VoiceMail() を使わず、
    Playback() で音源を再生してから Hangup() している。
    """
    number = ext.extension
    out = io.StringIO()
    out.write(
        f"exten => {number},1,"
        f"NoOp(留守番電話専用内線 {number} / モード={ext.voicemail_only_mode})\n"
    )
    out.write(" same => n,Answer()\n")
    # 応答直後だと先頭が切れることがあるため、少し待ってから再生する
    out.write(" same => n,Wait(1)\n")

    greeting = audio_files.get(ext.voicemail_greeting_audio_id or 0)
    greeting_ref = (
        asterisk_sound_reference(greeting.category, greeting.storage_name)
        if greeting is not None and greeting.conversion_status == "ok"
        else None
    )

    if ext.voicemail_only_mode == "announce":
        # 録音しない: 音源を再生して切断するだけ
        if greeting_ref:
            out.write(f" same => n,Playback({greeting_ref})\n")
        else:
            # 音源未設定でも無音で切れないよう、既定の案内を流す
            out.write(" same => n,Playback(vm-goodbye)\n")
        out.write(" same => n,Hangup()\n")
    else:
        # 録音する: 独自の応答メッセージがあれば先に流し、
        # VoiceMail() 側の案内は s オプションで省いて二重再生を防ぐ
        if greeting_ref:
            out.write(f" same => n,Playback({greeting_ref})\n")
            vm_opts = "s"
        else:
            # 音源未設定なら Asterisk 既定の「不在」案内を使う
            vm_opts = "u"
        out.write(_voicemail_hook_setup_lines(number))
        out.write(f" same => n,VoiceMail({number}@default,{vm_opts})\n")
        out.write(" same => n,Hangup()\n")
    return out.getvalue()


def _render_ivr_context(
    ivr: Ivr, audio_files: dict[int, AudioFile], vm_only: set[str] | None = None,
    no_answer_map: dict[str, list[str]] | None = None,
) -> str:
    """IVR コンテキスト [ivr-<name>] を生成。

    Background() でガイダンス再生中も DTMF を待ち、押されたキーで Goto。
    タイムアウトは 't' エクステンション、無効キーは 'i' エクステンション。
    """
    vm_only = vm_only or set()
    no_answer_map = no_answer_map or {}
    out = io.StringIO()
    ctx = f"ivr-{_ident(ivr.name)}"
    out.write(f"\n;=== IVR: {_text(ivr.display_name)} ===\n")
    out.write(f"[{ctx}]\n")
    out.write("include => fax-safety-net\n")

    # エントリポイント
    out.write(f"exten => s,1,NoOp(IVR {_text(ivr.name)})\n")
    # 発着信履歴: IVR に入ったことを記録 (この後キーを押せば上書きされる)
    for _a in _calllog_dest_actions("ivr", ivr.name, f"IVR {ivr.display_name}"):
        out.write(f" same => n,{_a}\n")
    out.write(" same => n,Answer()\n")
    out.write(" same => n,Set(IVR_RETRY=0)\n")

    # 挨拶ガイダンス再生 (Background = 鳴らしながらキー待ち)
    greeting = audio_files.get(ivr.greeting_audio_id) if ivr.greeting_audio_id else None
    if greeting is not None and greeting.conversion_status == "ok":
        ref = asterisk_sound_reference(greeting.category, greeting.storage_name)
        out.write(f" same => n(start),Background({ref})\n")
    else:
        out.write(" same => n(start),Background(silence/1)\n")
    out.write(f" same => n,WaitExten({ivr.timeout_seconds})\n")

    # 各キーの動作
    for entry in ivr.entries:
        out.write(_ivr_entry_to_dialplan(entry, vm_only, no_answer_map))

    # タイムアウト処理
    timeout = audio_files.get(ivr.timeout_audio_id) if ivr.timeout_audio_id else None
    out.write("exten => t,1,NoOp(IVR timeout)\n")
    if timeout is not None and timeout.conversion_status == "ok":
        ref = asterisk_sound_reference(timeout.category, timeout.storage_name)
        out.write(f" same => n,Playback({ref})\n")
    out.write(" same => n,Set(IVR_RETRY=$[${IVR_RETRY}+1])\n")
    out.write(f" same => n,GotoIf($[${{IVR_RETRY}} < {ivr.max_retries}]?{ctx},s,start)\n")
    out.write(_ivr_fallback_lines(ivr, vm_only, no_answer_map))

    # 無効キー処理
    invalid = audio_files.get(ivr.invalid_audio_id) if ivr.invalid_audio_id else None
    out.write("exten => i,1,NoOp(IVR invalid key)\n")
    if invalid is not None and invalid.conversion_status == "ok":
        ref = asterisk_sound_reference(invalid.category, invalid.storage_name)
        out.write(f" same => n,Playback({ref})\n")
    out.write(" same => n,Set(IVR_RETRY=$[${IVR_RETRY}+1])\n")
    out.write(f" same => n,GotoIf($[${{IVR_RETRY}} < {ivr.max_retries}]?{ctx},s,start)\n")
    out.write(_ivr_fallback_lines(ivr, vm_only, no_answer_map))

    return out.getvalue()


def _ivr_entry_to_dialplan(entry, vm_only: set[str] | None = None,  # type: ignore[no-untyped-def]
                           no_answer_map: dict[str, list[str]] | None = None) -> str:
    """IVR の 1 エントリ (キー → 動作) をダイヤルプラン行に変換。"""
    vm_only = vm_only or set()
    no_answer_map = no_answer_map or {}
    key = entry.key_input
    action = entry.action
    val = entry.action_value or ""
    label = f" ; {entry.label}" if entry.label else ""

    if action == "extension":
        # 内線を呼び出したあと、応答が無かった場合の処理を続ける。
        # これが無いと、電話機が起動していない内線へ転送したときに
        # そのまま切断されてしまう (案内も留守番電話も流れない)。
        na = "".join(f" same => n,{a}\n" for a in no_answer_map.get(val, []))
        return (
            f"exten => {key},1,{_dial_or_voicemail_only(val, 30, vm_only)}{label}\n"
            f"{na}"
            " same => n,Hangup()\n"
        )
    if action == "ring_group":
        return f"exten => {key},1,Goto(ring-group-{_ident(val)},s,1){label}\n"
    if action == "queue":
        return f"exten => {key},1,Goto(queue-{_ident(val)},s,1){label}\n"
    if action == "ivr":
        return f"exten => {key},1,Goto(ivr-{_ident(val)},s,1){label}\n"
    if action == "voicemail":
        if val in vm_only:
            # 留守番電話専用内線なら応答メッセージの再生から通す
            return (
                f"exten => {key},1,NoOp(IVR to voicemail {val}){label}\n"
                f" same => n,Goto(from-internal,{val},1)\n"
            )
        return (
            f"exten => {key},1,NoOp(IVR to voicemail {val}){label}\n"
            f"{_voicemail_hook_setup_lines(val)}"
            f" same => n,VoiceMail({val}@default)\n"
            " same => n,Hangup()\n"
        )
    if action == "playback_hangup":
        return f"exten => {key},1,Playback({val}){label}\n same => n,Hangup()\n"
    return f"exten => {key},1,Hangup(){label}\n"


def _ivr_fallback_lines(ivr: Ivr, vm_only: set[str] | None = None,
                        no_answer_map: dict[str, list[str]] | None = None) -> str:
    """IVR の最終フォールバック (max_retries 超過時)。"""
    vm_only = vm_only or set()
    no_answer_map = no_answer_map or {}
    a = ivr.fallback_action
    v = ivr.fallback_value or ""
    if a == "extension":
        na = "".join(f" same => n,{x}\n" for x in no_answer_map.get(v, []))
        return (
            f" same => n,{_dial_or_voicemail_only(v, 30, vm_only)}\n"
            f"{na}"
            " same => n,Hangup()\n"
        )
    if a == "ring_group":
        return f" same => n,Goto(ring-group-{_ident(v)},s,1)\n"
    if a == "queue":
        return f" same => n,Goto(queue-{_ident(v)},s,1)\n"
    if a == "voicemail":
        if v in vm_only:
            return f" same => n,Goto(from-internal,{v},1)\n"
        return (
            f"{_voicemail_hook_setup_lines(v)}"
            f" same => n,VoiceMail({v}@default)\n"
            " same => n,Hangup()\n"
        )
    return " same => n,Hangup()\n"


def _render_fax_context(fax: FaxConfig, hook_token: str) -> str:
    """FAX 受信用コンテキスト [fax-receive] を生成。

    呼び出し方:
      - 内線テスト: from-internal から fax.fax_extension にダイヤル
      - 着信: 着信ルートで FAX DID を Goto(fax-receive,s,1)

    ReceiveFAX で TIFF を spool に保存後、System() で本ツールの
    受信フックを curl で叩く。本ツール側で TIFF→PDF・リネーム・メール送信。
    """
    spool = str(settings.fax_spool_dir).rstrip("/")
    hook_url = (
        f"http://127.0.0.1:{settings.port}/fax/hook/received"
        f"?token={hook_token}"
    )
    out = io.StringIO()
    out.write("\n;=== FAX 受信 ===\n")
    out.write("[fax-receive]\n")
    out.write("exten => s,1,NoOp(FAX receive start from ${CALLERID(num)})\n")
    for _a in _calllog_dest_actions("fax", "", "FAX 受信"):
        out.write(f" same => n,{_a}\n")
    out.write(" same => n,Answer()\n")
    out.write(" same => n,Set(FAXFILE=" + spool + "/rx_${UNIQUEID}.tif)\n")
    out.write(f" same => n,Set(FAXOPT(ecm)={'yes' if fax.ecm_enabled else 'no'})\n")
    out.write(" same => n,Set(FAXOPT(headerinfo)=Received by Asterisk)\n")
    out.write(f" same => n,Set(FAXOPT(minrate)={fax.minrate})\n")
    out.write(f" same => n,Set(FAXOPT(maxrate)={fax.maxrate})\n")
    if fax.station_id:
        out.write(f" same => n,Set(FAXOPT(localstationid)={fax.station_id})\n")
    # 'f' = T.38 対応チャネルでの音声フォールバックを許可。
    # SendFAX 側と同じ理由 (t38_udptl=yes によりチャネルが T.38対応と
    # みなされるため、相手が T.38 に応じない場合は音声モードへ
    # フォールバックできるようにしておく)。
    out.write(" same => n,ReceiveFAX(${FAXFILE},f)\n")
    out.write(" same => n,NoOp(FAXSTATUS=${FAXSTATUS} PAGES=${FAXPAGES})\n")
    out.write(
        " same => n,System(curl -s -m 20 -G "
        f"'{hook_url}' "
        "--data-urlencode 'file=${FAXFILE}' "
        "--data-urlencode 'src=${CALLERID(num)}' "
        "--data-urlencode 'status=${FAXSTATUS}' "
        "--data-urlencode 'pages=${FAXPAGES}' "
        "--data-urlencode 'uniqueid=${UNIQUEID}')\n"
    )
    out.write(" same => n,Hangup()\n\n")

    # FAX 送信: 本ツールが AMI Originate で Local/s@fax-send-out を起こす。
    # 変数 FAX_FILE / FAX_DEST / FAX_TRUNK / FAX_SID で送信内容を受け取る。
    #
    # Originate で Local チャネルの一方がこの s,1 に入る。
    # Dial で外線へ発信し、相手が応答したら SendFAX を実行する。
    # Dial 後に SendFAX を置くと「発信側」で実行されるため、
    # Dial の代わりに、応答時に同一チャネルで SendFAX するよう
    # M() (マクロ) ではなく、Dial + ${DIALSTATUS} 判定で行う。
    out.write("[fax-send-out]\n")
    out.write("exten => s,1,NoOp(FAX send to ${FAX_DEST} via ${FAX_TRUNK})\n")
    out.write(f" same => n,Set(FAXOPT(ecm)={'yes' if fax.ecm_enabled else 'no'})\n")
    out.write(f" same => n,Set(FAXOPT(minrate)={fax.minrate})\n")
    out.write(f" same => n,Set(FAXOPT(maxrate)={fax.maxrate})\n")
    out.write(" same => n,ExecIf($[\"${FAX_SID}\" != \"\"]"
              "?Set(FAXOPT(localstationid)=${FAX_SID}))\n")
    out.write(" same => n,ExecIf($[\"${FAX_HDR}\" != \"\"]"
              "?Set(FAXOPT(headerinfo)=${FAX_HDR}))\n")
    # 外線へ発信。
    #
    # 重要: b() ではなく U() を使う。
    #   b(...) は「相手が応答する前 (呼出中/アーリーメディア段階)」に
    #   発信先チャネル側で Gosub を実行する。
    #   U(...) は「相手が本当に応答 (200 OK) した後」に Gosub を実行する。
    #
    # ひかり電話 HGW は、183 Session Progress (呼出音) の段階ではまだ
    # 実際のアナログ FAX 回線に接続しておらず、本当に接続されるのは
    # 200 OK (応答) の時点である。b() だと「まだ実際の FAX 機には
    # 繋がっていない、呼出中の段階」で SendFAX が音を送り始めてしまい、
    # その音がどこにも届かず、相手からの応答も一切検知できないまま
    # 延々とネゴシエーションを繰り返す不具合の原因になっていた。
    # U() にすることで、本当に接続が確立してから送信を開始する。
    out.write(
        " same => n,Dial(PJSIP/${FAX_DEST}@${FAX_TRUNK},60,"
        "U(fax-send-exec^s^1))\n"
    )
    out.write(" same => n,NoOp(DIALSTATUS=${DIALSTATUS} FAXSTATUS=${FAXSTATUS})\n")
    out.write(" same => n,Hangup()\n\n")

    # 発信先 (外線) チャネル側で実行されるサブルーチン。
    # ここで SendFAX を呼ぶと、応答済みの相手チャネルに対して送信される。
    out.write("[fax-send-exec]\n")
    out.write("exten => s,1,NoOp(SendFAX ${FAX_FILE} -> ${FAX_DEST})\n")
    out.write(f" same => n,Set(FAXOPT(ecm)={'yes' if fax.ecm_enabled else 'no'})\n")
    # FAXOPT は Dial() が生成する新チャネル (実際に SendFAX を実行する側)
    # には自動継承されないため、[fax-send-out] だけでなくここでも
    # 明示的に再設定する (ecm と同じ理由・同じパターン)。
    out.write(f" same => n,Set(FAXOPT(minrate)={fax.minrate})\n")
    out.write(f" same => n,Set(FAXOPT(maxrate)={fax.maxrate})\n")
    # SendFAX オプション 'f' = T.38 対応チャネルでの音声フォールバックを
    # 許可。トランクに t38_udptl=yes を付与している (v0.3.11〜) ため、
    # このチャネルは Asterisk から見て「T.38対応」と判定され、SendFAX は
    # まず T.38 での送信を試みる。相手 (HGW 等) が T.38 再招待を受け付け
    # なかった場合、'f' が無いと Asterisk はそこで送信自体を諦めてしまう
    # ("Audio FAX not allowed ... T.38 negotiation failed; aborting")。
    # 'f' を付けることで T.38 が失敗しても音声 (G.711) モードへ自動的に
    # フォールバックし、送信を継続できる。
    out.write(" same => n,SendFAX(${FAX_FILE},df)\n")
    out.write(" same => n,Return()\n\n")
    return out.getvalue()


def _hikari_did_dispatcher(
    t: Trunk, inbounds, system_language: str = "",  # type: ignore[no-untyped-def]
    app_settings: AppSettings | None = None, has_blocklist: bool = False,
    vm_only: set[str] | None = None,
    no_answer_exts: dict[str, list[str]] | None = None,
) -> str:
    """ひかり電話オフィスA 着信用の DID 振り分けダイヤルプラン。

    HGW/OG から PBX への INVITE は Request-URI のユーザ部に DID が乗る。
    To ヘッダから DID を抽出し、各 DID ごとの転送先へ Goto する。
    """
    primary_did = (t.outbound_caller_id or "").strip()
    extra_dids = [n.strip() for n in (t.additional_did_numbers or "").split(",") if n.strip()]
    all_dids = [d for d in [primary_did, *extra_dids] if d]

    out = io.StringIO()
    out.write(
        ";=== ひかり電話オフィスA 着信ディスパッチャ ===\n"
        "; To ヘッダから着信先 DID を抽出し did-{trunk} コンテキストへ Goto\n"
    )
    out.write(
        "; 注意: HGW からの着信 INVITE の Request-URI には HGW 内線番号\n"
        "; (例: \"3\") のような短い1桁の値しか乗らない (本当の DID は To\n"
        "; ヘッダー側に入っており、下で PJSIP_HEADER から抽出している)。\n"
        "; パターンを _X. (2文字以上必須) にすると、1桁の着信に対して\n"
        "; allow_overlap=yes と相まって Asterisk が「まだ続きの桁が来る\n"
        "; かもしれない」と判断し 484 Address Incomplete を返して待ち\n"
        "; 続けてしまう (HGW は追加の桁を送ってこないため着信が繋がらない)。\n"
        "; _X! (0文字以上・即時確定) にすることで 1桁でも即座に完全一致\n"
        "; させ、484 待ちを起こさないようにする。\n"
    )
    out.write("exten => _X!,1,NoOp(Hikari INBOUND raw exten=${EXTEN})\n")
    # 発着信履歴の記録を仕込む (着信の一番最初に 1 回だけ)。
    # 着信先は後段 (着信ルート/時間条件/IVR 等) で上書きされる。
    for _a in _calllog_setup_actions(
        "in", "${CALLERID(num)}", trunk=t.name,
    ):
        out.write(f" same => n,{_a}\n")
    if system_language:
        # 外線 (トランク) 経由のチャネルには内線エンドポイントのような
        # language=ja 設定が付かないため、ここで明示的にセットする。
        # Goto() でコンテキストを移動してもチャネル変数として引き継がれる
        # ため、この1箇所の設定で以降のどのコンテキスト (did-*/
        # ring-group-*/ivr-*/fax-receive 等) でも日本語プロンプトになる。
        out.write(f" same => n,Set(CHANNEL(language)={system_language})\n")
    if app_settings is not None:
        # 迷惑電話ブロックリスト・国際着信判定 (該当すればここで
        # Hangup()/VoiceMail() まで完結し、後続の DID 振り分けには
        # 進まない。詳細は _nuisance_check_block() 参照)。
        out.write(_nuisance_check_block(app_settings, has_blocklist, f"from-trunk-{_ident(t.name)}"))
    out.write(" same => n,Set(TO_DID=${PJSIP_HEADER(read,To)})\n")
    out.write(" same => n,Set(TO_DID=${CUT(TO_DID,@,1)})\n")
    out.write(" same => n,Set(TO_DID=${CUT(TO_DID,:,2)})\n")
    out.write(f" same => n,Goto(did-{_ident(t.name)},${{TO_DID}},1)\n\n")
    out.write(
        "; fax_detect=yes (トランクエンドポイント) が通話中に CNG トーン\n"
        "; (FAX発信音) を検知すると、Asterisk は『検知した瞬間にチャネルが\n"
        "; 実行中のコンテキスト』内の 'fax' という名前のエクステンションへ\n"
        "; ジャンプする。CNG検知は着信から数秒かかるため、その頃には既に\n"
        "; Goto() で did-{trunk}/ring-group-N/ivr-N 等へ移動済みのことが\n"
        "; 多く、ここに直接書いても間に合わないケースがある\n"
        "; (\"FAX CNG detected on '...' but no fax extension in\n"
        "; 'ring-group-200'\" のようなエラーで発覚)。そのため共通の\n"
        "; [fax-safety-net] コンテキストを include することで、どの\n"
        "; 着信先コンテキストに居ても 'fax' が解決できるようにしている。\n"
    )
    out.write("include => fax-safety-net\n\n")

    # DID ごとの転送先コンテキスト
    out.write(f"[did-{_ident(t.name)}]\n")
    out.write("include => fax-safety-net\n")
    inbound_by_did: dict[str, InboundRoute] = {}
    catch_all: InboundRoute | None = None
    for ib in inbounds:
        if ib.did_number and ib.did_number.strip():
            inbound_by_did[ib.did_number.strip()] = ib
        else:
            catch_all = ib

    for did in all_dids:
        ib = inbound_by_did.get(did)
        if ib is not None:
            out.write(_inbound_route_to_dialplan_with_exten(ib, did, system_language, vm_only, no_answer_exts))
        else:
            out.write(
                f";=== DID {did}: 着信ルート未設定 → デフォルト動作 ===\n"
                f"exten => {did},1,NoOp(Hikari DID {did} no route)\n"
                f" same => n,Playback(vm-nobodyavail)\n"
                f" same => n,Hangup()\n"
            )

    # キャッチオール (未マッチ DID 用)
    out.write(";=== キャッチオール (どの DID にもマッチしなかったとき)==\n")
    if catch_all is not None:
        out.write(_inbound_route_to_dialplan_with_exten(catch_all, "_X.", system_language, vm_only, no_answer_exts))
    else:
        out.write("exten => _X.,1,NoOp(Hikari DID ${EXTEN} unmatched)\n")
        out.write(" same => n,Hangup()\n")

    return out.getvalue()


def _inbound_route_to_dialplan_with_exten(
    route: InboundRoute, exten_pattern: str, system_language: str = "",
    vm_only: set[str] | None = None,
    no_answer_exts: dict[str, list[str]] | None = None,
) -> str:
    """着信ルートを任意の exten パターンで生成。"""
    vm_only = vm_only or set()
    no_answer_exts = no_answer_exts or {}
    actions: list[str] = []
    dest = route.destination_type
    val = route.destination_value
    # 発着信履歴: かかってきた番号と着信先を記録する
    if route.did_number:
        actions.append(f"Set(__CL_DID={route.did_number})")
    actions.extend(_calllog_dest_actions(dest, val or "", f"{dest} {val or ''}".strip()))

    if dest == "extension":
        actions.append(
            _dial_or_voicemail_only(val, route.ring_seconds or 30, vm_only)
        )
        # 応答なし時のフォールバック (ウ: 応答なし→ボイスメール 等)
        na_type = route.no_answer_type
        na_val = route.no_answer_value
        if na_type == "voicemail" and na_val:
            if na_val in vm_only:
                actions.append(f"Goto(from-internal,{na_val},1)")
            else:
                actions.extend(_voicemail_hook_setup_actions(na_val))
                actions.append(f"VoiceMail({na_val}@default,u)")
        elif na_type == "extension" and na_val:
            actions.append(_dial_or_voicemail_only(na_val, 30, vm_only))
        elif na_type == "hangup":
            actions.append("Hangup()")
        elif not na_type and val in no_answer_exts:
            # 着信ルート側に応答なし設定が無く、転送先の内線に個別設定が
            # ある場合は、内線側の設定 (案内音声 + その後の動作) を使う。
            # from-internal の個別 exten は Dial から始まるため、ここでは
            # 応答なし後の処理だけを直接展開する。
            actions.extend(no_answer_exts[val])
    elif dest == "ring_group":
        actions.append(f"Goto(ring-group-{_ident(val)},s,1)")
    elif dest == "queue":
        actions.append(f"Goto(queue-{_ident(val)},s,1)")
    elif dest == "voicemail":
        if val in vm_only:
            # 留守番電話専用内線なら応答メッセージの再生から通す
            actions.append(f"Goto(from-internal,{val},1)")
        else:
            actions.extend(_voicemail_hook_setup_actions(val))
            actions.append(f"VoiceMail({val}@default)")
    elif dest == "ivr":
        actions.append(f"Goto(ivr-{_ident(val)},s,1)")
    elif dest == "fax":
        actions.append("Goto(fax-receive,s,1)")
    elif dest == "time_condition":
        actions.append(f"Goto(timecond-{_ident(val)},s,1)")
    elif dest == "hangup":
        actions.append("Hangup()")

    cid = ""
    # cid_prefix が None ならもちろん、過去のテンプレートのバグで
    # 文字列 "None" がそのまま保存されてしまったケースも防御的に弾く
    # (編集フォームで None が未入力欄にリテラル表示され、気付かず保存
    #  すると起きていた問題。v0.3.7 でフォーム側は修正済みだが、既存の
    #  壊れたデータに対する保険としてここでも弾く)。
    _cid_prefix = (route.cid_prefix or "").strip()
    if _cid_prefix and _cid_prefix != "None":
        cid = f"\n same => n,Set(CALLERID(name)={_cid_prefix}${{CALLERID(name)}})"
    rec = "\n same => n,MixMonitor(${UNIQUEID}.wav)" if route.record_call else ""
    # 外線 (トランク) 経由のチャネルには内線エンドポイントのような
    # language=ja 設定が付かないため、ここで明示的にセットしておく。
    # (未設定だと VoiceMail() 等の音声プロンプトが英語になってしまう)
    lang = f"\n same => n,Set(CHANNEL(language)={system_language})" if system_language else ""
    body = "\n".join(f" same => n,{a}" for a in actions)
    return f"""
;=== 着信ルート: {_text(route.name)} ({exten_pattern})==
exten => {exten_pattern},1,NoOp(INBOUND {_text(route.name)}){cid}{lang}{rec}
{body}
 same => n,Hangup()
"""


def _fallback_line(
    dest_type: str, dest_value: str, vm_only_numbers: set[str] | None = None,
    no_answer_map: dict[str, list[str]] | None = None,
) -> str:
    vm_only = vm_only_numbers or set()
    # 発着信履歴: 転送先が確定したので着信先を上書きする。
    # これが無いと、時間条件や IVR を経由した着信の「着信先」欄が
    # 途中の経路 (time_condition 等) のまま止まってしまう。
    cl = "".join(
        f" same => n,{a}\n"
        for a in _calllog_dest_actions(dest_type, dest_value, "")
    )
    if dest_type == "extension":
        # 応答が無かったときの処理 (案内再生 → 留守番電話) を続ける。
        # これが無いと、電話機が起動していない内線へ転送したときに
        # 次の行が Hangup になり、そのまま切断されてしまう。
        na = "".join(f" same => n,{a}\n" for a in (no_answer_map or {}).get(dest_value, []))
        return f"{cl} same => n,{_dial_or_voicemail_only(dest_value, 30, vm_only)}\n{na}"
    if dest_type == "voicemail":
        # 転送先が「留守番電話専用内線」の場合は、VoiceMail() を直接
        # 呼ばずに [from-internal] の専用 exten へ回す。そうしないと
        # 設定した応答メッセージが再生されず、Asterisk 標準の案内
        # (「トーンの後にメッセージを…」) がいきなり流れてしまう。
        if dest_value in vm_only:
            return f" same => n,Goto(from-internal,{dest_value},1)\n"
        return (
            f"{_voicemail_hook_setup_lines(dest_value)}"
            f" same => n,VoiceMail({dest_value}@default)\n"
        )
    if dest_type == "queue":
        # queue-{number} コンテキスト経由にすることで、そのキュー自身の
        # フォールバック設定 (メンバー不在時の転送先) も連鎖的に効く。
        return f" same => n,Goto(queue-{_ident(dest_value)},s,1)\n"
    return ""


def _dest_goto_line(
    dest_type: str, dest_value: str, vm_only_numbers: set[str] | None = None,
    no_answer_map: dict[str, list[str]] | None = None,
) -> str:
    """宛先タイプ/値から、その宛先を処理する1行を生成する。

    _fallback_line() (リンググループのフォールバックで使用、宛先の種類が
    限定的) より広い宛先タイプをサポートする汎用版。時間条件
    (TimeCondition) の「時間内/時間外の転送先」で使う。
    """
    vm_only = vm_only_numbers or set()
    if dest_type == "extension":
        # 応答が無かったときの処理 (案内再生 → 留守番電話) を続ける。
        # これが無いと、電話機が起動していない内線へ転送したときに
        # 次の行が Hangup になり、そのまま切断されてしまう。
        na = "".join(f" same => n,{a}\n" for a in (no_answer_map or {}).get(dest_value, []))
        return f" same => n,{_dial_or_voicemail_only(dest_value, 30, vm_only)}\n{na}"
    if dest_type == "ring_group":
        return f" same => n,Goto(ring-group-{_ident(dest_value)},s,1)\n"
    if dest_type == "queue":
        return f" same => n,Goto(queue-{_ident(dest_value)},s,1)\n"
    if dest_type == "ivr":
        return f" same => n,Goto(ivr-{_ident(dest_value)},s,1)\n"
    if dest_type == "voicemail":
        # 転送先が「留守番電話専用内線」の場合は、VoiceMail() を直接
        # 呼ばずに [from-internal] の専用 exten へ回す。そうしないと
        # 設定した応答メッセージが再生されず、Asterisk 標準の案内
        # (「トーンの後にメッセージを…」) がいきなり流れてしまう。
        if dest_value in vm_only:
            return f" same => n,Goto(from-internal,{dest_value},1)\n"
        return (
            f"{_voicemail_hook_setup_lines(dest_value)}"
            f" same => n,VoiceMail({dest_value}@default)\n"
        )
    # "hangup" および未知の値は安全側 (切断) に倒す。
    return " same => n,Hangup()\n"


def _render_queue_context(q: Queue, vm_only: set[str] | None = None,
                          no_answer_map: dict[str, list[str]] | None = None) -> str:
    """キューから [queue-{queue_number}] ダイヤルプランコンテキストを
    生成する。Queue() 実行後 (queues.conf の joinempty=strict により
    メンバー不在で入場できなかった場合や、応答なしタイムアウトなどで)
    次の行に進んだ場合、設定されていればフォールバック先へ Goto する。
    """
    vm_only = vm_only or set()
    out = io.StringIO()
    out.write(f"\n[queue-{_ident(q.queue_number)}]\n")
    out.write(f"exten => s,1,NoOp(Queue {_text(q.name)})\n")
    for _a in _calllog_dest_actions("queue", q.queue_number, f"キュー {q.name}"):
        out.write(f" same => n,{_a}\n")
    out.write(" same => n,Answer()\n")
    out.write(f" same => n,Queue({q.queue_number})\n")
    if q.fallback_type and q.fallback_value:
        out.write(_fallback_line(q.fallback_type, q.fallback_value, vm_only, no_answer_map))
    out.write(" same => n,Hangup()\n")
    return out.getvalue()


_MONTH_NAMES = {
    1: "jan", 2: "feb", 3: "mar", 4: "apr", 5: "may", 6: "jun",
    7: "jul", 8: "aug", 9: "sep", 10: "oct", 11: "nov", 12: "dec",
}


def _month_day_specs(sm: int, sd: int, em: int, ed: int) -> list[tuple[str, str]]:
    """開始/終了の月日から、GotoIfTime 用の (months, mdays) ペアの
    リストを返す。GotoIfTime の mdays は単一月内の日にち範囲しか
    表現できないため、月をまたぐ期間 (年またぎの 12/29〜1/3 を含む) は
    月ごとに分割する。
    """
    if sm == em:
        return [(_MONTH_NAMES[sm], f"{sd}-{ed}" if sd != ed else str(sd))]
    specs: list[tuple[str, str]] = []
    m = sm
    while True:
        if m == sm:
            specs.append((_MONTH_NAMES[m], f"{sd}-31"))
        elif m == em:
            specs.append((_MONTH_NAMES[m], f"1-{ed}"))
            break
        else:
            specs.append((_MONTH_NAMES[m], "*"))
        m = (m % 12) + 1
        if len(specs) > 12:  # 安全弁 (通常あり得ない無限ループ防止)
            break
    return specs


def _on_range_check_lines(pattern: DayPattern | None, true_dest: str) -> str:
    """パターンの on_ranges を順にチェックし、いずれかに一致すれば
    true_dest へ Goto する行を生成する (該当が無ければ次の行へ
    フォールスルーするので、呼び出し側で false 相当の処理を続けること)。
    パターン自体が無い (None) 、または on_ranges が空なら「常に一致
    しない」= 終日 OFF として扱うため、何も出力しない。

    true_dest は "コンテキスト名,ラベル名,1" の完全な3要素形式で渡す
    こと (単一ラベルだと「現在の extension 内の named priority」と
    解釈され、別の exten 宣言へジャンプできないため。詳細は
    _nuisance_check_block() のコメント参照)。
    """
    if pattern is None or not (pattern.on_ranges or "").strip():
        return ""
    out = io.StringIO()
    for rng in pattern.on_ranges.split(","):
        rng = rng.strip()
        if not rng:
            continue
        out.write(f" same => n,GotoIfTime({rng},*,*,*?{true_dest})\n")
    return out.getvalue()


def _render_time_condition_context(
    tc: TimeCondition, exceptions: list[CalendarException],
    vm_only: set[str] | None = None,
    national_holidays: list[NationalHoliday] | None = None,
    company_holidays: list[CompanyHoliday] | None = None,
    no_answer_map: dict[str, list[str]] | None = None,
) -> str:
    """時間条件から [timecond-{name}] ダイヤルプランコンテキストを
    生成する。

    判定順序:
      1. カレンダー例外 (祝日・盆休み・年末年始等、優先度順) に該当するか
      2. 該当しなければ、着信曜日の既定パターンを使う
      3. 決定したパターンの on_ranges (営業時間) に現在時刻が入っているか
         で in (時間内)/out (時間外) へ分岐

    GotoIfTime(times,weekdays,mdays,months?labeliftrue:labeliffalse) を
    使用 (Asterisk 公式ドキュメント準拠の構文)。年指定付きのカレンダー
    例外は、GotoIfTime に年の概念が無いため
    ${STRFTIME(,,%Y)} で現在年を取得し GotoIf で先にガードする。
    """
    out = io.StringIO()
    vm_only = vm_only or set()
    national_holidays = national_holidays or []
    company_holidays = company_holidays or []
    no_answer_map = no_answer_map or {}
    ctx = f"timecond-{_ident(tc.name)}"
    # 【重要】Goto()/GotoIf()/GotoIfTime() のジャンプ先は、必ず
    # "コンテキスト名,ラベル名,1" の完全な3要素形式で指定する。
    # "in" のような単一ラベルだと、Asterisk 公式ドキュメントの定義通り
    # 「[[context,]extension,]priority のうち priority だけ指定した」と
    # 解釈され、「現在実行中の extension 内の named priority」を探しに
    # いってしまい、別の exten 宣言へはジャンプできない (実機で
    # "No such label" エラーとなる。迷惑電話ブロック機能で同じ不具合が
    # 実際に発生したため、こちらも同じ方式に統一している)。
    in_dest = f"{ctx},in,1"
    out_dest = f"{ctx},out,1"

    out.write(f"\n[{ctx}]\n")
    out.write(f"exten => s,1,NoOp(時間条件: {_text(tc.name)})\n")

    if tc.use_calendar_exceptions:
        for exc in sorted(exceptions, key=lambda e: e.priority):
            if not exc.enabled:
                continue
            specs = _month_day_specs(exc.start_month, exc.start_day, exc.end_month, exc.end_day)
            guard_label = f"calskip{exc.id}"
            if exc.year:
                out.write(
                    f' same => n,GotoIf($["${{STRFTIME(,,%Y)}}" != "{exc.year}"]?{guard_label})\n'
                )
            for month_name, mdays in specs:
                out.write(f" same => n,GotoIfTime(*,*,{mdays},{month_name}?{ctx},cal{exc.id},1)\n")
            if exc.year:
                # guard_label は「同一 exten (s) 内の次の優先度」への
                # ジャンプなので、単一ラベルのままで正しく動作する。
                out.write(f" same => n({guard_label}),NoOp()\n")

    # --- 会社指定休日 (年末年始・お盆・GW 等) ---
    # 祝日より先に判定する (会社の休みを最優先にする)
    if tc.use_company_holidays:
        for ch in company_holidays:
            if not ch.enabled:
                continue
            specs = _month_day_specs(ch.start_month, ch.start_day, ch.end_month, ch.end_day)
            guard = f"chskip{ch.id}"
            if ch.year:
                out.write(
                    f' same => n,GotoIf($["${{STRFTIME(,,%Y)}}" != "{ch.year}"]?{guard})\n'
                )
            for month_name, mdays in specs:
                out.write(f" same => n,GotoIfTime(*,*,{mdays},{month_name}?{ctx},ch{ch.id},1)\n")
            if ch.year:
                out.write(f" same => n({guard}),NoOp()\n")

    # --- 国民の祝日 (内閣府データ) ---
    # 祝日は年ごとに実日付が違う (成人の日・春分の日など) ため、
    # 年をガードしたうえで日付ごとに判定する。
    if tc.use_national_holidays and national_holidays:
        current_guard_year = None
        for nh in national_holidays:
            if nh.year != current_guard_year:
                if current_guard_year is not None:
                    out.write(f" same => n(nhskip{current_guard_year}),NoOp()\n")
                current_guard_year = nh.year
                out.write(
                    f' same => n,GotoIf($["${{STRFTIME(,,%Y)}}" != "{nh.year}"]'
                    f"?nhskip{nh.year})\n"
                )
            month_name = _MONTH_NAMES[nh.holiday_date.month]
            out.write(
                f" same => n,GotoIfTime(*,*,{nh.holiday_date.day},{month_name}"
                f"?{ctx},nh,1)  ; {_text(nh.name)}\n"
            )
        if current_guard_year is not None:
            out.write(f" same => n(nhskip{current_guard_year}),NoOp()\n")

    for field, _jp in TimeCondition.WEEKDAY_FIELDS:
        out.write(f" same => n,GotoIfTime(*,{field},*,*?{ctx},wd_{field},1)\n")
    # どの曜日にも一致しない事態は通常起き得ないが、安全側で時間外へ。
    out.write(f" same => n,Goto({out_dest})\n")

    if tc.use_calendar_exceptions:
        for exc in sorted(exceptions, key=lambda e: e.priority):
            if not exc.enabled:
                continue
            check = _on_range_check_lines(exc.pattern, in_dest)
            out.write(f"exten => cal{exc.id},1,NoOp(カレンダー例外: {_text(exc.name)})\n")
            if check:
                out.write(check)
            out.write(f" same => n,Goto({out_dest})\n")

    # 指定休日に該当したときの処理 (指定休日ごとにパターンを持てる)
    if tc.use_company_holidays:
        for ch in company_holidays:
            if not ch.enabled:
                continue
            # 指定休日側にパターンがあればそれを、無ければ時間条件側の
            # 「指定休日の扱い」パターンを使う
            pattern = ch.pattern or tc.company_holiday_pattern
            check = _on_range_check_lines(pattern, in_dest)
            out.write(f"exten => ch{ch.id},1,NoOp(会社指定休日: {_text(ch.name)})\n")
            if check:
                out.write(check)
            out.write(f" same => n,Goto({out_dest})\n")

    # 祝日に該当したときの処理 (全祝日で共通のパターンを使う)
    if tc.use_national_holidays and national_holidays:
        check = _on_range_check_lines(tc.national_holiday_pattern, in_dest)
        out.write("exten => nh,1,NoOp(国民の祝日)\n")
        if check:
            out.write(check)
        out.write(f" same => n,Goto({out_dest})\n")

    for field, _jp in TimeCondition.WEEKDAY_FIELDS:
        pattern = getattr(tc, f"{field}_pattern")
        check = _on_range_check_lines(pattern, in_dest)
        out.write(f"exten => wd_{field},1,NoOp(曜日パターン: {_jp}曜日)\n")
        if check:
            out.write(check)
        out.write(f" same => n,Goto({out_dest})\n")

    out.write(f"exten => in,1,NoOp(時間条件 {_text(tc.name)}: 時間内)\n")
    out.write(_dest_goto_line(tc.inside_type, tc.inside_value, vm_only, no_answer_map))
    out.write(" same => n,Hangup()\n")
    out.write(f"exten => out,1,NoOp(時間条件 {_text(tc.name)}: 時間外)\n")
    out.write(_dest_goto_line(tc.outside_type, tc.outside_value, vm_only, no_answer_map))
    out.write(" same => n,Hangup()\n")
    return out.getvalue()


#--------------------------------------------------------------------------
# queues.conf
#--------------------------------------------------------------------------


async def render_queues_conf(db: AsyncSession) -> str:
    queues = (await db.scalars(select(Queue).where(Queue.enabled.is_(True)))).all()
    out = io.StringIO()
    out.write(_HEADER)
    out.write(
        "[general]\n"
        "persistentmembers=yes\n"
        "autofill=yes\n"
        "monitor-type=MixMonitor\n"
        "shared_lastcall=yes\n"
        # strict: メンバーが1人も居ない (または全員一時停止中) の場合、
        # 発信者をキューに入れず即座に次のダイヤルプラン優先度へ進む。
        # これにより [queue-{number}] コンテキストのフォールバック設定
        # (誰もログインしていない時間帯の転送先) が確実に機能する。
        "joinempty=strict\n"
        "leavewhenempty=strict\n\n"
    )
    for q in queues:
        out.write(f"[{_ident(q.queue_number)}]\n")
        out.write(f"; {_text(q.name)}\n")
        out.write(f"strategy={q.strategy}\n")
        out.write(f"timeout={q.timeout}\n")
        out.write(f"retry={q.retry}\n")
        out.write(f"wrapuptime={q.wrapuptime}\n")
        out.write(f"maxlen={q.maxlen}\n")
        out.write(f"musicclass={q.music_class}\n")
        for m in q.members:
            paused = ",1" if m.paused else ",0"
            out.write(f"member => PJSIP/{m.extension},{m.penalty}{paused}\n")
        out.write("\n")
    return out.getvalue()


#--------------------------------------------------------------------------
# voicemail.conf
#--------------------------------------------------------------------------


async def render_voicemail_conf(db: AsyncSession) -> str:
    exts = (
        await db.scalars(
            select(Extension).where(
                Extension.enabled.is_(True), Extension.voicemail_enabled.is_(True)
            )
        )
    ).all()

    app_settings_row = (await db.scalars(select(AppSettings).limit(1))).first()
    announce_dt = bool(
        app_settings_row.voicemail_announce_datetime if app_settings_row else False
    )

    out = io.StringIO()
    out.write(_HEADER)
    out.write(
        "[general]\n"
        "format=wav49|gsm|wav\n"
        "attach=no\n"
        "skipms=3000\n"
        "maxsilence=10\n"
        "silencethreshold=128\n"
        "maxlogins=3\n"
        "moveheard=yes\n"
        "charset=UTF-8\n"
        # envelope: メッセージ再生前に録音日時を読み上げるかどうか。
        #
        # 【重要・実機で確認した制約】
        # Asterisk の日本語モードの日時読み上げ (say.c の
        # ast_say_date_with_format_ja) は、日時の各要素ごとに専用の
        # 音声ファイルを必要とする:
        #   b/B/h (月) → digits/mon-0 〜 digits/mon-11
        #   d/e   (日) → digits/h-1_2 〜 digits/h-30_2, digits/nichi
        #   H/k   (時) → digits/<数字> + digits/ji
        #   M     (分) → digits/<数字> + digits/fun
        # しかし takao-tlasterisk-sound-ja 等の一般的な日本語音声パックには
        # これらのファイルが含まれておらず (digits/ 配下は 0〜50 等の素の
        # 数字のみ)、日時アナウンスを有効にすると必ず
        #   "File digits/h-30_2 does not exist in any format"
        # のエラーになり、*97 でのメッセージ再生がその時点で中断されて
        # 通話が切れてしまう。
        #
        # また m (月) のような英語モード用の指定子は日本語モードでは
        # そもそも解釈されない ("Unknown character in datetime format")。
        #
        # 録音日時は Web 画面の一覧と、留守番電話の通知メール本文
        # (本ツールが Python 側で日本語生成) で確認できるため、電話での
        # 日時アナウンスは無効にしている。
        # 「システム」画面の「留守番電話の録音日時を読み上げる」で
        # 切り替えられる (既定は無効)。
        f"envelope={'yes' if announce_dt else 'no'}\n"
    )
    out.write(
        "; メール通知は Asterisk 自身の sendmail 経由ではなく、本ツール\n"
        "; (Python) が録音完了フック経由で SMTP 送信する (件名・本文・\n"
        "; ファイル名を自由にカスタマイズでき、日時も日本語で確実に\n"
        "; 表示できるため。詳細は README「留守番電話」参照)。\n"
        "; そのため下記 mailbox 定義の email 欄は常に空にし、Asterisk\n"
        "; 自身のメール送信は行わせない。\n"
    )
    out.write("\n")
    out.write(
        "[zonemessages]\n"
        "; 日時アナウンスの書式 (envelope=yes のときに使われる)。\n"
        "; 日本語モードで使える指定子: A/a(曜日) B/b/h(月) d/e(日) Y(年)\n"
        ";   H/k(24時制の時) I/l(12時制の時) P/p(午前午後) M(分) S(秒)\n"
        ";   Q(今日/昨日/日付) q(同左・短縮) R(時:分) T(時:分:秒)\n"
        "; 英語モード用の m (月) 等は日本語モードでは解釈されないので\n"
        "; 使わないこと。またシングルクォートで囲んだ部分は「その名前の\n"
        "; 音声ファイルを再生する」意味なので、日本語テキストは書けない。\n"
        "japan=Asia/Tokyo|b d k M\n\n"
    )
    out.write("[default]\n")
    for e in exts:
        pin = e.voicemail_pin or "1234"
        # mailbox => pin,name,email,pager,options
        # email 欄は常に空 (理由は上記コメント参照)。実際のメール
        # 送信要否・宛先・添付有無・送信後削除は、本ツールが
        # Extension.voicemail_email 等を見て録音完了フック内で判定する。
        out.write(f"{e.extension} => {pin},{e.display_name},,,tz=japan\n")
    return out.getvalue()


# ===========================================================================
# musiconhold.conf — 保留音
# ===========================================================================


async def render_musiconhold_conf(db: AsyncSession) -> str:
    """保留音クラスごとに [section] を生成 (Asterisk 22 構文)。

    Asterisk の musiconhold.conf は `key => value` 構文 (一部 `=` でも可だが
    公式 sample に合わせる)。

    files モードで、各クラスのディレクトリ配下のファイルを並べる。
    Asterisk は拡張子なしで指定するため、files= は格納フォルダで列挙。
    """
    classes = list(
        (await db.scalars(select(MohClass).where(MohClass.enabled.is_(True)))).all()
    )

    out = io.StringIO()
    out.write(_HEADER)

    # default クラスが無い場合のフォールバック (Asterisk が起動時に文句を言わないように)
    has_default = any(c.is_default or c.class_name == "default" for c in classes)
    if not has_default:
        out.write(
            ";=== フォールバック default クラス (DB 未登録時)==\n"
            "[default]\n"
            "mode => files\n"
            f"directory => {settings.asterisk_sounds_dir}/moh\n"
            "sort => random\n\n"
        )

    for c in classes:
        sec = "default" if c.is_default else c.class_name
        out.write(f";=== {_text(c.display_name)} ===\n")
        out.write(f"[{sec}]\n")
        out.write("mode => files\n")
        out.write(f"directory => {settings.asterisk_sounds_dir}/moh\n")
        out.write(f"sort => {c.sort_mode}\n")
        out.write("\n")
    return out.getvalue()


# ===========================================================================
# res_parking.conf — コールパーク (Asterisk 22)
# ===========================================================================
# 注意: Asterisk 12+ では parking は res_parking モジュールで処理される。
#       ファイル名は features.conf ではなく res_parking.conf。
#       構文も '=' ではなく '=>' を使う必要がある。


async def render_res_parking_conf(db: AsyncSession) -> str:
    """res_parking モジュール用の parkinglot 定義 (Asterisk 22 構文)。

    要点:
      - すべて `key => value` 構文 (`=` ではない)
      - `parkingtime` (時間秒)
      - `comebacktoorigin = yes` のときは comebackcontext は不要
      - `comebacktoorigin = no` のときに comebackcontext を指定
    """
    lots = list(
        (await db.scalars(select(ParkingLot).where(ParkingLot.enabled.is_(True)))).all()
    )

    out = io.StringIO()
    out.write(_HEADER)
    out.write(
        "[general]\n"
        "parkeddynamic => yes\n\n"
    )

    if not lots:
        # 'default' は名前付き parkinglot が指定されないときに使われる必須セクション
        out.write(
            ";=== default パーク (DB 未登録時のフォールバック)==\n"
            "[default]\n"
            "parkext => 700\n"
            "parkpos => 701-720\n"
            "context => parkedcalls\n"
            "parkingtime => 120\n"
            "comebacktoorigin => no\n"
            "comebackcontext => from-internal\n"
            "comebackdialtime => 30\n"
            "courtesytone => beep\n"
            "parkedplay => caller\n"
            "findslot => first\n"
            "parkedmusicclass => default\n\n"
        )
    else:
        # res_parking.so は [default] という名前のロットが最低 1 つ
        # 存在しないと初期化に失敗する ("Module not initialized")。
        # DB上で誰も is_default=True になっていない場合 (Web UI で
        # 「デフォルト」のチェックを入れ忘れた等)、[park] のような
        # 独自名だけのロットしか無い状態になり、この初期化エラーを
        # 引き起こす。ユーザーの設定ミスに関わらず必ず [default] が
        # 1 つ出力されるよう、誰も is_default でなければ最初の
        # (enabled な) ロットを強制的に default 扱いにする。
        has_explicit_default = any(lot.is_default for lot in lots)
        for idx, lot in enumerate(lots):
            is_this_default = lot.is_default or (not has_explicit_default and idx == 0)
            sec = "default" if is_this_default else lot.name
            out.write(f";=== {_text(lot.name)} ===\n")
            out.write(f"[{sec}]\n")
            out.write(f"parkext => {lot.park_ext}\n")
            out.write(f"parkpos => {lot.park_pos_start}-{lot.park_pos_end}\n")
            out.write("context => parkedcalls\n")
            out.write(f"parkingtime => {lot.park_time_seconds}\n")
            # comeback_context が未設定なら parker (パークした人) に戻す。
            # 設定済みならそのコンテキストへ戻す。
            # 注意: Asterisk は comebackcontext の右辺が空だと
            # res_parking モジュールがロード失敗するため、必ず値ありで出力する。
            cb_ctx = (lot.comeback_context or "").strip()
            if cb_ctx:
                out.write("comebacktoorigin => no\n")
                out.write(f"comebackcontext => {cb_ctx}\n")
            else:
                out.write("comebacktoorigin => yes\n")
            out.write("comebackdialtime => 30\n")
            out.write("courtesytone => beep\n")
            out.write("parkedplay => caller\n")
            out.write(f"findslot => {lot.findslot or 'first'}\n")
            moh = (lot.moh_class or "").strip() or "default"
            out.write(f"parkedmusicclass => {moh}\n\n")

    return out.getvalue()


# ===========================================================================
# 静的 conf 群 (DB に依存しないが必須なもの)
# ===========================================================================


def render_asterisk_conf() -> str:
    """asterisk.conf — ディレクトリ配置とランタイム (Asterisk 22)。

    `[directories]` セクションでパス定義、`[options]` でランタイム設定。
    """
    return _HEADER + """[directories]
astetcdir => /etc/asterisk
astmoddir => /usr/lib/asterisk/modules
astvarlibdir => /var/lib/asterisk
astdbdir => /var/lib/asterisk
astkeydir => /var/lib/asterisk
astdatadir => /var/lib/asterisk
astagidir => /var/lib/asterisk/agi-bin
astspooldir => /var/spool/asterisk
astrundir => /var/run/asterisk
astlogdir => /var/log/asterisk
astsbindir => /usr/sbin

[options]
verbose = 3
debug = 0
maxcalls = 0
maxload = 0.9
cache_record_files = yes
record_cache_dir = /tmp
transcode_via_sln = yes
runuser = asterisk
rungroup = asterisk
languageprefix = yes
defaultlanguage = ja
"""


def render_modules_conf() -> str:
    """modules.conf (Asterisk 20/22 LTS 対応)。

    方針: autoload=yes に任せ、特定モジュールだけ明示制御する。
    Stasis 等は内部依存があるので autoload に任せる方が安全。

    Asterisk 21+ で完全削除されたモジュール (chan_sip, app_macro, chan_mgcp,
    chan_skinny, chan_alsa, app_meetme, res_monitor, app_cdr, app_osplookup)
    は記述しない (load/noload どちらでも警告/エラー)。
    """
    return _HEADER + """[modules]
autoload=yes

; - 明示的に無効化したい古い/不要モジュール (Asterisk 20 のみ)--
; chan_sip は 21 で削除済。20 でも PJSIP に統一するため無効化。
noload => chan_sip.so

; res_hep* (Homer SIP capture) — 未使用環境では除外
noload => res_hep.so
noload => res_hep_pjsip.so
noload => res_hep_rtcp.so

; PRI/DAHDI 系 — 不要環境では除外 (ハードウェアなしでロード失敗するため)
noload => chan_dahdi.so
noload => res_pjsip_dlg_options.so

; Adaptive RealTime 系 — 使わない
noload => res_config_pgsql.so
noload => res_config_mysql.so
noload => res_config_odbc.so

; FAX (spandsp) は autoload=yes により自動ロードされる:
;   res_fax.so          (FAX コア / ReceiveFAX・SendFAX アプリ)
;   res_fax_spandsp.so  (spandsp バックエンド: T.38 / G.711 FAX)
; 明示ロードしたい場合は下記コメントを外す:
; load => res_fax.so
; load => res_fax_spandsp.so
"""


def render_logger_conf() -> str:
    """logger.conf — ログ出力。

    'fax' レベルを console/full に追加しておくと、res_fax_spandsp の
    T.30 プロトコルレベルの詳細ログ (相手からの DIS/DCS 検知有無等、
    送信側の frame ログだけでは分からない受信側の状況) が出力される
    ようになる。これが無いと 'fax set debug on' を打っても実質何も
    出力されない (Asterisk の既知の挙動: logger.conf 側で明示的に
    'fax' レベルを有効にする必要がある)。
    """
    return _HEADER + """[general]
dateformat=%F %T.%3q
queue_log=yes
rotatestrategy=rotate

[logfiles]
console => notice,warning,error,fax
messages => notice,warning,error
full => notice,warning,error,debug,verbose,dtmf,fax
"""


def render_rtp_conf() -> str:
    """rtp.conf — RTP ポート範囲。"""
    return _HEADER + f"""[general]
rtpstart={settings.rtp_start}
rtpend={settings.rtp_end}
rtpchecksums=no
strictrtp=yes
icesupport=no
"""


def render_manager_conf() -> str:
    """manager.conf — AMI。"""
    return _HEADER + f"""[general]
enabled = yes
port = 5038
bindaddr = 0.0.0.0
displayconnects = no
allowmultiplelogin = yes

[{settings.ami_user}]
secret = {settings.ami_secret}
deny = 0.0.0.0/0.0.0.0
permit = {settings.ami_permit}
read = system,call,log,verbose,command,agent,user,config,dialplan,reporting,cdr,originate
write = system,call,log,verbose,command,agent,user,config,dialplan,reporting,originate,reload
writetimeout = 5000
"""


def render_stasis_conf() -> str:
    """stasis.conf — Stasis モジュール設定 (Asterisk 20/22)。

    最低限 [declined_message_types] セクションが存在する必要がある
    (Asterisk が起動時に必須で読みに行く)。
    通常はデフォルト動作で OK なので空セクションだけ置く。
    """
    return _HEADER + """[threadpool]
; デフォルト動作のまま (initial_size=5, idle_timeout=20, max_size=50)

[declined_message_types]
; この空セクションが必須。Stasis 初期化に失敗する。
; 特定のメッセージタイプを拒否したい場合のみ decline= を書く。
"""


def render_cdr_conf() -> str:
    """cdr.conf — Call Detail Record。"""
    return _HEADER + """[general]
enable=yes
unanswered=no
endbeforehexten=no
initiatedseconds=no

[csv]
usegmtime=no
loaduniqueid=yes
accountlogs=yes
"""


def render_acl_conf() -> str:
    """acl.conf — IP アクセス制御。空でも置いておく。"""
    return _HEADER + """[general]
; named ACL を定義する場合はここに [acl_name] セクションを追加。
; 例:
;   [local_only]
;   deny=0.0.0.0/0.0.0.0
;   permit=192.168.0.0/255.255.0.0
"""


def render_indications_conf() -> str:
    """indications.conf — トーン定義 (日本)。"""
    return _HEADER + """[general]
country=jp

[jp]
description = Japan
ringcadence = 1000,2000
dial = 400
busy = 400/500,0/500
ring = 400*25/1000,0/2000
congestion = 400/250,0/250
callwaiting = 440/300,0/9700
dialrecall = !350+440/100,!0/100,!350+440/100,!0/100,!350+440/100,!0/100,350+440
record = 1400/500,0/15000
info = !950/330,!1400/330,!1800/330,0
"""


def render_udptl_conf() -> str:
    """udptl.conf — T.38 FAX 用 UDPTL ポート範囲。"""
    return _HEADER + """[general]
udptlstart=4000
udptlend=4999
udptlchecksums=no
udptlfecentries=3
udptlfecspan=3
use_even_ports=no
"""
    """manager.conf — AMI。"""
    return _HEADER + f"""[general]
enabled = yes
port = 5038
bindaddr = 0.0.0.0
displayconnects = no
allowmultiplelogin = yes

[{settings.ami_user}]
secret = {settings.ami_secret}
deny = 0.0.0.0/0.0.0.0
permit = {settings.ami_permit}
read = system,call,log,verbose,command,agent,user,config,dialplan,reporting,cdr,originate
write = system,call,log,verbose,command,agent,user,config,dialplan,reporting,originate,reload
writetimeout = 5000
"""


def render_features_conf() -> str:
    """features.conf — 通話中の機能ボタン (Asterisk 22 対応)。

    重要: Asterisk 12+ ではパーク設定 (parkext / parkpos / parkingtime /
    findslot / parkedmusicclass 等) は features.conf ではなく
    res_parking.conf に移行している。
    features.conf には [general] の DTMF タイミング設定と、
    [featuremap] (転送/録音/切断のキー割当て) のみ残す。

    *0 でパーク、*1 で転送 (盲目)、*2 で転送 (相談付き)、*4 で通話録音 (MixMonitor)。
    Dial の T/t フラグと連動して機能する。

    注意: [featuremap] の `automon` (Monitor アプリによる録音) は
    main/features_config.c の aco オプション一覧から既に削除されており、
    Asterisk 22 で指定すると
      "Could not find option suitable for category 'featuremap' named 'automon'"
    というエラーで featuremap セクション全体の読み込みが失敗する
    (features.conf.sample のコメント例には今も残っているが実際は無効)。
    録音機能は `automixmon` (MixMonitor ベース) のみを使う。
    """
    return _HEADER + """[general]
transferdigittimeout => 3
xfersound => beep
xferfailsound => beeperr
pickupexten => *8
featuredigittimeout => 1000
atxfernoanswertimeout => 15
atxferdropcall => no
atxferloopdelay => 10
atxfercallbackretries => 2

[featuremap]
blindxfer => *1
atxfer => *2
disconnect => **
automixmon => *4
parkcall => *0
"""


#--------------------------------------------------------------------------
# まとめて書き出し
#--------------------------------------------------------------------------


async def write_all_configs(
    db: AsyncSession,
    out_dir: Path | None = None,
    include_bootstrap: bool = False,
) -> dict[str, Path]:
    """全 conf を生成してファイルに保存。書き込んだパスを返す。

    通常モード (include_bootstrap=False):
        DB 由来の設定ファイルのみ生成。Asterisk が apt 等から
        インストールした /etc/asterisk/ に「上書き」する想定。
        起動に必須な asterisk.conf / modules.conf / stasis.conf 等は
        Asterisk パッケージのものをそのまま使う。
        対象: pjsip / extensions / queues / voicemail / musiconhold /
              res_parking / features / rtp / manager

    Bootstrap モード (include_bootstrap=True):
        完全に空の /etc/asterisk/ にこのディレクトリを置けば動くよう
        起動に必須な静的 conf も含めて生成する。
        通常使うことはない (Asterisk を自前ビルドした特殊環境向け)。
        対象: 通常モード + asterisk / modules / logger / stasis /
              cdr / acl / indications / udptl
    """
    out_dir = out_dir or settings.asterisk_config_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    files: dict[str, str] = {}

    #-- DB 由来 (常に上書きする)--
    files["pjsip.conf"] = await render_pjsip_conf(db)
    files["extensions.conf"] = await render_extensions_conf(db)
    files["queues.conf"] = await render_queues_conf(db)
    files["voicemail.conf"] = await render_voicemail_conf(db)
    files["musiconhold.conf"] = await render_musiconhold_conf(db)
    # Asterisk 12+: パークは res_parking.conf
    files["res_parking.conf"] = await render_res_parking_conf(db)

    #-- DB 由来ではないが運用設定 (このアプリで管理する)--
    files["features.conf"] = render_features_conf()
    files["rtp.conf"] = render_rtp_conf()
    files["manager.conf"] = render_manager_conf()

    #-- Bootstrap モード: Asterisk 起動に必須の静的 conf--
    if include_bootstrap:
        files["asterisk.conf"] = render_asterisk_conf()
        files["modules.conf"] = render_modules_conf()
        files["logger.conf"] = render_logger_conf()
        files["stasis.conf"] = render_stasis_conf()
        files["cdr.conf"] = render_cdr_conf()
        files["acl.conf"] = render_acl_conf()
        files["indications.conf"] = render_indications_conf()
        files["udptl.conf"] = render_udptl_conf()

    written: dict[str, Path] = {}
    for name, content in files.items():
        p = out_dir / name
        p.write_text(content, encoding="utf-8")
        written[name] = p
    return written
