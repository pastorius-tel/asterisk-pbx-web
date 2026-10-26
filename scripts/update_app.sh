#!/usr/bin/env bash
# =============================================================================
# 本ツール (Asterisk PBX Web Management) の更新スクリプト。
#
# すでに動いているサーバーに新しい版を入れる。設定 (.env)・データベース
# (pbx.db)・音源・バックアップはインストール先に残ったまま、プログラムと
# 依存ライブラリだけを入れ替える。
#
# 使い方:
#   sudo bash update_app.sh                       # 同じ場所にある zip を使う
#   sudo APP_ZIP=~/asterisk-pbx-web-0.8.0.zip bash update_app.sh
#   sudo INSTALL_DIR=/opt/asterisk-pbx-web bash update_app.sh
#
# 行うこと:
#   1. .env と pbx.db をバックアップ (*.bak-日時)
#   2. サービスを停止
#   3. 新しい版を上書き展開 (.env / pbx.db / .venv / 音源には触れない)
#   4. 配布版 (通常版 / Raspberry Pi 版) に合わせて、動作確認済みの版で
#      依存ライブラリを入れ直す (requirements/*.lock)
#   5. systemd のユニットを新しい起動方法に合わせる (必要な場合のみ)
#   6. サービスを起動
# =============================================================================

if [ -z "${BASH_VERSION:-}" ]; then
    if command -v bash >/dev/null 2>&1; then
        exec bash "$0" "$@"
    fi
    echo "エラー: このスクリプトの実行には bash が必要です。" >&2
    exit 1
fi

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/var/www/asterisk-pbx-web}"
RUN_USER="${RUN_USER:-asterisk}"
UNIT="${UNIT:-/etc/systemd/system/asterisk-pbx-web.service}"

log()  { echo -e "\n\033[1;36m[$(date '+%H:%M:%S')] $*\033[0m"; }
ok()   { echo -e "  \033[1;32m OK \033[0m $*"; }
warn() { echo -e "  \033[1;33mWARN\033[0m $*" >&2; }
err()  { echo -e "\033[1;31mERROR: $*\033[0m" >&2; exit 1; }

# --- netcheck: begin (install_all.sh にも同じものがある) ---
# インターネット (apt の取得元 / PyPI) に届くかを最初に確かめる。
# 名前解決 (DNS) ができないまま進むと、apt-get update は警告を出すだけで
# 成功扱い (終了コード 0) になり、数分待たされたあと pip の段階で止まって
# 原因が分かりにくい。先に確かめ、調べ方を表示して止める。
check_network() {
    if [ "${SKIP_NETCHECK:-0}" = "1" ]; then
        echo "  ネットワーク確認を省略します (SKIP_NETCHECK=1)"
        return 0
    fi
    local hosts=(pypi.org files.pythonhosted.org) bad_dns=() h apt_hosts
    # apt の取得元 (ports.ubuntu.com / archive.ubuntu.com 等。コメント行は除く)
    apt_hosts="$(grep -rhE '^[[:space:]]*[^#[:space:]].*https?://' \
                    /etc/apt/sources.list /etc/apt/sources.list.d/ 2>/dev/null \
                 | grep -oE 'https?://[^/[:space:]]+' | sed -E 's#^https?://##' \
                 | sort -u | head -3 || true)"
    for h in $apt_hosts; do hosts+=("$h"); done
    # プロキシ経由の環境では外部の名前を直接引けないことがあるため、
    # その場合は名前解決の確認を省き、実際の接続確認だけにする。
    if [ -z "${https_proxy:-}${HTTPS_PROXY:-}${http_proxy:-}${HTTP_PROXY:-}" ]; then
        for h in "${hosts[@]}"; do
            getent ahosts "$h" >/dev/null 2>&1 || bad_dns+=("$h")
        done
    fi
    local https_ok=1
    if [ "${#bad_dns[@]}" -eq 0 ] && command -v curl >/dev/null 2>&1; then
        curl -sS --max-time 20 -o /dev/null https://pypi.org/simple/pip/ 2>/dev/null || https_ok=0
    fi
    if [ "${#bad_dns[@]}" -eq 0 ] && [ "$https_ok" = "1" ]; then
        echo -e "  \033[1;32m OK \033[0m インターネット接続 (apt の取得元 / PyPI)"
        return 0
    fi
    echo "" >&2
    if [ "${#bad_dns[@]}" -gt 0 ]; then
        echo -e "\033[1;31mERROR: 名前解決 (DNS) ができません: ${bad_dns[*]}\033[0m" >&2
    else
        echo -e "\033[1;31mERROR: インターネット (https://pypi.org) に接続できません。\033[0m" >&2
    fi
    cat >&2 <<'NETHELP'
  このまま進めても、パッケージや Python ライブラリを取得できずに失敗します。
  次のコマンドで原因を確認してください:

    ping -c 3 8.8.8.8                            # IP アドレスで外へ出られるか
    ping -c 3 pypi.org                           # 名前で外へ出られるか (DNS)
    resolvectl status | grep -A2 'DNS Servers'   # 使っている DNS サーバー
    ip route | grep default                      # デフォルトゲートウェイ

  ・8.8.8.8 には届くのに pypi.org に届かない → DNS サーバーの設定がありません。
    固定 IP にしている場合は /etc/netplan/ の設定ファイルに nameservers を
    追加して、sudo netplan apply を実行してください。
  ・8.8.8.8 にも届かない → LAN ケーブル・ゲートウェイ (ルーター) の設定を
    確認してください。

  プロキシや社内ミラーを使う環境で、この確認だけを省く場合は
  SKIP_NETCHECK=1 を付けて実行してください (例: sudo SKIP_NETCHECK=1 bash ...)。
NETHELP
    exit 1
}
# --- netcheck: end ---

