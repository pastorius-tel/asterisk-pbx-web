"""DB (pbx.db) + .env のバックアップ / 復元。

これまでの障害 (.venv / .env / pbx.db が消失し、5月時点の古いバックアップ
しか無かった事例) を踏まえ、Web UI からワンクリックでバックアップを作成・
ダウンロードできるようにする。

安全性の方針:
  - バックアップ作成: sqlite3.Connection.backup() (ライブDBのアトミックな
    スナップショット API) を使うため、稼働中でも壊れたコピーにならない。
  - 復元: アップロードされた zip は「ステージング領域」に展開するだけに
    留め、稼働中の pbx.db を直接上書きしない。gunicorn は複数ワーカー
    (-w 2 等) で動作しており、各ワーカーが SQLite ファイルへの接続を
    保持しているため、実行中のプロセスから安全に「差し替えて即反映」
    することはできない (ワーカーによって新旧データが混在する恐れがある)。
    確定はサービス再起動を伴う手動コピーで行う (画面にコマンドを表示)。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.config import settings

log = logging.getLogger(__name__)

_ENV_PATH = Path(".env")
_STAGING_DIRNAME = "_staged_restore"


def _sqlite_db_path() -> Path | None:
    """database_url から sqlite ファイルパスを取り出す。sqlite 以外は None。"""
    if not settings.is_sqlite:
        return None
    # 例: "sqlite+aiosqlite:///./pbx.db" -> "./pbx.db"
    url = settings.database_url
    marker = "///"
    idx = url.find(marker)
    if idx == -1:
        return None
    return Path(url[idx + len(marker):])


@dataclass
class BackupInfo:
    name: str
    size_bytes: int
    created_at: datetime


def list_backups() -> list[BackupInfo]:
    d = settings.backup_dir
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("backup_*.zip"), reverse=True):
        st = p.stat()
        out.append(
            BackupInfo(
                name=p.name,
                size_bytes=st.st_size,
                created_at=datetime.fromtimestamp(st.st_mtime),
            )
        )
    return out


def _do_sqlite_backup_sync(src_path: Path, dst_path: Path) -> None:
    """sqlite3 の公式 backup API でライブ DB を安全にスナップショット。"""
    src = sqlite3.connect(str(src_path))
    try:
        dst = sqlite3.connect(str(dst_path))
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


async def create_backup() -> BackupInfo:
    """pbx.db (安全なライブスナップショット) + .env を zip にまとめて保存。"""
    db_path = _sqlite_db_path()
    if db_path is None:
        raise RuntimeError(
            "SQLite 以外の DATABASE_URL ではこのバックアップ機能は使えません。"
        )
    if not db_path.exists():
        raise RuntimeError(f"DB ファイルが見つかりません: {db_path}")

    backup_dir = settings.backup_dir
    backup_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    tmp_db = backup_dir / f".tmp_{ts}.db"
    zip_path = backup_dir / f"backup_{ts}.zip"

    await asyncio.to_thread(_do_sqlite_backup_sync, db_path, tmp_db)

    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(tmp_db, "pbx.db")
            if _ENV_PATH.exists():
                zf.write(_ENV_PATH, ".env")
            zf.writestr(
                "MANIFEST.txt",
                f"created_at={datetime.now().isoformat()}\n"
                f"source_db={db_path}\n"
                f"includes_env={_ENV_PATH.exists()}\n",
            )
    finally:
        tmp_db.unlink(missing_ok=True)

    st = zip_path.stat()
    log.warning("backup: created %s (%d bytes)", zip_path.name, st.st_size)
    return BackupInfo(
        name=zip_path.name, size_bytes=st.st_size,
        created_at=datetime.fromtimestamp(st.st_mtime),
    )


def backup_path(name: str) -> Path:
    """バックアップファイル名からパスを解決 (ディレクトリトラバーサル防止)。"""
    if "/" in name or "\\" in name or not name.startswith("backup_") or not name.endswith(".zip"):
        raise ValueError("不正なファイル名です。")
    p = settings.backup_dir / name
    if not p.exists():
        raise FileNotFoundError(name)
    return p


def delete_backup(name: str) -> None:
    p = backup_path(name)
    p.unlink()
    log.warning("backup: deleted %s", name)


@dataclass
class StagedRestoreInfo:
    has_db: bool
    has_env: bool
    staged_dir: Path
    apply_commands: str


def staging_dir() -> Path:
    return settings.backup_dir / _STAGING_DIRNAME


async def stage_restore(upload_bytes: bytes, original_filename: str) -> StagedRestoreInfo:
    """アップロードされたバックアップ zip (または単体 .db) を検証してステージング。

    稼働中の pbx.db is 直接上書きしない。ここで展開した内容を、
    管理者が systemctl stop → cp → systemctl start で確定させる。
    """
    stage = staging_dir()
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True, exist_ok=True)

    def _write_and_extract() -> tuple[bool, bool]:
        has_db = False
        has_env = False
        if original_filename.lower().endswith(".zip"):
            tmp_zip = stage / "_upload.zip"
            tmp_zip.write_bytes(upload_bytes)
            with zipfile.ZipFile(tmp_zip) as zf:
                names = zf.namelist()
                if "pbx.db" in names:
                    zf.extract("pbx.db", stage)
                    has_db = True
                if ".env" in names:
                    zf.extract(".env", stage)
                    has_env = True
            tmp_zip.unlink(missing_ok=True)
        elif original_filename.lower().endswith(".db"):
            (stage / "pbx.db").write_bytes(upload_bytes)
            has_db = True
        else:
            raise ValueError(
                "対応していないファイル形式です。"
                "本ツールでダウンロードした backup_*.zip、または .db ファイルを指定してください。"
            )
        return has_db, has_env

    has_db, has_env = await asyncio.to_thread(_write_and_extract)

    if not has_db:
        shutil.rmtree(stage, ignore_errors=True)
        raise ValueError("アップロードされたファイルに pbx.db が含まれていません。")

    # 整合性チェック: sqlite ファイルとして開けるか
    def _check_sqlite() -> None:
        con = sqlite3.connect(str(stage / "pbx.db"))
        try:
            con.execute("PRAGMA integrity_check")
        finally:
            con.close()

    try:
        await asyncio.to_thread(_check_sqlite)
    except sqlite3.DatabaseError as exc:
        shutil.rmtree(stage, ignore_errors=True)
        raise ValueError(f"pbx.db が壊れているか SQLite ファイルではありません: {exc}") from exc

    db_path = _sqlite_db_path()
    app_dir = Path(".").resolve()
    cmds = [
        "# 以下を実機のターミナルで実行して復元を確定してください",
        "# (systemctl 以外は sudo なしで実行し、今の所有者を維持してください):",
        "sudo systemctl stop asterisk-pbx-web",
        f"cp {db_path} {db_path}.before_restore.$(date +%Y%m%d_%H%M%S)  # 念のため退避",
        f"cp {stage / 'pbx.db'} {db_path}",
    ]
    if has_env:
        cmds.append(
            f"cp {app_dir / '.env'} {app_dir / '.env'}.before_restore."
            "$(date +%Y%m%d_%H%M%S)  # 念のため退避"
        )
        cmds.append(f"cp {stage / '.env'} {app_dir / '.env'}")
    cmds += [
        f"rm -f {db_path}-shm {db_path}-wal",
        "sudo systemctl start asterisk-pbx-web",
    ]

    return StagedRestoreInfo(
        has_db=has_db, has_env=has_env, staged_dir=stage,
        apply_commands="\n".join(cmds),
    )
