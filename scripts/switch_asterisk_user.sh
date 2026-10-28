#!/usr/bin/env bash
# =============================================================================
# Asterisk 本体を、root ではなく asterisk ユーザーで動かすように切り替える。
#
# ソースからビルドした Asterisk を、起動スクリプトの設定をしないまま使って
# いると、Asterisk 本体が root で動いていることがある。電話の着信は外から
# 届くので、万一 Asterisk に弱点があった場合、root のままだとサーバー全体を
# 操作されるおそれがある。asterisk ユーザーで動かせば被害を小さくできる。
# 本ツールの Web 画面を asterisk で動かす (switch_service_user.sh) 前にも必要。
#
# 使い方:
#   sudo bash switch_asterisk_user.sh              # 確認だけ (何も変更しない)
#   sudo APPLY=1 bash switch_asterisk_user.sh      # 切り替える (Asterisk を再起動します)
#   sudo ROLLBACK=1 bash switch_asterisk_user.sh   # 元に戻す (root で動かす)
#
# 切り替え (APPLY=1) で行うこと:
#   1. 通話中でないことを確かめる (通話中なら中止。FORCE=1 を付けると構わず進める)
#   2. Asterisk を止める (ここから再起動が終わるまで、電話は使えません)
#   3. Asterisk が書き込む場所 (/etc/asterisk・/var/lib/asterisk・
#      /var/spool/asterisk・/var/log/asterisk など) の持ち主を asterisk にする
#   4. asterisk.conf の [options] に runuser / rungroup = asterisk を書く。
#      起動方法 (systemd のユニット・起動スクリプト) に関係なく、Asterisk
#      自身が起動直後に asterisk へ切り替わる。元の asterisk.conf は
#      asterisk.conf.bak-日時 として残す
#   5. Asterisk を起動し、asterisk で動いていること・回線の登録が戻ったことを
#      確かめる。戻らなければ自動で root に戻す
#
# 環境変数:
#   AST_UNIT   Asterisk の systemd サービス名 (既定: asterisk)
#   NEW_USER   切り替え後のユーザー (既定: asterisk)
#   FORCE=1    通話中でも進める
# =============================================================================

if [ -z "${BASH_VERSION:-}" ]; then
    command -v bash >/dev/null 2>&1 && exec bash "$0" "$@"
    echo "エラー: このスクリプトの実行には bash が必要です。" >&2
    exit 1
fi

set -euo pipefail

AST_UNIT="${AST_UNIT:-asterisk}"
NEW_USER="${NEW_USER:-asterisk}"
APPLY="${APPLY:-0}"
ROLLBACK="${ROLLBACK:-0}"
FORCE="${FORCE:-0}"
# asterisk.conf に書いた行の目印 (元に戻すときに、この行だけを無効にする)
MARK="; switch_asterisk_user.sh"

