#!/usr/bin/env bash
# Asterisk 日本語音声プロンプト (takao-t/asterisk-sound-ja) インストールスクリプト。
#
# 配布元:
#   https://github.com/takao-t/asterisk-sound-ja  (GitHub アカウント: takao-t)
#   音源: Google Cloud TTS で生成された日本語音声 (wav 形式, 約 14MB / 約 540 ファイル)
#
# 【ライセンスについて — 必ずお読みください】
#   このリポジトリには LICENSE ファイルが無く、readme.txt にも利用条件の
#   記載がありません (2026-10-02 時点で確認)。
#   ライセンスが示されていない著作物は、既定では著作権者が全ての権利を
#   留保している状態です。つまり、
#     - 再配布してよいか
#     - business/商用環境で使ってよいか
#     - 改変してよいか
#   のいずれも、配布元から許諾を得ない限り保証されません。
#
#   そのため本スクリプトは、音声ファイルを本ツールに同梱せず、実行時に
#   配布元から利用者のサーバーへ直接ダウンロードするだけにしてあります
#   (本ツールは音声を再配布しません)。
#
#   業務で使用する場合は、配布元 (上記 GitHub) へ利用可否を確認して
#   ください。確認が取れない場合は、このスクリプトを使わず、本ツールの
#   音声合成機能 (Open JTalk) で必要な音声を生成する方法もあります。
#
#   なお音源は Google Cloud Text-to-Speech の出力です。生成物の利用条件
#   については Google Cloud の利用規約も併せてご確認ください。
#
# 使い方:
#   sudo bash install_japanese_sounds.sh
#
#   root は必須ではない。展開先 (既定 /var/lib/asterisk/sounds) に書き込める
#   ユーザーであれば実行できるため、本ツールの Web UI (「システム」画面) から
#   サービス実行ユーザーのまま実行できる。
#
# 動作:
#   1. GitHub raw から core-sound-ja.tgz をダウンロード (約 14MB)
#   2. /var/lib/asterisk/sounds/ に展開 (内部に ja/ ディレクトリができる)
#   3. 所有者を asterisk:asterisk に (root で実行した場合のみ)
#   4. Asterisk に dialplan reload を通知 (利用可能なら)
#
# Asterisk は wav から ulaw/alaw 等への変換を実行時に自動で行うため、
# wav のみで全コーデック (ひかり電話の ulaw 含む) に対応します。
#
# 既にインストール済みでも上書きで再展開されます (冪等)。
#
# 環境変数:
#   SOUNDS_ROOT      既定 "/var/lib/asterisk/sounds"
#                    (展開先。実際の音声は SOUNDS_ROOT/ja/ に置かれる)
#   DOWNLOAD_URL     既定 "https://raw.githubusercontent.com/takao-t/asterisk-sound-ja/master/core-sound-ja.tgz"
#   SKIP_RELOAD      "1" にすると最後の reload をスキップ

# ---------------------------------------------------------------------------
# bash で実行されているか確認する
#
# `sh install_all.sh` のように起動されると、Ubuntu では sh の実体が dash に
# なっているため `set -o pipefail` が無く、
#   install_all.sh: 40: set: Illegal option -o pipefail
# で即座に止まってしまう (シェバン行 #!/usr/bin/env bash は、sh に引数として
# 渡された場合は無視されるため)。
# ここで自分自身を bash で実行し直して、どちらの呼び出し方でも動くようにする。
# ※ この判定は dash でも解釈できる書き方にしておくこと。
# ---------------------------------------------------------------------------
if [ -z "${BASH_VERSION:-}" ]; then
    if command -v bash >/dev/null 2>&1; then
        exec bash "$0" "$@"
    fi
    echo "エラー: このスクリプトの実行には bash が必要です。" >&2
    echo "  sudo apt install -y bash" >&2
    exit 1
fi

set -euo pipefail

