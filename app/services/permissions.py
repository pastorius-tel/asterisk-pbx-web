"""ファイル権限の診断と、権限エラーの分かりやすい説明。

本ツールは Asterisk の設定ファイル・音源・スプールを直接読み書きする
ため、サービス実行ユーザー (既定 asterisk) にそれらへの権限が無いと
機能が静かに、あるいは 500 エラーで失敗する。原因がパスごとに違うので
「どのディレクトリに書けなかったか」と「どう直すか」をその場で示せる
ようにしてある。

提供するもの:
  describe_permission_error() … 例外を日本語の原因 + 対処コマンドに変換
  check_all()                 … 必要な全パスの読み書き可否を一覧で返す
                                (システム画面の自己診断に使用)
"""

from __future__ import annotations

import getpass
import grp
import os
import pwd
from dataclasses import dataclass
from pathlib import Path

from app.config import settings

# ---------------------------------------------------------------------------
# 実行ユーザーの情報
# ---------------------------------------------------------------------------


def current_user() -> str:
    """プロセスの実行ユーザー名。取得できなければ uid を文字列で返す。"""
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except (KeyError, OSError):
        try:
            return getpass.getuser()
        except Exception:  # noqa: BLE001
            return str(os.geteuid())


def current_group() -> str:
    """プロセスの実行グループ名。取得できなければ gid を文字列で返す。"""
    try:
        return grp.getgrgid(os.getegid()).gr_name
    except (KeyError, OSError):
        return str(os.getegid())


def _owner_of(path: Path) -> str:
    try:
        return pwd.getpwuid(path.stat().st_uid).pw_name
    except (KeyError, OSError):
        return "不明"


def _safe_exists(path: Path) -> bool:
    """存在するかを調べる。調べられない場合は False。

    Path.exists() は親ディレクトリに実行 (検索) 権限が無いと
    PermissionError を投げる。権限の説明を作る処理自体がそれで落ちると
    元のエラーが「権限エラーハンドラ内の例外」に化けてしまうため
    (実際にそうなった)、ここで必ず握る。
    """
    try:
        return path.exists()
    except OSError:
        return False


def _nearest_existing(path: Path) -> Path:
    """path 自身か、存在を確認できる一番近い親ディレクトリを返す。"""
    p = path
    while not _safe_exists(p) and p != p.parent:
        p = p.parent
    return p


# 持ち主を丸ごと変えると OS が壊れるフォルダ。保存先がまだ無いとき、
# 「存在する一番近い親」がこれらになることがある (例: /var/lib/asterisk-pbx-web
# が無いと /var/lib)。以前はそれを対処コマンドに出していたため、
# 「sudo chown -R asterisk:asterisk /var/lib」のような危険な案内になっていた。
_SYSTEM_DIRS = frozenset({
    "/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/media", "/mnt",
    "/opt", "/proc", "/root", "/run", "/sbin", "/srv", "/sys", "/tmp", "/usr",
    "/usr/lib", "/usr/local", "/usr/share", "/var", "/var/cache", "/var/lib",
    "/var/log", "/var/run", "/var/spool", "/var/tmp",
})

# 本ツールと Asterisk だけが使う場所 (中身ごと持ち主を変えてよい)
_APP_AREAS = (
    "/etc/asterisk", "/var/lib/asterisk", "/var/spool/asterisk",
    "/var/log/asterisk", "/var/lib/asterisk-pbx-web",
)


def _in_app_area(path: Path) -> bool:
    s = str(path)
    roots = [*_APP_AREAS]
    try:
        roots.append(str(Path.cwd()))  # インストール先 (WorkingDirectory)
    except OSError:
        pass
    return any(s == r or s.startswith(r.rstrip("/") + "/") for r in roots)


def fix_all_command() -> str:
    """本ツールが使う場所の権限をまとめて直すコマンド。"""
    try:
        base = Path.cwd()  # インストール先 (サービスの WorkingDirectory)
    except OSError:
        base = Path("<インストール先>")
    return f"sudo APPLY=1 bash {base}/scripts/check_permissions.sh"


