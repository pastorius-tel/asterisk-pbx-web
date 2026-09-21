"""SQLAlchemy 非同期エンジン / セッション。

SQLite と PostgreSQL の両方を同じコードで扱える。
DATABASE_URL の prefix で自動判別する:

  sqlite+aiosqlite:///./pbx.db
  postgresql+asyncpg://user:pass@host/db
"""

from __future__ import annotations

import asyncio
import fcntl
import tempfile
from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings

# SQLite では多重接続のため check_same_thread=False が必要。
# また timeout を設けて、他処理が一時的に DB をロックしていても
# すぐ "database is locked" にならず最大 30 秒待つようにする。
_connect_args: dict = {}
if settings.is_sqlite:
    _connect_args = {"check_same_thread": False, "timeout": 30}

engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    future=True,
    connect_args=_connect_args,
)

if settings.is_sqlite:
    # WAL モード: 読み取りと書き込みの並行性を上げ、
    # 受信フックの配信処理中でも他リクエストがブロックされにくくする。
    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _rec):  # type: ignore[no-untyped-def]
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依存性注入用。リクエストごとに新しいセッションを払い出す。"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """初回起動時にテーブルを作成 (本番では Alembic を推奨)。"""
    # 全モデルを import してメタデータを揃える
    from app.models import (  # noqa: F401
        audio,
        blocked_number,
        calendar_exception,
        call_log,
        company_holiday,
        day_pattern,
        extension,
        fax,
        inbound_route,
        ivr,
        national_holiday,
        outbound_route,
        parking,
        phone_book,
        queue,
        ring_group,
        time_condition,
        trunk,
    )
    from app.models.base import Base  # noqa: F401  循環回避

    # gunicorn は複数ワーカーを同時に起動するため、各ワーカーが同時に
    # create_all を実行すると "table ... already exists" で衝突し、
    # 一部のワーカーが起動に失敗する。ファイルロックで 1 プロセスずつ
    # 実行されるようにする (最初の 1 つがテーブルを作り、残りは
    # 作成済みの状態で通過する)。
    lock_path = Path(tempfile.gettempdir()) / "asterisk-pbx-web-initdb.lock"
    lock_file = open(lock_path, "w")  # noqa: SIM115  ロック保持のため明示的に閉じる
    try:
        await asyncio.to_thread(fcntl.flock, lock_file, fcntl.LOCK_EX)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # 既存テーブルにモデル定義の新カラムが無ければ ALTER TABLE で追加。
            # create_all は新規テーブルしか作らないため、バージョンアップで
            # 列が増えたときに "no such column" を防ぐ簡易マイグレーション。
            await conn.run_sync(_auto_add_missing_columns)
            # モデルから削除された古い列のうち、NOT NULL 制約が残っていて
            # 新しい INSERT を失敗させるものを取り除く。
            await conn.run_sync(_auto_drop_obsolete_columns)
    finally:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
        finally:
            lock_file.close()


# バージョンアップでモデルから削除された列。
# SQLite では NOT NULL 列が残っていると、その列に値を入れない新しい
# INSERT が "NOT NULL constraint failed" で失敗してしまうため、
# 起動時に DROP COLUMN で取り除く。
# (実機で time_conditions.time_range によりこの問題が発生した)
_OBSOLETE_COLUMNS: dict[str, tuple[str, ...]] = {
    "time_conditions": ("time_range", "days_of_week", "days_of_month", "months"),
}


def _auto_drop_obsolete_columns(sync_conn) -> None:  # type: ignore[no-untyped-def]
    """モデルから削除済みの古い列を DROP COLUMN で取り除く。

    SQLite 3.35 以降 / PostgreSQL で動作する。取り除けない環境
    (古い SQLite 等) では警告を出すだけで起動は継続する。
    """
    import logging

    from sqlalchemy import inspect as sa_inspect

    log = logging.getLogger("app.database")
    insp = sa_inspect(sync_conn)
    existing_tables = set(insp.get_table_names())

    for table_name, columns in _OBSOLETE_COLUMNS.items():
        if table_name not in existing_tables:
            continue
        current = {c["name"] for c in insp.get_columns(table_name)}
        for col in columns:
            if col not in current:
                continue
            try:
                sync_conn.exec_driver_sql(
                    f"ALTER TABLE {table_name} DROP COLUMN {col}"
                )
                log.warning(
                    "DB マイグレーション: %s.%s (廃止された列) を削除しました",
                    table_name, col,
                )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "DB マイグレーション: %s.%s を削除できませんでした: %s "
                    "(この列が NOT NULL の場合、保存時にエラーになる可能性があります)",
                    table_name, col, e,
                )


def _auto_add_missing_columns(sync_conn) -> None:  # type: ignore[no-untyped-def]
    """モデル定義 vs 実 DB を比較し、不足カラムを ALTER TABLE ADD COLUMN。

    SQLite / PostgreSQL 双方で動く範囲の単純な追加のみ対応
    (型変更・削除・制約変更はしない)。デフォルト値はモデルの
    server_default が無いため、既存行には NULL もしくは型の既定が入る。
    起動ログに追加したカラムを出力する。
    """
    import logging

    from sqlalchemy import inspect as sa_inspect

    from app.models.base import Base

    log = logging.getLogger("app.database")
    inspector = sa_inspect(sync_conn)

    for table_name, table in Base.metadata.tables.items():
        if not inspector.has_table(table_name):
            continue
        existing = {c["name"] for c in inspector.get_columns(table_name)}
        for col in table.columns:
            if col.name in existing:
                continue
            # 型を方言ごとの DDL 文字列へ
            try:
                coltype = col.type.compile(dialect=sync_conn.dialect)
            except Exception:  # noqa: BLE001
                coltype = "VARCHAR"
            # 既定値: Boolean は 0/1、その他は型に任せて NULL 許容で追加
            default_sql = ""
            if hasattr(col.type, "python_type"):
                try:
                    if col.type.python_type is bool:
                        dflt = 1 if col.default and col.default.arg else 0
                        default_sql = f" DEFAULT {dflt}"
                    elif col.type.python_type is int and col.default is not None:
                        if getattr(col.default, "arg", None) is not None and \
                                not callable(col.default.arg):
                            default_sql = f" DEFAULT {col.default.arg}"
                except (NotImplementedError, AttributeError):
                    pass
            ddl = (
                f'ALTER TABLE {table_name} '
                f'ADD COLUMN {col.name} {coltype}{default_sql}'
            )
            try:
                sync_conn.exec_driver_sql(ddl)
                log.warning(
                    "auto-migration: added column %s.%s (%s)",
                    table_name, col.name, coltype,
                )
            except Exception as e:  # noqa: BLE001
                # 複数ワーカー (gunicorn -w N) で同時に init_db が走ると、
                # 一方が先に ADD COLUMN を完了し、他方が "duplicate column"
                # で失敗する。これは正常動作なので debug ログに落とす。
                emsg = str(e).lower()
                if "duplicate column" in emsg or "already exists" in emsg:
                    log.debug(
                        "auto-migration: %s.%s already added by another worker",
                        table_name, col.name,
                    )
                else:
                    log.error(
                        "auto-migration failed for %s.%s: %s",
                        table_name, col.name, e,
                    )