SOUNDS_ROOT="${SOUNDS_ROOT:-/var/lib/asterisk/sounds}"
DOWNLOAD_URL="${DOWNLOAD_URL:-https://raw.githubusercontent.com/takao-t/asterisk-sound-ja/master/core-sound-ja.tgz}"
SKIP_RELOAD="${SKIP_RELOAD:-0}"

log() { echo "[$(date '+%H:%M:%S')] $*"; }
err() { echo "ERROR: $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 権限の確認
#
# 以前はここで無条件に root を要求していた。しかし本ツールの Web UI
# (「システム」画面のインストールボタン) はサービス実行ユーザー
# (既定 asterisk) でこのスクリプトを起動するため、root 要求のせいで
#   ERROR: このスクリプトは root で実行してください
# となり、画面からは一度も成功しなかった。
#
# 実際に必要なのは root 権限ではなく「展開先に書き込めること」だけで、
# 既定の /var/lib/asterisk/ はインストーラが asterisk 所有にしている
# ため、サービスユーザーでもそのまま展開できる。
# そこで root かどうかではなく、書き込めるかどうかで判定する。
# ---------------------------------------------------------------------------
IS_ROOT=0
[ "$(id -u)" -eq 0 ] && IS_ROOT=1

# 展開先に書き込めるか (無ければ親ディレクトリに作れるか) を確かめる。
check_writable() {
    target="$1"
    probe="$target"
    # 存在する一番近い親まで遡る
    while [ ! -e "$probe" ] && [ "$probe" != "/" ]; do
        probe="$(dirname "$probe")"
    done
    [ -w "$probe" ]
}

if ! check_writable "${SOUNDS_ROOT}"; then
    echo "ERROR: ${SOUNDS_ROOT} に書き込めません ($(id -un) で実行中)" >&2
    echo "" >&2
    echo "次のどちらかで解決できます:" >&2
    echo "  1. root で実行する" >&2
    echo "       sudo bash $0" >&2
    echo "  2. 本ツールの実行ユーザーに書き込み権限を与える" >&2
    echo "       sudo FIX_PERMISSIONS=1 bash scripts/setup_dependencies.sh" >&2
    exit 1
fi

# 必要コマンド
for cmd in curl tar; do
    command -v $cmd >/dev/null 2>&1 \
        || err "$cmd が必要です。先に 'apt install $cmd' を実行してください"
done

# 展開先の親ディレクトリを準備
log "保存先: ${SOUNDS_ROOT}/ja/"
mkdir -p "${SOUNDS_ROOT}"

# ダウンロード
tmpdir="$(mktemp -d /tmp/asterisk-ja-sounds.XXXXXX)"
trap 'rm -rf "$tmpdir"' EXIT

tarball="${tmpdir}/core-sound-ja.tgz"
log "ダウンロード中: ${DOWNLOAD_URL}"
if ! curl -fsSL --connect-timeout 15 --max-time 600 -o "${tarball}" "${DOWNLOAD_URL}"; then
    err "ダウンロード失敗: ${DOWNLOAD_URL}"
fi
size=$(stat -c%s "${tarball}" 2>/dev/null || echo 0)
if [ "${size:-0}" -lt 1000000 ]; then
    err "ダウンロードしたファイルが小さすぎます (${size} bytes)。URL もしくはネットワークを確認してください。"
fi
log "  → 取得完了 ($(numfmt --to=iec "${size:-0}"))"

# 再インストールの場合、既存の ja/ が別ユーザー所有だと展開だけが失敗して
# 分かりにくいので、先に確認して具体的に知らせる。
# (例: 以前 sudo で入れたあと、画面から再実行した場合)
if [ -d "${SOUNDS_ROOT}/ja" ] && [ ! -w "${SOUNDS_ROOT}/ja" ]; then
    owner="$(stat -c '%U' "${SOUNDS_ROOT}/ja" 2>/dev/null || echo '不明')"
    echo "ERROR: ${SOUNDS_ROOT}/ja が ${owner} 所有のため上書きできません" \
         "($(id -un) で実行中)" >&2
    echo "" >&2
    echo "次のどちらかで解決できます:" >&2
    echo "  1. root で実行する" >&2
    echo "       sudo bash $0" >&2
    echo "  2. 所有者を本ツールの実行ユーザーに変更する" >&2
    echo "       sudo chown -R $(id -un):$(id -gn) ${SOUNDS_ROOT}/ja" >&2
    exit 1
fi

# 展開
log "展開中: ${tarball} → ${SOUNDS_ROOT}/"
# tarball 内は ja/xxx.wav の構造なので SOUNDS_ROOT 直下に展開すると
# SOUNDS_ROOT/ja/xxx.wav になる。
tar -xzf "${tarball}" -C "${SOUNDS_ROOT}" || err "展開失敗"

# 所有者・パーミッション
# chown は root でしか実行できない。非 root (Web UI からの実行) の場合は
# 展開したファイルが実行ユーザー所有になっており、Asterisk も同じ
# ユーザーで動いているため、そのままで読み取れる。
if [ "$IS_ROOT" -eq 1 ] && id asterisk >/dev/null 2>&1; then
    log "所有者を asterisk:asterisk に設定"
    chown -R asterisk:asterisk "${SOUNDS_ROOT}/ja"
else
    log "所有者の変更は skip ($(id -un) で実行中 / 展開したファイルは $(id -un) 所有)"
fi
# chmod は所有者であれば非 root でも実行できる。所有者でないファイルが
# 混ざっていてもインストール自体は成功しているので、失敗は無視する。
find "${SOUNDS_ROOT}/ja" -type d -exec chmod 755 {} \; 2>/dev/null || true
find "${SOUNDS_ROOT}/ja" -type f -exec chmod 644 {} \; 2>/dev/null || true

# 簡易動作確認: 主要ファイルがあるか
sample_files=("vm-intro" "auth-thankyou" "hello-world" "jp-arimasu")
log "インストール結果確認:"
ok=0; ng=0
for base in "${sample_files[@]}"; do
    found=$(find "${SOUNDS_ROOT}/ja" -name "${base}.wav" -print -quit 2>/dev/null || true)
    if [ -n "$found" ]; then
        echo "  OK : ${base}.wav"
        ok=$((ok+1))
    else
        echo "  NG : ${base}.wav (見つかりません)"
        ng=$((ng+1))
    fi
done

if [ $ok -eq 0 ]; then
    err "ファイルが正しく展開されていません。${SOUNDS_ROOT}/ja/ を確認してください。"
fi

total_files=$(find "${SOUNDS_ROOT}/ja" -type f | wc -l)
log "総ファイル数: ${total_files}"
log "ファイル形式: wav (Asterisk が再生時に必要なコーデックへ自動変換)"

# Asterisk reload (利用可能なら)
if [ "${SKIP_RELOAD}" != "1" ] && command -v asterisk >/dev/null 2>&1; then
    if asterisk -rx "core show version" >/dev/null 2>&1; then
        log "Asterisk に dialplan reload を通知"
        asterisk -rx "dialplan reload" >/dev/null 2>&1 || true
    else
        log "(Asterisk は稼働中でないため reload はスキップ)"
    fi
fi

log "完了: 日本語音声プロンプトを ${SOUNDS_ROOT}/ja/ にインストールしました。"
log "次にやること:"
log "  1. 本ツールの Web UI の「システム」画面でシステム言語が 'ja' であることを確認"
log "  2. ダッシュボードの「変更を Asterisk へ反映」で pjsip.conf を再生成"
log "     (内線エンドポイントに language=ja が付与されます)"
log ""
log ""
log "配布元: https://github.com/takao-t/asterisk-sound-ja"
log "【注意】配布元にライセンスの記載がありません。既定では著作権者が全ての"
log "        権利を留保している状態です。業務で使用する場合は配布元へ利用可否を"
log "        確認してください。本ツールはこの音声を再配布していません"
log "        (実行時に配布元から直接ダウンロードしています)。"
