#!/usr/bin/env bash
# Asterisk 22 (LTS) 本体をソースから再ビルドしてアップデートするスクリプト。
#
# ⚠️ 重要: これは Web UI からのワンクリック実行を想定していません。
#   - コンパイルに数分〜十数分かかります (サーバースペック次第)
#   - 実行中〜再起動時は全通話が切断されます
#   - 必ず営業時間外・メンテナンスウィンドウで、SSH 接続した状態で
#     手動実行してください
#
# ---------------------------------------------------------------------------
# 使い方
# ---------------------------------------------------------------------------
#
#   sudo bash update_asterisk.sh
#     最新の 22 系安定版 (22-current) に更新します。最も一般的な使い方。
#
#   sudo ASTERISK_VERSION=22.10.0 bash update_asterisk.sh
#     バージョンを明示指定します (例: 22.10.0)。特定バージョンで検証
#     したい場合や、最新版で不具合が出て少し前のバージョンに戻したい
#     場合に使います。指定できる値は
#     https://downloads.asterisk.org/pub/telephony/asterisk/ の
#     ファイル一覧 (asterisk-<バージョン>.tar.gz) を参照してください。
#
#   sudo SRC_DIR=/usr/local/src bash update_asterisk.sh
#     ソースの展開先を変えたい場合 (既定 /usr/src)。通常は変更不要です。
#
#   sudo ASTERISK_SERVICE_NAME=asterisk bash update_asterisk.sh
#     systemd のサービス名が環境によって異なる場合に指定します
#     (通常は既定の "asterisk" のままで問題ありません)。
#
# 環境変数は組み合わせても構いません:
#   sudo ASTERISK_VERSION=22.10.0 SRC_DIR=/usr/src bash update_asterisk.sh
#
# ---------------------------------------------------------------------------
# 動作の流れ
# ---------------------------------------------------------------------------
#
#   1. 現在の Asterisk バージョンとモジュール一覧を退避 (ロールバック参考用)
#      → /root/asterisk-update-backup/ に保存されます
#   2. 新バージョンのソースを /usr/src にダウンロード・展開
#   3. spandsp ライブラリ (FAX 機能に必須) が無ければ先にインストール
#   4. 既存の menuselect.makeopts (モジュール選択) を可能な限り引き継いで
#      再ビルド。加えて、本ツールの FAX 機能に必須の res_fax_spandsp を
#      毎回明示的に有効化する (下記「menuselect で必須のモジュール」参照)
#   5. サービス停止 → make install → サービス起動
#   6. 起動確認 + res_fax_spandsp が実際にロードされたかの確認
#
# 失敗した場合は、退避した情報を見ながら手動でロールバックしてください
# (このスクリプトは自動ロールバックしません)。
#
# ---------------------------------------------------------------------------
# menuselect で必須のモジュール (本ツールの機能を維持するために重要)
# ---------------------------------------------------------------------------
#
# Asterisk のソースビルドでは「menuselect」という仕組みで、どの機能
# モジュールを含めるかを選べます。デフォルトの設定 (make menuselect.makeopts
# だけを実行した状態) では、以下のモジュールが有効になりません。これは
# FAX トラブルシューティングで判明した、本ツールの動作に必須の設定です:
#
#   res_fax_spandsp
#     FAX 送受信で、相手先が T.38 (IP網でのFAX直接伝送) に対応していない
#     場合に、音声 (G.711) モードへ自動フォールバックするために必要な
#     モジュールです。これが無いと、ひかり電話等 T.38 非対応の回線では
#     FAX 送受信ができません。
#     ビルドの前提として spandsp ライブラリ (libspandsp-dev) が
#     インストールされている必要があり、無い場合は menuselect の画面上
#     でも選択できません (このスクリプトは自動でインストールします)。
#
# このスクリプトは、既存の menuselect.makeopts を引き継ぐ場合・新規に
# 生成する場合のどちらでも、ビルド直前に res_fax_spandsp を明示的に
# 有効化するため、通常は何も意識せず実行するだけで問題ありません。
# 万一 FAX 機能を使わない・意図的に外したい場合は、環境変数
# SKIP_FAX_SPANDSP=1 を指定してください。
#
#   sudo SKIP_FAX_SPANDSP=1 bash update_asterisk.sh

set -euo pipefail

ASTERISK_VERSION="${ASTERISK_VERSION:-22-current}"
SRC_DIR="${SRC_DIR:-/usr/src}"
SERVICE_NAME="${ASTERISK_SERVICE_NAME:-asterisk}"

log() { echo "[$(date '+%H:%M:%S')] $*"; }
err() { echo "ERROR: $*" >&2; exit 1; }
confirm() {
    read -r -p "$1 [yes/N]: " ans
    [ "$ans" = "yes" ] || { echo "中止しました。"; exit 1; }
}

if [ "$(id -u)" -ne 0 ]; then
    err "root で実行してください (sudo bash $0)"
fi

