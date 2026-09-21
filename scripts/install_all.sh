#!/usr/bin/env bash
# =============================================================================
# Asterisk PBX Web — 初回インストールスクリプト
#
# インストール直後の素の Ubuntu Server を、日本語環境の PBX サーバーとして
# 一通り使える状態にします。unzip も samba も入っていない前提です。
#
# 実行するとこうなります:
#   - 日本語ロケール / タイムゾーン (Asia/Tokyo) / 日本語対応の vim
#   - 基本ツール (unzip, curl, git, samba, net-tools 等)
#   - Python 3 + venv + 本ツールの依存パッケージ
#   - Asterisk 22 をソースからビルド (res_fax_spandsp 有効)
#   - 日本語音声パック
#   - 本ツールを /var/www/asterisk-pbx-web に配置し、systemd で自動起動
#   - ファイアウォール (ufw) に SIP/RTP/Web のポートを開放
#
# ---------------------------------------------------------------------------
# 使い方
# ---------------------------------------------------------------------------
#
#   # 1. このスクリプトと本ツールの zip を同じ場所に置く
#   # 2. root で実行する
#   sudo bash install_all.sh
#
#   # zip の場所を明示したいとき
#   sudo APP_ZIP=/home/ubuntu/asterisk-pbx-web.zip bash install_all.sh
#
# 主なオプション (環境変数):
#   APP_ZIP=<path>        本ツールの zip の場所 (既定: スクリプトと同じ場所を自動探索)
#   INSTALL_DIR=<path>    インストール先 (既定: /var/www/asterisk-pbx-web)
#   WEB_PORT=8080         Web 管理画面のポート
#   SKIP_ASTERISK=1       Asterisk のビルドを飛ばす (既に導入済みの場合)
#   SKIP_SAMBA=1          Samba を入れない
#   SKIP_UFW=1            ファイアウォール設定をしない
#   ASTERISK_VERSION=...  Asterisk のバージョン (既定: 22-current)
#
# ビルドを含むため、全体で 20〜40 分ほどかかります。
# =============================================================================

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/var/www/asterisk-pbx-web}"
WEB_PORT="${WEB_PORT:-8080}"
ASTERISK_VERSION="${ASTERISK_VERSION:-22-current}"
SRC_DIR="/usr/src"
RUN_USER="asterisk"

log()  { echo -e "\n\033[1;36m[$(date '+%H:%M:%S')] $*\033[0m"; }
ok()   { echo -e "  \033[1;32m OK \033[0m $*"; }
skip() { echo -e "  \033[1;33mskip\033[0m $*"; }
warn() { echo -e "  \033[1;33mWARN\033[0m $*" >&2; }
err()  { echo -e "\033[1;31mERROR: $*\033[0m" >&2; exit 1; }

if [ "$(id -u)" -ne 0 ]; then
    err "root で実行してください:  sudo bash $0"
fi

# apt のロック待ち (Ubuntu の自動更新と競合することがある)
wait_apt() {
    local waited=0
    while ! flock -n /var/lib/dpkg/lock-frontend true 2>/dev/null; do
        [ ! -e /var/lib/dpkg/lock-frontend ] && return 0
        [ "$waited" -eq 0 ] && log "他のプロセスが apt を使用中です。解放を待ちます…"
        [ "$waited" -ge 300 ] && err "apt のロックが解放されませんでした。しばらく待って再実行してください。"
        sleep 5; waited=$((waited + 5))
    done
    return 0
}

apt_install() {
    wait_apt
    DEBIAN_FRONTEND=noninteractive apt-get install -y "$@"
}

echo "============================================================"
echo " Asterisk PBX Web  初回インストール"
echo "============================================================"
echo "  インストール先 : $INSTALL_DIR"
echo "  Web ポート     : $WEB_PORT"
echo "  Asterisk       : ${SKIP_ASTERISK:+スキップ}${SKIP_ASTERISK:-$ASTERISK_VERSION をビルド}"
echo ""
echo "  ビルドを含むため 20〜40 分ほどかかります。"
echo "  実行中は全通話が使えません (新規構築を想定しています)。"
echo ""
read -r -p "続行しますか? [yes/N]: " _ans
[ "$_ans" = "yes" ] || { echo "中止しました。"; exit 0; }

# =============================================================================
log "1/10  パッケージリストの更新"
# =============================================================================
wait_apt
apt-get update -qq || warn "一部リポジトリの取得に失敗しましたが続行します"
ok "完了"

