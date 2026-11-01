#!/usr/bin/env bash
# =============================================================================
# Postfix (サーバー自身のメール送信) を、外部の SMTP サーバー (Gmail など)
# 経由で送るように設定する。
#
# ※ 本ツールの通知メール (受信 FAX・留守番電話) には Postfix は不要です。
#    これらは Web 画面の「FAX 送受信 → メール設定」の SMTP 設定を使い、
#    本ツールが直接送ります。Postfix が必要なのは、サーバー自身のメール
#    (cron の結果・自動更新の通知など、root 宛てのメール) を送りたい場合です。
#
# 使い方:
#   sudo bash setup_postfix.sh                           # 今の設定を表示 (何も変えない)
#   sudo APPLY=1 bash setup_postfix.sh                   # 設定する・変更する (質問に答える)
#   sudo TEST_TO=you@example.com bash setup_postfix.sh   # テストメールを送り、結果を表示
#   sudo DISABLE=1 bash setup_postfix.sh                 # 外部 SMTP 経由の送信をやめる
#
# APPLY=1 のときの環境変数 (省略すると質問します。今の値があればそれが既定値):
#   RELAY_HOST     SMTP サーバー (既定: smtp.gmail.com)
#   RELAY_PORT     ポート (既定: 587。465 も可)
#   SMTP_USER      ログインに使うアカウント (メールアドレス)
#   ROOT_FORWARD   root 宛てのメールの転送先 (空なら変更しない)
#   パスワードは画面に表示しない入力で尋ねます (シェルの履歴に残さないため)。
#   旧版の GMAIL_ADDRESS / GMAIL_APP_PASSWORD も受け付けます。
#
# 行うこと (APPLY=1):
#   - postfix と SASL 認証のモジュールが無ければ入れる
#   - main.cf を postconf で書き換える (元の main.cf は main.cf.bak-日時 で残す)
#       relayhost・SASL 認証・TLS 必須 (OS の証明書で検証)
#       inet_interfaces = loopback-only  … LAN 側に SMTP を開けない (送信専用)
#       送信元アドレスをログインしたアカウントに書き換える
#         (多くの SMTP サーバーは、アカウントと違う送信元を拒否するため)
#   - /etc/postfix/sasl_passwd (root だけが読める) にアカウントとパスワードを書く
# =============================================================================

if [ -z "${BASH_VERSION:-}" ]; then
    command -v bash >/dev/null 2>&1 && exec bash "$0" "$@"
    echo "エラー: このスクリプトの実行には bash が必要です。" >&2
    exit 1
fi

set -euo pipefail

APPLY="${APPLY:-0}"
DISABLE="${DISABLE:-0}"
TEST_TO="${TEST_TO:-}"
MAIN_CF="/etc/postfix/main.cf"
SASL_PASSWD="/etc/postfix/sasl_passwd"
SENDER_MAP="/etc/postfix/sender_canonical_relay"