# --- pipfix: begin (install_all.sh にも同じものがある) ---
# pip は自前の証明書一覧 (certifi) を同梱しており、OS の証明書を更新しても
# それを使い続ける。2026-10 の eMudhra/emSign 認証局の事故で失効した
# 証明書が、pip 26.2.2 より前の同梱一覧には残っている (第三者が PyPI との
# 通信に割り込む「なりすまし」の余地がある)。26.2.2 未満なら入れ替える。
# 入れ替えは古い pip 自身が行うため、PyPI 公開値の SHA256 で中身を照合し、
# 通信を差し替えられても偽物は入らないようにする。
PIP_FIXED_VERSION="26.2.2"
PIP_FIXED_SHA256_WHL="fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70"
PIP_FIXED_SHA256_SDIST="3a8e5c800492787f33a4114ef54444546c340df428974ebba17e0fd47e11af2f"
ensure_pip_fixed() {
    local py="$1" req rc
    if "$py" - "$PIP_FIXED_VERSION" <<'PYVER' 2>/dev/null
import sys
from importlib.metadata import version


def key(v):
    out = []
    for part in v.split("."):
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


sys.exit(0 if key(version("pip")) >= key(sys.argv[1]) else 1)
PYVER
    then
        return 0
    fi
    req="$(mktemp)"
    printf 'pip==%s --hash=sha256:%s --hash=sha256:%s\n' \
        "$PIP_FIXED_VERSION" "$PIP_FIXED_SHA256_WHL" "$PIP_FIXED_SHA256_SDIST" > "$req"
    rc=0
    "$py" -m pip install -q --disable-pip-version-check --require-hashes -r "$req" || rc=$?
    rm -f "$req"
    return "$rc"
}
# --- pipfix: end ---


[ "$(id -u)" -eq 0 ] || err "root で実行してください:  sudo bash $0"
[ -d "$INSTALL_DIR" ] || err "インストール先が見つかりません: $INSTALL_DIR"
[ -x "$INSTALL_DIR/.venv/bin/pip" ] || err "$INSTALL_DIR/.venv がありません。新規インストールは install_all.sh を使ってください。"

# -----------------------------------------------------------------------------
log "1/6  新しい版の取得元を確認"
# -----------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_APP=""
TMP_UNZIP=""
ZIP_PATH="${APP_ZIP:-}"
if [ -z "$ZIP_PATH" ]; then
    ZIP_PATH="$(find "$SCRIPT_DIR" -maxdepth 1 -name 'asterisk-pbx-web*.zip' | sort | tail -1)"
fi
if [ -n "$ZIP_PATH" ] && [ -f "$ZIP_PATH" ]; then
    TMP_UNZIP="$(mktemp -d)"
    trap 'rm -rf "$TMP_UNZIP"' EXIT
    unzip -qo "$ZIP_PATH" -d "$TMP_UNZIP"
    SRC_APP="$(find "$TMP_UNZIP" -maxdepth 2 -name 'pyproject.toml' -exec dirname {} \; | head -1)"
    [ -n "$SRC_APP" ] || err "zip の中に本ツールが見つかりません: $ZIP_PATH"
    ok "zip: $(basename "$ZIP_PATH")"
elif [ -f "$SCRIPT_DIR/../pyproject.toml" ]; then
    SRC_APP="$(cd "$SCRIPT_DIR/.." && pwd)"
    ok "ソースツリー: $SRC_APP"
else
    err "新しい版が見つかりません。APP_ZIP=/path/to/asterisk-pbx-web-x.y.z.zip を指定してください。"
fi

NEW_VER="$(sed -n 's/^version = "\(.*\)"/\1/p' "$SRC_APP/pyproject.toml" | head -1)"
OLD_VER="$(sed -n 's/^version = "\(.*\)"/\1/p' "$INSTALL_DIR/pyproject.toml" 2>/dev/null | head -1)"
echo "  現在の版 : ${OLD_VER:-不明}"
echo "  新しい版 : ${NEW_VER:-不明}"

# 依存ライブラリの取得にはインターネットが要る。サービスを止める前に確かめ、
# 届かないなら何も変えずに終える (止めたまま失敗して画面が使えなくなるのを防ぐ)。
log "事前確認  インターネット接続"
check_network

