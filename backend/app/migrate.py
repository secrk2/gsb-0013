"""轻量自动迁移：SQLite 不支持 "ALTER TABLE ADD COLUMN IF NOT EXISTS"，
启动时按 PRAGMA 检查缺列就补；Role 枚举扩了 INSPECTOR 后，SQLite 存的是
CHECK 约束文本，无法直接扩枚举，因此对 users 表做一次性「新旧表重建」。

仅面向演示库（SQLite，无 alembic），保证 docker compose down -v 前
已存在的 /data/baoguantong.db 也能平滑升级。
"""
from __future__ import annotations

import logging

from .database import Base, engine
from . import models  # noqa: F401  # 确保所有表注册到 metadata 再 create_all

log = logging.getLogger("baoguantong.migrate")

# 表 → 需补齐的列定义（列名, ALTER 用 SQL 片段）
_EXTRA_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "users": [
        ("yard_id", "INTEGER REFERENCES yards(id)"),
        ("inspector_certs", "VARCHAR(200) DEFAULT ''"),
        ("available", "BOOLEAN DEFAULT 1"),
        ("unavailable_reason", "VARCHAR(200) DEFAULT ''"),
    ],
    "inspections": [
        ("yard_id", "INTEGER REFERENCES yards(id)"),
        ("bay_id", "INTEGER REFERENCES bays(id)"),
        ("scheduled_end", "DATETIME"),
        ("due_at", "DATETIME"),
        ("finished_at", "DATETIME"),
        ("required_certs", "VARCHAR(200) DEFAULT 'normal'"),
        ("version", "INTEGER DEFAULT 1"),
        ("reassign_batch_id", "VARCHAR(40)"),
        ("updated_at", "DATETIME"),
    ],
}


def _existing_columns(conn, table: str) -> set[str]:
    rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def _rebuild_users_table(conn) -> None:
    """users 表的 role 列 CHECK 约束不含 inspector → 按新模型重建并搬数据。

    判据：已存在旧版 users 表且其 role CHECK 文本里没有 inspector。
    新库 create_all 建出来的表已含 inspector，直接跳过。
    """
    row = conn.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()
    if not row or not row[0] or "inspector" in row[0]:
        return
    log.info("users 表为旧结构（role 枚举缺 inspector），开始一次性重建 …")
    conn.exec_driver_sql("ALTER TABLE users RENAME TO users_old")
    users_table = Base.metadata.tables["users"]
    users_table.create(bind=conn, checkfirst=True)
    conn.exec_driver_sql(
        """
        INSERT INTO users
          (id, username, password_hash, display_name, role, enterprise_id, port, active, created_at,
           yard_id, inspector_certs, available, unavailable_reason)
        SELECT id, username, password_hash, display_name, role, enterprise_id, port, active, created_at,
               NULL, '', 1, ''
        FROM users_old
        """
    )
    conn.exec_driver_sql("DROP TABLE users_old")
    log.info("users 表重建完成")


def run_migrations() -> None:
    """建表后执行：users 枚举重建 + 补列。幂等。"""
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        try:
            _rebuild_users_table(conn)
        except Exception:
            log.exception("users 表重建失败，继续尝试其他迁移")

        for table, cols in _EXTRA_COLUMNS.items():
            try:
                existing = _existing_columns(conn, table)
            except Exception:
                continue
            for col, ddl in cols:
                if col not in existing:
                    try:
                        conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                        log.info("迁移：%s 补列 %s", table, col)
                    except Exception:
                        log.exception("补列失败 %s.%s", table, col)