log()  { echo -e "\n\033[1;36m[$(date '+%H:%M:%S')] $*\033[0m"; }
ok()   { echo -e "  \033[1;32m OK \033[0m $*"; }
info() { echo -e "  \033[1;37m -- \033[0m $*"; }
warn() { echo -e "  \033[1;33mWARN\033[0m $*" >&2; }
err()  { echo -e "\033[1;31mERROR: $*\033[0m" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || err "root で実行してください:  sudo bash $0"

installed() { command -v postconf >/dev/null 2>&1; }
pc() { postconf -h "$1" 2>/dev/null || true; }
# Berkeley DB (hash) を持たない新しい Postfix もあるため、既定の形式を使う
dbtype() { local t; t="$(pc default_database_type)"; printf '%s' "${t:-hash}"; }
cur_user() {   # sasl_passwd に書いてあるアカウント (パスワードは表示しない)
    [ -f "$SASL_PASSWD" ] || return 0
    awk 'NF>=2 {split($2,a,":"); print a[1]; exit}' "$SASL_PASSWD"
}

show_status() {
    log "Postfix の状態"
    if ! installed; then
        info "Postfix は入っていません (本ツールの通知メールには不要です)"
        return 0
    fi
    local relay user fwd
    echo "  版               : $(pc mail_version)"
    echo "  サービス         : $(systemctl is-active postfix 2>/dev/null || echo 不明)"
    relay="$(pc relayhost)"
    echo "  中継先 (relayhost): ${relay:-なし (自分で直接送る設定。家庭・社内の回線では届かないことが多い)}"
    echo "  待ち受け         : $(pc inet_interfaces)$([ "$(pc inet_interfaces)" = "loopback-only" ] && echo ' (サーバーの中からだけ。LAN には開いていません)')"
    echo "  TLS              : $(pc smtp_tls_security_level)"
    if [ "$(pc smtp_sasl_auth_enable)" = "yes" ]; then
        user="$(cur_user)"
        echo "  ログイン         : ${user:-未設定}  (パスワードは ${SASL_PASSWD} に保存・表示しません)"
    else
        echo "  ログイン         : なし"
    fi
    fwd="$(sed -n 's/^root:[[:space:]]*//p' /etc/aliases 2>/dev/null | head -1)"
    echo "  root 宛ての転送先: ${fwd:-なし}"
    local recent
    recent="$(mail_log_since "2 days ago" | grep -oE 'to=<[^>]+>.*status=[a-z]+( \(.{0,80})?' | tail -3 || true)"
    if [ -n "$recent" ]; then
        echo "  最近の送信結果   :"
        printf '%s\n' "$recent" | sed 's/^/    /'
    fi
}

# メールのログ (journal か /var/log/mail.log) のうち、指定時刻以降
mail_log_since() {
    local since="$1"
    if command -v journalctl >/dev/null 2>&1 \
       && journalctl --since "$since" -q -t postfix/smtp -t postfix/local -t postfix/qmgr -t postfix/bounce -t postfix/cleanup \
            --no-pager 2>/dev/null | grep -q .; then
        journalctl --since "$since" -q -t postfix/smtp -t postfix/local -t postfix/qmgr -t postfix/bounce -t postfix/cleanup --no-pager 2>/dev/null || true
    elif [ -f /var/log/mail.log ]; then
        tail -n 500 /var/log/mail.log
    fi
}

# -----------------------------------------------------------------------------
# テストメール
# -----------------------------------------------------------------------------
if [ -n "$TEST_TO" ]; then
    installed || err "Postfix が入っていません。先に sudo APPLY=1 bash $0 で設定してください。"
    case "$TEST_TO" in *@*.*) ;; *) err "TEST_TO にメールアドレスを指定してください。" ;; esac
    log "テストメールを ${TEST_TO} へ送ります"
    START="$(date '+%Y-%m-%d %H:%M:%S')"
    # ログからこのメールの結果を探せるよう、固有の Message-ID を付ける
    MSGID="pbxtest-$(date +%s)-${RANDOM}@$(hostname)"
    {
        echo "To: ${TEST_TO}"
        echo "Subject: Postfix test from $(hostname)"
        echo "Message-ID: <${MSGID}>"
        echo "MIME-Version: 1.0"
        echo "Content-Type: text/plain; charset=UTF-8"
        echo "Content-Transfer-Encoding: 8bit"
        echo ""
        echo "$(hostname) の Postfix からのテストメールです。"
        echo "送信日時: $(date '+%Y-%m-%d %H:%M:%S')"
    } | /usr/sbin/sendmail -i "$TEST_TO"
    RESULT=""
    for _try in $(seq 1 30); do
        sleep 1
        LOGS="$(mail_log_since "$START")"
        QID="$(printf '%s\n' "$LOGS" | grep -F "message-id=<${MSGID}>" \
               | sed -nE 's/.*postfix\/cleanup\[[0-9]+\]: ([0-9A-Za-z]+): .*/\1/p' | tail -1 || true)"
        [ -n "$QID" ] || continue
        RESULT="$(printf '%s\n' "$LOGS" | grep -F " ${QID}: to=<" | grep -oE 'status=[a-z]+.*' | tail -1 || true)"
        [ -n "$RESULT" ] && break
    done
    case "$RESULT" in
        status=sent*)     ok "送信できました: ${RESULT}" ;;
        status=deferred*) warn "まだ送れていません (再送待ち): ${RESULT}"
                          case "$RESULT" in
                              *"authentication failed"*|*"Authentication"*|*" 535"*|*"not accepted"*)
                                  warn "ログインできませんでした。アカウントとパスワードを確認してください (Gmail はアプリパスワードが必要です)。" ;;
                              *"not found"*|*"Name service"*|*"timed out"*|*"connect to"*|*"Network is unreachable"*)
                                  warn "SMTP サーバーに接続できませんでした。サーバー名・ポート・ネットワーク (名前解決・ファイアウォール) を確認してください。" ;;
                              *)  warn "アカウント・パスワード・ポートを確認してください。" ;;
                          esac
                          info "送信待ちのメールは自動で再送されます。消すには: sudo postsuper -d ALL deferred" ;;
        status=bounced*)  warn "送信できませんでした: ${RESULT}" ;;
        "")               warn "結果がログに出ていません。sudo mailq で送信待ちを確認してください。" ;;
        *)                info "${RESULT}" ;;
    esac
    exit 0
fi