# =============================================================================
log "2/10  日本語環境 (ロケール・タイムゾーン・vim)"
# =============================================================================
apt_install language-pack-ja locales tzdata vim
# 日本語ロケールを生成して既定にする
locale-gen ja_JP.UTF-8 >/dev/null
update-locale LANG=ja_JP.UTF-8 LANGUAGE="ja_JP:ja"
ok "ロケール ja_JP.UTF-8"

timedatectl set-timezone Asia/Tokyo 2>/dev/null \
    || ln -sf /usr/share/zoneinfo/Asia/Tokyo /etc/localtime
ok "タイムゾーン $(date '+%Z %z')"

# vim の日本語設定 (文字化け・カーソル位置ずれを防ぐ)
cat > /etc/vim/vimrc.local <<'VIMEOF'
" 日本語環境向けの基本設定
set encoding=utf-8
set fileencodings=utf-8,cp932,euc-jp,iso-2022-jp
set fileformats=unix,dos,mac
set ambiwidth=double   " 全角記号の幅ズレを防ぐ
set number
set expandtab tabstop=4 shiftwidth=4
set hlsearch incsearch ignorecase smartcase
syntax on
VIMEOF
ok "vim (日本語対応)"

# =============================================================================
log "3/10  基本ツール"
# =============================================================================
apt_install unzip zip curl wget git net-tools iputils-ping \
            build-essential ca-certificates gnupg lsb-release
ok "unzip, curl, git, build-essential 等"

if [ "${SKIP_SAMBA:-0}" = "1" ]; then
    skip "Samba (SKIP_SAMBA=1)"
else
    apt_install samba samba-common-bin
    ok "Samba (ファイル共有。共有設定は /etc/samba/smb.conf を編集してください)"
fi

# =============================================================================
log "4/10  Python 3 環境"
# =============================================================================
apt_install python3 python3-venv python3-pip python3-dev
ok "$(python3 --version)"

# =============================================================================
log "5/10  Asterisk 本体"
# =============================================================================
if [ "${SKIP_ASTERISK:-0}" = "1" ]; then
    skip "Asterisk (SKIP_ASTERISK=1)"
elif command -v asterisk >/dev/null 2>&1; then
    skip "Asterisk は既に導入済み ($(asterisk -V 2>/dev/null))"
else
    log "  Asterisk のビルドに必要なパッケージを導入"
    apt_install libedit-dev libjansson-dev libsqlite3-dev uuid-dev \
                libxml2-dev libspandsp-dev libssl-dev
    # FAX の音声モードに必須 (libspandsp2 はリリースで名前が変わる)
    if apt-cache show libspandsp2t64 >/dev/null 2>&1; then
        apt_install libspandsp2t64
    else
        apt_install libspandsp2 || true
    fi

    log "  ソースをダウンロード"
    cd "$SRC_DIR"
    DL_URL="https://downloads.asterisk.org/pub/telephony/asterisk/asterisk-${ASTERISK_VERSION}.tar.gz"
    curl -fI --connect-timeout 15 --max-time 30 "$DL_URL" >/dev/null 2>&1 \
        || err "Asterisk のダウンロード元に接続できません。ネットワーク設定を確認してください。"
    curl -fL --connect-timeout 15 --max-time 900 \
         -o "asterisk-${ASTERISK_VERSION}.tar.gz" "$DL_URL" \
        || err "Asterisk のダウンロードに失敗しました。"

    # tar tzf | head は SIGPIPE で落ちるため pipefail を一時的に外す
    set +o pipefail
    AST_DIR=$(tar tzf "asterisk-${ASTERISK_VERSION}.tar.gz" 2>/dev/null | head -1 | cut -d/ -f1)
    set -o pipefail
    [ -n "$AST_DIR" ] || err "ダウンロードしたファイルが壊れています。"
    tar xzf "asterisk-${ASTERISK_VERSION}.tar.gz"
    cd "$AST_DIR"

    log "  configure / menuselect (数分かかります)"
    ./configure --with-pjproject-bundled >/dev/null
    make menuselect.makeopts >/dev/null
    ./menuselect/menuselect --disable-category MENUSELECT_TESTS menuselect.makeopts
    # FAX の音声 (G.711) フォールバックに必須。既定では有効にならない
    ./menuselect/menuselect --enable res_fax_spandsp menuselect.makeopts \
        || warn "res_fax_spandsp を有効化できませんでした (FAX が使えない可能性があります)"

    log "  ビルド (10〜30 分かかります。そのままお待ちください)"
    make -j"$(nproc)" >/dev/null
    make install >/dev/null
    make samples >/dev/null
    make config >/dev/null
    ldconfig

    # asterisk ユーザーで動かす
    id -u asterisk >/dev/null 2>&1 || useradd -r -d /var/lib/asterisk -s /usr/sbin/nologin asterisk
    sed -i 's/^#AST_USER=.*/AST_USER="asterisk"/;s/^#AST_GROUP=.*/AST_GROUP="asterisk"/' /etc/default/asterisk 2>/dev/null || true
    chown -R asterisk:asterisk /var/{lib,log,spool}/asterisk /etc/asterisk /usr/lib/asterisk 2>/dev/null || true

    systemctl enable asterisk >/dev/null 2>&1 || true
    systemctl restart asterisk || warn "Asterisk の起動に失敗しました。後で確認してください。"
    sleep 3
    ok "Asterisk 導入完了 ($(asterisk -V 2>/dev/null || echo 'バージョン取得不可'))"