def fix_command_for(path: Path, *, is_dir: bool = True) -> str:
    """path に書き込めるようにするためのコマンド。出せない場合は空文字。

    - path か、本ツール・Asterisk 専用の場所にある親が存在する
      → その場所の持ち主を変える (chown -R)
    - それ以外の親 (/var/lib など) しか無い
      → 足りないフォルダのうち一番上のものだけを作る (install -d)。
        中のフォルダは本ツールが自分で作れる。既存のものは何も変えない
    """
    user, group = current_user(), current_group()
    target = _nearest_existing(path)
    if target == path or _in_app_area(target):
        if str(target) in _SYSTEM_DIRS:
            return ""
        return f"sudo chown -R {user}:{group} {target}"
    first = path
    while first.parent != target and first.parent != first:
        first = first.parent
    if first == path and not is_dir:
        # 書けないのはファイルを置くフォルダそのもの (本ツール専用ではない場所)
        return ""
    return f"sudo install -d -o {user} -g {group} {first}"


# ---------------------------------------------------------------------------
# 権限エラーの説明
# ---------------------------------------------------------------------------


def describe_permission_error(path: Path | str, exc: OSError) -> str:
    """OSError を「原因 + 対処コマンド」の日本語メッセージにする。

    権限以外の理由 (容量不足など) の場合は、そのまま読める形で返す。
    """
    p = Path(path)
    user = current_user()

    if isinstance(exc, PermissionError):
        target = _nearest_existing(p)
        owner = _owner_of(target)
        # 拡張子があればファイルとみなす (保存しようとした PDF・zip など)
        cmd = fix_command_for(p, is_dir=not p.suffix)
        if cmd:
            how = (
                f"次のどちらかで解決できます:\n"
                f"  1. {cmd}\n"
                f"  2. {fix_all_command()}\n"
            )
        else:
            how = (
                f"{target} はシステムのフォルダなので、持ち主は変えないでください。\n"
                f"  {fix_all_command()} を実行するか、\n"
                f"  .env で {user} が書き込める場所を指定してください。\n"
            )
        return (
            f"{p} に書き込めません (実行ユーザー: {user} / {target} の所有者: {owner})。\n"
            f"{how}"
            f"(元のエラー: {exc})"
        )
    if isinstance(exc, FileNotFoundError):
        return f"{p} が見つかりません。パス設定 (.env) を確認してください。(元のエラー: {exc})"
    if isinstance(exc, OSError) and exc.errno == 28:  # ENOSPC
        return f"{p} の保存先に空き容量がありません。(元のエラー: {exc})"
    return f"{p} への書き込みに失敗しました: {exc}"


# ---------------------------------------------------------------------------
# 自己診断
# ---------------------------------------------------------------------------


@dataclass
class PathCheck:
    """1 つのパスについての診断結果。"""

    label: str
    """画面に出す用途名 (例: 「Asterisk 設定ファイル」)。"""

    path: str
    exists: bool
    writable: bool
    readable: bool
    owner: str
    needs_write: bool
    """書き込みが必要なパスか (読み取りだけで良いものは False)。"""

    feature: str
    """この権限が無いと使えなくなる機能の説明。"""

    blocked: str = ""
    """フォルダ自体には書けても、中に書き込めないファイル・フォルダがある
    場合の最初の 1 件 (root の持ち物のまま残った設定ファイルなど)。"""

    @property
    def ok(self) -> bool:
        if self.needs_write:
            return self.writable and not self.blocked
        return self.readable

    @property
    def fix_command(self) -> str:
        """権限を直すためのコマンド (問題が無ければ空文字)。"""
        if self.ok:
            return ""
        return fix_command_for(Path(self.path))


# 中身まで調べるときの上限 (留守番電話などでファイルが多くても画面が遅くならないように)
_SCAN_LIMIT = 5000


def _first_unwritable(path: Path, limit: int = _SCAN_LIMIT) -> str:
    """path の中で書き込めない最初のファイル・フォルダ。無ければ空文字。

    フォルダに書けても、中のファイルが root の持ち物のままだと上書きや
    削除ができない (例: root で動いていた頃に書き出した設定ファイル)。
    数が多い場合は上限までしか見ない。
    """
    count = 0
    try:
        for root, dirs, files in os.walk(path, onerror=lambda _e: None):
            for name in (*dirs, *files):
                count += 1
                if count > limit:
                    return ""
                p = os.path.join(root, name)
                if os.path.islink(p):
                    continue
                if not os.access(p, os.W_OK):
                    return p
    except OSError:
        return ""
    return ""