# -----------------------------------------------------------------------------
# 外部 SMTP 経由の送信をやめる
# -----------------------------------------------------------------------------
if [ "$DISABLE" = "1" ]; then
    installed || err "Postfix が入っていません。"
    TS="$(date +%Y%m%d-%H%M%S)"
    cp -a "$MAIN_CF" "${MAIN_CF}.bak-${TS}"
    postconf -e "relayhost =" "smtp_sasl_auth_enable = no" "sender_canonical_maps ="
    rm -f "$SASL_PASSWD" "$SASL_PASSWD".* "$SENDER_MAP" "$SENDER_MAP".*
    systemctl reload postfix 2>/dev/null || systemctl restart postfix 2>/dev/null || true
    ok "外部 SMTP 経由の送信をやめ、保存していたパスワードを消しました (元の設定: ${MAIN_CF}.bak-${TS})"
    exit 0
fi

if [ "$APPLY" != "1" ]; then
    show_status
    echo ""
    echo "  設定する・変更するには:   sudo APPLY=1 bash $0"
    echo "  テストメールを送るには:   sudo TEST_TO=you@example.com bash $0"
    exit 0
fi

# -----------------------------------------------------------------------------
log "1/3  Postfix の準備"
# -----------------------------------------------------------------------------
if ! installed || ! dpkg -s libsasl2-modules >/dev/null 2>&1; then
    # 入れるときに質問を出さない (送信専用の「Satellite system」として入れ、
    # 中身はこのあと postconf で決める)
    echo "postfix postfix/main_mailer_type select Satellite system" | debconf-set-selections
    echo "postfix postfix/mailname string $(hostname -f 2>/dev/null || hostname)" | debconf-set-selections
    DEBIAN_FRONTEND=noninteractive apt-get install -y postfix libsasl2-modules ca-certificates >/dev/null \
        || err "postfix を入れられませんでした (sudo apt update を試してください)。"
    ok "postfix と SASL 認証のモジュールを入れました"
else
    ok "Postfix $(pc mail_version) (導入済み)"
fi

# -----------------------------------------------------------------------------
log "2/3  送信に使う SMTP サーバー"
# -----------------------------------------------------------------------------
CUR_RELAY="$(pc relayhost)"; CUR_RELAY="${CUR_RELAY#[}"
CUR_HOST="${CUR_RELAY%%]*}"; CUR_PORT="${CUR_RELAY##*:}"
[ "$CUR_PORT" = "$CUR_RELAY" ] && CUR_PORT=""
ask() {   # 変数名 説明 既定値
    local __v="$1" prompt="$2" def="$3" ans=""
    if [ -n "${!__v:-}" ]; then return 0; fi
    read -r -p "  ${prompt}${def:+ [${def}]}: " ans || true
    printf -v "$__v" '%s' "${ans:-$def}"
}
RELAY_HOST="${RELAY_HOST:-}"; RELAY_PORT="${RELAY_PORT:-}"
SMTP_USER="${SMTP_USER:-${GMAIL_ADDRESS:-}}"
SMTP_PASSWORD="${SMTP_PASSWORD:-${GMAIL_APP_PASSWORD:-}}"
[ -n "${GMAIL_ADDRESS:-}" ] && RELAY_HOST="${RELAY_HOST:-smtp.gmail.com}"
ROOT_FORWARD="${ROOT_FORWARD-__ask__}"

echo "  Gmail の場合: サーバー smtp.gmail.com / ポート 587 / パスワードは"
echo "  2 段階認証を有効にして https://myaccount.google.com/apppasswords で発行した"
echo "  16 桁のアプリパスワードです (普段のログインパスワードは使えません)。"
echo ""
ask RELAY_HOST "SMTP サーバー" "${CUR_HOST:-smtp.gmail.com}"
ask RELAY_PORT "ポート (587 または 465)" "${CUR_PORT:-587}"
ask SMTP_USER  "アカウント (メールアドレス)" "$(cur_user)"
case "$RELAY_PORT" in ''|*[!0-9]*) err "ポートは数字で指定してください。" ;; esac
[ -n "$RELAY_HOST" ] && [ -n "$SMTP_USER" ] || err "SMTP サーバーとアカウントを入力してください。"
case "$RELAY_HOST" in *[!A-Za-z0-9.-]*) err "SMTP サーバーの名前が正しくありません: ${RELAY_HOST}" ;; esac
case "$SMTP_USER" in *[[:space:]]*|*:*) err "アカウントに空白や「:」は使えません。" ;; esac

KEEP_PASSWORD=0
if [ -z "$SMTP_PASSWORD" ]; then
    if [ "$(cur_user)" = "$SMTP_USER" ]; then
        read -r -s -p "  パスワード (入力は表示されません。空のまま Enter で今のまま): " SMTP_PASSWORD || true
        echo ""
        [ -z "$SMTP_PASSWORD" ] && KEEP_PASSWORD=1
    else
        read -r -s -p "  パスワード (入力は表示されません): " SMTP_PASSWORD || true
        echo ""
    fi
