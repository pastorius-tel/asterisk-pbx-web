#!/usr/bin/env bash
# Asterisk PBX Web Management の稼働に必要な OS 側の依存関係を
# まとめてセットアップするスクリプト (FAX / ボイスメール通知メール関連)。
#
# これまでのトラブルシューティングで判明した、以下の作業を1本化しています:
#   1. FAX 送受信に必要なパッケージ (tiff2pdf / ghostscript / curl)
#   2. Ghostscript の AppArmor 制限緩和 (FAX 送信で 403/Permission denied になる件)
#   3. res_fax_spandsp.so に必要な spandsp ライブラリ (開発用 *-dev 含む)
#   4. res_fax_spandsp モジュール自体のビルド有無チェック
#      (無い場合、Asterisk の部分再ビルドが必要 — 自動実行はしません。
#       手順を案内するだけです。理由は update_asterisk.sh と同様、
#       ビルド中〜再起動時に全通話が切断されるため)
#   5. Postfix (Gmail 中継) — ボイスメール通知メールの送信経路
#   6. サーバーのタイムゾーンを Asia/Tokyo に設定
#      (既定で UTC のクラウド/VPS イメージが多く、そのままだと
#       Asterisk のログ・通話履歴・FAX受信日時・留守電録音日時が
#       すべて UTC 表示になってしまうため)
#
# ⚠️ 重要: これは Web UI からのワンクリック実行を想定していません。
#   SSH 接続した状態で、内容を確認しながら手動実行してください。
#   何度実行しても安全 (冪等) です — 既に導入済みの項目は自動的に
#   スキップされます。
#
# 使い方:
#   sudo bash setup_dependencies.sh                    # 対話式 (Gmail情報を都度質問)
#   sudo GMAIL_ADDRESS=you@gmail.com \
#        GMAIL_APP_PASSWORD=xxxxxxxxxxxxxxxx \
#        bash setup_dependencies.sh                    # 非対話 (環境変数で指定)
#   sudo SKIP_POSTFIX=1 bash setup_dependencies.sh      # Postfix 設定をスキップ
#   sudo SKIP_TIMEZONE=1 bash setup_dependencies.sh     # タイムゾーン設定をスキップ
#   sudo FIX_PERMISSIONS=1 bash setup_dependencies.sh   # 権限不足を自動修正 (既定は検出・警告のみ)
#   sudo APP_USER=ubuntu bash setup_dependencies.sh     # 本ツールの実行ユーザーを明示指定
#
# 実行後は必ず「検証結果」セクションの出力を確認してください。

set -euo pipefail

SKIP_POSTFIX="${SKIP_POSTFIX:-0}"

log()  { echo "[$(date '+%H:%M:%S')] $*"; }
ok()   { echo "[$(date '+%H:%M:%S')]  OK  $*"; }
skip() { echo "[$(date '+%H:%M:%S')] skip $*"; }
warn() { echo "[$(date '+%H:%M:%S')] WARN $*" >&2; }

# apt のロックが他プロセス (Ubuntu の自動更新 unattended-upgrades 等) に
# 握られていると
#   "Could not get lock /var/lib/dpkg/lock-frontend"
# で失敗するため、解放されるまで少し待つ。実機でこの競合により
# open-jtalk のインストールが失敗した事例があったため追加。
# shellcheck disable=SC2120  # 引数は任意 (省略時は既定の待ち時間)
wait_for_apt_lock() {
  local max_wait="${1:-120}"
  local waited=0
  # flock で dpkg のフロントエンドロックを試験的に取得してみる。
  # 取得できなければ他のプロセス (Ubuntu の自動更新 unattended-upgrades
  # 等) が apt を使用中。fuser は環境によって未インストールのことが
  # あるため、coreutils/util-linux に含まれる flock を使う。
  while ! flock -n /var/lib/dpkg/lock-frontend true 2>/dev/null; do
    if [ ! -e /var/lib/dpkg/lock-frontend ]; then
      return 0  # ロックファイル自体が無い環境ならそのまま進む
    fi
    if [ "$waited" -eq 0 ]; then
      log "他のプロセスが apt を使用中です。解放を待ちます (最大 ${max_wait} 秒)…"
    fi
    if [ "$waited" -ge "$max_wait" ]; then
      warn "apt のロックが ${max_wait} 秒待っても解放されませんでした"
      warn "しばらく待ってからこのスクリプトを再実行してください"
      return 1
    fi
    sleep 5
    waited=$((waited + 5))
  done
  [ "$waited" -gt 0 ] && log "apt のロックが解放されました (${waited} 秒待機)"
  return 0
}