def _check(
    label: str, path: Path, feature: str, *,
    needs_write: bool = True, deep: bool = False,
) -> PathCheck:
    """1 パス分の可否を調べる。

    「書けるか」は os.access だけでは足りない (ディレクトリが無い場合は
    親に作れるかを見る必要がある) ため、存在する一番近い親で判定する。
    deep=True なら、中のファイル・フォルダまで書き込めるかを調べる。
    """
    # uploads_dir / backup_dir は既定が相対パス (./uploads) なので、
    # 画面表示と修正コマンドが意味を持つよう絶対パスへ直す。
    path = path.expanduser()
    try:
        path = path.resolve()
    except OSError:
        path = path.absolute()
    exists = _safe_exists(path)
    probe = _nearest_existing(path)
    try:
        writable = os.access(probe, os.W_OK | os.X_OK)
        readable = os.access(probe, os.R_OK)
    except OSError:
        writable = readable = False
    blocked = ""
    if deep and exists and needs_write and writable:
        blocked = _first_unwritable(path)
    return PathCheck(
        label=label,
        path=str(path),
        exists=exists,
        writable=writable,
        readable=readable,
        owner=_owner_of(probe) if _safe_exists(probe) else "不明",
        needs_write=needs_write,
        feature=feature,
        blocked=blocked,
    )


def check_all() -> list[PathCheck]:
    """本ツールが読み書きする全パスを診断する。

    順序は「壊れたときの影響が大きいもの」から。
    """
    sounds = settings.asterisk_sounds_dir
    checks = [
        _check(
            "Asterisk 設定ファイル", settings.asterisk_config_dir,
            "「変更を Asterisk へ反映」(全機能の前提)", deep=True,
        ),
        _check(
            "音源の変換先", sounds,
            "音声ファイルの登録・保留音・アナウンス", deep=True,
        ),
        _check(
            "日本語音声", sounds.parent,
            "日本語音声プロンプトの入れ直し・日時読み上げ", deep=True,
        ),
        _check(
            "日本語音声の展開先", sounds.parent.parent,
            "日本語音声プロンプトのインストール",
        ),
        _check(
            "日時読み上げ音声", sounds.parent / "digits",
            "留守番電話の録音日時の読み上げ", deep=True,
        ),
        _check(
            "TTS 音響モデル", settings.hts_voice_dir,
            "声パックの追加・.htsvoice のアップロード", deep=True,
        ),
        _check(
            "アップロード一時置き場", settings.uploads_dir,
            "音声ファイル・FAX 原稿のアップロード",
        ),
        _check(
            "FAX スプール", settings.fax_spool_dir,
            "FAX の送受信 (Asterisk が受信した FAX をここに書きます)", deep=True,
        ),
        _check(
            "FAX 保存先", settings.fax_store_dir,
            "受信・送信 FAX の PDF 保存", deep=True,
        ),
        _check(
            "留守番電話スプール", settings.voicemail_spool_dir,
            "留守番電話の再生・削除", deep=True,
        ),
        _check(
            "バックアップ", settings.backup_dir,
            "設定のバックアップ・復元",
        ),
    ]
    db_dir = _database_dir()
    if db_dir is not None:
        checks.append(_check("データベース", db_dir, "全機能 (設定の保存)"))
    return checks


def _database_dir() -> Path | None:
    """SQLite のデータベースファイルがあるディレクトリ。

    SQLite 以外 (将来 PostgreSQL 等) の場合はファイル権限の話では
    ないので None を返す。
    """
    url = settings.database_url
    marker = "sqlite+aiosqlite:///"
    if not url.startswith(marker):
        return None
    raw = url[len(marker):]
    if not raw:
        return None
    # sqlite+aiosqlite:////abs/path → raw が "/abs/path"
    # sqlite+aiosqlite:///./rel.db  → raw が "./rel.db"
    return Path(raw).resolve().parent


def problems() -> list[PathCheck]:
    """問題があるものだけを返す (警告表示用)。"""
    return [c for c in check_all() if not c.ok]