fi
if [ "$KEEP_PASSWORD" = "0" ]; then
    [ -n "$SMTP_PASSWORD" ] || err "パスワードを入力してください。"
    # Gmail のアプリパスワードは画面に空白区切りで表示されるため、空白を取り除く
    [ "$RELAY_HOST" = "smtp.gmail.com" ] && SMTP_PASSWORD="${SMTP_PASSWORD// /}"
    case "$SMTP_PASSWORD" in *$'\n'*) err "パスワードに改行は使えません。" ;; esac
fi
if [ "$ROOT_FORWARD" = "__ask__" ]; then
    ROOT_FORWARD=""
    ask ROOT_FORWARD "root 宛てのメール (cron・自動更新の通知など) の転送先 (空なら変更しない)" ""
fi
if [ -n "$ROOT_FORWARD" ]; then
    case "$ROOT_FORWARD" in *[[:space:]]*|*,*) err "転送先は 1 つのメールアドレスで指定してください。" ;; *@*.*) ;; *) err "転送先がメールアドレスではありません: ${ROOT_FORWARD}" ;; esac
fi

# -----------------------------------------------------------------------------
log "3/3  設定の書き込み"
# -----------------------------------------------------------------------------
TS="$(date +%Y%m%d-%H%M%S)"
[ -f "$MAIN_CF" ] && cp -a "$MAIN_CF" "${MAIN_CF}.bak-${TS}" && ok "バックアップ: ${MAIN_CF}.bak-${TS}"
DBT="$(dbtype)"

if [ "$KEEP_PASSWORD" = "0" ]; then
    umask 077
    printf '[%s]:%s %s:%s\n' "$RELAY_HOST" "$RELAY_PORT" "$SMTP_USER" "$SMTP_PASSWORD" > "$SASL_PASSWD"
    umask 022
else
    # アカウントとパスワードはそのままで、サーバー名・ポートだけを合わせる
    sed -i -E "1s/^\[[^]]*\]:[0-9]+ /[${RELAY_HOST}]:${RELAY_PORT} /" "$SASL_PASSWD"
fi
chmod 600 "$SASL_PASSWD"
postmap "${DBT}:${SASL_PASSWD}"
chmod 600 "$SASL_PASSWD".* 2>/dev/null || true
unset SMTP_PASSWORD GMAIL_APP_PASSWORD
ok "${SASL_PASSWD} にアカウントを保存しました (root だけが読めます)"

# 送信元アドレスをログインしたアカウントにそろえる
printf '/.+/ %s\n' "$SMTP_USER" > "$SENDER_MAP"
chmod 644 "$SENDER_MAP"

WRAP="no"; [ "$RELAY_PORT" = "465" ] && WRAP="yes"
postconf -e \
    "relayhost = [${RELAY_HOST}]:${RELAY_PORT}" \
    "smtp_sasl_auth_enable = yes" \
    "smtp_sasl_password_maps = ${DBT}:${SASL_PASSWD}" \
    "smtp_sasl_security_options = noanonymous" \
    "smtp_sasl_tls_security_options = noanonymous" \
    "smtp_tls_security_level = encrypt" \
    "smtp_tls_wrappermode = ${WRAP}" \
    "smtp_tls_CAfile = /etc/ssl/certs/ca-certificates.crt" \
    "inet_interfaces = loopback-only" \
    "sender_canonical_maps = regexp:${SENDER_MAP}"
ok "main.cf を書き換えました (中継先 [${RELAY_HOST}]:${RELAY_PORT}・TLS 必須・LAN には開かない)"

if [ -n "$ROOT_FORWARD" ]; then
    touch /etc/aliases
    if grep -q '^root:' /etc/aliases; then
        sed -i "s|^root:.*|root: ${ROOT_FORWARD}|" /etc/aliases
    else
        echo "root: ${ROOT_FORWARD}" >> /etc/aliases
    fi
    newaliases
    ok "root 宛てのメールを ${ROOT_FORWARD} へ転送します"
fi

postfix check 2>&1 | sed 's/^/    /' || true
systemctl enable postfix >/dev/null 2>&1 || true
systemctl restart postfix || err "Postfix を起動できませんでした (journalctl -u postfix を確認してください)。"
ok "Postfix を再起動しました"

cat <<EOF

============================================================
 設定が終わりました。テストメールを送って確かめてください:

   sudo TEST_TO=you@example.com bash $0

 ※ 受信 FAX・留守番電話の通知メールは、この設定ではなく
   Web 画面の「FAX 送受信 → メール設定」を使います。
============================================================
EOF
