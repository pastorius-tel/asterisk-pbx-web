#!/usr/bin/env bash
# =============================================================================
# 本ツールの Web 画面 (asterisk-pbx-web サービス) を、root ではなく
# asterisk ユーザーで動かすように切り替える。
#
# 古い版のころに手作業で導入した環境では、サービスが User=root で動いて
# いることがある。万一 Web 画面に弱点があった場合、root のままだと
# サーバー全体を操作されるおそれがある。asterisk ユーザーで動かせば、
# 被害を本ツールと Asterisk の範囲に抑えられる。
# (install_all.sh で新しく入れた環境は、最初から asterisk で動いている)
#
# Asterisk 本体も root で動いている場合は、先に switch_asterisk_user.sh で
# 本体を切り替える (このスクリプトは、その状態を見つけると止まる)。
#
# 使い方:
#   sudo bash switch_service_user.sh              # 確認だけ (何も変更しない)
#   sudo APPLY=1 bash switch_service_user.sh      # 切り替える
#   sudo ROLLBACK=1 bash switch_service_user.sh   # 元に戻す (以前のユーザーで動かす)
#
# 切り替え (APPLY=1) で行うこと:
#   1. サービスを止め、.env と pbx.db をバックアップ (*.bak-日時)
#   2. インストール先の持ち主を asterisk にする
#   3. 本ツールが書き込む Asterisk 側の場所 (/etc/asterisk・音源・留守番電話・
#      FAX のスプールなど。.env で場所を変えている場合はその場所) に、
#      asterisk が書き込めるよう権限を付ける (足りない場所だけ)
#   4. systemd の追加設定ファイル (drop-in) で User=asterisk にし、プロセスの
#      権限を絞る設定 (NoNewPrivileges など) を加える。元のユニットファイルは
#      書き換えない
#   5. 起動して画面が応答するか確かめる。応答しなければ自動で元に戻す
#
# 環境変数:
#   UNIT_NAME    サービス名 (既定: asterisk-pbx-web)
#   NEW_USER     切り替え後のユーザー (既定: asterisk)
#   INSTALL_DIR  インストール先 (既定: ユニットの WorkingDirectory)
# =============================================================================

if [ -z "${BASH_VERSION:-}" ]; then
    command -v bash >/dev/null 2>&1 && exec bash "$0" "$@"
    echo "エラー: このスクリプトの実行には bash が必要です。" >&2
    exit 1
fi

set -euo pipefail

UNIT_NAME="${UNIT_NAME:-asterisk-pbx-web}"
NEW_USER="${NEW_USER:-asterisk}"
APPLY="${APPLY:-0}"
ROLLBACK="${ROLLBACK:-0}"
DROPIN_DIR="/etc/systemd/system/${UNIT_NAME}.service.d"
DROPIN="${DROPIN_DIR}/50-run-as-service-user.conf"

log()  { echo -e "\n\033[1;36m[$(date '+%H:%M:%S')] $*\033[0m"; }
ok()   { echo -e "  \033[1;32m OK \033[0m $*"; }
info() { echo -e "  \033[1;37m -- \033[0m $*"; }
todo() { echo -e "  \033[1;33m要対応\033[0m $*"; }
warn() { echo -e "  \033[1;33mWARN\033[0m $*" >&2; }
ng()   { echo -e "  \033[1;31m NG \033[0m $*"; }
err()  { echo -e "\033[1;31mERROR: $*\033[0m" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || err "root で実行してください:  sudo bash $0"
command -v systemctl >/dev/null 2>&1 || err "systemctl が見つかりません (systemd の環境が必要です)。"
systemctl cat "$UNIT_NAME" >/dev/null 2>&1 \
    || err "サービス ${UNIT_NAME} が見つかりません。名前が違う場合は UNIT_NAME=<名前> を付けてください。"
[ "$NEW_USER" != "root" ] || err "NEW_USER に root は指定できません。"

# 指定ユーザーとしてコマンドを実行する (権限の確認用)。
# runuser は root から使え、sudoers の設定に左右されない。
as_user() {
    if command -v runuser >/dev/null 2>&1; then
        runuser -u "$NEW_USER" -- "$@"
    else
        sudo -n -u "$NEW_USER" -- "$@"
    fi
}

unit_prop() { systemctl show -p "$1" --value "$UNIT_NAME" 2>/dev/null || true; }

# サービスを起動し、画面 (/login) が応答するまで待つ。0=応答あり
wait_web() {
    local code
    for _ in $(seq 1 15); do
        sleep 2
        systemctl is-active --quiet "$UNIT_NAME" || continue
        [ "$HTTP_CHECK" = "1" ] || return 0
        command -v curl >/dev/null 2>&1 || return 0
        code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' \
                "http://${WEB_HOST}:${WEB_PORT}/login" 2>/dev/null || true)"
        case "$code" in 200|30[0-9]) return 0 ;; esac
    done
    return 1
}

