#!/usr/bin/env bash
# Open JTalk の追加音響モデル (声) をインストールするスクリプト。
#
# Ubuntu の標準リポジトリには男性の声 (nitech-jp-atr503-m001) しか
# 用意されていないため、女性の声などを使いたい場合にこれを実行する。
#
# 使い方:
#   sudo bash install_tts_voices.sh          # 全て導入
#   sudo bash install_tts_voices.sh mei      # メイ (女性) のみ
#   sudo bash install_tts_voices.sh tohoku   # 東北 f01 (女性) のみ
#
# 導入されるもの:
#   mei    : MMDAgent Example に含まれる女性音声「メイ」
#            (通常/喜び/悲しみ/怒り/照れ の 5 種類)
#            名古屋工業大学 MMDAgent プロジェクト
#   tohoku : htsvoice-tohoku-f01 の女性音声
#            (通常/喜び/悲しみ/怒り の 4 種類)
#
# 導入後は Web 画面「音源」→「文章から音声を作る」の
# 「声」ドロップダウンに自動的に現れます (再起動不要)。

set -euo pipefail

VOICE_DIR="/usr/share/hts-voice"
TARGET="${1:-all}"

log()  { echo "[$(date '+%H:%M:%S')] $*"; }
ok()   { echo "[$(date '+%H:%M:%S')]  OK  $*"; }
warn() { echo "[$(date '+%H:%M:%S')] WARN $*" >&2; }
err()  { echo "ERROR: $*" >&2; exit 1; }

if [ "$(id -u)" -ne 0 ]; then
    err "root で実行してください (sudo bash $0)"
fi

for cmd in curl unzip; do
    command -v "$cmd" >/dev/null 2>&1 || {
        log "$cmd が無いためインストールします"
        apt-get install -y "$cmd" || err "$cmd のインストールに失敗しました"
    }
done

mkdir -p "$VOICE_DIR"
TMPDIR_WORK="$(mktemp -d)"
# shellcheck disable=SC2064
trap "rm -rf '$TMPDIR_WORK'" EXIT

install_mei() {
    if [ -f "$VOICE_DIR/mei/mei_normal.htsvoice" ]; then
        ok "メイ (女性) は導入済みです"
        return 0
    fi
    local url="https://sourceforge.net/projects/mmdagent/files/MMDAgent_Example/MMDAgent_Example-1.8/MMDAgent_Example-1.8.zip/download"
    log "メイ (女性音声) をダウンロード中… (約 100MB)"
    if ! curl -fL --connect-timeout 20 --max-time 900 \
         -o "$TMPDIR_WORK/mmdagent.zip" "$url"; then
        warn "ダウンロードに失敗しました (ネットワーク/配布元の状況をご確認ください)"
        return 1
    fi
    log "展開中…"
    unzip -qo "$TMPDIR_WORK/mmdagent.zip" -d "$TMPDIR_WORK" \
        || { warn "展開に失敗しました"; return 1; }
    local src
    src="$(find "$TMPDIR_WORK" -type d -name mei -path '*Voice*' | head -1)"
    if [ -z "$src" ]; then
        warn "アーカイブ内に Voice/mei が見つかりませんでした"
        return 1
    fi
    mkdir -p "$VOICE_DIR/mei"
    cp "$src"/*.htsvoice "$VOICE_DIR/mei/" \
        || { warn "コピーに失敗しました"; return 1; }
    ok "メイ (女性) を導入しました: $(find "$VOICE_DIR/mei" -name '*.htsvoice' | wc -l) 種類"
}

install_tohoku() {
    if [ -f "$VOICE_DIR/tohoku-f01/tohoku-f01-neutral.htsvoice" ]; then
        ok "東北 f01 (女性) は導入済みです"
        return 0
    fi
    log "東北 f01 (女性音声) をダウンロード中…"
    local base="https://raw.githubusercontent.com/icn-lab/htsvoice-tohoku-f01/master"
    mkdir -p "$VOICE_DIR/tohoku-f01"
    local got=0
    for v in neutral happy sad angry; do
        if curl -fsL --connect-timeout 20 --max-time 300 \
             -o "$VOICE_DIR/tohoku-f01/tohoku-f01-$v.htsvoice" \
             "$base/tohoku-f01-$v.htsvoice"; then
            got=$((got + 1))
        else
            warn "tohoku-f01-$v の取得に失敗しました"
            rm -f "$VOICE_DIR/tohoku-f01/tohoku-f01-$v.htsvoice"
        fi
    done
    if [ "$got" -eq 0 ]; then
        warn "東北 f01 を 1 つも取得できませんでした"
        rmdir "$VOICE_DIR/tohoku-f01" 2>/dev/null || true
        return 1
    fi
    ok "東北 f01 (女性) を導入しました: ${got} 種類"
}

case "$TARGET" in
    all)
        install_mei || warn "メイの導入をスキップしました"
        install_tohoku || warn "東北 f01 の導入をスキップしました"
        ;;
    mei)    install_mei ;;
    tohoku) install_tohoku ;;
    *)      err "不明な指定です: $TARGET (all / mei / tohoku のいずれか)" ;;
esac

echo ""
echo "========================================================="
echo " 現在インストールされている音響モデル"
echo "========================================================="
find "$VOICE_DIR" -name "*.htsvoice" | sort | sed 's|^|  |'
echo ""
echo "Web 画面「音源」→「文章から音声を作る」の「声」から選べます。"