log()  { echo -e "\n\033[1;36m[$(date '+%H:%M:%S')] $*\033[0m"; }
ok()   { echo -e "  \033[1;32m OK \033[0m $*"; }
info() { echo -e "  \033[1;37m -- \033[0m $*"; }
todo() { echo -e "  \033[1;33m要対応\033[0m $*"; }
warn() { echo -e "  \033[1;33mWARN\033[0m $*" >&2; }
ng()   { echo -e "  \033[1;31m NG \033[0m $*"; }
err()  { echo -e "\033[1;31mERROR: $*\033[0m" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || err "root で実行してください:  sudo bash $0"
command -v systemctl >/dev/null 2>&1 || err "systemctl が見つかりません (systemd の環境が必要です)。"
command -v asterisk >/dev/null 2>&1 || err "asterisk コマンドが見つかりません。"
[ "$NEW_USER" != "root" ] || err "NEW_USER に root は指定できません。"
id -u "$NEW_USER" >/dev/null 2>&1 || err "ユーザー ${NEW_USER} がありません。"
getent group "$NEW_USER" >/dev/null 2>&1 || err "グループ ${NEW_USER} がありません。"

as_user() {
    if command -v runuser >/dev/null 2>&1; then
        runuser -u "$NEW_USER" -- "$@"
    else
        sudo -n -u "$NEW_USER" -- "$@"
    fi
}
ast_pid()  { pgrep -o -x asterisk 2>/dev/null || true; }
ast_cli()  { timeout 15 asterisk -rx "$1" 2>/dev/null || true; }
pid_user() { ps -o user= -p "$1" 2>/dev/null | tr -d ' ' || true; }
active_calls() {
    local n
    n="$(ast_cli "core show channels count" | sed -n 's/^[[:space:]]*\([0-9][0-9]*\) active call.*/\1/p' | head -1)"
    printf '%s' "${n:-0}"
}
registered_count() {
    ast_cli "pjsip show registrations" | grep -cE '[[:space:]]Registered([[:space:]]|$)' || true
}

# Asterisk が完全に起動するまで待つ。0=起動した
wait_booted() {
    local _
    for _ in $(seq 1 30); do
        sleep 2
        [ -n "$(ast_pid)" ] || continue
        if timeout 10 asterisk -rx "core waitfullybooted" >/dev/null 2>&1; then
            return 0
        fi
    done
    return 1
}
wait_stopped() {
    local _
    for _ in $(seq 1 30); do
        [ -z "$(ast_pid)" ] && return 0
        sleep 1
    done
    return 1
}

# --- asterisk.conf の場所 (起動コマンドの -C が優先) ---
AST_PID="$(ast_pid)"
AST_CMD=""
[ -n "$AST_PID" ] && AST_CMD="$(tr '\0' ' ' < "/proc/$AST_PID/cmdline" 2>/dev/null || true)"
AST_CONF="$(printf '%s' "$AST_CMD" | grep -oE -- '(^| )-C +[^ ]+' | head -1 | sed -E 's/^ ?-C +//' || true)"
AST_CONF="${AST_CONF:-/etc/asterisk/asterisk.conf}"

# asterisk.conf の [options] の runuser / rungroup を NEW_USER にする。
# 既存の行 (コメントアウトされたものを含む) があればそこを書き換え、
# 無ければ [options] の最後に足す。ファイルの持ち主と権限は変えない。
set_runuser() {
    local f="$1" tmp
    tmp="$(mktemp)"
    awk -v u="$NEW_USER" -v mark="$MARK" '
        function line(k) { return k " = " u "\t\t" mark }
        function emit_missing() {
            if (!du) print line("runuser")
            if (!dg) print line("rungroup")
            du = 1; dg = 1
        }
        /^[[:space:]]*\[/ {
            if (inopt) emit_missing()
            inopt = ($0 ~ /^[[:space:]]*\[options\]/)
            if (inopt) seen = 1
            print; next
        }
        inopt && /^[[:space:]]*;?[[:space:]]*runuser[[:space:]]*=/ {
            if (!du) { print line("runuser"); du = 1 }
            else if ($0 !~ /^[[:space:]]*;/) print ";" $0
            else print
            next
        }
        inopt && /^[[:space:]]*;?[[:space:]]*rungroup[[:space:]]*=/ {
            if (!dg) { print line("rungroup"); dg = 1 }
            else if ($0 !~ /^[[:space:]]*;/) print ";" $0
            else print
            next
        }
        { print }
        END {
            if (inopt) emit_missing()
            else if (!seen) { print ""; print "[options]"; emit_missing() }
        }
    ' "$f" > "$tmp"
    cat "$tmp" > "$f"
    rm -f "$tmp"
}
# このスクリプトが書いた行だけを無効にする (コメントにする)
unset_runuser() {
    local f="$1" tmp
    tmp="$(mktemp)"
    awk -v mark="$MARK" '
        index($0, mark) && /^[[:space:]]*run(user|group)[[:space:]]*=/ { print ";" $0; next }
        { print }
    ' "$f" > "$tmp"
    cat "$tmp" > "$f"
    rm -f "$tmp"
}
# [options] で有効になっている runuser の値
conf_runuser() {
    awk '
        /^[[:space:]]*\[/ { inopt = ($0 ~ /^[[:space:]]*\[options\]/); next }
        inopt && /^[[:space:]]*runuser[[:space:]]*=/ {
            v = $0; sub(/^[^=]*=[[:space:]]*/, "", v); sub(/[[:space:]]*;.*$/, "", v); sub(/[[:space:]]+$/, "", v); r = v
        }
        END { print r }
    ' "$AST_CONF"
}

restart_asterisk() {
    systemctl stop "$AST_UNIT" 2>/dev/null || true
    wait_stopped || err "Asterisk が止まりません。"
    systemctl start "$AST_UNIT"
}

# -----------------------------------------------------------------------------
# 元に戻す
# -----------------------------------------------------------------------------
if [ "$ROLLBACK" = "1" ]; then
    log "元に戻す: Asterisk を root で動かします"
    grep -qF "$MARK" "$AST_CONF" 2>/dev/null \
        || err "${AST_CONF} にこのスクリプトが書いた設定がありません。元に戻すものはありません。"
    if [ -n "$AST_PID" ] && [ "$FORCE" != "1" ]; then
        calls="$(active_calls)"
        [ "$calls" = "0" ] || err "通話中です (${calls} 件)。通話が終わってから実行するか、FORCE=1 を付けてください。"
    fi
    unset_runuser "$AST_CONF"
    ok "${AST_CONF} の runuser / rungroup を無効にしました"
    restart_asterisk
    wait_booted || err "Asterisk が起動しません。journalctl -u ${AST_UNIT} を確認してください。"
    ok "Asterisk を $(pid_user "$(ast_pid)") で起動しました"
    info "ファイルの持ち主は ${NEW_USER} のままですが、root で動く場合は影響ありません。"
    exit 0
fi

# -----------------------------------------------------------------------------
log "1/3  今の状態を確認"
# -----------------------------------------------------------------------------
[ -n "$AST_PID" ] || err "Asterisk が動いていません。起動してから実行してください。"
CUR_USER="$(pid_user "$AST_PID")"
echo "  Asterisk の実行ユーザー : ${CUR_USER}"
echo "  切り替え後              : ${NEW_USER}"
echo "  設定ファイル            : ${AST_CONF}"
echo "  起動コマンド            : ${AST_CMD}"

if [ "$CUR_USER" = "$NEW_USER" ]; then
    ok "Asterisk はすでに ${NEW_USER} で動いています。切り替えは不要です。"
    exit 0
fi
[ -f "$AST_CONF" ] || err "${AST_CONF} がありません。"

BLOCKERS=()
CHANGES=()

# --- 起動方法 ---
# systemd のサービスとして動いていれば、止める・起動するを自動で行える
MANAGED=0
if systemctl cat "$AST_UNIT" >/dev/null 2>&1 && systemctl is-active --quiet "$AST_UNIT"; then
    MAIN_PID="$(systemctl show -p MainPID --value "$AST_UNIT" 2>/dev/null || true)"
    PPID_AST="$(ps -o ppid= -p "$AST_PID" 2>/dev/null | tr -d ' ' || true)"
    if [ "$MAIN_PID" = "$AST_PID" ] || [ "$MAIN_PID" = "$PPID_AST" ] \
       || grep -q "${AST_UNIT}.service" "/proc/$AST_PID/cgroup" 2>/dev/null; then
        MANAGED=1
    fi
fi
if [ "$MANAGED" = "1" ]; then
    ok "Asterisk は systemd のサービス ${AST_UNIT} として動いています"
else
    BLOCKERS+=("Asterisk が systemd のサービス ${AST_UNIT} として動いていないため、自動では再起動できません。サービス名が違う場合は AST_UNIT=<名前> を付けてください。")
fi

# --- 起動コマンドの -U / -G (asterisk.conf より優先される) ---
CMD_U="$(printf '%s' "$AST_CMD" | grep -oE -- '(^| )-U +[^ ]+' | head -1 | sed -E 's/^ ?-U +//' || true)"
CMD_G="$(printf '%s' "$AST_CMD" | grep -oE -- '(^| )-G +[^ ]+' | head -1 | sed -E 's/^ ?-G +//' || true)"
if [ -n "$CMD_U" ] && [ "$CMD_U" != "$NEW_USER" ]; then
    BLOCKERS+=("起動コマンドに -U ${CMD_U} が付いています (asterisk.conf より優先されます)。/etc/default/asterisk の AST_USER か、サービスの ExecStart を確認してください。")
fi
if [ -n "$CMD_G" ] && [ "$CMD_G" != "$NEW_USER" ]; then
    BLOCKERS+=("起動コマンドに -G ${CMD_G} が付いています (asterisk.conf より優先されます)。/etc/default/asterisk の AST_GROUP か、サービスの ExecStart を確認してください。")
fi

# --- asterisk.conf の runuser ---
CONF_RU="$(conf_runuser)"
if [ "$CONF_RU" = "$NEW_USER" ]; then
    info "${AST_CONF} には runuser = ${NEW_USER} が書かれています (再起動で切り替わります)"
elif [ -n "$CONF_RU" ]; then
    BLOCKERS+=("${AST_CONF} に runuser = ${CONF_RU} が書かれています。意図した設定か確認してください。")
else
    todo "${AST_CONF} の [options] に runuser / rungroup = ${NEW_USER} を書きます (元のファイルは .bak-日時 で残します)"
fi

# --- Asterisk が書き込む場所 ---
dir_of() {   # [directories] の値 (無ければ既定値)
    local key="$1" def="$2" v
    v="$(sed -n "s/^[[:space:]]*${key}[[:space:]]*=>\{0,1\}[[:space:]]*\([^;[:space:]]*\).*/\1/p" "$AST_CONF" | tail -1)"
    printf '%s' "${v:-$def}"
}
ETC_DIR="$(dir_of astetcdir /etc/asterisk)"
CAND=(
    "$ETC_DIR"
    "$(dir_of astvarlibdir /var/lib/asterisk)"
    "$(dir_of astdbdir /var/lib/asterisk)"
    "$(dir_of astkeydir /var/lib/asterisk)"
    "$(dir_of astdatadir /var/lib/asterisk)"
    "$(dir_of astagidir /var/lib/asterisk/agi-bin)"
    "$(dir_of astspooldir /var/spool/asterisk)"
    "$(dir_of astrundir /var/run/asterisk)"
    "$(dir_of astlogdir /var/log/asterisk)"
    "$(dir_of astcachedir /var/cache/asterisk)"
)
OWN_DIRS=()
for d in "${CAND[@]}"; do
    [ -d "$d" ] || continue
    real="$(readlink -f "$d")"
    case "$real" in /usr/*) continue ;; esac            # 読むだけの場所 (パッケージの置き場所)
    case "$real" in *asterisk*) ;; *)
        BLOCKERS+=("${d} は Asterisk 専用の場所に見えないため、自動では持ち主を変えません。"); continue ;;
    esac
    dup=0
    for o in "${OWN_DIRS[@]}"; do
        case "$real/" in "$o"/*) dup=1 ;; esac
    done
    [ "$dup" = "1" ] && continue
    OWN_DIRS+=("$real")
done
for d in "${OWN_DIRS[@]}"; do
    if [ -z "$(find "$d" ! -user "$NEW_USER" -print -quit 2>/dev/null)" ]; then
        ok "${d}"
    else
        todo "${d}  持ち主を ${NEW_USER} にします"
        CHANGES+=("$d")
    fi
done
under_own_dirs() {
    local p o
    p="$(readlink -f "$1" 2>/dev/null || printf '%s' "$1")"
    for o in "${OWN_DIRS[@]}"; do
        case "$p/" in "$o"/*) return 0 ;; esac
    done
    return 1
}

# --- 設定ファイルが参照する外部のファイル (証明書・保留音のフォルダ) ---
conf_values() {   # キー名の正規表現 → 値 (コメントを除く)
    grep -hE "^[[:space:]]*($1)[[:space:]]*=" "$ETC_DIR"/*.conf 2>/dev/null \
        | sed -E 's/^[^=]*=>?[[:space:]]*//; s/[[:space:]]*;.*$//; s/[[:space:]]+$//' || true
}
while IFS= read -r f; do
    [ -n "$f" ] || continue
    case "$f" in /*) ;; *) continue ;; esac
    under_own_dirs "$f" && continue
    if ! as_user test -r "$f" 2>/dev/null; then
        BLOCKERS+=("証明書などのファイル ${f} を ${NEW_USER} が読めません。TLS が使えなくなるため、${NEW_USER} が読めるようにしてください (例: グループ ${NEW_USER} に読み取り権限を付ける)。")
    fi
done < <(conf_values 'cert_file|priv_key_file|ca_list_file|ca_list_path|tlscertfile|tlsprivatekey|tlscafile|tlscapath')
while IFS= read -r f; do
    [ -n "$f" ] || continue
    case "$f" in /*) ;; *) continue ;; esac
    under_own_dirs "$f" && continue
    if ! as_user test -r "$f" 2>/dev/null || ! as_user test -x "$f" 2>/dev/null; then
        BLOCKERS+=("保留音のフォルダ ${f} を ${NEW_USER} が読めません。${NEW_USER} が読めるようにしてください。")
    fi
done < <(grep -hE '^[[:space:]]*directory[[:space:]]*=' "$ETC_DIR/musiconhold.conf" 2>/dev/null \
             | sed -E 's/^[^=]*=>?[[:space:]]*//; s/[[:space:]]*;.*$//; s/[[:space:]]+$//' || true)

# --- 1024 未満のポート (root 以外では開けない) ---
LOW_PORTS="$( { conf_values 'bind' | { grep -oE ':[0-9]+$' || true; } | tr -d ':'; \
                { grep -hE '^[[:space:]]*(bindport|tlsbindport)[[:space:]]*=' "$ETC_DIR/http.conf" 2>/dev/null || true; } \
                    | { grep -oE '[0-9]+' || true; } | head -2; } | awk '$1 < 1024' | sort -u | tr '\n' ' ' || true)"
if [ -n "${LOW_PORTS// /}" ]; then
    BLOCKERS+=("1024 未満のポート (${LOW_PORTS% }) で待ち受ける設定があります。root 以外ではこのポートを開けません。")
fi

# --- 通話と回線登録 ---
CALLS="$(active_calls)"
REG_BEFORE="$(registered_count)"
info "通話中: ${CALLS} 件 / 回線の登録 (Registered): ${REG_BEFORE} 件"

if [ "${#BLOCKERS[@]}" -gt 0 ]; then
    echo ""
    for b in "${BLOCKERS[@]}"; do ng "$b"; done
    echo ""
    err "上の NG を解決するまで切り替えられません (何も変更していません)。"
fi

if [ "$APPLY" != "1" ]; then
    echo ""
    echo "  確認だけを行いました (何も変更していません)。"
    echo "  切り替えるには次を実行してください。Asterisk を再起動するため、"
    echo "  通話が切れ、再起動が終わるまで (数十秒) 電話が使えません。"
    echo "  通話の少ない時間帯に行ってください:"
    echo ""
    echo "    sudo APPLY=1 bash $0"
    exit 0
fi

# -----------------------------------------------------------------------------
log "2/3  切り替え"
# -----------------------------------------------------------------------------
if [ "$CALLS" != "0" ] && [ "$FORCE" != "1" ]; then
    err "通話中です (${CALLS} 件)。通話が終わってから実行するか、FORCE=1 を付けてください。"
fi
TS="$(date +%Y%m%d-%H%M%S)"
CONF_BAK="${AST_CONF}.bak-${TS}"
cp -a "$AST_CONF" "$CONF_BAK"
ok "バックアップ: ${CONF_BAK}"

# ログのうち、再起動後に増えた部分だけを見るため、今の大きさを控える
LOG_DIR="$(dir_of astlogdir /var/log/asterisk)"
declare -A LOG_SIZE=()
for lf in "$LOG_DIR"/messages "$LOG_DIR"/messages.log "$LOG_DIR"/full "$LOG_DIR"/full.log; do
    [ -f "$lf" ] && LOG_SIZE["$lf"]="$(stat -c %s "$lf")"
done

systemctl stop "$AST_UNIT" 2>/dev/null || true
wait_stopped || err "Asterisk が止まりません (何も変更していません)。"
ok "Asterisk を停止しました"

# 途中で失敗したときに root で動かす設定に戻して起動する
revert_and_start() {
    systemctl stop "$AST_UNIT" 2>/dev/null || true
    wait_stopped || true
    unset_runuser "$AST_CONF"
    systemctl start "$AST_UNIT" 2>/dev/null || true
    if wait_booted; then
        warn "Asterisk を $(pid_user "$(ast_pid)") で起動し直しました。"
        warn "(ファイルの持ち主は ${NEW_USER} のままですが、root で動く場合は影響ありません)"
    else
        warn "Asterisk が起動しません。journalctl -u ${AST_UNIT} を確認してください。"
    fi
}

for d in "${CHANGES[@]}"; do
    chown -R "$NEW_USER:$NEW_USER" "$d"
    ok "${d}: 持ち主を ${NEW_USER} にしました"
done
if [ "$CONF_RU" != "$NEW_USER" ]; then
    set_runuser "$AST_CONF"
    [ "$(conf_runuser)" = "$NEW_USER" ] || { systemctl start "$AST_UNIT" || true; err "${AST_CONF} を書き換えられませんでした。"; }
    ok "${AST_CONF}: runuser / rungroup = ${NEW_USER} を書きました"
fi

# -----------------------------------------------------------------------------
log "3/3  起動と確認"
# -----------------------------------------------------------------------------
systemctl start "$AST_UNIT" || true
if ! wait_booted; then
    journalctl -u "$AST_UNIT" -n 30 --no-pager 2>/dev/null || true
    warn "Asterisk が起動しないため、root で動かす設定に戻します。"
    revert_and_start
    err "切り替えに失敗しました。上のログを確認してください。"
fi
NOW_USER="$(pid_user "$(ast_pid)")"
if [ "$NOW_USER" != "$NEW_USER" ]; then
    warn "Asterisk が ${NOW_USER} で動いています (${NEW_USER} に切り替わっていません)。元に戻します。"
    revert_and_start
    err "切り替えに失敗しました。起動コマンドに -U が付いていないか確認してください。"
fi
ok "Asterisk が ${NEW_USER} で起動しました"

# 回線の登録が戻るのを待つ (HGW などへの登録には少し時間がかかる)
REG_AFTER=0
for _ in $(seq 1 30); do
    REG_AFTER="$(registered_count)"
    [ "$REG_AFTER" -ge "$REG_BEFORE" ] && break
    sleep 2
done
if [ "$REG_AFTER" -lt "$REG_BEFORE" ]; then
    warn "回線の登録が戻りません (切り替え前 ${REG_BEFORE} 件 → 今 ${REG_AFTER} 件)。元に戻します。"
    ast_cli "pjsip show registrations" | sed 's/^/    /' >&2
    revert_and_start
    err "切り替えに失敗しました。"
fi
ok "回線の登録: ${REG_AFTER} 件 (切り替え前 ${REG_BEFORE} 件)"

# 再起動後のログに、権限の問題が出ていないか
PERM_LINES=""
for lf in "${!LOG_SIZE[@]}"; do
    [ -f "$lf" ] || continue
    PERM_LINES+="$(tail -c +"$(( ${LOG_SIZE[$lf]} + 1 ))" "$lf" 2>/dev/null \
                   | grep -E 'Permission denied|Unable to open|Unable to create' | head -5 || true)"
done
if [ -n "$PERM_LINES" ]; then
    warn "ログに権限の問題が出ています (動作を確認してください):"
    printf '%s\n' "$PERM_LINES" | sed 's/^/    /' >&2
else
    ok "ログに権限の問題は出ていません"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cat <<EOF

============================================================
 切り替えが終わりました。Asterisk は ${NEW_USER} で動いています。

 電話機から次を確かめてください。
   - 内線どうしの通話・外線の発着信
   - 留守番電話の録音と再生
   - FAX の受信

 続けて、Web 画面も ${NEW_USER} で動かす場合:
   sudo bash ${SCRIPT_DIR}/switch_service_user.sh

 元に戻す場合 (Asterisk を再起動します):
   sudo ROLLBACK=1 bash $0
============================================================
EOF