if [ "$(id -u)" -ne 0 ]; then
  echo "root 権限で実行してください (sudo bash setup_dependencies.sh)" >&2
  exit 1
fi

# =========================================================
# 1. FAX 送受信に必要なパッケージ
# =========================================================
log "=== 1. FAX 送受信パッケージ (tiff2pdf / ghostscript / curl) ==="
# 一部のサードパーティリポジトリ (Node.js 等、本ツールとは無関係のもの) で
# 取得エラーが出ても、必要なパッケージが標準リポジトリにあれば導入は
# 続行できるため、update 自体の失敗でスクリプトを止めない。
wait_for_apt_lock 120 || true
apt-get update -qq || warn "apt-get update で一部リポジトリの取得に失敗した可能性がありますが、続行します"
NEED_PKGS=()
for pkg_check in "libtiff-tools:tiff2pdf" "ghostscript:gs" "curl:curl"; do
  pkg="${pkg_check%%:*}"; bin="${pkg_check##*:}"
  if command -v "$bin" >/dev/null 2>&1; then
    skip "$pkg (コマンド $bin は既に利用可能)"
  else
    NEED_PKGS+=("$pkg")
  fi
done
if [ "${#NEED_PKGS[@]}" -gt 0 ]; then
  log "インストール: ${NEED_PKGS[*]}"
  wait_for_apt_lock 120 || true
  apt-get install -y "${NEED_PKGS[@]}"
  ok "FAX 関連パッケージをインストールしました"
else
  ok "FAX 関連パッケージは全て導入済み"
fi

# =========================================================
# 2. Ghostscript の AppArmor 制限緩和
# =========================================================
log "=== 2. Ghostscript AppArmor 設定 (FAX送信の Permission denied 対策) ==="
GS_APPARMOR_LOCAL="/etc/apparmor.d/local/gs"
GS_APPARMOR_RULE="/var/spool/asterisk/fax/** rw,"
if [ -f "$GS_APPARMOR_LOCAL" ] && grep -qF "$GS_APPARMOR_RULE" "$GS_APPARMOR_LOCAL" 2>/dev/null; then
  skip "AppArmor 設定は既に反映済み ($GS_APPARMOR_LOCAL)"
elif command -v apparmor_parser >/dev/null 2>&1; then
  mkdir -p "$(dirname "$GS_APPARMOR_LOCAL")"
  echo "$GS_APPARMOR_RULE" | tee -a "$GS_APPARMOR_LOCAL" >/dev/null
  apparmor_parser -r /etc/apparmor.d/gs 2>/dev/null || warn "apparmor_parser の再読込に失敗 (AppArmor 自体が無効な環境の可能性。無視して問題ありません)"
  ok "AppArmor 設定を追加しました ($GS_APPARMOR_LOCAL)"
else
  skip "AppArmor が導入されていない環境のためスキップ"
fi

# =========================================================
# 3. spandsp ライブラリ (開発用 *-dev 含む)
# =========================================================
log "=== 3. spandsp ライブラリ (res_fax_spandsp.so のビルドに必要) ==="
if ldconfig -p | grep -q libspandsp; then
  skip "libspandsp は既に導入済み"