fi

# =============================================================================
log "6/10  本ツールの配置"
# =============================================================================
# インストール元を決める。次の順で探す:
#   1. APP_ZIP で明示された zip
#   2. このスクリプトと同じ場所にある asterisk-pbx-web*.zip
#   3. ソースツリー (git clone した中など)。scripts/ の 1 つ上に
#      pyproject.toml があれば、そこをそのまま使う
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_APP=""
TMP_UNZIP=""

ZIP_PATH="${APP_ZIP:-}"
if [ -z "$ZIP_PATH" ]; then
    ZIP_PATH="$(find "$SCRIPT_DIR" -maxdepth 1 -name 'asterisk-pbx-web*.zip' | head -1)"
fi

if [ -n "$ZIP_PATH" ] && [ -f "$ZIP_PATH" ]; then
    TMP_UNZIP="$(mktemp -d)"
    unzip -qo "$ZIP_PATH" -d "$TMP_UNZIP"
    SRC_APP="$(find "$TMP_UNZIP" -maxdepth 2 -name 'pyproject.toml' -exec dirname {} \; | head -1)"
    [ -n "$SRC_APP" ] || err "zip の中に本ツールが見つかりません: $ZIP_PATH"
    ok "zip から取得 ($(basename "$ZIP_PATH"))"
elif [ -f "$SCRIPT_DIR/../pyproject.toml" ]; then
    SRC_APP="$(cd "$SCRIPT_DIR/.." && pwd)"
    ok "ソースツリーから取得 ($SRC_APP)"
else
    err "本ツールのファイルが見つかりません。
  - git clone した場合は、その中の scripts/install_all.sh を実行してください
  - zip で配布されたものを使う場合は APP_ZIP=/path/to/asterisk-pbx-web.zip を指定してください"
fi

# 配置先と取得元が同じ場合 (INSTALL_DIR の中で git clone した等) はコピー不要
mkdir -p "$(dirname "$INSTALL_DIR")"
mkdir -p "$INSTALL_DIR"
if [ "$(cd "$SRC_APP" && pwd)" != "$(cd "$INSTALL_DIR" && pwd)" ]; then
    # .git や .venv は配置先に持ち込まない (リポジトリをそのまま
    # /var/www へコピーすると、以後 git pull の対象が分裂して混乱するため)
    tar -C "$SRC_APP" --exclude='.git' --exclude='.venv' --exclude='__pycache__' \
        --exclude='*.pyc' -cf - . | tar -C "$INSTALL_DIR" -xf -
fi
[ -n "$TMP_UNZIP" ] && rm -rf "$TMP_UNZIP"
ok "$INSTALL_DIR へ配置"

# .env を用意 (無ければサンプルから作り、秘密鍵とパスワードを自動生成)
#
# 管理パスワードは必ず自動生成する。未設定のままだとログイン画面が出ず、
# ネットワーク上の誰でも PBX を操作できる状態になるため。
# 生成したパスワードはインストール完了時に画面へ表示する。
ADMIN_PW=""
if [ ! -f "$INSTALL_DIR/.env" ] && [ -f "$INSTALL_DIR/.env.example" ]; then
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    SECRET="$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 40)"
    AMI_SECRET="$(head -c 16 /dev/urandom | base64 | tr -d '/+=' | head -c 20)"
    ADMIN_PW="$(head -c 16 /dev/urandom | base64 | tr -d '/+=' | head -c 16)"
    sed -i "s|^SECRET_KEY=.*|SECRET_KEY=${SECRET}|" "$INSTALL_DIR/.env"
    sed -i "s|^AMI_SECRET=.*|AMI_SECRET=${AMI_SECRET}|" "$INSTALL_DIR/.env"
    sed -i "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=${ADMIN_PW}|" "$INSTALL_DIR/.env"
    sed -i "s|^PORT=.*|PORT=${WEB_PORT}|" "$INSTALL_DIR/.env"
    sed -i "s|^DEBUG=.*|DEBUG=false|" "$INSTALL_DIR/.env"
    # LAN の他の PC から使う前提のインストーラーなので全インターフェースで待ち受ける
    # (上でログインパスワードを設定済み)
    sed -i "s|^HOST=.*|HOST=0.0.0.0|" "$INSTALL_DIR/.env"
    ok ".env を生成 (SECRET_KEY / AMI_SECRET / ADMIN_PASSWORD は自動生成)"