echo "=============================================="
echo " Asterisk 本体アップデートスクリプト"
echo "=============================================="
echo ""
echo "現在のバージョン:"
asterisk -rx "core show version" 2>/dev/null || echo "  (Asterisk が起動していないか確認できません)"
echo ""
echo "取得予定バージョン: ${ASTERISK_VERSION}"
echo ""
echo "⚠️  このスクリプトを実行すると:"
echo "  - ビルドに数分〜十数分かかります"
echo "  - 完了時にサービスを再起動し、全通話が切断されます"
echo "  - 既存の /etc/asterisk/*.conf には触れません (本ツールが別途管理)"
echo ""
confirm "上記を理解した上で続行しますか?"

# 1. 現状の退避
log "現在のバージョン・モジュール一覧を退避"
mkdir -p /root/asterisk-update-backup
BACKUP_TS="$(date +%Y%m%d_%H%M%S)"
asterisk -rx "core show version" > "/root/asterisk-update-backup/version-before-${BACKUP_TS}.txt" 2>&1 || true
asterisk -rx "module show" > "/root/asterisk-update-backup/modules-before-${BACKUP_TS}.txt" 2>&1 || true

# 既存の menuselect.makeopts を探して引き継ぐ
EXISTING_MAKEOPTS=""
for d in "${SRC_DIR}"/asterisk-*/; do
    if [ -f "${d}menuselect.makeopts" ]; then
        EXISTING_MAKEOPTS="${d}menuselect.makeopts"
    fi
done
if [ -n "$EXISTING_MAKEOPTS" ]; then
    log "既存のモジュール選択を発見: ${EXISTING_MAKEOPTS}"
    cp "$EXISTING_MAKEOPTS" /root/asterisk-update-backup/menuselect.makeopts.backup
else
    log "既存の menuselect.makeopts が見つかりません (初回相当の設定でビルドします)"
fi

# 2. spandsp ライブラリ (res_fax_spandsp に必須) の事前チェック
#    menuselect の画面/コマンドで res_fax_spandsp を選択するには、
#    ビルド時点でこのライブラリが入っている必要があるため、
#    ソースのダウンロードより前に済ませておく。
if [ "${SKIP_FAX_SPANDSP:-0}" = "1" ]; then
    log "SKIP_FAX_SPANDSP=1 のため spandsp のチェックをスキップします"
elif ldconfig -p | grep -q libspandsp; then
    log "spandsp ライブラリは導入済みです"
else
    log "spandsp ライブラリが見つかりません。FAX 機能 (res_fax_spandsp) に必須のため、先にインストールします"
    apt-get update -qq || log "WARN: apt-get update で一部リポジトリの取得に失敗した可能性がありますが、続行します"
    RUNTIME_PKG="libspandsp2t64"
    if ! apt-cache show "$RUNTIME_PKG" >/dev/null 2>&1; then
        RUNTIME_PKG="libspandsp2"
    fi
    apt-get install -y libspandsp-dev "$RUNTIME_PKG" \
        || err "spandsp ライブラリのインストールに失敗しました。手動で 'sudo apt install libspandsp-dev' を実行してから再実行してください。"
fi

# 3. ダウンロード・展開
DOWNLOAD_URL="https://downloads.asterisk.org/pub/telephony/asterisk/asterisk-${ASTERISK_VERSION}.tar.gz"

# 本体ダウンロードの前に、軽量な接続確認 (HEAD リクエストのみ) を行う。
# 万一ネットワーク的に到達できない場合、大きな tar.gz のダウンロードで
# 無言のまま長時間待たされる (進捗が見えず「止まった」ように見える)
# のを避け、早期に分かりやすいエラーで知らせるため。
log "ダウンロード元への疎通確認中: ${DOWNLOAD_URL}"
if ! curl -fsI --connect-timeout 15 --max-time 30 "$DOWNLOAD_URL" >/dev/null 2>&1; then
    err "ダウンロード元 (downloads.asterisk.org) に接続できません。
  サーバーのネットワーク/ファイアウォール設定でこのホストへの HTTPS
  通信が許可されているか確認してください。手動で疎通確認するには:
    curl -v --connect-timeout 10 -o /dev/null '${DOWNLOAD_URL}'"
fi
log "疎通確認OK。ソースをダウンロード: asterisk-${ASTERISK_VERSION}.tar.gz"
cd "$SRC_DIR"
# --connect-timeout: 接続確立自体のタイムアウト。--max-time: 転送全体の
# 上限 (低速回線でも通常数十MBなので 900 秒あれば十分だが、万一の
# ネットワーク詰まりで無限に待たされないようにする)。-s (silent) は
# 使わず進捗バーを表示し、「止まっているように見える」不安を防ぐ。
curl -fL --connect-timeout 15 --max-time 900 \
    -o "asterisk-${ASTERISK_VERSION}.tar.gz" \
    "$DOWNLOAD_URL" \
    || err "ダウンロード失敗。バージョン指定が正しいか確認してください。"

