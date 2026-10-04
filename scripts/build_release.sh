#!/usr/bin/env bash
# =============================================================================
# 配布用 zip を 2 種類作る (開発者向け)。
#
#   dist/asterisk-pbx-web-<版>.zip             … 通常版
#   dist/asterisk-pbx-web-<版>-rpi-armv7.zip   … Raspberry Pi 版 (32bit ARM)
#
# 中身のアプリは同じで、違うのは次の 2 つだけ:
#   EDITION      … インストーラ・更新スクリプト・画面が配布版を判別する
#   VERSIONS.md  … その配布版の apt / Python ライブラリの版数一覧
#                  (docs/versions/<配布版>.md をコピーする)
#
# 使い方:
#   bash scripts/build_release.sh
# =============================================================================

if [ -z "${BASH_VERSION:-}" ]; then
    command -v bash >/dev/null 2>&1 && exec bash "$0" "$@"
    echo "エラー: bash が必要です。" >&2; exit 1
fi
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VER="$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/pyproject.toml" | head -1)"
[ -n "$VER" ] || { echo "pyproject.toml から版が読めません" >&2; exit 1; }
DIST="$ROOT/dist"
mkdir -p "$DIST"

for f in requirements/normal.lock requirements/rpi-armv7.lock \
         docs/versions/normal.md docs/versions/rpi-armv7.md; do
    [ -f "$ROOT/$f" ] || { echo "見つかりません: $f" >&2; exit 1; }
done

build() {
    local edition="$1" suffix="$2"
    local work out
    work="$(mktemp -d)"
    out="$DIST/asterisk-pbx-web-${VER}${suffix}.zip"

    # 開発用・実行時に生成されるものは入れない
    tar -C "$ROOT" \
        --exclude='.git' --exclude='.venv' --exclude='dist' \
        --exclude='__pycache__' --exclude='*.pyc' --exclude='.ruff_cache' \
        --exclude='*.egg-info' --exclude='.env' --exclude='*.db' --exclude='*.db-*' \
        --exclude='uploads' --exclude='backups' --exclude='.session_secret' \
        --exclude='.initdb.lock' --exclude='EDITION' --exclude='VERSIONS.md' \
        -cf - . | { mkdir -p "$work/asterisk-pbx-web"; tar -C "$work/asterisk-pbx-web" -xf -; }

    echo "$edition" > "$work/asterisk-pbx-web/EDITION"
    cp "$ROOT/docs/versions/${edition}.md" "$work/asterisk-pbx-web/VERSIONS.md"

    rm -f "$out"
    (cd "$work" && zip -qr "$out" asterisk-pbx-web)
    rm -rf "$work"
    echo "  $(basename "$out")  ($(du -h "$out" | cut -f1))"
}

echo "版 $VER の配布物を作ります:"
build normal ""
build rpi-armv7 "-rpi-armv7"
echo "出力先: $DIST"