else
  log "インストール: libspandsp-dev libspandsp2t64"
  # ディストリビューションにより実行時ライブラリのパッケージ名が
  # 異なることがある (例: libspandsp2t64 が無ければ libspandsp2)。
  # 見つかった方を使う。
  RUNTIME_PKG="libspandsp2t64"
  if ! apt-cache show "$RUNTIME_PKG" >/dev/null 2>&1; then
    RUNTIME_PKG="libspandsp2"
  fi
  wait_for_apt_lock 120 || true
  apt-get install -y libspandsp-dev "$RUNTIME_PKG"
  ok "spandsp ライブラリをインストールしました"
fi

# =========================================================
# 4. res_fax_spandsp.so のビルド状況チェック (自動ビルドはしない)
# =========================================================
log "=== 4. res_fax_spandsp.so の状態確認 ==="
if command -v asterisk >/dev/null 2>&1 && asterisk -rx "module show like res_fax_spandsp" 2>/dev/null | grep -q "Running"; then
  ok "res_fax_spandsp.so は既にロード済み (FAX送受信は既に使える状態です)"
else
  warn "res_fax_spandsp.so が未ロードです。spandsp ライブラリを"
  warn "新たにインストールした場合、Asterisk の部分再ビルドが必要です"
  warn "(自動実行はしません。以下を手動で行ってください):"
  cat >&2 <<'EOS'

    cd /usr/src/asterisk-22.*/          # 実際のソースディレクトリに移動
    sudo ./configure --with-pjproject-bundled
    sudo make menuselect.makeopts
    sudo ./menuselect/menuselect --enable res_fax_spandsp menuselect.makeopts
    sudo make -j$(nproc)
    sudo systemctl stop asterisk-pbx-web
    sudo systemctl stop asterisk
    sudo make install
    sudo systemctl start asterisk
    sudo systemctl start asterisk-pbx-web

EOS
fi

# =========================================================
# 5. Postfix (Gmail 中継) — ボイスメール通知メール送信経路
# =========================================================
log "=== 5. Postfix (Gmail 中継) ==="
if [ "$SKIP_POSTFIX" = "1" ]; then
  skip "SKIP_POSTFIX=1 のためスキップ"
