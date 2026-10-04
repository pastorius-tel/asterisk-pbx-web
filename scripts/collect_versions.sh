#!/usr/bin/env bash
# =============================================================================
# 動作環境のバージョン一覧を出力する。
#
# OS / CPU / Python / Asterisk / apt パッケージ / Python ライブラリの版数を
# まとめて表示する。不具合の報告や、通常版と Raspberry Pi 版の環境を
# 比べるときに使う。Web 画面の「システム」→「バージョン情報」と同じ内容を、
# 画面が動いていないときでも出せるようにしたもの。
#
# 使い方:
#   bash collect_versions.sh                    # 画面に表示
#   bash collect_versions.sh > versions.txt     # ファイルに保存
#   INSTALL_DIR=/opt/asterisk-pbx-web bash collect_versions.sh
#
# root 権限は不要 (Asterisk のモジュール一覧だけは sudo で実行すると出る)。
# =============================================================================

if [ -z "${BASH_VERSION:-}" ]; then
    command -v bash >/dev/null 2>&1 && exec bash "$0" "$@"
    echo "エラー: bash が必要です。" >&2; exit 1
fi

INSTALL_DIR="${INSTALL_DIR:-/var/www/asterisk-pbx-web}"
VENV_PY="$INSTALL_DIR/.venv/bin/python"

line() { printf '%s\n' "------------------------------------------------------------"; }
row()  { printf '  %-34s %s\n' "$1" "$2"; }

echo "Asterisk PBX Web Management — バージョン一覧"
echo "取得日時: $(date '+%Y-%m-%d %H:%M:%S %Z')"
line

# --- 本ツール ---------------------------------------------------------------
echo "[本ツール]"
APP_VER="$(sed -n 's/^version = "\(.*\)"/\1/p' "$INSTALL_DIR/pyproject.toml" 2>/dev/null | head -1)"
EDITION="$(tr -d '[:space:]' < "$INSTALL_DIR/EDITION" 2>/dev/null)"
row "インストール先" "$INSTALL_DIR"
row "版" "${APP_VER:-(不明)}"
row "配布版 (EDITION)" "${EDITION:-(ファイル無し: CPU から自動判定)}"
if systemctl list-unit-files asterisk-pbx-web.service >/dev/null 2>&1; then
    row "サービス状態" "$(systemctl is-active asterisk-pbx-web 2>/dev/null)"
    row "ワーカー" "$(grep -o 'uvicorn[._a-z]*\.UvicornWorker' /etc/systemd/system/asterisk-pbx-web.service 2>/dev/null | head -1)"
fi
line

# --- OS / ハードウェア --------------------------------------------------------
echo "[OS / ハードウェア]"
row "OS" "$(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME")"
row "CPU アーキテクチャ" "$(uname -m)"
row "dpkg アーキテクチャ" "$(dpkg --print-architecture 2>/dev/null)"
row "カーネル" "$(uname -r)"
if [ -r /proc/device-tree/model ]; then
    row "機種" "$(tr -d '\0' < /proc/device-tree/model)"
fi
row "メモリ" "$(free -h 2>/dev/null | awk '/^Mem:/{print $2 " (空き " $7 ")"}')"
row "glibc" "$(ldd --version 2>/dev/null | head -1 | awk '{print $NF}')"
line

# --- Python ---------------------------------------------------------------------
echo "[Python]"
for p in python3 python3.10 python3.11 python3.12 python3.13 python3.14; do
    command -v "$p" >/dev/null 2>&1 || continue
    v="$("$p" -c 'import platform; print(platform.python_version())' 2>/dev/null)"
    note=""
    case "$v" in *a*|*b*|*rc*) note="  ※正式版ではありません (リリース候補版など)";; esac
    row "$p ($(command -v "$p"))" "$v$note"
done
if [ -x "$VENV_PY" ]; then
    row "本ツールの仮想環境 (.venv)" "$("$VENV_PY" -c 'import platform,sys; print(platform.python_version(), sys.executable)')"
    row "pip" "$("$VENV_PY" -m pip --version 2>/dev/null | awk '{print $2}')"