fi

# .env は管理パスワードを含むので、所有者以外から読めないようにする
chmod 600 "$INSTALL_DIR/.env" 2>/dev/null || true

# =============================================================================
log "7/10  Python 依存パッケージ"
# =============================================================================
cd "$INSTALL_DIR"
python3 -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate
pip install -q --upgrade pip setuptools wheel

ARCH="$(uname -m)"
echo "  アーキテクチャ: ${ARCH}"

# Raspberry Pi 等の ARM では、一部のパッケージにビルド済み配布物が無く
# ソースからのコンパイルが必要になる。その場合に必要な開発用パッケージを
# 先に入れておく (x86_64 では通常使われない)。
case "$ARCH" in
    arm*|aarch64)
        echo "  ARM 環境のため、ビルドに必要なパッケージを追加します…"
        deactivate
        apt_install libffi-dev libssl-dev python3-dev cargo pkg-config >/dev/null 2>&1 \
            || warn "ビルド用パッケージの一部が入りませんでした"
        # shellcheck disable=SC1091
        . "$INSTALL_DIR/.venv/bin/activate"
        ;;
esac

# まず通常どおり入れてみる
if pip install -q -e . 2>/tmp/pip_err.log; then
    ok "仮想環境 (.venv) と依存パッケージ"
else
    warn "依存パッケージのインストールに失敗しました。軽量構成で再試行します。"
    echo "  --- エラーの末尾 ---"
    tail -5 /tmp/pip_err.log | sed 's/^/    /'
    echo "  --------------------"
    # uvicorn[standard] は uvloop/httptools/watchfiles 等の C 拡張を
    # 引き込む。32bit ARM ではこれらのビルドに失敗しやすいので、
    # 純 Python だけで動く最小構成にフォールバックする
    # (性能はやや落ちるが、PBX 管理用途では体感差はほぼ無い)。
    if pip install -q -e . --no-deps \
       && pip install -q fastapi "uvicorn" gunicorn "sqlalchemy[asyncio]" \
                         aiosqlite pydantic pydantic-settings jinja2 \
                         python-multipart itsdangerous smbprotocol; then
        ok "仮想環境 (.venv) と依存パッケージ (軽量構成)"
        warn "uvicorn の高速化オプション (uvloop 等) は入っていません。動作に支障はありません。"
    else
        deactivate
        err "依存パッケージのインストールに失敗しました。詳細: /tmp/pip_err.log"
    fi
fi
deactivate

# =============================================================================
log "8/10  OS 側の依存パッケージ (FAX・音源・音声合成)"
# =============================================================================
if [ -f "$INSTALL_DIR/scripts/setup_dependencies.sh" ]; then
    # Postfix は対話が必要なのでここではスキップ (後で個別に実行する案内を出す)
    SKIP_POSTFIX=1 SKIP_TIMEZONE=1 FIX_PERMISSIONS=1 APP_USER="$RUN_USER" \
        bash "$INSTALL_DIR/scripts/setup_dependencies.sh" 2>&1 | grep -E "OK|WARN|skip|===" || true
    ok "依存パッケージと権限設定"
else
    warn "setup_dependencies.sh が見つかりません"
fi

# 日本語音声パック
if [ -d /var/lib/asterisk/sounds/ja ] && [ -n "$(ls -A /var/lib/asterisk/sounds/ja 2>/dev/null)" ]; then
    skip "日本語音声パックは導入済み"
elif [ -f "$INSTALL_DIR/scripts/install_japanese_sounds.sh" ]; then
    if SKIP_RELOAD=1 bash "$INSTALL_DIR/scripts/install_japanese_sounds.sh" >/dev/null 2>&1; then
        ok "日本語音声パック"
    else
        warn "日本語音声パックの導入に失敗しました (後で scripts/install_japanese_sounds.sh を実行してください)"
    fi