else
  if ! command -v postfix >/dev/null 2>&1; then
    log "インストール: postfix mailutils libsasl2-modules"
    wait_for_apt_lock 120 || true
    DEBIAN_FRONTEND=noninteractive apt-get install -y postfix mailutils libsasl2-modules
    ok "Postfix をインストールしました"
  else
    skip "postfix は既に導入済み"
  fi

  MAIN_CF="/etc/postfix/main.cf"
  if grep -q "^relayhost = \[smtp.gmail.com\]:587" "$MAIN_CF" 2>/dev/null; then
    skip "main.cf の Gmail 中継設定は既に反映済み"
  else
    log "main.cf に Gmail 中継設定を追記します"
    # Postfix パッケージが最初から生成する main.cf には、これから
    # 追記するのと同じキーが (スペース有り "key = value" の場合も
    # スペース無し "key=value" の場合もあり) 既に存在することがある。
    # そのまま追記すると値が重複して postmap 実行時に警告が出る
    # (動作上は後勝ちで実害は無いが、見た目が紛らわしいので該当キーの
    # 既存行は空白の有無を問わず先に取り除いておく)。
    for key in relayhost smtp_sasl_auth_enable smtp_sasl_password_maps \
               smtp_sasl_security_options smtp_tls_security_level smtp_tls_CAfile; do
      sed -i "/^${key}[[:space:]]*=/d" "$MAIN_CF"
    done
    {
      echo ""
      echo "# --- Gmail 中継設定 (setup_dependencies.sh により追加) ---"
      echo "relayhost = [smtp.gmail.com]:587"
      echo "smtp_sasl_auth_enable = yes"
      echo "smtp_sasl_password_maps = hash:/etc/postfix/sasl_passwd"
      echo "smtp_sasl_security_options = noanonymous"
      echo "smtp_tls_security_level = encrypt"
      echo "smtp_tls_CAfile = /etc/ssl/certs/ca-certificates.crt"
    } >> "$MAIN_CF"
    ok "main.cf を更新しました"
  fi

  SASL_PASSWD="/etc/postfix/sasl_passwd"
  if [ -f "$SASL_PASSWD" ] && [ -s "$SASL_PASSWD" ]; then
    skip "sasl_passwd は既に設定済み ($SASL_PASSWD)"
  else
    GMAIL_ADDRESS="${GMAIL_ADDRESS:-}"
    GMAIL_APP_PASSWORD="${GMAIL_APP_PASSWORD:-}"
    if [ -z "$GMAIL_ADDRESS" ]; then
      echo ""
      echo "  Gmail 中継用の認証情報を入力してください"
      echo "  (通常のログインパスワードではなく、2段階認証を有効にした上で"
      echo "   https://myaccount.google.com/apppasswords で発行した"
      echo "   16桁のアプリパスワードを使用してください)"
      read -r -p "  Gmail アドレス: " GMAIL_ADDRESS
    fi
    if [ -z "$GMAIL_APP_PASSWORD" ]; then
      read -r -s -p "  アプリパスワード (16桁・入力は非表示): " GMAIL_APP_PASSWORD
      echo ""
    fi
    if [ -z "$GMAIL_ADDRESS" ] || [ -z "$GMAIL_APP_PASSWORD" ]; then
      warn "Gmail アドレス/アプリパスワードが未入力のため sasl_passwd の作成をスキップしました"
      warn "後で /etc/postfix/sasl_passwd に以下の書式で1行追加し、"
      warn "sudo postmap /etc/postfix/sasl_passwd を実行してください:"
      warn "  [smtp.gmail.com]:587 アドレス@gmail.com:アプリパスワード"
    else
      echo "[smtp.gmail.com]:587 ${GMAIL_ADDRESS}:${GMAIL_APP_PASSWORD}" > "$SASL_PASSWD"
      chmod 600 "$SASL_PASSWD"
      postmap "$SASL_PASSWD"
      chmod 600 "${SASL_PASSWD}.db"
      ok "sasl_passwd を作成しました"
    fi
  fi

  systemctl restart postfix 2>&1 || warn "systemctl restart postfix に失敗しました (systemd が無い環境等では手動で 'service postfix restart' を試してください)"
  systemctl enable postfix >/dev/null 2>&1 || true
  ok "Postfix の再起動を試みました"
fi

# =========================================================
# 6. サーバーのタイムゾーンを Asia/Tokyo に設定
# =========================================================
log "=== 6. タイムゾーン (Asia/Tokyo) ==="
if [ "${SKIP_TIMEZONE:-0}" = "1" ]; then
  skip "SKIP_TIMEZONE=1 のためスキップ"
elif ! command -v timedatectl >/dev/null 2>&1; then
  skip "timedatectl が無い環境 (systemd 以外) のためスキップ。手動で /etc/localtime を設定してください"
else
  current_tz="$(timedatectl show --property=Timezone --value 2>/dev/null || echo '')"
  if [ "$current_tz" = "Asia/Tokyo" ]; then
    skip "既に Asia/Tokyo に設定済み"
  else
    if timedatectl set-timezone Asia/Tokyo 2>&1; then
      ok "タイムゾーンを Asia/Tokyo に変更しました (旧設定: ${current_tz:-不明})"
      warn "既に起動中の Asterisk / 本ツールには反映されません。"
      warn "反映するには次を実行してください (この時だけ通話が切断されます):"
      warn "  sudo systemctl restart asterisk && sudo systemctl restart asterisk-pbx-web"
    else
      warn "timedatectl set-timezone に失敗しました (systemd が無い環境等)。"
      warn "手動で /etc/localtime を設定してください:"
      warn "  sudo ln -sf /usr/share/zoneinfo/Asia/Tokyo /etc/localtime"
    fi
  fi