else
    row "本ツールの仮想環境 (.venv)" "(見つかりません: $VENV_PY)"
fi
line

# --- Asterisk --------------------------------------------------------------------
echo "[Asterisk]"
if command -v asterisk >/dev/null 2>&1; then
    row "asterisk -V" "$(asterisk -V 2>/dev/null)"
    mods="$(asterisk -rx 'module show like fax' 2>/dev/null | grep -E '^res_fax' | awk '{print $1 " (" $(NF-2) ")"}' | tr '\n' ' ')"
    row "FAX モジュール" "${mods:-(取得できず: sudo で実行すると表示されます)}"
else
    row "asterisk" "(見つかりません)"
fi
line

# --- apt パッケージ -----------------------------------------------------------
echo "[apt パッケージ]"
PKGS=(
    python3 python3-venv python3-pip python3-dev python3.11 python3.11-venv python3.11-dev
    build-essential libffi-dev pkg-config
    libtiff-tools ghostscript libspandsp2t64 libspandsp2 libspandsp-dev
    ffmpeg open-jtalk open-jtalk-mecab-naist-jdic hts-voice-nitech-jp-atr503-m001
    curl sqlite3 libsqlite3-dev libssl-dev libedit-dev libjansson-dev libxml2-dev uuid-dev
    postfix mailutils libsasl2-modules samba ufw language-pack-ja tzdata
)
for p in "${PKGS[@]}"; do
    st="$(dpkg-query -W -f='${db:Status-Status}\t${Version}' "$p" 2>/dev/null)"
    case "$st" in
        installed*) row "$p" "${st#*$'\t'}" ;;
        *)          row "$p" "-" ;;
    esac
done
line

# --- Python ライブラリ ---------------------------------------------------------
echo "[Python ライブラリ (本ツールの仮想環境)]"
if [ -x "$VENV_PY" ]; then
    LOCK=""
    case "$EDITION" in
        rpi-armv7) LOCK="$INSTALL_DIR/requirements/rpi-armv7.lock" ;;
        normal)    LOCK="$INSTALL_DIR/requirements/normal.lock" ;;
    esac
    "$VENV_PY" - "$LOCK" <<'PY'
import importlib.metadata as md, re, sys, platform
lock_path = sys.argv[1] if len(sys.argv) > 1 else ""
locked = {}
if lock_path:
    try:
        for raw in open(lock_path, encoding="utf-8"):
            spec = raw.split("#", 1)[0].split(";", 1)
            if "==" not in spec[0]:
                continue
            marker = spec[1].strip() if len(spec) > 1 else ""
            if "python_full_version < '3.11'" in marker and sys.version_info >= (3, 11):
                continue
            if "python_full_version >= '3.11'" in marker and sys.version_info < (3, 11):
                continue
            if "win32" in marker and "!=" not in marker:
                continue
            n, v = spec[0].strip().split("==", 1)
            locked[re.sub(r"[-_.]+", "-", n).lower()] = v.strip()
    except OSError:
        pass
skip = {"pip", "setuptools", "wheel", "asterisk-pbx-web"}
rows = []
for d in md.distributions():
    name = d.metadata.get("Name")
    if not name:
        continue
    key = re.sub(r"[-_.]+", "-", name).lower()
    if key in skip:
        continue
    note = ""
    if key in locked and locked[key] != d.version:
        note = f"  ← 動作確認版は {locked[key]}"
    rows.append((key, d.version, note))
for key, ver, note in sorted(rows):
    print(f"  {key:34s} {ver}{note}")
diff = sum(1 for r in rows if r[2])
print()
if locked:
    print(f"  動作確認版 ({lock_path.rsplit('/', 1)[-1]}) と異なるもの: {diff} 件")
PY
else
    echo "  (仮想環境が見つかりません)"
fi
line
