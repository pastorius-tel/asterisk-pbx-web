#!/usr/bin/env bash
# =============================================================================
# 本ツールと Asterisk が読み書きするフォルダを、本ツールの実行ユーザー
# (既定 asterisk) が使えるかを確かめ、足りなければ直す。
#
# 本ツールは root では動かないため、/var/lib や /var/spool の下に自分で
# フォルダを作ることができない。インストール時に用意されていないフォルダが
# あると、その機能だけが動かなくなる (例: 受信 FAX の PDF 保存先が無く、
# PDF 変換が失敗していた)。ここで全部の置き場をまとめて確かめる。
#
# 使い方:
#   sudo bash check_permissions.sh             # 確認だけ (何も変更しない)
#   sudo APPLY=1 bash check_permissions.sh     # 足りない権限・フォルダを直す
#
# 確認する場所 (.env で場所を変えている場合はその場所):
#   インストール先            .env・データベース・アップロード・バックアップ
#   /etc/asterisk             Asterisk の設定ファイル (中のファイルまで)
#   /var/spool/asterisk/voicemail  留守番電話のメッセージ
#   /var/spool/asterisk/fax   FAX のスプール。Asterisk が受信した FAX を
#                             ここに書くため、無いと FAX を受信できない
#   /var/lib/asterisk-pbx-web 受信・送信 FAX の PDF (fax)・音声合成の声 (hts-voice)
#   /var/lib/asterisk/sounds/ja    日本語音声・アップロードした音源・日時読み上げ
#   /var/lib/asterisk/sounds  画面から日本語音声を入れるときの展開先
#
# 標準以外の場所 (.env で指定した Samba の共有フォルダなど) は、持ち主や
# グループを勝手に変えず、必要なコマンドを表示するだけにする。
#
# 環境変数:
#   APP_USER     確かめるユーザー (既定: サービスの User=。root なら asterisk)
#   INSTALL_DIR  インストール先 (既定: サービスの WorkingDirectory)
#   UNIT_NAME    サービス名 (既定: asterisk-pbx-web)
#   QUIET=1      問題のある場所と変更した内容だけを表示する
#
# switch_service_user.sh からは、このファイルを読み込んで (source) 関数だけを使う。
# =============================================================================

if [ -z "${BASH_VERSION:-}" ]; then
    command -v bash >/dev/null 2>&1 && exec bash "$0" "$@"
    echo "エラー: このスクリプトの実行には bash が必要です。" >&2
    exit 1
fi

# --- 表示 (読み込んだ側で定義済みならそちらを使う) ---
declare -F log  >/dev/null || log()  { echo -e "\n\033[1;36m[$(date '+%H:%M:%S')] $*\033[0m"; }
declare -F ok   >/dev/null || ok()   { echo -e "  \033[1;32m OK \033[0m $*"; }
declare -F info >/dev/null || info() { echo -e "  \033[1;37m -- \033[0m $*"; }
declare -F todo >/dev/null || todo() { echo -e "  \033[1;33m要対応\033[0m $*"; }
declare -F warn >/dev/null || warn() { echo -e "  \033[1;33mWARN\033[0m $*" >&2; }
declare -F ng   >/dev/null || ng()   { echo -e "  \033[1;31m NG \033[0m $*"; }
perm_ok()   { [ "${QUIET:-0}" = "1" ] || ok "$@"; }
perm_info() { [ "${QUIET:-0}" = "1" ] || info "$@"; }

# =============================================================================
# ここから関数 (PERM_USER と PERM_INSTALL_DIR を決めてから perm_init を呼ぶ)
# =============================================================================

perm_as_user() {
    if command -v runuser >/dev/null 2>&1; then
        runuser -u "$PERM_USER" -- "$@"
    else
        sudo -n -u "$PERM_USER" -- "$@"
    fi
}