# -----------------------------------------------------------------------------
# 元に戻す
# -----------------------------------------------------------------------------
if [ "$ROLLBACK" = "1" ]; then
    log "元に戻す: ${UNIT_NAME} を切り替え前のユーザーで動かします"
    [ -f "$DROPIN" ] || err "切り替えの設定 (${DROPIN}) がありません。元に戻すものはありません。"
    rm -f "$DROPIN"
    rmdir "$DROPIN_DIR" 2>/dev/null || true
    systemctl daemon-reload
    systemctl restart "$UNIT_NAME"
    sleep 3
    if systemctl is-active --quiet "$UNIT_NAME"; then
        ok "${UNIT_NAME} を $(unit_prop User | sed 's/^$/root/') で起動しました"
        info "ファイルの持ち主は ${NEW_USER} のままですが、root で動く場合は影響ありません。"
    else
        journalctl -u "$UNIT_NAME" -n 30 --no-pager || true
        err "起動に失敗しました。上のログを確認してください。"
    fi
    exit 0
fi

# -----------------------------------------------------------------------------
log "1/3  今の状態を確認"
# -----------------------------------------------------------------------------
CUR_USER="$(unit_prop User)"
[ -n "$CUR_USER" ] || CUR_USER="root"
if [ -z "${INSTALL_DIR:-}" ]; then
    INSTALL_DIR="$(unit_prop WorkingDirectory)"
    INSTALL_DIR="${INSTALL_DIR#[-!]}"
fi
[ -n "$INSTALL_DIR" ] && [ -d "$INSTALL_DIR" ] \
    || err "インストール先が分かりません。INSTALL_DIR=/path/to/asterisk-pbx-web を付けてください。"
INSTALL_DIR="$(cd "$INSTALL_DIR" && pwd -P)"
[ -f "$INSTALL_DIR/pyproject.toml" ] || err "${INSTALL_DIR} に本ツールが見つかりません。"

id -u "$NEW_USER" >/dev/null 2>&1 || err "ユーザー ${NEW_USER} がありません (Asterisk が入っていれば通常は存在します)。"
getent group "$NEW_USER" >/dev/null 2>&1 || err "グループ ${NEW_USER} がありません。"

echo "  サービス         : ${UNIT_NAME}"
echo "  今の実行ユーザー : ${CUR_USER}"
echo "  切り替え後       : ${NEW_USER}"
echo "  インストール先   : ${INSTALL_DIR}"

BLOCKERS=()
CHANGES=()

# --- 場所の確認・修正は check_permissions.sh と共通 (.env で変えた場所も見る) ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$SCRIPT_DIR/check_permissions.sh" ] || err "${SCRIPT_DIR}/check_permissions.sh がありません。"
# shellcheck source=check_permissions.sh
. "$SCRIPT_DIR/check_permissions.sh"
# shellcheck disable=SC2034  # check_permissions.sh の関数が使う
PERM_USER="$NEW_USER"
# shellcheck disable=SC2034
PERM_INSTALL_DIR="$INSTALL_DIR"
perm_init
ENV_FILE="$PERM_ENV_FILE"
DB_FILE="$PERM_DB_FILE"

# --- 待ち受けアドレス (画面の応答確認と、1024 未満のポートの判定に使う) ---
EXEC_START="$(unit_prop ExecStart)"
EXEC_BIN="$(printf '%s' "$EXEC_START" | sed -n 's/.*path=\([^ ;]*\).*/\1/p' | head -1)"
BIND="$(printf '%s' "$EXEC_START" | grep -oE -- '(-b|--bind)[= ]+[^ ;]+' | head -1 | sed -E 's/^(-b|--bind)[= ]+//' || true)"
WEB_PORT=""; WEB_HOST="127.0.0.1"; HTTP_CHECK=1
if [ "${BIND#unix:}" != "$BIND" ]; then
    HTTP_CHECK=0; WEB_PORT="0"            # UNIX ソケットで待ち受け (前段に別の Web サーバー)