# 展開先ディレクトリ名を tar の一覧から取得する。
# 【重要な注意】"tar tzf FILE | head -1 | cut -d/ -f1" は、
# set -o pipefail が有効な状態だと不具合を起こす典型パターン:
#   head -1 は1行読んだ時点で即座に exit し、その入力元パイプを閉じる
#   → tar はまだ大量の残りの一覧をそのパイプへ書き込もうとする
#   → 書き込み先が閉じているため tar は SIGPIPE を受けて終了 (終了コード 141)
#   → pipefail によりパイプライン全体が「失敗」扱いになる
#   → set -e によりスクリプトがエラーメッセージも出さずそのまま終了する
# (実機で実際に "ダウンロードは成功するのに、直後に何も表示されず
#  シェルプロンプトに戻ってしまう" という形で発生することを確認済み)
# この1行だけ pipefail を外し、SIGPIPE で止まった tar の終了コードが
# 全体の失敗として扱われないようにする (cut の出力さえ受け取れれば
# 十分なため、tar 自身の終了コードは元々気にしていない)。
set +o pipefail
NEW_DIR_NAME=$(tar tzf "asterisk-${ASTERISK_VERSION}.tar.gz" 2>/dev/null | head -1 | cut -d/ -f1)
set -o pipefail
if [ -z "$NEW_DIR_NAME" ]; then
    err "ダウンロードした tar.gz からディレクトリ名を取得できませんでした。ファイルが壊れている可能性があります。"
fi
log "展開中: ${NEW_DIR_NAME}"
tar xzf "asterisk-${ASTERISK_VERSION}.tar.gz"
cd "$NEW_DIR_NAME"

# 4. configure + menuselect (既存設定を引き継ぐ)
log "configure 実行中"
./configure --with-pjproject-bundled

if [ -n "$EXISTING_MAKEOPTS" ] && [ -f /root/asterisk-update-backup/menuselect.makeopts.backup ]; then
    log "既存のモジュール選択を引き継ぎます"
    cp /root/asterisk-update-backup/menuselect.makeopts.backup menuselect.makeopts
else
    make menuselect.makeopts
    ./menuselect/menuselect --disable-category MENUSELECT_TESTS menuselect.makeopts
fi

# 引き継いだ設定・新規生成した設定のどちらであっても、FAX 機能に必須の
# res_fax_spandsp を毎回明示的に有効化する (--enable は既に選択済みでも
# 無害なため、常に実行して確実性を担保する)。これにより「アップデート
# したら FAX が使えなくなった」という事故を防ぐ。
if [ "${SKIP_FAX_SPANDSP:-0}" = "1" ]; then
    log "SKIP_FAX_SPANDSP=1 のため res_fax_spandsp の有効化をスキップします"
else
    log "res_fax_spandsp (FAX機能) を有効化"
    ./menuselect/menuselect --enable res_fax_spandsp menuselect.makeopts \
        || log "WARN: res_fax_spandsp の有効化に失敗しました。ビルド後に 'module show like fax' で確認してください。"
fi

# 5. ビルド
log "ビルド開始 (並列 $(nproc) — 数分〜十数分かかります)"
make -j"$(nproc)"

# 6. サービス停止 → インストール → 起動
log "本ツール (asterisk-pbx-web) を先に停止 (設定ファイルの反映作業と競合しないように)"
systemctl stop asterisk-pbx-web 2>/dev/null || true

log "Asterisk サービスを停止"
systemctl stop "$SERVICE_NAME"

log "make install 実行中"
make install
ldconfig

log "Asterisk サービスを起動"
systemctl start "$SERVICE_NAME"
sleep 3

log "本ツール (asterisk-pbx-web) を起動"
systemctl start asterisk-pbx-web 2>/dev/null || true

# 7. 確認
log "起動確認:"
sleep 2
if asterisk -rx "core show version" 2>/dev/null; then
    log "完了: Asterisk が起動しています。上記バージョンを確認してください。"
else
    err "Asterisk の起動確認に失敗しました。'systemctl status asterisk' と 'journalctl -u asterisk -n 100' を確認してください。"
fi

log ""
if [ "${SKIP_FAX_SPANDSP:-0}" = "1" ]; then
    log "SKIP_FAX_SPANDSP=1 が指定されていたため res_fax_spandsp の確認はスキップします"
elif asterisk -rx "module show like res_fax_spandsp" 2>/dev/null | grep -q "Running"; then
    log "確認OK: res_fax_spandsp.so がロードされています (FAX機能は維持されています)"
else
    log "WARN: res_fax_spandsp.so がロードされていないようです。FAX機能に影響する可能性があります。"
    log "  sudo asterisk -rx 'module show like fax' で状態を確認し、"
    log "  必要なら 'sudo asterisk -rx \"module load res_fax_spandsp.so\"' を試してください。"
fi

log ""
log "念のため、以下も確認してください:"
log "  sudo asterisk -rx 'pjsip show endpoints'   (内線・トランクが復帰しているか)"
log "  sudo asterisk -rx 'module show' | grep -i fax   (FAXモジュールが引き継がれているか)"
