#!/usr/bin/env bash
# Asterisk 日本語音声プロンプト (takao-t/asterisk-sound-ja) インストールスクリプト。
#
# 出典:
#   https://github.com/takao-t/asterisk-sound-ja
#   作者: 高橋たかお氏 (voipinfo.jp 管理者)
#   音源: Google GCP TTS で生成された日本語音声 (wav 形式, 約 14MB / 約 540 ファイル)
#
# 使い方:
#   sudo bash install_japanese_sounds.sh
#
# 動作:
#   1. GitHub raw から core-sound-ja.tgz をダウンロード (約 14MB)
#   2. /var/lib/asterisk/sounds/ に展開 (内部に ja/ ディレクトリができる)
#   3. 所有者を asterisk:asterisk に
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

set -euo pipefail

SOUNDS_ROOT="${SOUNDS_ROOT:-/var/lib/asterisk/sounds}"
DOWNLOAD_URL="${DOWNLOAD_URL:-https://raw.githubusercontent.com/takao-t/asterisk-sound-ja/master/core-sound-ja.tgz}"
SKIP_RELOAD="${SKIP_RELOAD:-0}"

log() { echo "[$(date '+%H:%M:%S')] $*"; }
err() { echo "ERROR: $*" >&2; exit 1; }

# root 確認
if [ "$(id -u)" -ne 0 ]; then
    err "このスクリプトは root で実行してください (sudo bash $0)"
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

# 展開
log "展開中: ${tarball} → ${SOUNDS_ROOT}/"
# tarball 内は ja/xxx.wav の構造なので SOUNDS_ROOT 直下に展開すると
# SOUNDS_ROOT/ja/xxx.wav になる。
tar -xzf "${tarball}" -C "${SOUNDS_ROOT}" || err "展開失敗"

# 所有者・パーミッション
if id asterisk >/dev/null 2>&1; then
    log "所有者を asterisk:asterisk に設定"
    chown -R asterisk:asterisk "${SOUNDS_ROOT}/ja"
fi
find "${SOUNDS_ROOT}/ja" -type d -exec chmod 755 {} \;
find "${SOUNDS_ROOT}/ja" -type f -exec chmod 644 {} \;

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
log "出典: https://github.com/takao-t/asterisk-sound-ja (CC0 / Google TTS 生成)"
