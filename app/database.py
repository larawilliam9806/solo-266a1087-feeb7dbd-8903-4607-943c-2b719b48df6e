"""SQLite 连接、建表与事务管理。

并发模型：
* 启用 WAL，允许读写并发；
* 每个写请求以 ``BEGIN IMMEDIATE`` 立即取得保留锁，写事务之间完全串行；
* 锁等待由 ``busy_timeout`` 阻塞，而不是直接报 SQLITE_BUSY；
* 任何校验失败都会回滚，原始读数/校准单与已落地的小时结果保持原状；
* 成功提交时原始数据与受影响小时的重算结果在同一个事务里一起生效。
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .config import BUSY_TIMEOUT_MS, get_db_path

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS readings (
    id          INTEGER PRIMARY KEY,
    number      TEXT NOT NULL UNIQUE,
    point       TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('primary', 'backup')),
    start_ts    INTEGER NOT NULL,
    end_ts      INTEGER NOT NULL,
    db_value    REAL NOT NULL,
    created_at  INTEGER NOT NULL
);

-- 同一仪器（测点 + 主备角色）的区间互不重叠，靠该索引做排他检查与取数。
CREATE INDEX IF NOT EXISTS idx_readings_instrument
    ON readings(point, role, start_ts, end_ts);

CREATE TABLE IF NOT EXISTS calibrations (
    id          INTEGER PRIMARY KEY,
    number      TEXT NOT NULL UNIQUE,
    point       TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('primary', 'backup')),
    start_ts    INTEGER NOT NULL,
    end_ts      INTEGER NOT NULL,
    offset_db   REAL NOT NULL,
    withdrawn   INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

-- 区间重叠检查只针对未撤回的校准单。
CREATE INDEX IF NOT EXISTS idx_calibrations_active
    ON calibrations(point, role, start_ts, end_ts, withdrawn);

CREATE TABLE IF NOT EXISTS hourly_results (
    point           TEXT NOT NULL,
    hour_start      INTEGER NOT NULL,
    leq_db          REAL NOT NULL,
    covered_seconds INTEGER NOT NULL,
    primary_seconds INTEGER NOT NULL,
    backup_seconds  INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    PRIMARY KEY (point, hour_start)
);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """打开一个数据库连接（每请求独立连接）。"""
    path = db_path or str(get_db_path())
    conn = sqlite3.connect(
        path,
        timeout=BUSY_TIMEOUT_MS / 1000.0,
        isolation_level=None,  # 关闭自动事务，手工 BEGIN/COMMIT
        # 每个请求通过依赖取得自己的连接，不会被多线程共享；
        # 但 FastAPI 线程池可能在不同工作线程中使用同一依赖对象，
        # 因此关闭同线程限制，跨线程安全由“每请求一连接”保证。
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(db_path: str | None = None) -> None:
    """初始化数据库文件与表结构（幂等）。"""
    path = db_path or str(get_db_path())
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """开启立即写事务，异常时回滚，正常退出时提交。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