elif [ -n "$BIND" ]; then
    WEB_PORT="${BIND##*:}"
    h="${BIND%:*}"; h="${h#[}"; h="${h%]}"
    case "$h" in ""|0.0.0.0|::|"$BIND") ;; *) WEB_HOST="$h" ;; esac
else
    WEB_PORT="$(printf '%s' "$EXEC_START" | grep -oE -- '--port[= ]+[0-9]+' | head -1 | grep -oE '[0-9]+$' || true)"
fi
case "$WEB_PORT" in ''|*[!0-9]*) WEB_PORT="$(perm_env_get PORT)" ;; esac
case "$WEB_PORT" in ''|*[!0-9]*) WEB_PORT="8080" ;; esac
NEED_BIND_CAP=0
if [ "$HTTP_CHECK" = "1" ]; then
    [ "$WEB_PORT" -lt 1024 ] && NEED_BIND_CAP=1
    echo "  待ち受け         : ${BIND:-ポート ${WEB_PORT}}$([ "$NEED_BIND_CAP" = 1 ] && echo ' (1024 未満のため、ポートを開く権限だけ追加します)')"
else
    echo "  待ち受け         : ${BIND}"
fi

# --- Asterisk 本体の実行ユーザー ---
# Asterisk が root で動いていると、留守番電話や FAX のファイルが root の
# 持ち物になり、asterisk で動く本ツールから削除・移動できなくなる。
AST_PID="$(pgrep -o -x asterisk 2>/dev/null || true)"
if [ -z "$AST_PID" ]; then
    BLOCKERS+=("Asterisk が動いていません。Asterisk を起動してから実行してください。")
else
    AST_UID="$(ps -o uid= -p "$AST_PID" 2>/dev/null | tr -d ' ' || true)"
    if [ "$AST_UID" = "$(id -u "$NEW_USER")" ]; then
        ok "Asterisk 本体は ${NEW_USER} で動いています"
    else
        AST_WHO="$(id -nu "$AST_UID" 2>/dev/null || echo "uid $AST_UID")"
        BLOCKERS+=("Asterisk 本体が ${AST_WHO} で動いています。このまま切り替えると、Asterisk が作る留守番電話や FAX のファイルを本ツールが扱えなくなります。先に Asterisk 本体を ${NEW_USER} で動かしてください:  sudo bash $(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/switch_asterisk_user.sh")
    fi
fi


# --- インストール先までのフォルダを NEW_USER が通れるか ---
# (インストール先そのものは持ち主を変えるので、その手前まで)
IFS='/' read -r -a parts <<< "${INSTALL_DIR#/}"
blocked_parents() {
    local d="" p
    for p in "${parts[@]}"; do
        d="$d/$p"
        [ "$d" = "$INSTALL_DIR" ] && break
        as_user test -x "$d" 2>/dev/null || printf '%s\n' "$d"
    done
}
TRAVERSE_DIRS=()
while IFS= read -r d; do [ -n "$d" ] && TRAVERSE_DIRS+=("$d"); done < <(blocked_parents)
if [ "${#TRAVERSE_DIRS[@]}" -gt 0 ]; then
    if command -v setfacl >/dev/null 2>&1; then
        for d in "${TRAVERSE_DIRS[@]}"; do
            todo "${NEW_USER} が ${d} を通れません → このフォルダの「通過」だけを ${NEW_USER} に許可します (中の一覧は見せません)"
        done
    else
        BLOCKERS+=("${NEW_USER} が ${TRAVERSE_DIRS[0]} を通れません。通過だけを許可するために acl パッケージが必要です:  sudo apt install acl")
    fi
else
    ok "インストール先までのフォルダを ${NEW_USER} が通れます"
fi

# --- 仮想環境の Python ---
# 仮想環境は元になった Python (例: /usr/bin/python3.12) を使う。それが root や
# 個人のホームの中にあると、asterisk では実行できない (作り直しが必要)。
# インストール先の中のファイルは持ち主を変えれば読めるので、import の
# 確認は持ち主を変えたあとにも行う。
PY="$INSTALL_DIR/.venv/bin/python"
PY_OK=0
py_import() { as_user "$PY" -I -c 'import fastapi, sqlalchemy, jinja2' 2>&1; }
if [ ! -x "$PY" ]; then
    BLOCKERS+=("${PY} がありません。")