fi

# =========================================================
# 7. 音源 (MP3) / 保留音クラスに必要な ffmpeg
# =========================================================
log "=== 7. 音源変換パッケージ (ffmpeg) ==="
# アップロードされた MP3/M4A/OGG 等は、Asterisk 自体に再生させるのでは
# なく、ffmpeg で Asterisk 標準対応の WAV (8kHz mono 16bit PCM) に事前
# 変換してから保存する設計になっている。そのため Asterisk 本体の
# ビルド・menuselect の選択とは完全に無関係で、Asterisk を再ビルド
# しても音源機能には一切影響しない (ffmpeg は OS レベルの独立した
# コマンドのため)。ただし ffmpeg 自体がサーバーに入っていないと
# そもそも変換できないため、ここで導入を確認する。
if command -v ffmpeg >/dev/null 2>&1; then
  skip "ffmpeg (コマンド ffmpeg は既に利用可能)"
else
  log "インストール: ffmpeg"
  wait_for_apt_lock 120 || true
  apt-get install -y ffmpeg
  ok "ffmpeg をインストールしました"
fi

# =========================================================
# 7-2. 日本語音声合成 (Open JTalk)
# =========================================================
log "=== 7-2. 日本語音声合成パッケージ (Open JTalk) ==="
# 「音源」画面の「文章から音声を作る」機能で使う。日本語テキストから
# 留守番電話・IVR の案内音声を生成できるほか、Asterisk 日本語モードの
# 日時読み上げに必要な音声ファイル (digits/ji, digits/fun 等。一般的な
# 日本語音声パックには含まれていない) の一括生成にも使う。
if [ "${SKIP_TTS:-0}" = "1" ]; then
  skip "SKIP_TTS=1 のためスキップ"
elif command -v open_jtalk >/dev/null 2>&1; then
  skip "open_jtalk は既に利用可能"
else
  log "インストール: open-jtalk 一式"
  wait_for_apt_lock 120 || true
  apt-get install -y open-jtalk open-jtalk-mecab-naist-jdic \
    hts-voice-nitech-jp-atr503-m001 \
    || warn "open-jtalk のインストールに失敗しました (音声合成機能のみ利用できません)"
  if command -v open_jtalk >/dev/null 2>&1; then
    ok "open-jtalk をインストールしました"
  fi
fi

# =========================================================
# 8. 権限設定 (conf ファイル書き込み・音源/FAX/留守番電話の読み書き)
# =========================================================
log "=== 8. 権限設定 (Asterisk 関連ディレクトリへの読み書き) ==="
# 本ツール (asterisk-pbx-web) は以下へ読み書きできる必要がある:
#   /etc/asterisk                        設定ファイル (.conf) の生成
#   /var/lib/asterisk/sounds/ja/managed  音源 (MP3) アップロード先
#   /var/spool/asterisk/fax              FAX 受信スプール (Asterisk 側)
#   /var/lib/asterisk-pbx-web/fax        FAX 保存先 (本ツール管理)
#   /var/spool/asterisk/voicemail        留守番電話メッセージ (一覧・再生・削除)
#
# ここは chown/usermod というシステムに影響の大きい操作を伴うため、
# 既定では「検出して警告するだけ」に留める。実際に変更を適用したい
# 場合は FIX_PERMISSIONS=1 を指定すること:
#   sudo FIX_PERMISSIONS=1 bash setup_dependencies.sh

# 本ツールの実行ユーザーを特定 (systemd の User= から取得。
# 取れなければ sudo 実行者、それも無ければ不明として警告のみ)
APP_USER="${APP_USER:-}"
if [ -z "$APP_USER" ]; then
  APP_USER="$(systemctl show -p User --value asterisk-pbx-web 2>/dev/null || true)"