fi

# =============================================================================
log "9/10  サービス登録 (systemd)"
# =============================================================================
chown -R "$RUN_USER":"$RUN_USER" "$INSTALL_DIR"

cat > /etc/systemd/system/asterisk-pbx-web.service <<EOF
[Unit]
Description=Asterisk PBX Web Management
After=network.target asterisk.service
# 連続再起動の抑制は [Unit] セクションに書く
# ([Service] に書くと "Unknown key" の警告が出る)
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_USER}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${INSTALL_DIR}/.env
ExecStart=${INSTALL_DIR}/.venv/bin/gunicorn app.main:app \\
  -k uvicorn.workers.UvicornWorker \\
  -w 2 -b 0.0.0.0:${WEB_PORT} \\
  --timeout 120 --graceful-timeout 30 \\
  --access-logfile - --error-logfile -
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable asterisk-pbx-web >/dev/null 2>&1
systemctl restart asterisk-pbx-web
sleep 3
if systemctl is-active --quiet asterisk-pbx-web; then
    ok "サービス起動 (asterisk-pbx-web)"
else
    warn "サービスが起動していません: sudo journalctl -u asterisk-pbx-web -n 50"
fi

# =============================================================================
log "10/10  ファイアウォール"
# =============================================================================
if [ "${SKIP_UFW:-0}" = "1" ]; then
    skip "ufw (SKIP_UFW=1)"
elif ! command -v ufw >/dev/null 2>&1; then
    skip "ufw が入っていないためスキップ"
elif ! ufw status 2>/dev/null | grep -q "Status: active"; then
    skip "ufw は無効です (有効にする場合は下記ポートの開放が必要)"
    echo "       sudo ufw allow 22/tcp          # SSH"
    echo "       sudo ufw allow ${WEB_PORT}/tcp        # Web 管理画面"
    echo "       sudo ufw allow 5060/udp        # SIP"
    echo "       sudo ufw allow 10000:20000/udp # RTP (音声)"
else
    ufw allow 22/tcp >/dev/null
    ufw allow "${WEB_PORT}"/tcp >/dev/null
    ufw allow 5060/udp >/dev/null
    ufw allow 10000:20000/udp >/dev/null
    ok "SSH / Web(${WEB_PORT}) / SIP(5060) / RTP(10000-20000) を開放"
fi

# =============================================================================
# 完了
# =============================================================================
IP_ADDR="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<EOF

============================================================
 インストール完了
============================================================

  Web 管理画面 : http://${IP_ADDR:-<サーバーのIP>}:${WEB_PORT}/

  インストール先 : ${INSTALL_DIR}
  サービス名     : asterisk-pbx-web
  実行ユーザー   : ${RUN_USER}

------------------------------------------------------------
 ログイン情報 (必ず控えてください)
------------------------------------------------------------

  ユーザー名 : admin
  パスワード : ${ADMIN_PW:-(既存の .env の設定をそのまま使用)}

  この画面から PBX の全設定を変更できます。パスワードは
  ${INSTALL_DIR}/.env の ADMIN_PASSWORD に保存されています。
  変更する場合はこのファイルを編集し、サービスを再起動してください:
    sudo systemctl restart asterisk-pbx-web

------------------------------------------------------------
 次に行うこと
------------------------------------------------------------

 1. Web 画面を開き、「トランク」で回線 (ひかり電話等) を登録
 2. 「内線」で電話機を登録
 3. 「着信ルート」で着信先を設定
 4. 左下の「変更を Asterisk へ反映」を実行

 メール通知 (留守電・FAX) を使う場合は、Postfix の設定が必要です:
   sudo bash ${INSTALL_DIR}/scripts/setup_dependencies.sh
   (Gmail 中継の設定を対話で行えます)

 音声合成の声を増やす場合:
   sudo bash ${INSTALL_DIR}/scripts/install_tts_voices.sh

------------------------------------------------------------
 よく使うコマンド
------------------------------------------------------------

   sudo systemctl status asterisk-pbx-web    # 本ツールの状態
   sudo journalctl -u asterisk-pbx-web -f    # 本ツールのログ
   sudo asterisk -rvvv                       # Asterisk コンソール
   sudo systemctl restart asterisk-pbx-web   # 再起動

 日本語表示・タイムゾーンの反映には、一度ログアウトして
 入り直すか、再起動してください。

============================================================
EOF