else
    REAL_PY="$(readlink -f "$PY")"
    if ! perm_inside_install "$REAL_PY" && ! as_user test -x "$REAL_PY" 2>/dev/null; then
        BLOCKERS+=("仮想環境の元になっている Python (${REAL_PY}) を ${NEW_USER} が実行できません。仮想環境を、誰でも使える場所の Python (例: /usr/bin/python3) で作り直す必要があります。")
    elif [ "${#TRAVERSE_DIRS[@]}" -eq 0 ] && py_import >/dev/null; then
        PY_OK=1
        ok "仮想環境の Python を ${NEW_USER} で実行できます (${REAL_PY})"
    else
        info "仮想環境の Python は ${REAL_PY} です。読み込みの確認は持ち主を変えたあとに行います"
    fi
fi
if [ -n "$EXEC_BIN" ]; then
    REAL_BIN="$(readlink -f "$EXEC_BIN" 2>/dev/null || echo "$EXEC_BIN")"
    if ! perm_inside_install "$REAL_BIN" && ! as_user test -x "$REAL_BIN" 2>/dev/null; then
        BLOCKERS+=("起動コマンド ${EXEC_BIN} を ${NEW_USER} が実行できません。")
    fi
fi

# --- 書き込みが必要な場所 (check_permissions.sh と共通) ---
perm_classify
CHANGES=("${PERM_CHANGES[@]}")
BLOCKERS+=("${PERM_BLOCKERS[@]}")
[ "$PERM_ADD_GROUP" = "1" ] && CHANGES+=("group")

if [ "$CUR_USER" = "$NEW_USER" ]; then
    ok "サービスはすでに ${NEW_USER} で動いています"
else
    todo "サービスを ${NEW_USER} で動かす追加設定を作ります: ${DROPIN}"
fi

if [ "${#BLOCKERS[@]}" -gt 0 ]; then
    echo ""
    for b in "${BLOCKERS[@]}"; do ng "$b"; done
    echo ""
    err "上の NG を解決するまで切り替えられません (何も変更していません)。"
fi

if [ "$APPLY" != "1" ]; then
    echo ""
    if [ "$CUR_USER" = "$NEW_USER" ] && [ "${#CHANGES[@]}" -eq 0 ] && [ "${#TRAVERSE_DIRS[@]}" -eq 0 ]; then
        ok "切り替え済みで、権限にも問題はありません。"
    else
        echo "  確認だけを行いました (何も変更していません)。"
        echo "  切り替えるには次を実行してください (Web 画面が十数秒止まります。通話には影響しません):"
        echo ""
        echo "    sudo APPLY=1 bash $0"
    fi
    exit 0
fi

# -----------------------------------------------------------------------------
log "2/3  切り替え"
# -----------------------------------------------------------------------------
TS="$(date +%Y%m%d-%H%M%S)"
ACLS=()

# 途中で中止するときに、ここまでの変更を戻す (サービスは元のユーザーで再開)
abort_switch() {
    local msg="$1" d
    for d in "${ACLS[@]}"; do setfacl -x "u:${NEW_USER}" "$d" 2>/dev/null || true; done
    if [ "${APP_CHOWNED:-0}" = "1" ] && [ "$CUR_USER" != "root" ]; then
        chown -R "$CUR_USER:" "$INSTALL_DIR" 2>/dev/null || true
    fi
    systemctl start "$UNIT_NAME" 2>/dev/null || true
    err "$msg"
}

for d in "${TRAVERSE_DIRS[@]}"; do
    setfacl -m "u:${NEW_USER}:x" "$d" 2>/dev/null \
        || abort_switch "${d} に ACL を設定できませんでした (ACL に対応していないファイルシステムの可能性があります)。何も変更していません。"
    ACLS+=("$d")
    ok "${d} の通過を ${NEW_USER} に許可しました (ACL)"
done

systemctl stop "$UNIT_NAME" 2>/dev/null || true
ok "${UNIT_NAME} を停止しました"

for f in "$ENV_FILE" "$DB_FILE"; do
    if [ -n "$f" ] && [ -f "$f" ]; then
        cp -a "$f" "$f.bak-$TS"; chmod 600 "$f.bak-$TS"
        ok "バックアップ: $f.bak-$TS"
    fi
done

# まずインストール先だけ持ち主を変え、Python が動くか確かめる。
# 動かなければ、ほかの場所には手を付けずに中止する。
APP_CHOWNED=0
chown -R "$NEW_USER:$NEW_USER" "$INSTALL_DIR"
APP_CHOWNED=1
# 合言葉などが入るので、持ち主 (= NEW_USER) だけが読めるようにする
[ -f "$ENV_FILE" ] && chmod 600 "$ENV_FILE"
ok "${INSTALL_DIR}: 持ち主を ${NEW_USER} にしました"
if [ "$PY_OK" != "1" ]; then
    if ! out="$(py_import)"; then
        echo "$out" | tail -5 >&2
        abort_switch "仮想環境の Python を ${NEW_USER} で実行できないため中止しました (サービスは元のユーザーで再開しました)。"
    fi
    ok "仮想環境の Python を ${NEW_USER} で実行できます"