fi
if [ -z "$APP_USER" ] || [ "$APP_USER" = "root" ]; then
  APP_USER="${SUDO_USER:-}"
fi

if [ -z "$APP_USER" ]; then
  warn "本ツールの実行ユーザーを自動判別できませんでした。"
  warn "環境変数 APP_USER=<ユーザー名> を指定して再実行するか、"
  warn "README の「本ツールが Asterisk の conf / 音源ディレクトリに"
  warn "書き込める権限を付与」の手順を手動で実施してください。"
else
  log "本ツールの実行ユーザーとして '${APP_USER}' を使用します"
  log "(誤っている場合は APP_USER=<正しいユーザー名> を指定して再実行してください)"

  NEED_FIX=0

  # asterisk グループへの所属確認
  if getent group asterisk >/dev/null 2>&1; then
    if id -nG "$APP_USER" 2>/dev/null | grep -qw asterisk; then
      skip "${APP_USER} は既に asterisk グループに所属"
    else
      NEED_FIX=1
      if [ "${FIX_PERMISSIONS:-0}" = "1" ]; then
        usermod -aG asterisk "$APP_USER"
        ok "${APP_USER} を asterisk グループに追加しました (反映にはサービス再起動が必要です)"
      else
        warn "${APP_USER} が asterisk グループに未所属です。実行してください:"
        warn "  sudo usermod -aG asterisk ${APP_USER}"
      fi
    fi
  else
    warn "asterisk グループが見つかりません (Asterisk のインストール方法によっては存在しないことがあります)"
  fi

  # /etc/asterisk のグループ書き込み権限
  # (このスクリプト自体は root で動くため単純な [-w] では判定できない。
  #  実際に APP_USER としてテストする)
  if [ -d /etc/asterisk ]; then
    if sudo -u "$APP_USER" test -w /etc/asterisk 2>/dev/null; then
      skip "/etc/asterisk は ${APP_USER} から書き込み可能です"
    else
      NEED_FIX=1
      if [ "${FIX_PERMISSIONS:-0}" = "1" ]; then
        # chmod g+w だけでは、ディレクトリの所有グループが asterisk
        # 以外 (よくあるのは root) のままだと意味が無い (APP_USER を
        # asterisk グループに入れても、所有グループが違えば "その他"
        # 扱いになり書き込めない)。所有グループも asterisk に揃える。
        chgrp -R asterisk /etc/asterisk 2>/dev/null || true
        chmod -R g+w /etc/asterisk
        ok "/etc/asterisk の所有グループを asterisk にし、書き込み権限を付与しました"
      else
        warn "/etc/asterisk が ${APP_USER} から書き込みできません。実行してください:"
        warn "  sudo chgrp -R asterisk /etc/asterisk"
        warn "  sudo chmod -R g+w /etc/asterisk"
      fi
    fi
  fi

  # 音源 (MP3) アップロード先
  SOUNDS_DIR="/var/lib/asterisk/sounds/ja/managed"
  if [ ! -d "$SOUNDS_DIR" ]; then
    NEED_FIX=1
    if [ "${FIX_PERMISSIONS:-0}" = "1" ]; then
      mkdir -p "${SOUNDS_DIR}"/{moh,park,ivr,voicemail,custom}
      chown -R asterisk:asterisk "$SOUNDS_DIR" 2>/dev/null || true
      chmod -R g+w "$SOUNDS_DIR"
      ok "${SOUNDS_DIR} を作成し権限を設定しました"
    else
      warn "${SOUNDS_DIR} がまだありません (音源アップロード時に自動作成されますが、"
      warn "先に作っておきたい場合は次を実行してください):"
      warn "  sudo mkdir -p ${SOUNDS_DIR}/{moh,park,ivr,voicemail,custom}"
      warn "  sudo chown -R asterisk:asterisk ${SOUNDS_DIR}"
      warn "  sudo chmod -R g+w ${SOUNDS_DIR}"
    fi
  else
    skip "${SOUNDS_DIR} は存在します"
  fi

  # FAX 受信スプール (Asterisk 側)
  FAX_SPOOL="/var/spool/asterisk/fax"
  if [ -d "$FAX_SPOOL" ]; then
    if [ "${FIX_PERMISSIONS:-0}" = "1" ]; then
      chown -R asterisk:asterisk "$FAX_SPOOL" 2>/dev/null || true
      chmod -R g+rw "$FAX_SPOOL"
      ok "${FAX_SPOOL} の権限を設定しました"
    else
      skip "${FAX_SPOOL} は存在します (権限確認は FIX_PERMISSIONS=1 で実施)"
    fi
  else
    log "${FAX_SPOOL} はまだありません (FAX 機能を有効化して初回反映すると作成されます)"
  fi

  # FAX 保存先 (Asterisk とは無関係、本ツールが変換後の PDF を保存する
  # 場所。asterisk グループは不要で、単純に APP_USER の所有物にする)
  FAX_STORE="/var/lib/asterisk-pbx-web/fax"
  if [ -d "$FAX_STORE" ]; then
    if [ "${FIX_PERMISSIONS:-0}" = "1" ]; then
      chown -R "${APP_USER}:${APP_USER}" "$FAX_STORE" 2>/dev/null || true
      ok "${FAX_STORE} の所有者を ${APP_USER} にしました"
    else
      skip "${FAX_STORE} は存在します (権限確認は FIX_PERMISSIONS=1 で実施)"
    fi
  else
    log "${FAX_STORE} はまだありません (FAX 機能を有効化して初回受信すると作成されます)"
  fi

  # 留守番電話スプール (一覧・再生・削除に読み書き権限が必要)
  VM_SPOOL="/var/spool/asterisk/voicemail"
  if [ -d "$VM_SPOOL" ]; then
    if [ "${FIX_PERMISSIONS:-0}" = "1" ]; then
      chgrp -R asterisk "$VM_SPOOL" 2>/dev/null || true
      chmod -R g+rw "$VM_SPOOL" 2>/dev/null || true
      ok "${VM_SPOOL} の所有グループを asterisk にし、読み書き権限を付与しました"
    else
      skip "${VM_SPOOL} は存在します (権限確認は FIX_PERMISSIONS=1 で実施)"
    fi
  else
    log "${VM_SPOOL} はまだありません (留守番電話が有効な内線に、最初のメッセージが届くと作成されます)"
  fi

  if [ "$NEED_FIX" = "1" ] && [ "${FIX_PERMISSIONS:-0}" != "1" ]; then
    warn ""
    warn "上記の未設定項目をまとめて自動修正したい場合は、次のように再実行してください:"
    warn "  sudo FIX_PERMISSIONS=1 bash setup_dependencies.sh"
  fi