# .env の値 (シェルとして実行はしない。値は表示しない)
perm_env_get() {
    local key="$1" line
    [ -f "$PERM_ENV_FILE" ] || return 0
    line="$(grep -iE "^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*=" "$PERM_ENV_FILE" | tail -1 || true)"
    [ -n "$line" ] || return 0
    line="${line#*=}"
    line="$(printf '%s' "$line" | sed -E 's/\r$//; s/^[[:space:]]+//')"
    case "$line" in
        \"*) line="${line#\"}"; line="${line%%\"*}" ;;
        \'*) line="${line#\'}"; line="${line%%\'*}" ;;
        *)   line="$(printf '%s' "$line" | sed -E 's/[[:space:]]+#.*$//; s/[[:space:]]+$//')" ;;
    esac
    printf '%s' "$line"
}
# 相対パスは WorkingDirectory (= インストール先) から見た場所。空なら空
perm_abs_path() {
    case "$1" in
        "") ;;
        /*) printf '%s' "$1" ;;
        *)  printf '%s' "$PERM_INSTALL_DIR/${1#./}" ;;
    esac
}
perm_inside_install() { case "$1/" in "$PERM_INSTALL_DIR"/*) return 0 ;; esac; return 1; }

# 自動で権限を変えてよい場所か (Asterisk と本ツールの標準の置き場だけ)
perm_auto_ok() {
    local p
    p="$(readlink -f "$1" 2>/dev/null || printf '%s' "$1")"
    case "$p/" in
        /etc/asterisk/*|/var/spool/asterisk/*|/var/lib/asterisk/*|/var/lib/asterisk-pbx-web/*) return 0 ;;
    esac
    return 1
}

perm_owned_tree() { [ -z "$(find "$1" ! -user "$PERM_USER" -print -quit 2>/dev/null)" ]; }
perm_writable_tree() {
    perm_as_user test -w "$1" 2>/dev/null || return 1
    [ -z "$(perm_as_user find "$1" ! -writable -print -quit 2>/dev/null)" ]
}
perm_in_group() { id -nG "$PERM_USER" 2>/dev/null | tr ' ' '\n' | grep -qx "$1"; }

perm_init() {
    PERM_ENV_FILE="$PERM_INSTALL_DIR/.env"
    PERM_GROUP="$(id -gn "$PERM_USER" 2>/dev/null || echo "$PERM_USER")"

    # Asterisk と共有する場所のグループ。Asterisk が動いていればそのグループ
    local ast_pid
    ast_pid="$(pgrep -o -x asterisk 2>/dev/null || true)"
    PERM_SHARE_GROUP=""
    [ -n "$ast_pid" ] && PERM_SHARE_GROUP="$(ps -o group= -p "$ast_pid" 2>/dev/null | tr -d ' ' || true)"
    [ "$PERM_SHARE_GROUP" = "root" ] && PERM_SHARE_GROUP=""
    if [ -z "$PERM_SHARE_GROUP" ]; then
        if getent group asterisk >/dev/null 2>&1; then PERM_SHARE_GROUP="asterisk"; else PERM_SHARE_GROUP="$PERM_GROUP"; fi
    fi

    P_CONF_DIR="$(perm_abs_path "$(perm_env_get ASTERISK_CONFIG_DIR)")"
    P_VM_DIR="$(perm_abs_path "$(perm_env_get VOICEMAIL_SPOOL_DIR)")"
    P_FAX_SPOOL="$(perm_abs_path "$(perm_env_get FAX_SPOOL_DIR)")"
    P_FAX_STORE="$(perm_abs_path "$(perm_env_get FAX_STORE_DIR)")"
    P_HTS_DIR="$(perm_abs_path "$(perm_env_get HTS_VOICE_DIR)")"
    P_SND_DIR="$(perm_abs_path "$(perm_env_get ASTERISK_SOUNDS_DIR)")"
    P_UPL_DIR="$(perm_abs_path "$(perm_env_get UPLOADS_DIR)")"
    P_BAK_DIR="$(perm_abs_path "$(perm_env_get BACKUP_DIR)")"
    P_CONF_DIR="${P_CONF_DIR:-/etc/asterisk}"
    P_VM_DIR="${P_VM_DIR:-/var/spool/asterisk/voicemail}"
    P_FAX_SPOOL="${P_FAX_SPOOL:-/var/spool/asterisk/fax}"
    P_FAX_STORE="${P_FAX_STORE:-/var/lib/asterisk-pbx-web/fax}"
    P_HTS_DIR="${P_HTS_DIR:-/var/lib/asterisk-pbx-web/hts-voice}"
    P_SND_DIR="${P_SND_DIR:-/var/lib/asterisk/sounds/ja/managed}"
    P_DATA_BASE="/var/lib/asterisk-pbx-web"   # 本ツールのデータ置き場 (固定)
    P_JA_DIR="/var/lib/asterisk/sounds/ja"     # 日本語音声・日時読み上げ (固定)
    P_SOUNDS_ROOT="/var/lib/asterisk/sounds"   # 日本語音声パックの展開先 (固定)

    local db_url
    db_url="$(perm_env_get DATABASE_URL)"
    PERM_DB_FILE=""
    case "${db_url:-sqlite+aiosqlite:///./pbx.db}" in
        sqlite*)
            PERM_DB_FILE="${db_url:-sqlite+aiosqlite:///./pbx.db}"
            PERM_DB_FILE="${PERM_DB_FILE#*:///}"; PERM_DB_FILE="${PERM_DB_FILE%%\?*}"
            PERM_DB_FILE="$(perm_abs_path "$PERM_DB_FILE")"
            ;;
    esac

    # 種類:
    #   app   : インストール先。持ち主ごと PERM_USER に
    #   own   : 本ツールだけが使う場所。無ければ作り、持ち主ごと PERM_USER に
    #   ownx  : 同上だが、無ければ作らない
    #   share : Asterisk と共有する場所。持ち主は変えず、グループを Asterisk の
    #           グループにして書き込み可に
    #   sharec: 同上で、無ければ作る (無いと Asterisk が書き込めないもの)
    #   dir   : そのフォルダ自体だけ (中は変えない) グループ書き込み可に
    #   dbfile: インストール先の外にあるデータベースファイル (と -wal / -shm)
    PERM_TARGETS=()
    _pt() { PERM_TARGETS+=("$1|$2|$3"); }   # 種類|場所|説明
    _pt app    "$PERM_INSTALL_DIR" "インストール先 (.env・データベース・アップロード・バックアップ)"
    _pt share  "$P_CONF_DIR"       "Asterisk の設定ファイル"
    _pt share  "$P_VM_DIR"         "留守番電話のメッセージ"
    _pt sharec "$P_FAX_SPOOL"      "FAX のスプール (Asterisk が受信した FAX を書く場所)"
    case "$P_FAX_STORE/ $P_HTS_DIR/" in
        *"$P_DATA_BASE"/*) _pt own "$P_DATA_BASE" "本ツールのデータ置き場" ;;
    esac
    perm_inside_install "$P_FAX_STORE" || _pt own "$P_FAX_STORE" "受信・送信 FAX の PDF"
    perm_inside_install "$P_HTS_DIR"   || _pt own "$P_HTS_DIR"   "音声合成の声データ"
    _pt ownx   "$P_JA_DIR"         "日本語音声・日時読み上げ"
    perm_inside_install "$P_SND_DIR"   || _pt own "$P_SND_DIR"   "アップロードした音源・保留音"
    _pt dir    "$P_SOUNDS_ROOT"    "日本語音声パックの展開先"
    if [ -n "$P_UPL_DIR" ] && ! perm_inside_install "$P_UPL_DIR"; then _pt own "$P_UPL_DIR" "アップロード"; fi
    if [ -n "$P_BAK_DIR" ] && ! perm_inside_install "$P_BAK_DIR"; then _pt own "$P_BAK_DIR" "バックアップ"; fi
    if [ -n "$PERM_DB_FILE" ] && ! perm_inside_install "$PERM_DB_FILE"; then _pt dbfile "$PERM_DB_FILE" "データベース"; fi
    unset -f _pt
}

# 場所が PERM_USER から使えるか。0=問題なし 1=要修正 2=まだ無い
perm_needs_fix() {
    local kind="$1" path="$2" real f
    [ -e "$path" ] || return 2
    real="$(readlink -f "$path")"
    case "$kind" in
        dir)
            perm_as_user test -w "$real" 2>/dev/null ;;
        app)
            perm_owned_tree "$real" ;;
        own|ownx)
            # 標準以外の場所は持ち主を変えないので、書き込めるかどうかで判定する
            if perm_auto_ok "$real"; then perm_owned_tree "$real"; else perm_writable_tree "$real"; fi ;;
        share|sharec)
            perm_writable_tree "$real" ;;
        dbfile)
            for f in "$real" "$real-wal" "$real-shm"; do
                [ -e "$f" ] || continue
                [ "$(stat -c %U "$f")" = "$PERM_USER" ] || return 1
            done
            return 0 ;;
    esac
}

# 全部の場所を確かめて表示し、PERM_CHANGES (直すもの) と PERM_BLOCKERS
# (自動では直さないもの) に振り分ける
perm_classify() {
    PERM_CHANGES=()
    PERM_BLOCKERS=()
    local t kind path desc rc
    for t in "${PERM_TARGETS[@]}"; do
        IFS='|' read -r kind path desc <<< "$t"
        rc=0; perm_needs_fix "$kind" "$path" || rc=$?
        if [ "$rc" = "0" ]; then
            perm_ok "${path}  (${desc})"
            continue
        fi
        if [ "$rc" = "2" ]; then
            case "$kind" in
                own|sharec)
                    if perm_auto_ok "$path"; then
                        todo "${path}  まだ無いので作ります  (${desc})"; PERM_CHANGES+=("$t")
                    else
                        PERM_BLOCKERS+=("${path} (${desc}) がありません。標準以外の場所のため自動では作りません。${PERM_USER} が書き込める状態で用意してください:  sudo install -d -o ${PERM_USER} -g ${PERM_GROUP} ${path}")
                    fi ;;
                share) perm_info "${path}  まだありません (Asterisk が作ります)" ;;
                *)     perm_info "${path}  まだありません" ;;
            esac
            continue
        fi
        # rc=1 (要修正)
        case "$kind" in
            own|ownx|share|sharec)
                if ! perm_auto_ok "$path"; then
                    PERM_BLOCKERS+=("${path} (${desc}) に ${PERM_USER} が書き込めません。標準以外の場所のため、持ち主やグループは自動では変えません。次のように ${PERM_USER} だけに書き込みを許可してください:  sudo setfacl -R -m u:${PERM_USER}:rwX -m d:u:${PERM_USER}:rwX ${path}")
                    continue
                fi ;;
            dbfile)
                if ! perm_as_user test -w "$(dirname "$(readlink -f "$path")")" 2>/dev/null; then
                    PERM_BLOCKERS+=("データベース (${path}) のフォルダに ${PERM_USER} が書き込めません。SQLite は同じフォルダに作業用ファイルを作るため、そのフォルダに ${PERM_USER} が書き込めるよう手動で権限を付けてください。")
                    continue
                fi ;;
        esac
        case "$kind" in
            app|own|ownx|dbfile) todo "${path}  持ち主を ${PERM_USER} にします  (${desc})" ;;
            share|sharec)        todo "${path}  グループを ${PERM_SHARE_GROUP} にして書き込み可にします  (${desc})" ;;
            dir)                 todo "${path}  このフォルダだけグループを ${PERM_SHARE_GROUP} にして書き込み可にします  (${desc})" ;;
        esac
        PERM_CHANGES+=("$t")
    done

    # 共有する場所はグループで書き込むので、PERM_USER がそのグループに入っている必要がある
    PERM_ADD_GROUP=0
    if [ "$PERM_SHARE_GROUP" != "$PERM_GROUP" ] && ! perm_in_group "$PERM_SHARE_GROUP"; then
        todo "${PERM_USER} をグループ ${PERM_SHARE_GROUP} に加えます (Asterisk と共有する場所を使うため。反映にはサービスの再起動が必要です)"
        PERM_ADD_GROUP=1
    fi
}

# path を作る。途中で新しく作ったフォルダも、標準の置き場の中なら PERM_USER の持ち物にする
# (mkdir -p で作った親が root の持ち物になり、本ツールが中に作れなくなるのを防ぐ)
perm_mkdir() {
    local p="$1" d created=()
    d="$p"
    while [ ! -e "$d" ] && [ "$d" != "/" ]; do created+=("$d"); d="$(dirname "$d")"; done
    mkdir -p "$p"
    for d in "${created[@]}"; do
        perm_auto_ok "$d" && chown "$PERM_USER:$PERM_GROUP" "$d"
    done
    return 0
}

# PERM_CHANGES を適用する。PERM_SKIP_APP=1 ならインストール先 (app) は飛ばす
perm_apply() {
    local t kind path desc real f
    for t in "${PERM_CHANGES[@]}"; do
        IFS='|' read -r kind path desc <<< "$t"
        case "$kind" in
            app)
                [ "${PERM_SKIP_APP:-0}" = "1" ] && continue
                chown -R "$PERM_USER:$PERM_GROUP" "$path"
                ok "${path}: 持ち主を ${PERM_USER} にしました"
                ;;
            own|ownx)
                [ "$kind" = "own" ] && [ ! -e "$path" ] && perm_mkdir "$path"
                chown -R "$PERM_USER:$PERM_GROUP" "$(readlink -f "$path")"
                ok "${path}: 持ち主を ${PERM_USER} にしました"
                ;;
            share|sharec)
                if [ ! -e "$path" ]; then
                    perm_mkdir "$path"
                    chown "$PERM_USER:$PERM_SHARE_GROUP" "$path"; chmod 775 "$path"
                    ok "${path}: 作成しました"
                else
                    real="$(readlink -f "$path")"
                    chgrp -R "$PERM_SHARE_GROUP" "$real"
                    chmod -R g+rwX "$real"
                    ok "${path}: グループを ${PERM_SHARE_GROUP} にし、書き込み可にしました"
                fi
                ;;
            dir)
                chgrp "$PERM_SHARE_GROUP" "$path"; chmod g+rwx "$path"
                ok "${path}: グループを ${PERM_SHARE_GROUP} にし、書き込み可にしました"
                ;;
            dbfile)
                real="$(readlink -f "$path")"
                for f in "$real" "$real-wal" "$real-shm"; do
                    [ -e "$f" ] && chown "$PERM_USER:$PERM_GROUP" "$f"
                done
                ok "${path}: 持ち主を ${PERM_USER} にしました"
                ;;
        esac
    done
    if [ "${PERM_ADD_GROUP:-0}" = "1" ]; then
        usermod -aG "$PERM_SHARE_GROUP" "$PERM_USER"
        ok "${PERM_USER} をグループ ${PERM_SHARE_GROUP} に加えました (サービスの再起動で反映されます)"
    fi
}

# もう一度確かめ、まだ使えない場所を表示する。0=全部使える
perm_verify() {
    local t kind path desc rc failed=0
    for t in "${PERM_TARGETS[@]}"; do
        IFS='|' read -r kind path desc <<< "$t"
        rc=0; perm_needs_fix "$kind" "$path" || rc=$?
        if [ "$rc" = "1" ] || { [ "$rc" = "2" ] && [ "$kind" = "sharec" ]; }; then
            ng "${path} (${desc}) をまだ ${PERM_USER} が使えません"; failed=1
        fi
    done
    return "$failed"
}

# =============================================================================
# 単体で実行したとき
# =============================================================================
perm_main() {
    set -euo pipefail
    local UNIT_NAME="${UNIT_NAME:-asterisk-pbx-web}" unit_user script_dir
    [ "$(id -u)" -eq 0 ] || { echo "ERROR: root で実行してください:  sudo bash $0" >&2; exit 1; }
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

    unit_user="$(systemctl show -p User --value "$UNIT_NAME" 2>/dev/null || true)"
    PERM_USER="${APP_USER:-}"
    if [ -z "$PERM_USER" ]; then
        if [ -n "$unit_user" ] && [ "$unit_user" != "root" ]; then PERM_USER="$unit_user"; else PERM_USER="asterisk"; fi
    fi
    id -u "$PERM_USER" >/dev/null 2>&1 || { echo "ERROR: ユーザー ${PERM_USER} がありません。" >&2; exit 1; }

    PERM_INSTALL_DIR="${INSTALL_DIR:-}"
    if [ -z "$PERM_INSTALL_DIR" ]; then
        PERM_INSTALL_DIR="$(systemctl show -p WorkingDirectory --value "$UNIT_NAME" 2>/dev/null || true)"
        PERM_INSTALL_DIR="${PERM_INSTALL_DIR#[-!]}"
    fi
    if [ -z "$PERM_INSTALL_DIR" ] || [ ! -f "$PERM_INSTALL_DIR/pyproject.toml" ]; then
        PERM_INSTALL_DIR="$(cd "$script_dir/.." && pwd)"
    fi
    [ -f "$PERM_INSTALL_DIR/pyproject.toml" ] \
        || { echo "ERROR: インストール先が分かりません。INSTALL_DIR=/path/to/asterisk-pbx-web を付けてください。" >&2; exit 1; }
    PERM_INSTALL_DIR="$(cd "$PERM_INSTALL_DIR" && pwd -P)"

    if [ "${QUIET:-0}" != "1" ]; then
        log "本ツールが使うフォルダの権限を確認"
        echo "  確かめるユーザー : ${PERM_USER}"
        echo "  インストール先   : ${PERM_INSTALL_DIR}"
        if [ -n "$unit_user" ] && [ "$unit_user" = "root" ]; then
            info "サービスは今 root で動いているため、実際には権限の問題は起きません (${PERM_USER} で動かしたときの準備状況を表示します)"
        fi
    fi

    perm_init
    perm_classify

    local rc=0
    if [ "${APPLY:-0}" = "1" ]; then
        if [ "${#PERM_CHANGES[@]}" -gt 0 ] || [ "$PERM_ADD_GROUP" = "1" ]; then
            perm_apply
            perm_verify || rc=1
        fi
    elif [ "${#PERM_CHANGES[@]}" -gt 0 ] || [ "$PERM_ADD_GROUP" = "1" ]; then
        rc=1
    fi

    local b
    for b in "${PERM_BLOCKERS[@]}"; do ng "$b"; rc=1; done

    if [ "${QUIET:-0}" != "1" ]; then
        echo ""
        if [ "$rc" = "0" ]; then
            ok "本ツールが使う場所すべてを ${PERM_USER} が使えます。"
        elif [ "${APPLY:-0}" != "1" ] && [ "${#PERM_CHANGES[@]}" -gt 0 ]; then
            echo "  確認だけを行いました (何も変更していません)。直すには次を実行してください:"
            echo ""
            echo "    sudo APPLY=1 bash $0"
        fi
    fi
    return "$rc"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    perm_main "$@"
fi