fi

# ほかの場所 (インストール先は上で済ませた)
PERM_SKIP_APP=1 perm_apply

if [ "$CUR_USER" != "$NEW_USER" ]; then
    mkdir -p "$DROPIN_DIR"
    {
        echo "# 本ツールの Web 画面を root ではなく ${NEW_USER} ユーザーで動かす設定"
        echo "# (scripts/switch_service_user.sh が ${TS} に作成。切り替え前: ${CUR_USER})"
        echo "# 元に戻す: sudo ROLLBACK=1 bash scripts/switch_service_user.sh"
        echo "#   (またはこのファイルを消して systemctl daemon-reload → restart)"
        echo "[Service]"
        echo "User=${NEW_USER}"
        echo "Group=${NEW_USER}"
        echo "# 万一 Web 画面に侵入されても、OS 全体へ被害が及びにくくする"
        echo "NoNewPrivileges=true"
        echo "PrivateTmp=true"
        echo "ProtectSystem=true"
        echo "ProtectKernelTunables=true"
        echo "ProtectKernelModules=true"
        echo "ProtectControlGroups=true"
        echo "RestrictSUIDSGID=true"
        echo "RestrictRealtime=true"
        echo "LockPersonality=true"
        echo "# gunicorn の管理用ソケット (gunicorn.ctl) の置き場所。指定しないと"
        echo "# ${NEW_USER} のホームに作ろうとし、書き込めない場合にエラーが記録される"
        echo "RuntimeDirectory=${UNIT_NAME}"
        echo "Environment=XDG_RUNTIME_DIR=/run/${UNIT_NAME}"
        if [ "$NEED_BIND_CAP" = "1" ]; then
            echo "# 1024 未満のポートで待ち受けるための権限だけを渡す"
            echo "AmbientCapabilities=CAP_NET_BIND_SERVICE"
        fi
    } > "$DROPIN"
    chmod 644 "$DROPIN"
    ok "追加設定を作りました: ${DROPIN}"
fi

# -----------------------------------------------------------------------------
log "3/3  起動と確認"
# -----------------------------------------------------------------------------
systemctl daemon-reload
systemctl start "$UNIT_NAME" || true
if ! wait_web; then
    journalctl -u "$UNIT_NAME" -n 40 --no-pager || true
    if [ "$CUR_USER" != "$NEW_USER" ]; then
        warn "画面が応答しないため、${CUR_USER} で動かす設定に戻します。"
        rm -f "$DROPIN"; rmdir "$DROPIN_DIR" 2>/dev/null || true
        systemctl daemon-reload
        systemctl restart "$UNIT_NAME" || true
        sleep 3
        if systemctl is-active --quiet "$UNIT_NAME"; then
            warn "${CUR_USER} で起動し直しました。"
            [ "$CUR_USER" = "root" ] && warn "(ファイルの持ち主は ${NEW_USER} のままですが、root で動く場合は影響ありません)"
        fi
    fi
    err "切り替えに失敗しました。上のログを確認してください。"
fi
MAIN_PID="$(unit_prop MainPID)"
RUN_AS="$(ps -o user= -p "${MAIN_PID:-0}" 2>/dev/null | tr -d ' ' || true)"
ok "${UNIT_NAME} が起動し、画面が応答しています (実行ユーザー: ${RUN_AS:-$NEW_USER})"

if perm_verify; then ok "本ツールが使う場所すべてを ${NEW_USER} が使えます"; fi

cat <<EOF

============================================================
 切り替えが終わりました。Web 画面は ${NEW_USER} で動いています。

 Web 画面を開き、次の操作がいつも通りできるか確かめてください。
   - 左下の「変更を Asterisk へ反映」
   - 留守番電話の一覧・再生
   - FAX の一覧 (受信済みの PDF が開けるか)

 これからの更新 (update_app.sh) では RUN_USER を付けないでください
 (付ける場合は RUN_USER=${NEW_USER})。

 元に戻す場合:  sudo ROLLBACK=1 bash $0
============================================================
EOF
if [ -d "$INSTALL_DIR/.git" ]; then
    echo " (git を使う場合は sudo -u ${NEW_USER} git pull のように ${NEW_USER} で実行してください)"
fi