fi

# =========================================================
# 検証結果
# =========================================================
echo ""
echo "========================================================="
echo " 検証結果"
echo "========================================================="

echo -n "  tiff2pdf (libtiff-tools) : "
command -v tiff2pdf >/dev/null 2>&1 && echo "OK" || echo "未導入"

echo -n "  gs (ghostscript)         : "
command -v gs >/dev/null 2>&1 && echo "OK" || echo "未導入"

echo -n "  curl                     : "
command -v curl >/dev/null 2>&1 && echo "OK" || echo "未導入"

echo -n "  ffmpeg (音源/保留音変換) : "
command -v ffmpeg >/dev/null 2>&1 && echo "OK" || echo "未導入"

echo -n "  open_jtalk (音声合成)    : "
command -v open_jtalk >/dev/null 2>&1 && echo "OK" || echo "未導入"

echo -n "  libspandsp               : "
ldconfig -p | grep -q libspandsp && echo "OK" || echo "未導入"

echo -n "  res_fax_spandsp.so       : "
if command -v asterisk >/dev/null 2>&1 && asterisk -rx "module show like res_fax_spandsp" 2>/dev/null | grep -q "Running"; then
  echo "OK (ロード済み)"
else
  echo "未ロード (上記 4. の手順で部分再ビルドが必要)"