# -----------------------------------------------------------------------------
log "2/6  バックアップ"
# -----------------------------------------------------------------------------
TS="$(date +%Y%m%d_%H%M%S)"
for f in .env pbx.db; do
    if [ -f "$INSTALL_DIR/$f" ]; then
        cp -a "$INSTALL_DIR/$f" "$INSTALL_DIR/$f.bak-$TS"
        ok "$f → $f.bak-$TS"
    fi
done

# -----------------------------------------------------------------------------
log "3/6  サービス停止と上書き展開"
# -----------------------------------------------------------------------------
systemctl stop asterisk-pbx-web 2>/dev/null || true
if [ "$(cd "$SRC_APP" && pwd)" != "$(cd "$INSTALL_DIR" && pwd)" ]; then
    # 設定・データ・仮想環境は持ち込まない (配布物には元々含まれないが念のため)
    tar -C "$SRC_APP" \
        --exclude='.git' --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='.env' --exclude='pbx.db*' --exclude='uploads' --exclude='backups' \
        --exclude='.session_secret' --exclude='.initdb.lock' \
        -cf - . | tar -C "$INSTALL_DIR" -xf -
fi
ok "$INSTALL_DIR へ展開"

# -----------------------------------------------------------------------------
log "4/6  依存ライブラリ (動作確認済みの版)"
# -----------------------------------------------------------------------------
ARCH="$(uname -m)"
IS_ARM32=0
case "$ARCH" in armv6l|armv7l|armv7|armhf|arm) IS_ARM32=1 ;; esac
EDITION="$(tr -d '[:space:]' < "$INSTALL_DIR/EDITION" 2>/dev/null || true)"
[ -n "$EDITION" ] || { [ "$IS_ARM32" = "1" ] && EDITION="rpi-armv7" || EDITION="normal"; }
[ "$EDITION" = "normal" ] && [ "$IS_ARM32" = "1" ] && EDITION="rpi-armv7"
case "$EDITION" in
    normal)    LOCK="requirements/normal.lock";    EXTRAS="[fast,smb]" ;;
    rpi-armv7) LOCK="requirements/rpi-armv7.lock"; EXTRAS="" ;;
    *) err "EDITION の値が不正です: $EDITION" ;;
esac
# Raspberry Pi 版でも、すでに SMB 保存用のライブラリを入れていれば残す
if [ "$EDITION" = "rpi-armv7" ] && "$INSTALL_DIR/.venv/bin/python" -c "import smbclient" 2>/dev/null; then
    EXTRAS="[smb]"
fi
echo "  配布版 : $EDITION   Python : $("$INSTALL_DIR/.venv/bin/python" -V 2>&1)"

cd "$INSTALL_DIR"
# 既存の venv の pip が、失効した証明書を同梱した古い版なら入れ替える
if ensure_pip_fixed "$INSTALL_DIR/.venv/bin/python"; then
    ok "pip: $("$INSTALL_DIR/.venv/bin/python" -m pip --version 2>/dev/null | awk '{print $2}') (証明書の問題に対応済みの版)"
else
    warn "pip を ${PIP_FIXED_VERSION} に更新できませんでした (このまま続行します)。"
fi
if ! "$INSTALL_DIR/.venv/bin/pip" install -q -c "$LOCK" -e ".${EXTRAS}" 2>/tmp/pip_update_err.log; then
    tail -15 /tmp/pip_update_err.log | sed 's/^/    /'
    warn "依存ライブラリの更新に失敗しました。サービスは元のライブラリのまま起動します。"
    warn "詳細: /tmp/pip_update_err.log"
else
    ok "requirements/$(basename "$LOCK") の版で入れ直しました"
fi

# -----------------------------------------------------------------------------
log "5/6  systemd ユニットの確認"
# -----------------------------------------------------------------------------
# uvicorn の uvicorn.workers は非推奨になり、将来の版で削除される。
# 新しい起動方法 (uvicorn_worker パッケージ) に切り替える。
if [ -f "$UNIT" ] && grep -q "uvicorn.workers.UvicornWorker" "$UNIT"; then
    sed -i 's/uvicorn\.workers\.UvicornWorker/uvicorn_worker.UvicornWorker/' "$UNIT"
    systemctl daemon-reload
    ok "ワーカーを uvicorn_worker.UvicornWorker に切り替えました"
else
    ok "変更なし"
fi

# -----------------------------------------------------------------------------
log "6/6  起動"
# -----------------------------------------------------------------------------
chown -R "$RUN_USER":"$RUN_USER" "$INSTALL_DIR"
systemctl start asterisk-pbx-web
sleep 3
if systemctl is-active --quiet asterisk-pbx-web; then
    ok "asterisk-pbx-web を起動しました (${OLD_VER:-?} → ${NEW_VER:-?})"
else
    journalctl -u asterisk-pbx-web -n 30 --no-pager || true
    err "起動に失敗しました。上のログを確認してください。元に戻す場合は *.bak-$TS を戻してください。"
fi

cat <<EOF

============================================================
 更新が終わりました。

 最後に Web 画面を開き、左下の「変更を Asterisk へ反映」を
 必ず 1 回押してください。ダイヤルプランはこのボタンを押した
 ときに書き出されます。
============================================================
EOF