fi

if [ "$SKIP_POSTFIX" != "1" ]; then
  echo -n "  postfix                  : "
  systemctl is-active --quiet postfix 2>/dev/null && echo "OK (稼働中)" || echo "停止中 (systemd の無い環境ではこの判定自体ができません)"
  echo -n "  sasl_passwd 設定         : "
  [ -s "/etc/postfix/sasl_passwd" ] && echo "OK" || echo "未設定"
fi

if [ "${SKIP_TIMEZONE:-0}" != "1" ] && command -v timedatectl >/dev/null 2>&1; then
  echo -n "  タイムゾーン             : "
  timedatectl show --property=Timezone --value 2>/dev/null || echo "取得できず"
fi

echo ""
echo "  Postfix の送信テスト (未実施の場合は下記を実行してください):"
echo "    echo \"テスト本文\" | mail -s \"Postfixテスト\" 宛先アドレス@example.com"
echo "    → 届かない場合は: sudo tail -50 /var/log/mail.log"
echo ""
# =========================================================
# このスクリプトでは導入しないもの (残りの手順を案内)
# =========================================================
echo ""
echo "========================================================="
echo " 次に行う作業"
echo "========================================================="
echo ""
echo "このスクリプトは OS 側の依存パッケージと権限だけを整えます。"
echo "以下は別途必要です (未完了のものがあれば案内します)。"
echo ""

_remaining=0

# 1. Asterisk 本体
if command -v asterisk >/dev/null 2>&1; then
  echo "  [済] Asterisk 本体: $(asterisk -V 2>/dev/null || echo 'インストール済み')"
else
  _remaining=$((_remaining + 1))
  echo "  [未] Asterisk 本体がインストールされていません"
  echo "       このツールは Asterisk を管理するものなので、先に Asterisk を"
  echo "       導入してください (ソースからのビルドを推奨。既に導入済みなら"
  echo "       バージョンアップは scripts/update_asterisk.sh が使えます)。"
fi

# 2. 日本語音声パック
if [ -d /var/lib/asterisk/sounds/ja ]; then
  echo "  [済] 日本語音声パック"
else
  _remaining=$((_remaining + 1))
  echo "  [未] 日本語音声パック (音声ガイダンスが英語になります)"
  echo "       sudo bash scripts/install_japanese_sounds.sh"
fi

# 3. 本ツールの Python 依存
if [ -d "$(dirname "$0")/../.venv" ]; then
  echo "  [済] Python 仮想環境 (.venv)"
else
  _remaining=$((_remaining + 1))
  echo "  [未] 本ツールの Python 依存パッケージ"
  echo "       cd $(cd "$(dirname "$0")/.." && pwd)"
  echo "       python3 -m venv .venv && . .venv/bin/activate && pip install -e ."
fi

# 4. 追加の声 (任意)
if [ -d /usr/share/hts-voice/tohoku-f01 ] || [ -d /usr/share/hts-voice/mei ]; then
  echo "  [済] 音声合成の追加音響モデル (女性の声)"
else
  echo "  [任意] 音声合成の声は標準 (男性) のみです。女性の声を追加するには:"
  echo "       sudo bash scripts/install_tts_voices.sh"
  echo "       (Web 画面「音源」→「文章から音声を作る」からも追加できます)"
fi

echo ""
if [ "$_remaining" -eq 0 ]; then
  echo " 必須の項目はすべて揃っています。"
  echo " Web 画面で設定したあと「変更を Asterisk へ反映」を実行してください。"
else
  echo " 上記 [未] の項目を実施してください。"
fi
echo "========================================================="
echo ""

log "セットアップスクリプト完了"
